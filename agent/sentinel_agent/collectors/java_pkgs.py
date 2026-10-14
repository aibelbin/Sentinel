"""Installed JDKs and JREs.

Java has no device-wide package manager equivalent to dpkg or pip, so this
reports installed runtimes rather than Maven artifacts. Artifact-level
inventory belongs with project-dependency scanning, which is deferred.
"""

import re

from sentinel_agent import paths
from sentinel_agent.collectors import CollectorSkipped, run

# java-17-openjdk-amd64, java-1.11.0-openjdk-amd64, temurin-21-jdk, zulu17...
_VERSION_IN_NAME = re.compile(r"(\d+(?:\.\d+)*)")
# openjdk version "17.0.12" 2024-07-16   /   java version "1.8.0_422"
_VERSION_LINE = re.compile(r'version "([^"]+)"')


def parse_default_runtime(stderr: str) -> str | None:
    """`java -version` writes to stderr, not stdout."""
    match = _VERSION_LINE.search(stderr or "")
    return match.group(1) if match else None


def parse_jvm_dir(names: list[str]) -> list[dict]:
    packages: list[dict] = []
    for name in sorted(names):
        version_match = _VERSION_IN_NAME.search(name)
        packages.append(
            {
                "ecosystem": "java",
                "name": name,
                "version": version_match.group(1) if version_match else "unknown",
                "scope": "system",
                "meta": {"source": "jvm_dir"},
            }
        )
    return packages


def collect() -> list[dict]:
    packages: list[dict] = []

    if paths.JVM_DIR.is_dir():
        try:
            # Symlinks like `default-java` point at a real entry already listed.
            names = [
                entry.name
                for entry in paths.JVM_DIR.iterdir()
                if entry.is_dir() and not entry.is_symlink()
            ]
        except OSError as exc:
            raise CollectorSkipped(f"cannot read {paths.JVM_DIR}: {exc}") from exc
        packages.extend(parse_jvm_dir(names))

    # `java -version` exits 0 and prints to stderr, so capture it directly
    # rather than through run(), which returns stdout.
    import shutil
    import subprocess

    if shutil.which("java") is not None:
        try:
            completed = subprocess.run(  # noqa: S603 - argv list, shell=False
                ["java", "-version"], capture_output=True, text=True, timeout=30, check=False
            )
            version = parse_default_runtime(completed.stderr)
            if version:
                packages.append(
                    {
                        "ecosystem": "java",
                        "name": "java-default-runtime",
                        "version": version,
                        "scope": "system",
                        "meta": {"source": "java -version"},
                    }
                )
        except (subprocess.TimeoutExpired, OSError):
            pass

    if not packages:
        raise CollectorSkipped("no JDK found under /usr/lib/jvm and no java on PATH")
    return packages
