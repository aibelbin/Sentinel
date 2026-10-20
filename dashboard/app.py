"""Sentinel dashboard.

A thin client over the Sentinel API. It holds a session bearer token and has no
database driver installed, so the separation from PostgreSQL is enforced by
what is available rather than by discipline.
"""

import streamlit as st

import views_device
import views_devices
import views_liva
import views_settings
from api_client import Api, ApiError, Unauthorized

st.set_page_config(page_title="Sentinel", page_icon="🛡️", layout="wide")

PAGES = {
    "devices": ("Devices", views_devices.render),
    "liva": ("Liva", views_liva.render),
    "settings": ("Settings", views_settings.render),
}


def _clear_session() -> None:
    for key in (
        "token", "username", "page", "device_id", "new_device", "rotated_key",
        "pkg_page", "pkg_view_key", "pending_delete",
        "liva_chat_id", "liva_bypass", "liva_decision",
    ):
        st.session_state.pop(key, None)


def _render_login() -> None:
    st.title("🛡️ Sentinel")
    st.caption("Device inventory and management")

    _, middle, _ = st.columns([1, 2, 1])
    with middle, st.form("login"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Sign in", type="primary", use_container_width=True):
            try:
                result = Api().login(username, password)
            except ApiError as exc:
                st.error(exc.message)
            else:
                st.session_state["token"] = result["token"]
                st.session_state["username"] = result["username"]
                st.session_state["page"] = "devices"
                st.rerun()


def _render_sidebar(api: Api) -> None:
    with st.sidebar:
        st.title("🛡️ Sentinel")
        st.caption(f"Signed in as **{st.session_state.get('username', '?')}**")
        st.divider()

        current = st.session_state.get("page", "devices")
        # The device detail page is reached from the list, so the nav highlights
        # Devices while it is open.
        nav_current = "devices" if current == "device" else current
        for key, (label, _) in PAGES.items():
            if st.button(
                label,
                use_container_width=True,
                type="primary" if key == nav_current else "secondary",
                key=f"nav_{key}",
            ):
                st.session_state["page"] = key
                st.session_state.pop("device_id", None)
                st.rerun()

        st.divider()
        if st.button("Sign out", use_container_width=True):
            api.logout()
            _clear_session()
            st.rerun()


def main() -> None:
    if not st.session_state.get("token"):
        _render_login()
        return

    api = Api(st.session_state["token"])
    _render_sidebar(api)

    page = st.session_state.get("page", "devices")
    try:
        if page == "device":
            views_device.render(api)
        else:
            PAGES.get(page, PAGES["devices"])[1](api)
    except Unauthorized:
        # An expired session is a clean re-login, not a traceback.
        _clear_session()
        st.warning("Your session expired. Please sign in again.")
        st.rerun()


main()
