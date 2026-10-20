import base64

from app.cards import split_card
from app.downloads import Downloads

KEY = "sk_dev_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"


def card():
    return {
        "kind": "credentials", "device": "web-01", "api_key": KEY,
        "commands": [f"sudo sentinel-watchdog enroll --server https://s --key {KEY}"],
        "files": [{"name": "p.deb", "mime": "application/x-deb",
                   "data_b64": base64.b64encode(b"DEB").decode()}],
        "warnings": [],
    }


def test_downloads_are_single_use_and_owner_bound():
    now = [0.0]
    store = Downloads(ttl=10, clock=lambda: now[0])
    did = store.put("owner-a", "p.deb", "application/x-deb", b"DEB")
    assert store.take(did, "owner-b") is None
    assert store.take(did, "owner-a") == ("p.deb", "application/x-deb", b"DEB")
    assert store.take(did, "owner-a") is None


def test_downloads_expire():
    now = [0.0]
    store = Downloads(ttl=10, clock=lambda: now[0])
    did = store.put("o", "n", "m", b"x")
    now[0] = 11.0
    assert store.take(did, "o") is None


def test_split_card_live_keeps_the_key_and_swaps_bytes_for_a_download_id():
    downloads = Downloads()
    live, stored = split_card(card(), downloads, "o")
    assert live["api_key"] == KEY
    f = live["files"][0]
    assert "data_b64" not in f and f["size"] == 3
    assert downloads.take(f["download_id"], "o")[2] == b"DEB"


def test_split_card_stored_copy_has_no_secret_and_no_bytes():
    _, stored = split_card(card(), Downloads(), "o")
    assert KEY not in str(stored)
    assert stored["secret_shown_once"] is True
    assert "data_b64" not in str(stored) and "download_id" not in str(stored)
    assert stored["files"] == [{"name": "p.deb", "size": 3}]
