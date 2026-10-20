import base64
import json

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.agent import BackendResult
from app.main import create_app
from app.store import Store

H = {"Authorization": "Bearer tok", "X-Liva-User": "admin"}


class FakeBackend:
    calls = []

    def __init__(self, token):
        self.token = token

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def list_tools(self):
        return [{"type": "function", "function": {"name": n, "description": "", "parameters": {}}}
                for n in ("list_devices", "delete_device", "build_package")]

    async def call(self, name, args):
        FakeBackend.calls.append((name, args, self.token))
        if name == "build_package":
            card = {"kind": "download", "files": [{"name": "p.deb", "mime": "application/x-deb",
                                                   "data_b64": base64.b64encode(b"DEB").decode()}]}
            return BackendResult("built", [card])
        return BackendResult(f"{name} ok", [])


def make_client(replies, valid=True):
    queue = list(replies)

    async def chat_fn(messages, tools):
        return queue.pop(0)

    async def verify(token):
        if not valid:
            raise HTTPException(401, "expired")

    FakeBackend.calls = []
    app = create_app(store=Store(":memory:"), backend_factory=FakeBackend, chat_fn=chat_fn,
                     verify=verify, supervise=False)
    return TestClient(app)


def events(response):
    return [json.loads(line) for line in response.text.splitlines() if line]


def call(call_id, name, args=None):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args or {})}}


def test_healthz_needs_no_auth():
    client = make_client([])
    body = client.get("/healthz").json()
    assert body["ok"] is True and "groq_configured" in body


def test_endpoints_require_a_valid_session():
    client = make_client([], valid=False)
    assert client.get("/chats").status_code == 401
    assert client.get("/chats", headers=H).status_code == 401
    ok = make_client([])
    assert ok.get("/chats", headers={"X-Liva-User": "a"}).status_code == 401


def test_chat_roundtrip_streams_ndjson_and_persists():
    client = make_client([{"role": "assistant", "content": "hi there"}])
    chat = client.post("/chats", headers=H).json()
    r = client.post(f"/chats/{chat['id']}/messages", headers=H, json={"content": "hello", "bypass": False})
    assert r.headers["content-type"].startswith("application/x-ndjson")
    assert [e["type"] for e in events(r)] == ["message", "done"]
    detail = client.get(f"/chats/{chat['id']}", headers=H).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["chat"]["title"] == "hello"


def test_token_is_forwarded_to_the_tool_backend():
    client = make_client([{"role": "assistant", "content": None, "tool_calls": [call("c1", "list_devices")]},
                          {"role": "assistant", "content": "ok"}])
    chat = client.post("/chats", headers=H).json()
    client.post(f"/chats/{chat['id']}/messages", headers=H, json={"content": "list", "bypass": False})
    assert FakeBackend.calls == [("list_devices", {}, "tok")]


def test_approval_flow_end_to_end():
    client = make_client([{"role": "assistant", "content": None, "tool_calls": [call("c1", "delete_device", {"device": "w"})]},
                          {"role": "assistant", "content": "Deleted."}])
    chat = client.post("/chats", headers=H).json()
    first = events(client.post(f"/chats/{chat['id']}/messages", headers=H, json={"content": "del", "bypass": False}))
    assert [e["type"] for e in first] == ["pending_action", "done"]
    assert FakeBackend.calls == []

    detail = client.get(f"/chats/{chat['id']}", headers=H).json()
    assert detail["pending"][0]["call_id"] == "c1"

    blocked = client.post(f"/chats/{chat['id']}/messages", headers=H, json={"content": "more", "bypass": False})
    assert blocked.status_code == 409

    second = events(client.post(f"/chats/{chat['id']}/approvals/c1", headers=H, json={"approve": True, "bypass": False}))
    assert [e["type"] for e in second][-2:] == ["message", "done"]
    assert FakeBackend.calls == [("delete_device", {"device": "w"}, "tok")]


def test_denial_does_not_execute():
    client = make_client([{"role": "assistant", "content": None, "tool_calls": [call("c1", "delete_device", {"device": "w"})]},
                          {"role": "assistant", "content": "ok, not deleting"}])
    chat = client.post("/chats", headers=H).json()
    client.post(f"/chats/{chat['id']}/messages", headers=H, json={"content": "del", "bypass": False})
    client.post(f"/chats/{chat['id']}/approvals/c1", headers=H, json={"approve": False, "bypass": False})
    assert FakeBackend.calls == []


def test_approving_an_unknown_call_is_404():
    client = make_client([])
    chat = client.post("/chats", headers=H).json()
    r = client.post(f"/chats/{chat['id']}/approvals/nope", headers=H, json={"approve": True, "bypass": False})
    assert r.status_code == 404


def test_download_is_single_use_and_owner_bound():
    client = make_client([{"role": "assistant", "content": None, "tool_calls": [call("c1", "build_package")]},
                          {"role": "assistant", "content": "ready"}])
    chat = client.post("/chats", headers=H).json()
    evs = events(client.post(f"/chats/{chat['id']}/messages", headers=H, json={"content": "pkg", "bypass": False}))
    card = [e for e in evs if e["type"] == "card"][0]["card"]
    did = card["files"][0]["download_id"]

    assert client.get(f"/downloads/{did}", headers={**H, "Authorization": "Bearer other"}).status_code == 404
    got = client.get(f"/downloads/{did}", headers=H)
    assert got.status_code == 200 and got.content == b"DEB"
    assert "p.deb" in got.headers["content-disposition"]
    assert client.get(f"/downloads/{did}", headers=H).status_code == 404


def test_chats_are_private_to_their_owner_and_deletable():
    client = make_client([])
    chat = client.post("/chats", headers=H).json()
    other = {**H, "X-Liva-User": "bob"}
    assert client.get(f"/chats/{chat['id']}", headers=other).status_code == 404
    assert client.delete(f"/chats/{chat['id']}", headers=H).status_code == 204
    assert client.get(f"/chats/{chat['id']}", headers=H).status_code == 404


def test_missing_groq_key_surfaces_as_an_error_event(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    async def verify(token):
        return None

    app = create_app(store=Store(":memory:"), backend_factory=FakeBackend, verify=verify, supervise=False)
    client = TestClient(app)
    chat = client.post("/chats", headers=H).json()
    evs = events(client.post(f"/chats/{chat['id']}/messages", headers=H, json={"content": "hi", "bypass": False}))
    assert evs[0]["type"] == "error" and "GROQ_API_KEY" in evs[0]["message"]


def test_audit_endpoint_lists_tool_calls():
    client = make_client([{"role": "assistant", "content": None, "tool_calls": [call("c1", "list_devices")]},
                          {"role": "assistant", "content": "ok"}])
    chat = client.post("/chats", headers=H).json()
    client.post(f"/chats/{chat['id']}/messages", headers=H, json={"content": "x", "bypass": False})
    rows = client.get("/audit", headers=H).json()
    assert rows[0]["tool"] == "list_devices" and rows[0]["mode"] == "auto"


def test_a_call_interrupted_mid_turn_does_not_wedge_or_rerun(monkeypatch):
    client = make_client([{"role": "assistant", "content": "back again"}])
    chat = client.post("/chats", headers=H).json()
    store = client.app.state.store
    store.add_message(chat["id"], {"role": "user", "content": "list"})
    store.add_message(chat["id"], {"role": "assistant", "content": None,
                                   "tool_calls": [call("c1", "list_devices")]})   # stream died here
    evs = events(client.post(f"/chats/{chat['id']}/messages", headers=H, json={"content": "hello?", "bypass": False}))
    assert [e["type"] for e in evs] == ["message", "done"]
    assert FakeBackend.calls == []            # the dangling call was settled, not re-executed
    roles = [m["role"] for m in store.model_messages(chat["id"])]
    assert roles == ["user", "assistant", "tool", "user", "assistant"]
