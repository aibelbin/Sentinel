"""Liva's chat service. Reachable only from the dashboard, over the Compose network."""

import asyncio
import hashlib
import json
import logging
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Awaitable, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .agent import McpBackend, Turn, run_turn
from .config import get_settings
from .downloads import Downloads
from .groq import chat_completion
from .sentinel import Sentinel, SentinelError
from .store import Store

log = logging.getLogger("liva")


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    bypass: bool = False


class ApprovalIn(BaseModel):
    approve: bool
    bypass: bool = False


@dataclass
class Session:
    token: str
    owner: str
    owner_key: str


async def _verify_with_sentinel(token: str) -> None:
    """The session must still be valid on Sentinel; a revoked token cuts Liva off."""
    client = Sentinel(token, get_settings().sentinel_url)
    try:
        await client.get("/config")
    except SentinelError as exc:
        if exc.status == 401:
            raise HTTPException(401, "Your Sentinel session has expired.") from exc
        raise HTTPException(502, f"Cannot verify the session: {exc.message}") from exc
    finally:
        await client.aclose()


async def _supervise_mcp() -> None:
    """Keeps the tool server running; a crash there never reaches the chat service."""
    while True:
        process = await asyncio.create_subprocess_exec(sys.executable, "-m", "app.mcp_server")
        try:
            code = await process.wait()
            log.warning("MCP server exited with %s; restarting", code)
        except asyncio.CancelledError:
            process.terminate()
            await process.wait()
            raise
        await asyncio.sleep(1)


async def _retention(app: FastAPI, days: int) -> None:
    while True:
        try:
            app.state.store.prune(days)
        except Exception:
            log.exception("retention sweep failed")
        await asyncio.sleep(86400)


def create_app(
    *,
    store: Store | None = None,
    backend_factory: Callable[[str], McpBackend] | None = None,
    chat_fn: Callable[..., Awaitable[dict]] | None = None,
    verify: Callable[[str], Awaitable[None]] | None = None,
    supervise: bool = True,
) -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        get_store(app)
        tasks = [asyncio.create_task(_retention(app, settings.retention_days))]
        if supervise:
            tasks.append(asyncio.create_task(_supervise_mcp()))
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def get_store(app: FastAPI) -> Store:
        # Opened on first use so importing this module never touches the disk.
        if app.state.store is None:
            app.state.store = Store(settings.db_path)
        return app.state.store

    app = FastAPI(title="Liva", lifespan=lifespan)
    app.state.store = store
    app.state.downloads = Downloads()
    backend_factory = backend_factory or (lambda token: McpBackend(settings.mcp_url, token))
    verify = verify or _verify_with_sentinel

    def db_dep(request: Request) -> Store:
        return get_store(request.app)

    async def session(
        authorization: str | None = Header(default=None),
        x_liva_user: str | None = Header(default=None),
    ) -> Session:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "Missing session token.")
        token = authorization.split(" ", 1)[1].strip()
        await verify(token)
        return Session(token, x_liva_user or "admin", hashlib.sha256(token.encode()).hexdigest()[:16])

    def owned_chat(db: Store, chat_id: str, who: Session) -> dict:
        chat = db.get_chat(chat_id, who.owner)
        if chat is None:
            raise HTTPException(404, "No such chat.")
        return chat

    def stream(request: Request, db: Store, who: Session, chat_id: str, bypass: bool) -> StreamingResponse:
        async def chat(messages: list[dict], tools: list[dict]) -> dict:
            if chat_fn is not None:
                return await chat_fn(messages, tools)
            return await chat_completion(messages, tools, settings=get_settings())

        async def body():
            try:
                async with backend_factory(who.token) as backend:
                    turn = Turn(
                        store=db, chat_id=chat_id, owner=who.owner, owner_key=who.owner_key,
                        backend=backend, chat=chat, downloads=request.app.state.downloads,
                        bypass=bypass, max_steps=settings.max_steps,
                    )
                    async for event in run_turn(turn):
                        yield json.dumps(event) + "\n"
            except Exception as exc:
                log.exception("turn failed")
                yield json.dumps({"type": "error", "message": f"Liva hit a problem: {exc}"}) + "\n"
                yield json.dumps({"type": "done"}) + "\n"

        return StreamingResponse(body(), media_type="application/x-ndjson")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "groq_configured": bool(get_settings().groq_api_key)}

    @app.get("/chats")
    def list_chats(who: Session = Depends(session), db: Store = Depends(db_dep)) -> list[dict]:
        return db.list_chats(who.owner)

    @app.post("/chats")
    def create_chat(who: Session = Depends(session), db: Store = Depends(db_dep)) -> dict:
        return db.create_chat(who.owner)

    @app.get("/chats/{chat_id}")
    def get_chat(chat_id: str, who: Session = Depends(session), db: Store = Depends(db_dep)) -> dict:
        chat = owned_chat(db, chat_id, who)
        pending = [
            {"call_id": p["call_id"], "tool": p["tool"], "arguments": p["arguments"],
             "description": p["description"]}
            for p in db.list_pending(chat_id)
        ]
        return {"chat": chat, "messages": db.display_messages(chat_id), "pending": pending}

    @app.delete("/chats/{chat_id}", status_code=204)
    def delete_chat(chat_id: str, who: Session = Depends(session), db: Store = Depends(db_dep)) -> Response:
        owned_chat(db, chat_id, who)
        db.delete_chat(chat_id, who.owner)
        return Response(status_code=204)

    @app.post("/chats/{chat_id}/messages")
    def post_message(
        chat_id: str, body: MessageIn, request: Request,
        who: Session = Depends(session), db: Store = Depends(db_dep),
    ) -> StreamingResponse:
        chat = owned_chat(db, chat_id, who)
        if db.list_pending(chat_id):
            raise HTTPException(409, "Approve or deny the pending action first.")
        if chat["title"] == "New chat":
            db.set_title(chat_id, body.content.strip()[:40])
        db.add_message(chat_id, {"role": "user", "content": body.content})
        return stream(request, db, who, chat_id, body.bypass)

    @app.post("/chats/{chat_id}/approvals/{call_id}")
    def resolve_approval(
        chat_id: str, call_id: str, body: ApprovalIn, request: Request,
        who: Session = Depends(session), db: Store = Depends(db_dep),
    ) -> StreamingResponse:
        owned_chat(db, chat_id, who)
        row = db.get_pending(call_id)
        if row is None or row["chat_id"] != chat_id or row["status"] != "pending":
            raise HTTPException(404, "No such pending action.")
        db.set_pending_status(call_id, "approved" if body.approve else "denied")
        return stream(request, db, who, chat_id, body.bypass)

    @app.get("/downloads/{download_id}")
    def download(download_id: str, request: Request, who: Session = Depends(session)) -> Response:
        item = request.app.state.downloads.take(download_id, who.owner_key)
        if item is None:
            raise HTTPException(404, "That download has expired or was already used.")
        name, mime, data = item
        return Response(data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/audit")
    def audit(limit: int = 100, who: Session = Depends(session), db: Store = Depends(db_dep)) -> list[dict]:
        return db.list_audit(who.owner, min(max(limit, 1), 500))

    return app


app = create_app()
