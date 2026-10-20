import time

from app.store import Store

KEY = "sk_dev_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"


def test_chats_are_scoped_to_their_owner():
    store = Store(":memory:")
    mine = store.create_chat("admin")
    store.create_chat("someone-else")
    assert [c["id"] for c in store.list_chats("admin")] == [mine["id"]]
    assert store.get_chat(mine["id"], "someone-else") is None
    store.delete_chat(mine["id"], "someone-else")
    assert store.get_chat(mine["id"], "admin") is not None
    store.delete_chat(mine["id"], "admin")
    assert store.get_chat(mine["id"], "admin") is None


def test_ui_messages_are_kept_for_display_but_never_sent_to_the_model():
    store = Store(":memory:")
    chat = store.create_chat("admin")
    store.add_message(chat["id"], {"role": "user", "content": "hi"})
    store.add_message(chat["id"], {"role": "ui", "card": {"kind": "download"}})
    store.add_message(chat["id"], {"role": "assistant", "content": "hello"})
    assert [m["role"] for m in store.model_messages(chat["id"])] == ["user", "assistant"]
    assert [m["role"] for m in store.display_messages(chat["id"])] == ["user", "ui", "assistant"]


def test_pending_lifecycle():
    store = Store(":memory:")
    chat = store.create_chat("admin")
    store.create_pending("call_1", chat["id"], "delete_device", {"device": "web-01"}, "Delete web-01")
    assert store.get_pending("call_1")["status"] == "pending"
    assert store.list_pending(chat["id"])[0]["arguments"] == {"device": "web-01"}
    store.set_pending_status("call_1", "approved")
    assert store.get_pending("call_1")["status"] == "approved"
    assert store.list_pending(chat["id"]) == []


def test_audit_redacts_secrets_even_if_one_slips_in():
    store = Store(":memory:")
    chat = store.create_chat("admin")
    store.add_audit(owner="admin", chat_id=chat["id"], tool="create_device",
                    args={"name": f"x {KEY}"}, tier="auto", mode="auto", ok=True,
                    detail=f"key {KEY}", duration_ms=12)
    row = store.list_audit("admin")[0]
    assert KEY not in str(row)
    assert row["tool"] == "create_device" and row["mode"] == "auto"


def test_prune_removes_old_chats_and_audit():
    store = Store(":memory:")
    old = store.create_chat("admin")
    store.add_audit(owner="admin", chat_id=old["id"], tool="t", args={}, tier="auto",
                    mode="auto", ok=True, detail="", duration_ms=1)
    removed = store.prune(30, now=time.time() + 31 * 86400)
    assert removed >= 2
    assert store.get_chat(old["id"], "admin") is None
    assert store.list_audit("admin") == []
    fresh = store.create_chat("admin")
    assert store.prune(30) == 0
    assert store.get_chat(fresh["id"], "admin") is not None
