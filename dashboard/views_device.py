"""Device detail: host facts, resources, packages, change log, tasks."""

import streamlit as st

import device_actions
import format as fmt
from api_client import Api, ApiError, public_server_url

PAGE_SIZE = 100


def _back_button() -> None:
    if st.button("← All devices"):
        st.session_state["page"] = "devices"
        st.session_state.pop("device_id", None)
        st.rerun()


def _render_header(api: Api, device: dict) -> None:
    title, action = st.columns([4, 1])
    title.header(f"{fmt.status_badge(device['status'])}  {device['name']}")

    if action.button("🔄 Collect now", type="primary", use_container_width=True):
        try:
            api.post(f"/devices/{device['id']}/tasks", {"type": "collect_inventory"})
            st.success("Queued. The device picks it up within a minute.")
        except ApiError as exc:
            st.error(exc.message)

    if not device["enabled"]:
        st.warning("This device is disabled. It cannot authenticate until re-enabled.")

    collector_status = device.get("last_collector_status") or {}
    failed = {
        name: detail
        for name, detail in collector_status.items()
        if not str(detail).startswith("ok")
    }
    if failed:
        # Shown prominently: a partial inventory that looks complete is worse
        # than one that admits what is missing.
        lines = "\n".join(f"- **{name}** — {detail}" for name, detail in sorted(failed.items()))
        st.warning(
            f"The last report was partial. These collectors did not run:\n\n{lines}"
        )


def _render_facts(device: dict) -> None:
    resources = device.get("resources") or {}

    columns = st.columns(4)
    columns[0].metric("CPU", fmt.percent(resources.get("cpu_percent")))
    columns[1].metric("Memory", fmt.percent(fmt.memory_used_percent(resources)))
    columns[2].metric("Disk (fullest)", fmt.percent(fmt.worst_disk_percent(resources)))
    columns[3].metric("Packages", device["package_count"])

    if resources:
        st.caption(f"Sampled {fmt.relative_time(resources.get('sampled_at'))}")
    else:
        st.caption("No resource sample yet.")

    left, right = st.columns(2)
    with left:
        st.subheader("Host")
        st.markdown(
            f"""
| | |
|---|---|
| Hostname | {device.get('hostname') or '—'} |
| OS | {' '.join(p for p in (device.get('os_name'), device.get('os_version')) if p) or '—'} |
| Kernel | {device.get('kernel') or '—'} |
| Architecture | {device.get('arch') or '—'} |
| CPU | {device.get('cpu_model') or '—'} |
| Cores | {device.get('cpu_cores') or '—'} |
| Memory | {fmt.human_bytes(device.get('total_memory_bytes'))} |
| Uptime | {fmt.duration((resources or {}).get('uptime_seconds'))} |
"""
        )
    with right:
        st.subheader("Agent")
        load = resources or {}
        load_text = (
            f"{load.get('load_1')} / {load.get('load_5')} / {load.get('load_15')}"
            if load.get("load_1") is not None
            else "—"
        )
        st.markdown(
            f"""
| | |
|---|---|
| Device ID | `{device['id']}` |
| Agent version | {device.get('agent_version') or '—'} |
| Enrolled | {fmt.absolute_time(device.get('created_at'))} |
| Last seen | {fmt.relative_time(device.get('last_seen_at'))} |
| Last inventory | {fmt.relative_time(device.get('last_report_at'))} |
| Load 1/5/15 | {load_text} |
| Poll interval | {device['config']['poll_interval_seconds']}s |
| Inventory every | {fmt.duration(device['config']['inventory_interval_seconds'])} |
"""
        )

    disks = (resources or {}).get("disks") or []
    if disks:
        st.subheader("Filesystems")
        st.dataframe(
            [
                {
                    "Mount": disk["mountpoint"],
                    "Type": disk["filesystem"],
                    "Size": fmt.human_bytes(disk["total_bytes"]),
                    "Free": fmt.human_bytes(disk["free_bytes"]),
                    "Used": fmt.percent(
                        100.0 * (disk["total_bytes"] - disk["free_bytes"]) / disk["total_bytes"]
                        if disk["total_bytes"]
                        else None
                    ),
                }
                for disk in disks
            ],
            use_container_width=True,
            hide_index=True,
        )


def _render_packages(api: Api, device: dict) -> None:
    eco_column, search_column = st.columns([1, 2])
    ecosystem = eco_column.selectbox(
        "Ecosystem", ["all", "os", "python", "npm", "java"], format_func=lambda value: (
            "All" if value == "all" else fmt.ECOSYSTEM_LABEL.get(value, value)
        )
    )
    query = search_column.text_input("Search packages", placeholder="e.g. openssl")

    # Reset paging when the view changes: holding offset=300 across a new
    # search renders an empty table under a "showing 301-5" caption.
    view_key = (device["id"], ecosystem, query)
    if st.session_state.get("pkg_view_key") != view_key:
        st.session_state["pkg_view_key"] = view_key
        st.session_state["pkg_page"] = 0

    page = st.session_state.get("pkg_page", 0)
    try:
        result = api.get(
            f"/devices/{device['id']}/packages",
            ecosystem=None if ecosystem == "all" else ecosystem,
            q=query or None,
            limit=PAGE_SIZE,
            offset=page * PAGE_SIZE,
        )
    except ApiError as exc:
        st.error(exc.message)
        return

    total = result["total"]
    if total == 0:
        st.info("No packages reported yet. Use “Collect now” to trigger inventory.")
        return

    st.caption(
        f"{total} package(s) — showing {result['offset'] + 1}"
        f"–{min(result['offset'] + PAGE_SIZE, total)}"
    )
    st.dataframe(
        [
            {
                "Ecosystem": fmt.ECOSYSTEM_LABEL.get(item["ecosystem"], item["ecosystem"]),
                "Name": item["name"],
                "Version": item["version"],
                "Scope": item["scope"],
                "First seen": fmt.absolute_time(item["first_seen_at"]),
            }
            for item in result["items"]
        ],
        use_container_width=True,
        hide_index=True,
    )

    previous, nxt = st.columns(2)
    if page > 0 and previous.button("← Previous", key="pkg_prev"):
        st.session_state["pkg_page"] = page - 1
        st.rerun()
    if result["offset"] + PAGE_SIZE < total and nxt.button("Next →", key="pkg_next"):
        st.session_state["pkg_page"] = page + 1
        st.rerun()


def _render_changes(api: Api, device: dict) -> None:
    try:
        result = api.get(f"/devices/{device['id']}/changes", limit=PAGE_SIZE)
    except ApiError as exc:
        st.error(exc.message)
        return

    if result["total"] == 0:
        st.info(
            "No changes yet. The first inventory establishes the baseline; "
            "changes appear from the second report onward."
        )
        return

    st.caption(f"{result['total']} change(s) recorded")
    st.dataframe(
        [
            {
                "When": fmt.absolute_time(item["detected_at"]),
                "Change": fmt.CHANGE_LABEL.get(item["change_type"], item["change_type"]),
                "Ecosystem": fmt.ECOSYSTEM_LABEL.get(item["ecosystem"], item["ecosystem"]),
                "Package": item["name"],
                "From": item["old_version"] or "—",
                "To": item["new_version"] or "—",
            }
            for item in result["items"]
        ],
        use_container_width=True,
        hide_index=True,
    )


def _render_tasks(api: Api, device: dict) -> None:
    try:
        tasks = api.get(f"/devices/{device['id']}/tasks", limit=50)
        reports = api.get(f"/devices/{device['id']}/reports", limit=20)
    except ApiError as exc:
        st.error(exc.message)
        return

    st.subheader("Tasks")
    if tasks["total"] == 0:
        st.caption("No tasks requested yet.")
    else:
        st.dataframe(
            [
                {
                    "Requested": fmt.absolute_time(item["created_at"]),
                    "Type": item["type"],
                    "Status": "⚠️ abandoned" if item["abandoned"] else item["status"],
                    "Finished": fmt.relative_time(item["completed_at"])
                    if item["completed_at"]
                    else "—",
                    "Result": (item.get("error") or "")[:80]
                    or (str(item.get("result") or "")[:80]),
                }
                for item in tasks["items"]
            ],
            use_container_width=True,
            hide_index=True,
        )

    st.subheader("Inventory reports")
    if reports["total"] == 0:
        st.caption("No reports received yet.")
        return
    st.dataframe(
        [
            {
                "Collected": fmt.absolute_time(item["collected_at"]),
                "Received": fmt.relative_time(item["received_at"]),
                "Packages": item["package_count"],
                "Changes": item["change_count"],
                # A spooled report that arrived after a newer one is recorded
                # but deliberately changes nothing.
                "Applied": "no (superseded)" if item["superseded"] else "yes",
            }
            for item in reports["items"]
        ],
        use_container_width=True,
        hide_index=True,
    )


def _render_manage(api: Api, device: dict) -> None:
    st.subheader("Rename")
    with st.form("rename_device"):
        new_name = st.text_input("Name", value=device["name"])
        if st.form_submit_button("Save") and new_name.strip() != device["name"]:
            try:
                api.patch(f"/devices/{device['id']}", {"name": new_name.strip()})
                st.success("Renamed.")
                st.rerun()
            except ApiError as exc:
                st.error(exc.message)

    st.subheader("Availability")
    label = "Disable device" if device["enabled"] else "Enable device"
    st.caption(
        "A disabled device is refused at authentication; its history is kept."
        if device["enabled"]
        else "Enabling lets the agent authenticate again."
    )
    if st.button(label):
        try:
            api.patch(f"/devices/{device['id']}", {"enabled": not device["enabled"]})
            st.rerun()
        except ApiError as exc:
            st.error(exc.message)

    st.subheader("Rotate key")
    st.caption(
        "Issues a new key and revokes the current one. The device stops "
        "reporting until it is re-enrolled with the new key, and its history "
        "is preserved."
    )
    if st.button("Rotate API key"):
        try:
            st.session_state["rotated_key"] = api.post(f"/devices/{device['id']}/rotate-key")
        except ApiError as exc:
            st.error(exc.message)

    rotated = st.session_state.get("rotated_key")
    if rotated and rotated["device_id"] == device["id"]:
        st.success("New key issued. Copy it now — it cannot be shown again.")
        st.code(rotated["api_key"], language=None)
        server_url = public_server_url()
        try:
            script = api.post_raw(
                "/installer",
                {
                    "server_url": server_url,
                    "api_key": rotated["api_key"],
                    "insecure": server_url.startswith("http://"),
                },
            )
            st.download_button(
                "⬇️  Download installer with the new key",
                data=script,
                file_name=f"sentinel-install-{device['name']}.sh",
                mime="text/x-shellscript",
            )
            st.caption("Run it on the device with --force-reenroll to switch keys.")
        except ApiError as exc:
            st.warning(f"Could not build the installer: {exc.message}")
        if st.button("Dismiss", key="dismiss_rotated"):
            del st.session_state["rotated_key"]
            st.rerun()

    st.subheader("Delete")
    if not device_actions.is_pending(device["id"]):
        st.caption("Removes the device and all of its inventory, history and tasks.")
        if st.button("Delete this device…"):
            device_actions.request_delete(device["id"], device["name"])
            st.rerun()

    if device_actions.render_confirmation(api, package_count=device.get("package_count")):
        st.session_state["page"] = "devices"
        st.session_state.pop("device_id", None)
        st.rerun()


def render(api: Api) -> None:
    device_id = st.session_state.get("device_id")
    if not device_id:
        st.session_state["page"] = "devices"
        st.rerun()
        return

    _back_button()

    try:
        device = api.get(f"/devices/{device_id}")
    except ApiError as exc:
        st.error(exc.message)
        if exc.status == 404:
            st.session_state["page"] = "devices"
            st.session_state.pop("device_id", None)
        return

    _render_header(api, device)

    overview, packages, changes, history, manage = st.tabs(
        ["Overview", "Packages", "Changes", "History", "Manage"]
    )
    with overview:
        _render_facts(device)
    with packages:
        _render_packages(api, device)
    with changes:
        _render_changes(api, device)
    with history:
        _render_tasks(api, device)
    with manage:
        _render_manage(api, device)
