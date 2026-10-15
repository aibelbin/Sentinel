"""Operational commands. Run inside the API container.

    python -m app.cli seed
    python -m app.cli create-device --name web-01 [--deb] [--rotate] [--stdout]
    python -m app.cli list-devices
    python -m app.cli reset-admin-password [--password X]
    python -m app.cli prune [--days N]
"""

import argparse
import getpass
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, func, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    GLOBAL_CONFIG_ID,
    AdminSession,
    AdminUser,
    AgentConfig,
    Device,
    DeviceKey,
    InventoryReport,
    PackageChange,
    Task,
)
from app.security import hash_password, new_device_key
from app.services import agent_config, agent_release, debian_package
from app.services.installer import build_installer
from app.status import derive_status


def cmd_seed(_args: argparse.Namespace) -> int:
    """Create the administrator and global config row if they do not exist.

    Idempotent, and never overwrites an existing admin: the environment
    variables are a first-boot seed, not a source of truth.
    """
    settings = get_settings()
    with SessionLocal() as db:
        existing = db.execute(select(func.count()).select_from(AdminUser)).scalar_one()
        if existing:
            print("admin already exists, leaving it untouched")
        else:
            db.add(
                AdminUser(
                    username=settings.admin_username,
                    password_hash=hash_password(settings.admin_password),
                )
            )
            print(f"created admin '{settings.admin_username}'")

        if db.get(AgentConfig, GLOBAL_CONFIG_ID) is None:
            db.add(AgentConfig(id=GLOBAL_CONFIG_ID, device_id=None))
            print("created global agent config")

        db.commit()
    return 0


def _issue_key(db, device: Device) -> str:
    """Issue a key, revoking any live one. Returns the plaintext, once."""
    now = datetime.now(timezone.utc)
    live = db.execute(
        select(DeviceKey).where(DeviceKey.device_id == device.id, DeviceKey.revoked_at.is_(None))
    ).scalars().all()
    for row in live:
        row.revoked_at = now
    db.flush()

    plaintext, key_hash = new_device_key()
    db.add(DeviceKey(device_id=device.id, key_hash=key_hash, created_at=now))
    return plaintext


def cmd_create_device(args: argparse.Namespace) -> int:
    """Register a device and emit its installer.

    The same thing the dashboard's "Add device" does, for when you are already
    on the server. The key is shown once here and nowhere else: only its hash
    is stored.
    """
    settings = get_settings()
    server_url = (args.server_url or settings.public_api_url or "").rstrip("/")
    if not server_url:
        print(
            "no server URL: pass --server-url, or set PUBLIC_API_URL for this service",
            file=sys.stderr,
        )
        return 1
    if not server_url.startswith(("http://", "https://")):
        print(f"server URL must start with http:// or https:// (got {server_url!r})",
              file=sys.stderr)
        return 1

    insecure = bool(args.insecure)
    if server_url.startswith("http://") and not insecure:
        print(
            f"refusing to bake a plain-HTTP URL ({server_url}) into an installer; "
            "pass --insecure for local testing",
            file=sys.stderr,
        )
        return 1

    name = args.name.strip()
    if not name:
        print("a device name is required", file=sys.stderr)
        return 1

    with SessionLocal() as db:
        device = db.execute(select(Device).where(Device.name == name)).scalar_one_or_none()

        if device is not None and not args.rotate:
            print(
                f"device {name!r} already exists. Its key cannot be recovered, only "
                "replaced -- pass --rotate to issue a new one, which stops the old "
                "key working.",
                file=sys.stderr,
            )
            return 1

        created = device is None
        if created:
            device = Device(name=name)
            db.add(device)
            db.flush()

        api_key = _issue_key(db, device)
        version, _sha256 = agent_release.current_release()
        agent_release.record(db)

        if args.deb:
            # The package carries no credentials -- it is identical for every
            # machine -- so the key is handed over as an enroll command.
            try:
                payload = debian_package.build(settings.agent_source_dir, version)
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                db.rollback()
                print(f"could not build the package: {exc}", file=sys.stderr)
                return 1
            db.commit()

            out = Path(args.out) if args.out else Path.cwd() / debian_package.filename(version)
            if out.is_dir():
                out = out / debian_package.filename(version)
            out.write_bytes(payload)
            insecure_flag = " --insecure" if insecure else ""
            print(f"{'created' if created else 'rotated key for'} {name}")
            print(f"  device id : {device.id}")
            print(f"  version   : {version}")
            print(f"  package   : {out} ({len(payload)} bytes)")
            print()
            print("  On the machine:")
            print(f"    sudo apt install ./{out.name}")
            print(f"    sudo sentinel-agent enroll --server {server_url} "
                  f"--key {api_key}{insecure_flag}")
            return 0

        try:
            script = build_installer(
                settings.agent_source_dir,
                version=version,
                server_url=server_url,
                api_key=api_key,
                insecure=insecure,
            )
        except (FileNotFoundError, ValueError) as exc:
            db.rollback()
            print(f"could not build the installer: {exc}", file=sys.stderr)
            return 1

        db.commit()
        device_id = str(device.id)

    if args.stdout:
        # Script on stdout so it can be redirected or piped from the host;
        # everything a human needs goes to stderr so it stays out of the file.
        sys.stdout.write(script)
        print(
            f"{'created' if created else 'rotated key for'} {name}\n"
            f"  device id : {device_id}\n"
            f"  api key   : {api_key}\n"
            f"  version   : {version}",
            file=sys.stderr,
        )
        return 0

    out = Path(args.out) if args.out else Path.cwd() / f"sentinel-install-{name}.sh"
    out.write_text(script, encoding="utf-8")
    # The script contains the key, so it must not be world-readable.
    os.chmod(out, stat.S_IRUSR | stat.S_IWUSR)

    print(f"{'created' if created else 'rotated key for'} {name}")
    print(f"  device id : {device_id}")
    print(f"  api key   : {api_key}")
    print(f"  version   : {version}")
    print(f"  installer : {out} (mode 600)")
    print()
    print("  Copy it to the machine and run:  sudo bash " + out.name)
    if not created:
        print("  The previous key no longer works; re-run with --force-reenroll there.")
    return 0


def cmd_list_devices(_args: argparse.Namespace) -> int:
    """What the fleet looks like, without needing the dashboard."""
    with SessionLocal() as db:
        devices = db.execute(select(Device).order_by(Device.name)).scalars().all()
        if not devices:
            print("no devices registered")
            return 0

        release = agent_release.record(db)
        db.commit()

        configs = agent_config.resolve_many(db, [d.id for d in devices])
        print(f"{'NAME':<20} {'STATUS':<9} {'AGENT':<14} {'UPDATE':<15} LAST SEEN")
        for device in devices:
            status = derive_status(
                enabled=device.enabled,
                last_seen_at=device.last_seen_at,
                poll_interval_seconds=configs[device.id].poll_interval_seconds,
            )
            seen = device.last_seen_at.strftime("%Y-%m-%d %H:%M:%SZ") if device.last_seen_at else "never"
            print(
                f"{device.name:<20} {status:<9} {str(device.agent_version or '-'):<14} "
                f"{agent_release.update_status(device, release):<15} {seen}"
            )
        print(f"\nserver is serving version {release.version}")
    return 0


def cmd_reset_admin_password(args: argparse.Namespace) -> int:
    """Manual password recovery, per the spec's no-email-flow decision."""
    password = args.password
    if not password:
        password = getpass.getpass("New admin password: ")
        if password != getpass.getpass("Confirm: "):
            print("passwords do not match", file=sys.stderr)
            return 1
    if len(password) < 8:
        print("password must be at least 8 characters", file=sys.stderr)
        return 1

    with SessionLocal() as db:
        admin = db.execute(select(AdminUser).order_by(AdminUser.id).limit(1)).scalar_one_or_none()
        if admin is None:
            print("no admin exists; run `seed` first", file=sys.stderr)
            return 1
        username = admin.username
        admin.password_hash = hash_password(password)
        # Every existing session is invalidated, so a stolen token cannot
        # outlive the password it was issued against.
        removed = db.execute(delete(AdminSession)).rowcount
        db.commit()
    print(f"password reset for '{username}'; {removed} session(s) invalidated")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    """Delete operational history past the retention window.

    device_package is current state and is never pruned.
    """
    # `or` would turn an explicit --days 0 ("prune everything up to now") into
    # the 30-day default and report success for a prune that did not happen.
    days = args.days if args.days is not None else get_settings().retention_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        changes = db.execute(
            delete(PackageChange).where(PackageChange.detected_at < cutoff)
        ).rowcount
        reports = db.execute(
            delete(InventoryReport).where(InventoryReport.received_at < cutoff)
        ).rowcount
        tasks = db.execute(delete(Task).where(Task.created_at < cutoff)).rowcount
        sessions = db.execute(delete(AdminSession).where(AdminSession.expires_at < now)).rowcount
        db.commit()

    print(
        f"pruned (older than {days}d): {changes} changes, {reports} reports, "
        f"{tasks} tasks, {sessions} expired sessions"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sentinel-admin", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed", help="create admin and global config if absent").set_defaults(
        func=cmd_seed
    )

    reset = sub.add_parser("reset-admin-password", help="set a new admin password")
    reset.add_argument("--password", help="read from a prompt when omitted")
    reset.set_defaults(func=cmd_reset_admin_password)

    create = sub.add_parser("create-device", help="register a device and write its installer")
    create.add_argument("--name", required=True, help="a label you choose, e.g. web-01")
    create.add_argument(
        "--server-url", help="address the device reports to (default: PUBLIC_API_URL)"
    )
    create.add_argument(
        "--insecure", action="store_true", help="allow a plain-HTTP server URL (testing only)"
    )
    create.add_argument(
        "--rotate",
        action="store_true",
        help="if the device already exists, issue a new key and revoke the old one",
    )
    create.add_argument("--out", help="where to write the installer")
    create.add_argument(
        "--deb",
        action="store_true",
        help="write the .deb instead of the script, and print the enroll command",
    )
    create.add_argument(
        "--stdout",
        action="store_true",
        help="write the script to stdout instead of a file, so it can be piped",
    )
    create.set_defaults(func=cmd_create_device)

    sub.add_parser("list-devices", help="show the fleet and its agent versions").set_defaults(
        func=cmd_list_devices
    )

    prune = sub.add_parser("prune", help="delete history past the retention window")
    prune.add_argument("--days", type=int, default=None)
    prune.set_defaults(func=cmd_prune)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
