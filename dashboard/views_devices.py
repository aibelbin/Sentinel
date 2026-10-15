"""Devices list: the one tab v1 ships."""

import streamlit as st

import device_actions
import format as fmt
from api_client import Api, ApiError, public_server_url


def _installer_bytes(api: Api, api_key: str) -> bytes | None:
    server_url = public_server_url()
    try:
        return api.post_raw(
            "/installer",
            {
                "server_url": server_url,
                "api_key": api_key,
                # A plain-HTTP platform URL only happens in local development;
                # the generated installer has to permit it explicitly.
                "insecure": server_url.startswith("http://"),
            },
        )
    except ApiError as exc:
        st.warning(f"Could not build the installer: {exc.message}")
        return None


def _render_new_device_panel(api: Api) -> None:
    """Shown once, right after creation, because the key is unrecoverable."""
    created = st.session_state.get("new_device")
    if not created:
        return

    with st.container(border=True):
        st.success(f"Device **{created['name']}** created.")
        st.markdown(
            "**Copy this key now.** It is stored only as a hash and cannot be "
            "shown again. If you lose it, rotate the key to issue a new one."
        )
        st.code(created["api_key"], language=None)

        script = _installer_bytes(api, created["api_key"])
        if script:
            st.markdown(
                "The installer below has this device's URL and key already "
                "filled in. Copy it to the machine and run it:"
            )
            st.code(f"sudo bash sentinel-install-{created['name']}.sh", language="bash")
            st.download_button(
                "⬇️  Download installer for this device",
                data=script,
                file_name=f"sentinel-install-{created['name']}.sh",
                mime="text/x-shellscript",
                type="primary",
            )

        if st.button("Done", key="dismiss_new_device"):
            del st.session_state["new_device"]
            st.rerun()


def _render_add_device(api: Api) -> None:
    with st.expander("➕  Add a device", expanded=not st.session_state.get("devices_exist", True)):
        with st.form("add_device", clear_on_submit=True):
            name = st.text_input(
                "Device name",
                placeholder="web-01",
                help="A label you choose. The platform generates the device ID.",
            )
            submitted = st.form_submit_button("Create device and issue key", type="primary")

        if submitted:
            if not name.strip():
                st.error("A device name is required.")
            else:
                try:
                    created = api.post("/devices", {"name": name.strip()})
                    st.session_state["new_device"] = created
                    st.rerun()
                except ApiError as exc:
                    st.error(exc.message)


def _render_generic_installer(api: Api) -> None:
    with st.expander("⬇️  Generic installer"):
        st.markdown(
            "This version prompts for the platform URL and key when it runs. "
            "Use it when you already have a key, or to reinstall on a device "
            "that is already enrolled."
        )
        try:
            script = api.get_raw("/installer")
        except ApiError as exc:
            st.warning(f"Could not build the installer: {exc.message}")
            return
        st.download_button(
            "Download sentinel-install.sh",
            data=script,
            file_name="sentinel-install.sh",
            mime="text/x-shellscript",
        )


def _summary_metrics(devices: list[dict]) -> None:
    counts = {"online": 0, "stale": 0, "offline": 0, "disabled": 0}
    for device in devices:
        counts[device["status"]] = counts.get(device["status"], 0) + 1

    columns = st.columns(5)
    columns[0].metric("Devices", len(devices))
    columns[1].metric("Online", counts["online"])
    columns[2].metric("Stale", counts["stale"])
    columns[3].metric("Offline", counts["offline"])
    columns[4].metric("Packages", sum(device["package_count"] for device in devices))


def render(api: Api) -> None:
    st.header("Devices")

    _render_new_device_panel(api)

    filter_column, search_column = st.columns([1, 2])
    status_filter = filter_column.selectbox(
        "Status", ["all", "online", "stale", "offline", "disabled"], index=0
    )
    query = search_column.text_input("Search", placeholder="name or hostname")

    try:
        devices = api.get(
            "/devices",
            status=None if status_filter == "all" else status_filter,
            q=query or None,
        )
    except ApiError as exc:
        st.error(exc.message)
        return

    st.session_state["devices_exist"] = bool(devices)

    _render_add_device(api)
    _render_generic_installer(api)

    if not devices:
        st.info(
            "No devices yet. Add one above, download its installer, and run it "
            "on the machine you want to monitor."
        )
        return

    _summary_metrics(devices)
    st.divider()

    rows = []
    for device in devices:
        resources = device.get("resources") or {}
        rows.append(
            {
                "Status": fmt.status_badge(device["status"]),
                "Name": device["name"],
                "Last seen": fmt.relative_time(device.get("last_seen_at")),
                "OS": " ".join(
                    part for part in (device.get("os_name"), device.get("os_version")) if part
                )
                or "—",
                "CPU": fmt.percent(resources.get("cpu_percent")),
                "Memory": fmt.percent(fmt.memory_used_percent(resources)),
                "Disk": fmt.percent(fmt.worst_disk_percent(resources)),
                "Packages": device["package_count"],
                "Agent": fmt.update_badge(device.get("update_status")),
            }
        )

    st.dataframe(rows, use_container_width=True, hide_index=True)

    st.caption("Open a device for its packages, change log and tasks.")
    by_name = {device["name"]: device for device in devices}
    chosen = st.selectbox("Device", list(by_name), index=0, label_visibility="collapsed")
    selected = by_name[chosen]

    open_column, delete_column = st.columns([3, 1])
    if open_column.button(f"Open {chosen}", type="primary", use_container_width=True):
        st.session_state["device_id"] = selected["id"]
        st.session_state["page"] = "device"
        device_actions.cancel()
        st.rerun()
    if delete_column.button("Delete…", use_container_width=True):
        device_actions.request_delete(selected["id"], selected["name"])
        st.rerun()

    if device_actions.render_confirmation(api, package_count=selected.get("package_count")):
        st.rerun()
