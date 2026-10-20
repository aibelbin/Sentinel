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


def _render_package_route(api: Api, created: dict) -> None:
    """The normal way in: apt installs the package and resolves python3 and
    cron, then the device is enrolled with its own key."""
    try:
        release = api.get("/watchdog-release")
        package = api.get_raw("/watchdog-package")
    except ApiError as exc:
        st.warning(
            f"Could not build the watchdog package: {exc.message}. "
            "Use the script below instead."
        )
        return

    server_url = public_server_url()
    st.markdown(
        "**Install with the package.** apt pulls in `python3` and `cron`, so "
        "the machine needs nothing prepared first."
    )
    st.download_button(
        f"⬇️  Download {release['package_filename']}",
        data=package,
        file_name=release["package_filename"],
        mime="application/vnd.debian.binary-package",
        type="primary",
    )
    st.markdown("Then, on the machine:")
    st.code(
        f"sudo apt install ./{release['package_filename']}\n"
        f"sudo sentinel-watchdog enroll --server {server_url} "
        f"--key {created['api_key']}"
        + (" --insecure" if server_url.startswith("http://") else ""),
        language="bash",
    )
    st.caption(
        "The package is the same for every machine, so it carries no key — "
        "which is why enrolling is a second step."
    )


def _render_script_route(api: Api, created: dict) -> None:
    """For machines that cannot reach an apt mirror."""
    with st.expander("No apt access? Use the self-contained script"):
        script = _installer_bytes(api, created["api_key"])
        if not script:
            return
        st.markdown(
            "One file with the watchdog embedded and this device's URL and key "
            "already filled in. It installs nothing from the network, so it "
            "cannot resolve dependencies — the machine needs `python3` and "
            "`cron`, or pass `--install-deps` to let it install them."
        )
        st.download_button(
            "⬇️  Download installer for this device",
            data=script,
            file_name=f"sentinel-install-{created['name']}.sh",
            mime="text/x-shellscript",
        )
        st.code(f"sudo bash sentinel-install-{created['name']}.sh", language="bash")


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

        _render_package_route(api, created)
        _render_script_route(api, created)

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
                "Watchdog": fmt.update_badge(device.get("update_status")),
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

    # Guarded on the armed device: without this, arming delete for one device
    # and then changing the selectbox shows that device's name beside another
    # device's package count.
    if device_actions.is_pending(selected["id"]):
        if device_actions.render_confirmation(
            api, package_count=selected.get("package_count")
        ):
            st.rerun()
    elif device_actions.is_pending():
        # Armed for a device that is no longer selected; drop it rather than
        # leave a stale panel describing the wrong machine.
        device_actions.cancel()
