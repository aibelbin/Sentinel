"""The diff engine: the piece with the most logic and the most ways to be wrong."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import ChangeType, Device, DevicePackage, PackageChange
from app.schemas import InventoryRequest
from app.services.inventory import apply_inventory, normalize_name

# Deliberately in the past: the server clamps a collected_at ahead of its own
# clock, so fixtures dated "today plus N hours" would be silently rewritten and
# the assertions below would depend on wall-clock timing.
T0 = datetime(2026, 9, 1, 6, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def device(db):
    row = Device(name="diff-target")
    db.add(row)
    db.commit()
    return row


def report(packages, collected_at=T0):
    return InventoryRequest(
        collected_at=collected_at,
        watchdog_version="1.0.0",
        collector_status={"os": "ok: 1"},
        packages=packages,
    )


def pkg(name, version, ecosystem="os", scope="system"):
    return {"ecosystem": ecosystem, "name": name, "version": version, "scope": scope}


def changes_for(db, device):
    return db.execute(
        select(PackageChange).where(PackageChange.device_id == device.id)
    ).scalars().all()


def current_for(db, device):
    rows = db.execute(
        select(DevicePackage).where(DevicePackage.device_id == device.id)
    ).scalars().all()
    return {(row.package.name, row.scope): row.version for row in rows}


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Flask_SQLAlchemy", "flask-sqlalchemy"),
        ("flask.sqlalchemy", "flask-sqlalchemy"),
        ("Flask--SQLAlchemy", "flask-sqlalchemy"),
        ("zope.interface", "zope-interface"),
    ],
)
def test_python_names_follow_pep503(raw, expected):
    assert normalize_name("python", raw) == expected


def test_other_ecosystems_are_case_folded_only():
    """npm and apt treat punctuation as significant, so it must survive."""
    assert normalize_name("npm", "@Scope/My_Pkg") == "@scope/my_pkg"
    assert normalize_name("os", "libSSL-Dev") == "libssl-dev"


# --------------------------------------------------------------------------- #
# Baseline and diffing
# --------------------------------------------------------------------------- #


def test_first_report_is_all_additions(db, device):
    result = apply_inventory(db, device, report([pkg("nginx", "1.24.0"), pkg("curl", "8.5.0")]))
    db.commit()

    assert result.package_count == 2
    assert result.change_count == 2
    assert {change.change_type for change in changes_for(db, device)} == {ChangeType.added}
    assert current_for(db, device) == {("nginx", "system"): "1.24.0", ("curl", "system"): "8.5.0"}


def test_identical_second_report_produces_no_changes(db, device):
    packages = [pkg("nginx", "1.24.0")]
    apply_inventory(db, device, report(packages))
    db.commit()

    result = apply_inventory(db, device, report(packages, collected_at=T0 + timedelta(hours=12)))
    db.commit()

    assert result.change_count == 0
    assert len(changes_for(db, device)) == 1  # just the original addition


def test_version_change_is_recorded_with_both_versions(db, device):
    apply_inventory(db, device, report([pkg("nginx", "1.24.0")]))
    db.commit()
    apply_inventory(
        db, device, report([pkg("nginx", "1.26.2")], collected_at=T0 + timedelta(hours=12))
    )
    db.commit()

    change = [c for c in changes_for(db, device) if c.change_type == ChangeType.version_changed]
    assert len(change) == 1
    assert change[0].old_version == "1.24.0"
    assert change[0].new_version == "1.26.2"
    assert current_for(db, device) == {("nginx", "system"): "1.26.2"}


def test_removal_is_recorded_and_current_state_drops_it(db, device):
    apply_inventory(db, device, report([pkg("nginx", "1.24.0"), pkg("curl", "8.5.0")]))
    db.commit()
    apply_inventory(
        db, device, report([pkg("nginx", "1.24.0")], collected_at=T0 + timedelta(hours=12))
    )
    db.commit()

    removed = [c for c in changes_for(db, device) if c.change_type == ChangeType.removed]
    assert len(removed) == 1
    assert removed[0].old_version == "8.5.0"
    assert removed[0].new_version is None
    assert ("curl", "system") not in current_for(db, device)


def test_first_seen_is_preserved_across_a_version_change(db, device):
    apply_inventory(db, device, report([pkg("nginx", "1.24.0")]))
    db.commit()
    later = T0 + timedelta(hours=12)
    apply_inventory(db, device, report([pkg("nginx", "1.26.0")], collected_at=later))
    db.commit()

    row = db.execute(
        select(DevicePackage).where(DevicePackage.device_id == device.id)
    ).scalar_one()
    assert row.first_seen_at == T0
    assert row.last_seen_at == later


# --------------------------------------------------------------------------- #
# The scope case that motivated the composite key
# --------------------------------------------------------------------------- #


def test_same_package_under_two_interpreters_does_not_collide(db, device):
    """Without `scope` in the key these two rows would fight over one row and
    every report would emit a false version_changed."""
    packages = [
        pkg("numpy", "1.26.4", ecosystem="python", scope="/usr/bin/python3.10"),
        pkg("numpy", "2.1.0", ecosystem="python", scope="/usr/bin/python3.12"),
    ]
    result = apply_inventory(db, device, report(packages))
    db.commit()

    assert result.change_count == 2
    assert current_for(db, device) == {
        ("numpy", "/usr/bin/python3.10"): "1.26.4",
        ("numpy", "/usr/bin/python3.12"): "2.1.0",
    }

    # The crucial part: reporting the same thing again is a no-op, not a flap.
    second = apply_inventory(db, device, report(packages, collected_at=T0 + timedelta(hours=12)))
    db.commit()
    assert second.change_count == 0


def test_one_package_catalogue_row_is_shared_across_scopes(db, device):
    apply_inventory(
        db,
        device,
        report(
            [
                pkg("numpy", "1.0", ecosystem="python", scope="/usr/bin/python3.10"),
                pkg("numpy", "2.0", ecosystem="python", scope="/usr/bin/python3.12"),
            ]
        ),
    )
    db.commit()

    rows = db.execute(
        select(DevicePackage).where(DevicePackage.device_id == device.id)
    ).scalars().all()
    assert len({row.package_id for row in rows}) == 1


# --------------------------------------------------------------------------- #
# Out-of-order delivery
# --------------------------------------------------------------------------- #


def test_older_report_is_recorded_but_not_applied(db, device):
    """A spooled report arriving after a newer one must not manufacture
    changes by diffing against state that has moved on."""
    apply_inventory(
        db, device, report([pkg("nginx", "1.26.0")], collected_at=T0 + timedelta(hours=12))
    )
    db.commit()

    stale = apply_inventory(db, device, report([pkg("nginx", "1.24.0")], collected_at=T0))
    db.commit()

    assert stale.superseded is True
    assert stale.change_count == 0
    assert stale.package_count == 1  # still counted, so the report is visible
    assert current_for(db, device) == {("nginx", "system"): "1.26.0"}


def test_duplicate_report_is_idempotent(db, device):
    """At-least-once delivery means the server sees repeats; a repeat must not
    double-count or re-diff."""
    packages = [pkg("nginx", "1.24.0")]
    apply_inventory(db, device, report(packages))
    db.commit()

    duplicate = apply_inventory(db, device, report(packages))
    db.commit()

    assert duplicate.superseded is True
    assert duplicate.change_count == 0
    assert len(changes_for(db, device)) == 1


def test_a_newer_report_still_applies_after_a_superseded_one(db, device):
    apply_inventory(db, device, report([pkg("nginx", "1.26.0")], collected_at=T0 + timedelta(hours=12)))
    db.commit()
    apply_inventory(db, device, report([pkg("nginx", "1.24.0")], collected_at=T0))
    db.commit()

    newest = apply_inventory(
        db, device, report([pkg("nginx", "1.27.0")], collected_at=T0 + timedelta(hours=24))
    )
    db.commit()

    assert newest.superseded is False
    assert newest.change_count == 1
    assert current_for(db, device) == {("nginx", "system"): "1.27.0"}


# --------------------------------------------------------------------------- #
# Degenerate input
# --------------------------------------------------------------------------- #


def test_duplicate_entries_in_one_report_collapse(db, device):
    """Two rows for the same identity would otherwise violate the primary key."""
    result = apply_inventory(
        db, device, report([pkg("nginx", "1.24.0"), pkg("nginx", "1.26.0")])
    )
    db.commit()

    assert result.change_count == 1
    assert current_for(db, device) == {("nginx", "system"): "1.26.0"}  # last wins


def test_empty_report_removes_everything_and_says_so(db, device):
    apply_inventory(db, device, report([pkg("nginx", "1.24.0")]))
    db.commit()

    result = apply_inventory(db, device, report([], collected_at=T0 + timedelta(hours=12)))
    db.commit()

    assert result.change_count == 1
    assert current_for(db, device) == {}


def test_naive_collected_at_is_treated_as_utc(db, device):
    result = apply_inventory(
        db, device, report([pkg("nginx", "1.24.0")], collected_at=T0.replace(tzinfo=None))
    )
    db.commit()
    assert result.collected_at == T0


def test_two_devices_do_not_interfere(db, device):
    other = Device(name="other-device")
    db.add(other)
    db.commit()

    apply_inventory(db, device, report([pkg("nginx", "1.24.0")]))
    apply_inventory(db, other, report([pkg("nginx", "1.26.0")]))
    db.commit()

    assert current_for(db, device) == {("nginx", "system"): "1.24.0"}
    assert current_for(db, other) == {("nginx", "system"): "1.26.0"}
