"""The conversation loop: model <-> tools, with approval pauses.

State lives in the stored messages, not in memory: whatever tool calls in the
last assistant message have no result yet are what remains to be done. That is
what makes an approval click a plain "run the loop again".
"""

import json
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from . import tiers
from .cards import split_card
from .downloads import Downloads
from .groq import GroqError
from .security import redact
from .store import Store

SYSTEM_PROMPT = (
    "You are Liva, the assistant built into Sentinel, a device inventory and management system "
    "for Ubuntu and Debian machines. You help the administrator inspect devices, installed "
    "software and changes, and manage devices, using only your tools.\n"
    "- Answer from tool results only. Never invent devices, versions or numbers. If a tool "
    "fails, say so plainly.\n"
    "- Device names or UUIDs both work for the `device` argument.\n"
    "- Creating a device or rotating a key shows the credentials, the .deb and install "
    "commands on the administrator's screen. You never see the key, so do not try to write "
    "it out; say it is shown once above and can be rotated if lost.\n"
    "- Deleting, rotating keys, changing configuration and clearing quarantine need the "
    "administrator's approval. If an action is waiting for approval, say so; do not claim it "
    "is done.\n"
    "- Be concise."
)

# Tool results older than the latest question are shortened in the request to
# the model, so a long chat cannot outgrow Groq's context or rate limits.
OLD_TOOL_RESULT_CHARS = 1000

NUDGE = (
    "Your last tool call was malformed. Call the tool again with valid JSON arguments that "
    "match its schema."
)


@dataclass
class BackendResult:
    text: str
    ui: list[dict]
    is_error: bool = False


class Backend(Protocol):
    async def list_tools(self) -> list[dict]: ...
    async def call(self, name: str, args: dict) -> BackendResult: ...


class McpBackend:
    """Talks to Liva's own MCP server, forwarding the administrator's token."""

    def __init__(self, url: str, token: str) -> None:
        self._url = url
        self._token = token
        self._stack = AsyncExitStack()
        self._session: ClientSession | None = None

    async def __aenter__(self) -> "McpBackend":
        read, write, _ = await self._stack.enter_async_context(
            streamablehttp_client(self._url, headers={"Authorization": f"Bearer {self._token}"})
        )
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._stack.aclose()

    async def list_tools(self) -> list[dict]:
        result = await self._session.list_tools()
        return [
            {"type": "function", "function": {
                "name": tool.name, "description": tool.description or "", "parameters": tool.inputSchema}}
            for tool in result.tools
        ]

    async def call(self, name: str, args: dict) -> BackendResult:
        result = await self._session.call_tool(name, args)
        text: list[str] = []
        ui: list[dict] = []
        for block in result.content:
            if block.type != "text":
                continue
            audience = (block.annotations.audience if block.annotations else None) or []
            if "user" in audience and "assistant" not in audience:
                try:
                    ui.append(json.loads(block.text))
                except ValueError:
                    pass
            else:
                text.append(block.text)
        return BackendResult("\n".join(text), ui, bool(result.isError))


@dataclass
class Turn:
    store: Store
    chat_id: str
    owner: str
    owner_key: str
    backend: Backend
    chat: Callable[[list[dict], list[dict]], Awaitable[dict]]
    downloads: Downloads
    bypass: bool
    max_steps: int


def unresolved_calls(history: list[dict]) -> list[dict]:
    for index in range(len(history) - 1, -1, -1):
        message = history[index]
        if message["role"] == "assistant":
            answered = {m.get("tool_call_id") for m in history[index + 1:] if m["role"] == "tool"}
            return [call for call in message.get("tool_calls") or [] if call["id"] not in answered]
    return []


def for_model(history: list[dict]) -> list[dict]:
    last_user = max((i for i, m in enumerate(history) if m["role"] == "user"), default=-1)
    out = []
    for index, message in enumerate(history):
        content = message.get("content") or ""
        if message["role"] == "tool" and index < last_user and len(content) > OLD_TOOL_RESULT_CHARS:
            message = {**message, "content": content[:OLD_TOOL_RESULT_CHARS] + " ... [older result shortened]"}
        out.append(message)
    return out


def _finish(turn: Turn, call_id: str, name: str, args: dict, text: str, *, ok: bool,
            tier: str | None, mode: str, started: float | None = None) -> dict:
    text = redact(text)
    turn.store.add_message(turn.chat_id, {"role": "tool", "tool_call_id": call_id, "content": text})
    turn.store.add_audit(
        owner=turn.owner, chat_id=turn.chat_id, tool=name, args=args, tier=tier or "unknown",
        mode=mode, ok=ok, detail=text[:500],
        duration_ms=int((time.monotonic() - started) * 1000) if started else 0,
    )
    return {"type": "tool_result", "name": name, "ok": ok, "summary": text[:300], "mode": mode}


async def _handle_call(turn: Turn, call: dict, known: set[str]) -> AsyncIterator[dict]:
    call_id, name = call["id"], call["function"]["name"]
    try:
        args = json.loads(call["function"].get("arguments") or "{}")
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
    except ValueError as exc:
        yield _finish(turn, call_id, name, {}, f"Invalid arguments: {exc}", ok=False, tier=None, mode="rejected")
        return

    tier = tiers.tier_of(name)
    if tier is None or name not in known:
        yield _finish(turn, call_id, name, args, f"Unknown tool {name!r}.", ok=False, tier=None, mode="rejected")
        return

    mode = "auto"
    if tier == tiers.APPROVAL:
        if turn.bypass:
            mode = "bypassed"
        else:
            row = turn.store.get_pending(turn.chat_id, call_id)
            # An approval covers one specific call. A reused id, or the same id
            # with different arguments, is a new request and must be asked again.
            if row is not None and (
                row["status"] == "used" or row["tool"] != name or row["arguments"] != args
            ):
                turn.store.delete_pending(turn.chat_id, call_id)
                row = None
            if row is None:
                turn.store.create_pending(call_id, turn.chat_id, name, args, tiers.describe(name, args))
                row = turn.store.get_pending(turn.chat_id, call_id)
            if row["status"] == "pending":
                yield {"type": "pending_action", "call_id": call_id, "tool": name,
                       "arguments": args, "description": row["description"]}
                return
            if row["status"] == "denied":
                turn.store.set_pending_status(turn.chat_id, call_id, "used")
                yield _finish(turn, call_id, name, args,
                              "The user declined this action. Do not retry it unless they ask.",
                              ok=False, tier=tier, mode="denied")
                return
            mode = "approved"

    yield {"type": "tool_call", "name": name, "arguments": args, "mode": mode}
    started = time.monotonic()
    try:
        result = await turn.backend.call(name, args)
    except Exception as exc:  # a tool-server fault must not end the conversation
        result = BackendResult(f"Tool failed: {exc}", [], True)

    if mode == "approved":
        turn.store.set_pending_status(turn.chat_id, call_id, "used")

    cards = [split_card(card, turn.downloads, turn.owner_key) for card in result.ui]
    yield _finish(turn, call_id, name, args, result.text, ok=not result.is_error,
                  tier=tier, mode=mode, started=started)
    for live, stored in cards:
        turn.store.add_message(turn.chat_id, {"role": "ui", "card": stored})
        yield {"type": "card", "card": live}


async def run_turn(turn: Turn) -> AsyncIterator[dict]:
    try:
        tools = await turn.backend.list_tools()
    except Exception as exc:
        yield {"type": "error", "message": f"Liva's tool server is unavailable: {exc}"}
        yield {"type": "done"}
        return
    known = {tool["function"]["name"] for tool in tools}

    steps = 0
    nudge: list[dict] = []
    while True:
        history = turn.store.model_messages(turn.chat_id)

        remaining = unresolved_calls(history)
        if remaining:
            for call in remaining:
                paused = False
                async for event in _handle_call(turn, call, known):
                    paused = paused or event["type"] == "pending_action"
                    yield event
                if paused:
                    yield {"type": "done"}
                    return
            continue

        if steps >= turn.max_steps:
            yield {"type": "error",
                   "message": f"Stopped after {turn.max_steps} steps without finishing. Ask me to continue."}
            yield {"type": "done"}
            return
        steps += 1

        try:
            reply = await turn.chat([{"role": "system", "content": SYSTEM_PROMPT}] + for_model(history) + nudge, tools)
        except GroqError as exc:
            if exc.code == "tool_use_failed" and not nudge:
                nudge = [{"role": "user", "content": NUDGE}]
                continue
            yield {"type": "error", "message": exc.message, "retry_after": exc.retry_after}
            yield {"type": "done"}
            return
        nudge = []

        turn.store.add_message(turn.chat_id, reply)
        if reply.get("content"):
            yield {"type": "message", "content": reply["content"]}
        if not reply.get("tool_calls"):
            yield {"type": "done"}
            return
