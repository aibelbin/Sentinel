"""Regression tests for defects found in review.

Each test names the failure it prevents, so a future change that reintroduces
one fails here rather than in production.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import AdminSession, Device, InventoryReport
from app.schemas import DeviceCreate, InventoryRequest
from app.services.inventory import apply_inventory


@pytest.fixture
def device(db):
    row = Device(name="skew-target")
    db.add(row)
    db.commit()
    return row


def report(packages, collected_at):
    return InventoryRequest(
        collected_at=collected_at,
        agent_version="1.0.0",
        collector_status={},
        packages=packages,
    )


def pkg(name, version):
    return {"ecosystem": "os", "name": name, "version": version, "scope": "system"}


# --------------------------------------------------------------------------- #
# A forward-skewed device clock must not freeze inventory permanently
# --------------------------------------------------------------------------- #


def test_future_collected_at_is_clamped(db, device):
    far_future = datetime.now(timezone.utc) + timedelta(days=365 * 4)
    result = apply_inventory(db, device, report([pkg("nginx", "1.24.0")], far_future))
    db.commit()

    assert result.superseded is False
    # Clamped, so it cannot become an unreachable high-water mark.
    assert result.collected_at <= datetime.now(timezone.utc) + timedelta(seconds=5)


def test_a_skewed_report_does_not_block_later_correct_reports(db, device):
    """The original defect: one report stamped years ahead made every
    subsequent report compare as older, so the device's inventory and change
    log froze with no in-app way to recover."""
    apply_inventory(
        db, device, report([pkg("nginx", "1.24.0")], datetime.now(timezone.utc) + timedelta(days=365 * 4))
    )
    db.commit()

    later = apply_inventory(
        db, device, report([pkg("nginx", "1.26.0")], datetime.now(timezone.utc))
    )
    db.commit()

    assert later.superseded is False
    assert later.change_count == 1

    rows = db.execute(
        select(InventoryReport).where(InventoryReport.device_id == device.id)
    ).scalars().all()
    assert len(rows) == 2
    assert sum(1 for row in rows if row.superseded) == 0


# --------------------------------------------------------------------------- #
# Blank device names
# --------------------------------------------------------------------------- #


def test_whitespace_only_device_name_is_rejected():
    """min_length=1 alone passes "   ", which then stored as an empty name and
    made the delete confirmation unsatisfiable."""
    with pytest.raises(ValueError):
        DeviceCreate(name="   ")


def test_device_name_is_stripped(client, auth):
    created = client.post("/api/v1/devices", json={"name": "  web-01  "}, headers=auth)
    assert created.status_code == 201
    assert created.json()["name"] == "web-01"


def test_blank_name_is_a_validation_error_over_http(client, auth):
    assert client.post("/api/v1/devices", json={"name": "  "}, headers=auth).status_code == 422
    assert client.patch(
        f"/api/v1/devices/{client.post('/api/v1/devices', json={'name': 'x'}, headers=auth).json()['device_id']}",
        json={"name": "   "},
        headers=auth,
    ).status_code == 422


# --------------------------------------------------------------------------- #
# Session lifetime
# --------------------------------------------------------------------------- #


def test_session_has_an_absolute_lifetime(client, auth, db):
    """Sliding expiry alone meant a token used every few hours never expired,
    so a stolen token stayed valid indefinitely."""
    assert client.get("/api/v1/devices", headers=auth).status_code == 200

    session = db.execute(select(AdminSession)).scalar_one()
    session.created_at = datetime.now(timezone.utc) - timedelta(days=2)
    db.commit()

    response = client.get("/api/v1/devices", headers=auth)
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "session_expired"

    # The row is removed, so the prune job is not the only thing reaping it.
    assert db.execute(select(AdminSession)).scalar_one_or_none() is None


def test_sliding_expiry_never_exceeds_the_absolute_deadline(client, auth, db):
    client.get("/api/v1/devices", headers=auth)
    session = db.execute(select(AdminSession)).scalar_one()
    created = session.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)

    expires = session.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)

    assert expires <= created + timedelta(hours=24, seconds=5)


# --------------------------------------------------------------------------- #
# prune --days 0
# --------------------------------------------------------------------------- #


def test_prune_days_zero_is_honoured(db, device, capsys):
    """`args.days or default` silently turned an explicit 0 into 30 and
    reported success for a prune that never happened."""
    from app.cli import main

    apply_inventory(db, device, report([pkg("nginx", "1.24.0")], datetime.now(timezone.utc)))
    db.commit()
    assert db.execute(select(InventoryReport)).scalars().all()

    main(["prune", "--days", "0"])

    assert db.execute(select(InventoryReport)).scalars().all() == []
    assert "older than 0d" in capsys.readouterr().out
