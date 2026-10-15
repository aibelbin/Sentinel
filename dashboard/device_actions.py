"""Device actions shared between the list and detail views.

Deletion lives here rather than in either view so that both entry points use
one confirmation flow. A destructive, irreversible action should not behave
differently depending on which page you reached it from.
"""

import streamlit as st

from api_client import Api, ApiError

PENDING_KEY = "pending_delete"


def request_delete(device_id: str, name: str) -> None:
    """Arm the confirmation panel for a device."""
    st.session_state[PENDING_KEY] = {"id": str(device_id), "name": name}


def is_pending(device_id: str | None = None) -> bool:
    pending = st.session_state.get(PENDING_KEY)
    if not pending:
        return False
    return device_id is None or pending["id"] == str(device_id)


def cancel() -> None:
    st.session_state.pop(PENDING_KEY, None)


def render_confirmation(api: Api, *, package_count: int | None = None) -> bool:
    """Render the armed confirmation panel. Returns True once deleted.

    Requires the device name to be typed rather than offering a bare
    "are you sure?": this drops the device, its current inventory, its whole
    change history and its task records, and there is no undo.
    """
    pending = st.session_state.get(PENDING_KEY)
    if not pending:
        return False

    name = pending["name"]
    device_id = pending["id"]

    with st.container(border=True):
        st.error(f"Delete **{name}**?")

        losses = [
            "the device record and its API key",
            "its current package inventory"
            + (f" ({package_count} packages)" if package_count else ""),
            "its entire change history",
            "its task and report history",
        ]
        st.markdown(
            "This permanently removes:\n"
            + "\n".join(f"- {item}" for item in losses)
            + "\n\nThere is no undo. The agent on that machine keeps running and "
            "will be refused with `invalid_or_revoked_key` on every beat until "
            "it is uninstalled with `sudo ./sentinel-install.sh --uninstall`."
        )

        # The device id is in the widget key so that arming the panel for a
        # different device does not inherit the text typed for the last one.
        typed = st.text_input(
            "Type the device name to confirm",
            key=f"delete_confirm_{device_id}",
            placeholder=name,
        )

        confirm_column, cancel_column = st.columns(2)
        if confirm_column.button(
            "Delete permanently", type="primary", key=f"delete_go_{device_id}",
            use_container_width=True,
        ):
            if typed.strip() != name:
                st.error("The name does not match; nothing was deleted.")
            else:
                try:
                    api.delete(f"/devices/{device_id}")
                except ApiError as exc:
                    st.error(exc.message)
                    return False
                cancel()
                st.success(f"Deleted {name}.")
                return True

        if cancel_column.button(
            "Cancel", key=f"delete_cancel_{device_id}", use_container_width=True
        ):
            cancel()
            st.rerun()

    return False
