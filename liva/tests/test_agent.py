import asyncio
import json

from app.agent import BackendResult, Turn, run_turn, unresolved_calls
from app.downloads import Downloads
from app.groq import GroqError
from app.store import Store

KEY = "sk_dev_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
TOOLS = [{"type": "function", "function": {"name": n, "description": "", "parameters": {"type": "object", "properties": {}}}}
         for n in ("list_devices", "create_device", "delete_device")]


class FakeBackend:
    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []

    async def list_tools(self):
        return TOOLS

    async def call(self, name, args):
        self.calls.append((name, args))
        result = self.results.get(name, BackendResult(f"{name} ok", []))
        return result(args) if callable(result) else result


def tool_call(call_id, name, args=None):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args or {})}}


def scripted(replies):
    """A chat function that returns queued replies (dict) or raises queued GroqErrors."""
    queue = list(replies)
    seen = []

    async def chat(messages, tools):
        seen.append(messages)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    chat.seen = seen
    return chat


def setup(replies, backend=None, bypass=False, max_steps=8):
    store = Store(":memory:")
    chat_row = store.create_chat("admin")
    store.add_message(chat_row["id"], {"role": "user", "content": "do it"})
    turn = Turn(store=store, chat_id=chat_row["id"], owner="admin", owner_key="ok",
                backend=backend or FakeBackend(), chat=scripted(replies), downloads=Downloads(),
                bypass=bypass, max_steps=max_steps)
    return store, turn


def drain(turn):
    async def go():
        return [event async for event in run_turn(turn)]
    return asyncio.run(go())


def types(events):
    return [e["type"] for e in events]


def assistant(content=None, calls=None):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = calls
    return msg


def test_plain_answer():
    _, turn = setup([assistant("hello")])
    events = drain(turn)
    assert types(events) == ["message", "done"]
    assert events[0]["content"] == "hello"


def test_auto_tool_runs_then_model_summarises():
    backend = FakeBackend()
    store, turn = setup([assistant(None, [tool_call("c1", "list_devices")]), assistant("2 devices")], backend)
    events = drain(turn)
    assert types(events) == ["tool_call", "tool_result", "message", "done"]
    assert backend.calls == [("list_devices", {})]
    assert events[0]["mode"] == "auto"
    audit = store.list_audit("admin")[0]
    assert audit["tool"] == "list_devices" and audit["mode"] == "auto"


def test_approval_tool_pauses_without_executing():
    backend = FakeBackend()
    store, turn = setup([assistant(None, [tool_call("c1", "delete_device", {"device": "web-01"})])], backend)
    events = drain(turn)
    assert types(events) == ["pending_action", "done"]
    assert events[0]["call_id"] == "c1" and "web-01" in events[0]["description"]
    assert backend.calls == []
    assert store.get_pending("c1")["status"] == "pending"


def test_approve_resumes_and_executes():
    backend = FakeBackend()
    store, turn = setup([assistant(None, [tool_call("c1", "delete_device", {"device": "web-01"})]),
                         assistant("Deleted.")], backend)
    drain(turn)
    store.set_pending_status("c1", "approved")
    events = drain(turn)
    assert backend.calls == [("delete_device", {"device": "web-01"})]
    assert events[0]["mode"] == "approved"
    assert types(events)[-2:] == ["message", "done"]


def test_deny_tells_the_model_and_does_not_execute():
    backend = FakeBackend()
    store, turn = setup([assistant(None, [tool_call("c1", "delete_device", {"device": "web-01"})]),
                         assistant("Okay, leaving it.")], backend)
    drain(turn)
    store.set_pending_status("c1", "denied")
    events = drain(turn)
    assert backend.calls == []
    result = [e for e in events if e["type"] == "tool_result"][0]
    assert result["ok"] is False and result["mode"] == "denied"
    tool_msgs = [m for m in store.model_messages(turn.chat_id) if m["role"] == "tool"]
    assert "declined" in tool_msgs[0]["content"]


def test_bypass_skips_approval_and_is_audited():
    backend = FakeBackend()
    store, turn = setup([assistant(None, [tool_call("c1", "delete_device", {"device": "w"})]),
                         assistant("done")], backend, bypass=True)
    events = drain(turn)
    assert "pending_action" not in types(events)
    assert backend.calls == [("delete_device", {"device": "w"})]
    assert store.list_audit("admin")[0]["mode"] == "bypassed"


def test_unknown_tool_and_bad_json_are_rejected_not_executed():
    backend = FakeBackend()
    bad = {"id": "c2", "type": "function", "function": {"name": "list_devices", "arguments": "{not json"}}
    store, turn = setup([assistant(None, [tool_call("c1", "rm_rf"), bad]), assistant("sorry")], backend)
    events = drain(turn)
    assert backend.calls == []
    assert [e["ok"] for e in events if e["type"] == "tool_result"] == [False, False]


def test_secrets_go_to_the_screen_but_not_the_model_or_the_database():
    card = {"kind": "credentials", "device": "web-01", "api_key": KEY, "commands": [f"enroll --key {KEY}"],
            "files": [], "warnings": []}
    backend = FakeBackend({"create_device": BackendResult(f"created, key leaked: {KEY}", [card])})
    store, turn = setup([assistant(None, [tool_call("c1", "create_device", {"name": "web-01"})]),
                         assistant("Here are the creds.")], backend)
    events = drain(turn)
    live = [e for e in events if e["type"] == "card"][0]["card"]
    assert live["api_key"] == KEY
    stored = json.dumps(store.display_messages(turn.chat_id)) + json.dumps(store.list_audit("admin"))
    assert KEY not in stored
    sent_to_model = json.dumps(turn.chat.seen)
    assert KEY not in sent_to_model


def test_step_cap_stops_a_runaway_loop():
    replies = [assistant(None, [tool_call(f"c{i}", "list_devices")]) for i in range(10)]
    _, turn = setup(replies, max_steps=3)
    events = drain(turn)
    assert events[-2]["type"] == "error" and "3 steps" in events[-2]["message"]


def test_rate_limit_is_reported_not_retried():
    _, turn = setup([GroqError(429, "Groq rate limit reached. Retry in 9s.", retry_after=9)])
    events = drain(turn)
    assert events[0]["type"] == "error" and events[0]["retry_after"] == 9


def test_malformed_tool_call_gets_one_repair_attempt():
    chat_calls = [GroqError(400, "bad", code="tool_use_failed"), assistant("recovered")]
    _, turn = setup(chat_calls)
    events = drain(turn)
    assert types(events) == ["message", "done"]
    assert "malformed" in json.dumps(turn.chat.seen[1])

    _, turn = setup([GroqError(400, "bad", code="tool_use_failed"), GroqError(400, "bad again", code="tool_use_failed")])
    assert drain(turn)[0]["type"] == "error"


def test_backend_failure_is_a_tool_error_not_a_crash():
    class Boom(FakeBackend):
        async def call(self, name, args):
            raise RuntimeError("mcp down")

    _, turn = setup([assistant(None, [tool_call("c1", "list_devices")]), assistant("could not")], Boom())
    events = drain(turn)
    assert [e for e in events if e["type"] == "tool_result"][0]["ok"] is False
    assert types(events)[-1] == "done"


def test_unresolved_calls_only_looks_at_the_last_assistant_message():
    history = [
        {"role": "assistant", "content": None, "tool_calls": [tool_call("a", "x"), tool_call("b", "y")]},
        {"role": "tool", "tool_call_id": "a", "content": "ok"},
    ]
    assert [c["id"] for c in unresolved_calls(history)] == ["b"]
    assert unresolved_calls([{"role": "assistant", "content": "hi"}]) == []
    assert unresolved_calls([]) == []
