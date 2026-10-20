from app import tiers
from app.security import mask_key, redact


def test_mask_key_keeps_prefix_and_tail_only():
    key = "sk_dev_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    masked = mask_key(key)
    assert masked.startswith("sk_dev_")
    assert masked.endswith("2345")
    assert "ABCDEFGH" not in masked


def test_redact_replaces_keys_anywhere_in_text():
    key = "sk_dev_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    text = f"run enroll --key {key} now, again {key}"
    out = redact(text)
    assert key not in out
    assert out.count("sk_dev_…2345") == 2


def test_redact_leaves_ordinary_text_alone():
    assert redact("device sk-web is fine, sk_dev_ short") == "device sk-web is fine, sk_dev_ short"


def test_approval_set_is_exactly_the_destructive_tools():
    approval = {n for n, t in tiers.TIERS.items() if t == tiers.APPROVAL}
    assert approval == {"delete_device", "rotate_device_key", "update_config", "clear_quarantine"}


def test_tier_of_unknown_tool_is_none():
    assert tiers.tier_of("rm_rf") is None
    assert tiers.tier_of("list_devices") == tiers.AUTO


def test_describe_is_plain_english_and_includes_arguments():
    assert "web-01" in tiers.describe("delete_device", {"device": "web-01"})
    assert "web-01" in tiers.describe("rotate_device_key", {"device": "web-01"})
    text = tiers.describe("update_config", {"poll_interval_seconds": 60})
    assert "poll_interval_seconds" in text and "60" in text
    assert tiers.describe("clear_quarantine", {})
