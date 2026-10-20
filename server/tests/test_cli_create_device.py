"""The server-side CLI for registering devices."""

import re
import stat
from pathlib import Path

import pytest
from sqlalchemy import select

from app.cli import main
from app.models import Device, DeviceKey
from app.services import watchdog_release
from app.services.installer import clear_caches


@pytest.fixture(autouse=True)
def _caches():
    watchdog_release.clear_caches()
    clear_caches()
    yield
    watchdog_release.clear_caches()
    clear_caches()


@pytest.fixture
def https_url(monkeypatch):
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("PUBLIC_API_URL", "https://sentinel.example")
    get_settings.cache_clear()
    yield "https://sentinel.example"
    get_settings.cache_clear()


def run(*argv, out_dir: Path | None = None) -> int:
    args = list(argv)
    if out_dir is not None:
        args += ["--out", str(out_dir / "install.sh")]
    return main(args)


# --------------------------------------------------------------------------- #
# Creating
# --------------------------------------------------------------------------- #


def test_creates_a_device_and_writes_an_installer(db, https_url, tmp_path, capsys):
    assert run("create-device", "--name", "web-01", out_dir=tmp_path) == 0

    device = db.execute(select(Device).where(Device.name == "web-01")).scalar_one()
    assert device.enabled is True

    script = (tmp_path / "install.sh").read_text()
    assert "SERVER_URL='https://sentinel.example'" in script
    assert re.search(r"API_KEY='sk_dev_[A-Za-z0-9_-]+'", script)
    assert "WATCHDOG_BUNDLE_B64='" in script

    out = capsys.readouterr().out
    assert "created web-01" in out
    assert str(device.id) in out


def test_the_key_it_prints_is_the_one_in_the_script(db, https_url, tmp_path, capsys):
    run("create-device", "--name", "web-01", out_dir=tmp_path)
    printed = re.search(r"api key\s*:\s*(sk_dev_[A-Za-z0-9_-]+)", capsys.readouterr().out)
    assert printed, "no key printed"

    script = (tmp_path / "install.sh").read_text()
    assert f"API_KEY='{printed.group(1)}'" in script


def test_the_key_works_against_the_api(client, db, https_url, tmp_path, capsys):
    """The point of the command: the key it hands out actually enrolls."""
    run("create-device", "--name", "web-01", out_dir=tmp_path)
    key = re.search(r"api key\s*:\s*(sk_dev_[A-Za-z0-9_-]+)", capsys.readouterr().out).group(1)

    response = client.post(
        "/api/v1/watchdog/enroll",
        json={"watchdog_version": "abcdef012345", "host": {"hostname": "web-01"}},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "web-01"


def test_only_the_hash_is_stored(db, https_url, tmp_path, capsys):
    run("create-device", "--name", "web-01", out_dir=tmp_path)
    key = re.search(r"api key\s*:\s*(sk_dev_[A-Za-z0-9_-]+)", capsys.readouterr().out).group(1)

    rows = db.execute(select(DeviceKey)).scalars().all()
    assert len(rows) == 1
    assert rows[0].key_hash != key
    assert len(rows[0].key_hash) == 64


def test_the_installer_is_not_world_readable(db, https_url, tmp_path):
    """It contains the key, so it must not be readable by other users."""
    run("create-device", "--name", "web-01", out_dir=tmp_path)
    mode = stat.S_IMODE((tmp_path / "install.sh").stat().st_mode)
    assert mode == 0o600


def test_stdout_mode_emits_only_the_script(db, https_url, capsys):
    """So it can be redirected from the host without the diagnostics landing
    in the file."""
    assert run("create-device", "--name", "web-01", "--stdout") == 0

    captured = capsys.readouterr()
    assert captured.out.startswith("#!/usr/bin/env bash")
    assert "api key" not in captured.out
    assert "api key" in captured.err


# --------------------------------------------------------------------------- #
# Refusing to do the wrong thing
# --------------------------------------------------------------------------- #


def test_a_duplicate_name_is_refused_with_advice(db, https_url, tmp_path, capsys):
    run("create-device", "--name", "web-01", out_dir=tmp_path)
    capsys.readouterr()

    assert run("create-device", "--name", "web-01", out_dir=tmp_path) == 1
    err = capsys.readouterr().err
    assert "already exists" in err
    assert "--rotate" in err
    # And it did not quietly issue a second key.
    assert len(db.execute(select(DeviceKey)).scalars().all()) == 1


def test_plain_http_is_refused_without_insecure(db, monkeypatch, tmp_path, capsys):
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("PUBLIC_API_URL", "http://localhost:8000")
    get_settings.cache_clear()

    assert run("create-device", "--name", "web-01", out_dir=tmp_path) == 1
    assert "refusing to bake a plain-HTTP URL" in capsys.readouterr().err
    assert db.execute(select(Device)).scalars().all() == []
    get_settings.cache_clear()


def test_plain_http_is_allowed_with_insecure(db, monkeypatch, tmp_path):
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("PUBLIC_API_URL", "http://localhost:8000")
    get_settings.cache_clear()

    assert run("create-device", "--name", "sim-01", "--insecure", out_dir=tmp_path) == 0
    assert "INSECURE=1" in (tmp_path / "install.sh").read_text()
    get_settings.cache_clear()


def test_a_missing_server_url_is_refused(db, monkeypatch, tmp_path, capsys):
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("PUBLIC_API_URL", "")
    get_settings.cache_clear()

    assert run("create-device", "--name", "web-01", out_dir=tmp_path) == 1
    assert "no server URL" in capsys.readouterr().err
    get_settings.cache_clear()


def test_a_blank_name_is_refused(db, https_url, tmp_path, capsys):
    assert run("create-device", "--name", "   ", out_dir=tmp_path) == 1
    assert "name is required" in capsys.readouterr().err


def test_a_garbage_server_url_is_refused(db, https_url, tmp_path, capsys):
    assert run("create-device", "--name", "web-01", "--server-url", "sentinel.example",
               out_dir=tmp_path) == 1
    assert "must start with" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Rotating
# --------------------------------------------------------------------------- #


def test_rotate_issues_a_new_key_and_revokes_the_old(client, db, https_url, tmp_path, capsys):
    run("create-device", "--name", "web-01", out_dir=tmp_path)
    first = re.search(r"api key\s*:\s*(sk_dev_[A-Za-z0-9_-]+)", capsys.readouterr().out).group(1)

    assert run("create-device", "--name", "web-01", "--rotate", out_dir=tmp_path) == 0
    out = capsys.readouterr().out
    second = re.search(r"api key\s*:\s*(sk_dev_[A-Za-z0-9_-]+)", out).group(1)

    assert second != first
    assert "rotated key for web-01" in out

    beat = {"watchdog_version": "abcdef012345"}
    assert client.post(
        "/api/v1/watchdog/heartbeat", json=beat, headers={"Authorization": f"Bearer {first}"}
    ).status_code == 403
    assert client.post(
        "/api/v1/watchdog/heartbeat", json=beat, headers={"Authorization": f"Bearer {second}"}
    ).status_code == 200


def test_rotate_keeps_the_same_device(db, https_url, tmp_path):
    run("create-device", "--name", "web-01", out_dir=tmp_path)
    before = db.execute(select(Device).where(Device.name == "web-01")).scalar_one().id

    run("create-device", "--name", "web-01", "--rotate", out_dir=tmp_path)
    after = db.execute(select(Device).where(Device.name == "web-01")).scalar_one().id

    # History and identity survive a rotation; only the credential changes.
    assert before == after
    assert len(db.execute(select(DeviceKey)).scalars().all()) == 2


def test_rotate_on_a_new_name_just_creates_it(db, https_url, tmp_path, capsys):
    assert run("create-device", "--name", "fresh", "--rotate", out_dir=tmp_path) == 0
    assert "created fresh" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


def test_list_devices_is_empty_to_begin_with(db, https_url, capsys):
    assert main(["list-devices"]) == 0
    assert "no devices registered" in capsys.readouterr().out


def test_list_devices_shows_what_was_created(db, https_url, tmp_path, capsys):
    run("create-device", "--name", "web-01", out_dir=tmp_path)
    capsys.readouterr()

    assert main(["list-devices"]) == 0
    out = capsys.readouterr().out
    assert "web-01" in out
    # Never reported in, so it is offline and outside the update mechanism.
    assert "offline" in out
    assert "not managed" in out
    assert "serving version" in out
