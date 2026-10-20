"""The Liva tab: a chat with the assistant.

All logic lives in Liva; this only renders. If Liva cannot be reached the tab
says so and everything else in the dashboard keeps working.
"""

import streamlit as st

from liva_client import Liva, LivaError, LivaUnavailable

UNAVAILABLE = "Liva is unavailable right now. The rest of Sentinel is unaffected."


def _client() -> Liva:
    return Liva(st.session_state["token"], st.session_state.get("username", "admin"))


LIVE_CARDS = "liva_live_cards"


def _keep_live(liva: Liva, card: dict) -> dict:
    """Fetches a card's one-time downloads now and remembers the card.

    The key and the files are shown once and cannot be fetched again, so they
    are held in this session until dismissed. Otherwise any rerun (an approval
    prompt, a widget click) would erase the only copy before it was copied.
    """
    card = {**card, "files": [dict(f) for f in card.get("files", [])]}
    for file in card["files"]:
        download_id = file.pop("download_id", None)
        if download_id:
            try:
                file["data"] = liva.download(download_id)
            except (LivaError, LivaUnavailable):
                pass
    st.session_state.setdefault(LIVE_CARDS, []).append(card)
    return card


def _render_live_cards(liva: Liva) -> None:
    for index, card in enumerate(list(st.session_state.get(LIVE_CARDS, []))):
        _render_card(liva, card, f"keep_{index}")
        if st.button("Dismiss (the key is gone once you do)", key=f"keep_dismiss_{index}"):
            st.session_state[LIVE_CARDS].pop(index)
            st.rerun()


def _render_card(liva: Liva, card: dict, key: str) -> None:
    with st.container(border=True):
        if card.get("kind") == "credentials":
            st.markdown(f"**Credentials for `{card['device']}`**")
            if card.get("secret_shown_once"):
                st.caption("The key was shown once and is not stored. Rotate it if you lost it.")
            elif card.get("api_key"):
                st.warning("Copy the key now. It is shown once and cannot be recovered.")
                st.code(card["api_key"])
            if card.get("commands"):
                st.code("\n".join(card["commands"]), language="bash")
            for warning in card.get("warnings", []):
                st.warning(warning)
        else:
            st.markdown("**Download**")

        for index, file in enumerate(card.get("files", [])):
            if file.get("data") is not None:
                st.download_button(f"⬇️  {file['name']}", data=file["data"], file_name=file["name"],
                                   key=f"{key}_dl_{index}")
                continue
            download_id = file.get("download_id")
            if not download_id:
                # Downloads are single-use and held in memory, so a reloaded
                # chat only remembers that the file was offered.
                st.caption(f"{file['name']} (already offered)")
                continue
            try:
                data = liva.download(download_id)
            except (LivaError, LivaUnavailable):
                st.caption(f"{file['name']} (download expired)")
                continue
            st.download_button(f"⬇️  {file['name']}", data=data, file_name=file["name"],
                               key=f"{key}_dl_{index}")


def _render_event(liva: Liva, event: dict, state: dict) -> None:
    kind = event["type"]
    if kind == "message":
        st.markdown(event["content"])
    elif kind == "tool_call":
        tag = {"bypassed": " (bypass)", "approved": " (approved)"}.get(event["mode"], "")
        st.caption(f"🔧 {event['name']}{tag}")
    elif kind == "tool_result" and not event["ok"]:
        st.caption(f"⚠️ {event['name']}: {event['summary']}")
    elif kind == "card":
        state["cards"] = state.get("cards", 0) + 1
        _render_card(liva, _keep_live(liva, event["card"]), f"live_{state['cards']}")
    elif kind == "pending_action":
        state["paused"] = True
    elif kind == "error":
        st.error(event["message"])


def _run_stream(liva: Liva, events) -> bool:
    """Renders a turn as it arrives. True if it stopped to ask for approval."""
    state: dict = {}
    with st.chat_message("assistant"):
        try:
            for event in events:
                _render_event(liva, event, state)
        except LivaUnavailable:
            st.error("Lost the connection to Liva.")
        except LivaError as exc:
            st.error(exc.message)
    return bool(state.get("paused"))


def _render_history(liva: Liva, messages: list[dict]) -> None:
    for index, message in enumerate(messages):
        role = message["role"]
        if role == "user":
            with st.chat_message("user"):
                st.markdown(message["content"])
        elif role == "assistant" and message.get("content"):
            with st.chat_message("assistant"):
                st.markdown(message["content"])
        elif role == "ui":
            with st.chat_message("assistant"):
                _render_card(liva, message["card"], f"hist_{index}")


def render(api) -> None:
    st.title("Liva")
    liva = _client()
    try:
        health = liva.health()
    except LivaUnavailable:
        st.info(UNAVAILABLE)
        return

    if not health.get("groq_configured"):
        st.warning("Chat is not configured: set GROQ_API_KEY in the server's .env and restart Liva.")
        return

    try:
        _render_chat(liva)
    except LivaUnavailable:
        st.info(UNAVAILABLE)
    except LivaError as exc:
        st.error(exc.message)


def _render_chat(liva: Liva) -> None:
    top = st.columns([3, 1.4, 1])
    chats = liva.chats()
    labels = {chat["id"]: chat["title"] for chat in chats}
    current = st.session_state.get("liva_chat_id")
    if current not in labels:
        current = None

    with top[0]:
        options = [None] + list(labels)
        picked = st.selectbox(
            "Chat", options, index=options.index(current),
            format_func=lambda cid: "➕ New chat" if cid is None else labels[cid],
            label_visibility="collapsed",
        )
    if picked != current:
        st.session_state["liva_chat_id"] = picked
        st.session_state.pop(LIVE_CARDS, None)
        st.rerun()
    with top[1]:
        bypass = st.toggle(
            "Bypass approvals", key="liva_bypass",
            help="Run destructive actions without asking. Resets when you sign out.",
        )
    with top[2]:
        if current and st.button("Delete chat", use_container_width=True):
            liva.delete_chat(current)
            st.session_state.pop("liva_chat_id", None)
            st.rerun()

    if bypass:
        st.error("⚠️ BYPASS MODE: Liva can delete devices, rotate keys and change config without asking.")

    detail = liva.chat(current) if current else {"messages": [], "pending": []}
    _render_history(liva, detail["messages"])
    _render_live_cards(liva)
    pending = detail["pending"]

    # A click is recorded and handled on the next run, so the approval panel is
    # gone from the screen by the time the action's output streams in.
    decision = st.session_state.pop("liva_decision", None)
    if decision and current:
        call_id, approve = decision
        if _run_stream(liva, liva.decide(current, call_id, approve, bypass)):
            st.rerun()
        pending = []

    if pending:
        action = pending[0]
        with st.container(border=True):
            st.markdown(f"**Waiting for your approval:** {action['description']}")
            st.code(f"{action['tool']}({action['arguments']})")
            approve_col, deny_col = st.columns(2)
            if approve_col.button("Approve", key=f"liva_ok_{action['call_id']}", type="primary",
                                  use_container_width=True):
                st.session_state["liva_decision"] = (action["call_id"], True)
                st.rerun()
            if deny_col.button("Deny", key=f"liva_no_{action['call_id']}", use_container_width=True):
                st.session_state["liva_decision"] = (action["call_id"], False)
                st.rerun()
        return

    prompt = st.chat_input("Ask Liva about your devices, or tell her what to do")
    if not prompt:
        return
    if current is None:
        current = liva.new_chat()["id"]
        st.session_state["liva_chat_id"] = current
    with st.chat_message("user"):
        st.markdown(prompt)
    if _run_stream(liva, liva.send(current, prompt, bypass)):
        st.rerun()
