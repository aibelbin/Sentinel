"""Collector parsing, against real command output captured as fixtures."""

import pytest

from sentinel_agent.collectors import CollectorSkipped, run
from sentinel_agent.collectors import java_pkgs, npm_pkgs, os_pkgs, python_pkgs

# --------------------------------------------------------------------------- #
# dpkg
# --------------------------------------------------------------------------- #

DPKG_OUTPUT = """ii\tnginx\t1.24.0-2ubuntu7
ii\tcurl\t8.5.0-2ubuntu10.6
rc\told-package\t1.0-1
ii\tlibssl3t64\t3.0.13-0ubuntu3.4
iU\thalf-installed\t2.0
ii\tpython3\t3.12.3-0ubuntu2
"""


def test_dpkg_keeps_only_installed_packages():
    packages = os_pkgs.parse(DPKG_OUTPUT)
    names = {package["name"] for package in packages}

    assert names == {"nginx", "curl", "libssl3t64", "python3"}
    # rc means removed-but-config-remains: the package is NOT installed, and
    # reporting it would overstate what is on the device.
    assert "old-package" not in names
    # iU is unpacked-but-not-configured, which is also not installed.
    assert "half-installed" not in names


def test_dpkg_output_is_tagged_as_os_scope_system():
    package = os_pkgs.parse("ii\tnginx\t1.24.0\n")[0]
    assert package == {
        "ecosystem": "os",
        "name": "nginx",
        "version": "1.24.0",
        "scope": "system",
        "meta": {},
    }


@pytest.mark.parametrize("line", ["", "garbage", "ii\tonly-two-fields", "ii\t\t1.0", "ii\tname\t"])
def test_dpkg_ignores_malformed_lines(line):
    assert os_pkgs.parse(line + "\n") == []


def test_dpkg_collector_runs_against_a_stub(stub_bin):
    stub_bin("dpkg-query", stdout=DPKG_OUTPUT)
    assert len(os_pkgs.collect()) == 4


# --------------------------------------------------------------------------- #
# pip
# --------------------------------------------------------------------------- #

PIP_OUTPUT = """[
  {"name": "requests", "version": "2.31.0"},
  {"name": "Flask_SQLAlchemy", "version": "3.1.1"},
  {"name": "urllib3", "version": "2.2.1"}
]"""


def test_pip_parsing_records_the_interpreter_as_scope():
    packages = python_pkgs.parse(PIP_OUTPUT, scope="/usr/bin/python3.12")
    assert len(packages) == 3
    assert all(package["scope"] == "/usr/bin/python3.12" for package in packages)
    assert all(package["ecosystem"] == "python" for package in packages)
    # Normalization is the server's job; the agent reports what pip said.
    assert {package["name"] for package in packages} == {
        "requests", "Flask_SQLAlchemy", "urllib3"
    }


def test_pip_non_json_output_is_skipped_not_crashed():
    with pytest.raises(CollectorSkipped):
        python_pkgs.parse("WARNING: pip is being invoked by an old script", scope="x")


def test_pip_unexpected_shape_is_skipped():
    with pytest.raises(CollectorSkipped):
        python_pkgs.parse('{"packages": []}', scope="x")


def test_pip_empty_list_is_valid():
    assert python_pkgs.parse("[]", scope="/usr/bin/python3") == []


def test_pip_entries_missing_fields_are_dropped():
    packages = python_pkgs.parse(
        '[{"name": "ok", "version": "1.0"}, {"name": "", "version": "2.0"}, {"version": "3.0"}]',
        scope="x",
    )
    assert [package["name"] for package in packages] == ["ok"]


# --------------------------------------------------------------------------- #
# npm
# --------------------------------------------------------------------------- #

NPM_OUTPUT = """{
  "name": "lib",
  "dependencies": {
    "json": {"version": "11.0.0"},
    "typescript": {"version": "5.4.5"},
    "@scope/tool": {"version": "0.3.1"}
  }
}"""


def test_npm_parses_global_dependencies():
    packages = npm_pkgs.parse(NPM_OUTPUT)
    assert {package["name"] for package in packages} == {"json", "typescript", "@scope/tool"}
    assert all(package["ecosystem"] == "npm" for package in packages)


def test_npm_tree_without_dependencies_is_empty_not_an_error():
    assert npm_pkgs.parse('{"name": "lib"}') == []
    assert npm_pkgs.parse("") == []


def test_npm_entries_without_a_version_are_dropped():
    """An unmet peer dependency appears with no version field."""
    packages = npm_pkgs.parse('{"dependencies": {"ok": {"version": "1.0"}, "broken": {}}}')
    assert [package["name"] for package in packages] == ["ok"]


def test_npm_non_json_output_is_skipped():
    with pytest.raises(CollectorSkipped):
        npm_pkgs.parse("npm ERR! code ELSPROBLEMS")


def test_npm_nonzero_exit_with_usable_output_still_parses(stub_bin):
    """npm ls -g exits 1 on unmet peer deps but prints a perfectly good tree."""
    stub_bin("npm", stdout=NPM_OUTPUT, stderr="npm ERR! ELSPROBLEMS", exit_code=1)
    assert len(npm_pkgs.collect()) == 3


# --------------------------------------------------------------------------- #
# java
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ('openjdk version "17.0.12" 2024-07-16\nOpenJDK Runtime Environment', "17.0.12"),
        ('java version "1.8.0_422"\nJava(TM) SE Runtime Environment', "1.8.0_422"),
        ('openjdk version "21.0.4" 2024-07-16 LTS', "21.0.4"),
    ],
)
def test_java_version_is_read_from_stderr(stderr, expected):
    assert java_pkgs.parse_default_runtime(stderr) == expected


def test_java_version_absent_returns_none():
    assert java_pkgs.parse_default_runtime("command not found") is None
    assert java_pkgs.parse_default_runtime("") is None


def test_jvm_directory_names_yield_versions():
    packages = java_pkgs.parse_jvm_dir(
        ["java-17-openjdk-amd64", "java-1.11.0-openjdk-amd64", "temurin-21-jdk", "weird-name"]
    )
    versions = {package["name"]: package["version"] for package in packages}
    assert versions["java-17-openjdk-amd64"] == "17"
    assert versions["java-1.11.0-openjdk-amd64"] == "1.11.0"
    assert versions["temurin-21-jdk"] == "21"
    # Rather than guessing, an unparseable name is reported honestly.
    assert versions["weird-name"] == "unknown"


# --------------------------------------------------------------------------- #
# The run() helper
# --------------------------------------------------------------------------- #


def test_missing_binary_is_skipped_not_an_error():
    with pytest.raises(CollectorSkipped, match="not installed"):
        run(["definitely-not-a-real-binary-xyz"])


def test_nonzero_exit_is_skipped_with_the_stderr_hint(stub_bin):
    stub_bin("failing-tool", stderr="permission denied", exit_code=1)
    with pytest.raises(CollectorSkipped, match="permission denied"):
        run(["failing-tool"])


def test_timeout_is_skipped(stub_bin, tmp_path):
    script = tmp_path / "bin" / "slow-tool"
    script.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    script.chmod(0o755)
    with pytest.raises(CollectorSkipped, match="timed out"):
        run(["slow-tool"], timeout=1)


def test_collect_all_survives_one_failing_collector(stub_bin, monkeypatch):
    """One broken collector must not cost the whole report."""
    from sentinel_agent import collectors

    stub_bin("dpkg-query", stdout=DPKG_OUTPUT)
    monkeypatch.setattr(
        python_pkgs, "collect", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    packages, status = collectors.collect_all()

    assert len(packages) == 4  # the dpkg results survived
    assert status["os"].startswith("ok")
    assert "RuntimeError" in status["python"]
    assert "npm" in status and "java" in status
