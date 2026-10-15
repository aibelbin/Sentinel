"""Global agent configuration."""

import streamlit as st

from api_client import Api, ApiError


def _render_agent_version(api: Api) -> None:
    st.subheader("Agent version")
    try:
        release = api.get("/agent-release")
    except ApiError as exc:
        st.warning(f"Could not read the agent version: {exc.message}")
        return

    columns = st.columns(3)
    columns[0].metric("Current version", release["version"][:8])
    columns[1].metric(
        "Machines updated", f"{release['devices_on_version']}/{release['devices_total']}"
    )
    columns[2].metric("Status", "Held back" if release["quarantined"] else "Rolling out")

    st.caption(
        "The version is a hash of the agent code on this server, so deploying "
        "changes it automatically. Machines pick it up on their next heartbeat."
    )

    if release["quarantined"]:
        st.error(
            f"**This version is held back.** A machine reported it failed, so it is "
            f"not being sent to anyone else.\n\nReason: {release['failure_reason']}"
        )
        st.markdown(
            "Deploying a new version clears this on its own. Use the button "
            "below only if you believe the failure was a one-off rather than a "
            "problem with the build."
        )
        if st.button("Try this version again"):
            try:
                api.post("/agent-release/clear-quarantine")
                st.success("Cleared. Machines will retry on their next heartbeat.")
                st.rerun()
            except ApiError as exc:
                st.error(exc.message)

    st.divider()


def render(api: Api) -> None:
    st.header("Settings")
    _render_agent_version(api)
    st.caption(
        "These are the global defaults delivered to agents on their next "
        "heartbeat. Nothing is pushed to devices; they converge within a minute."
    )

    try:
        config = api.get("/config")
    except ApiError as exc:
        st.error(exc.message)
        return

    with st.form("agent_config"):
        poll = st.number_input(
            "Heartbeat interval (seconds)",
            min_value=30,
            max_value=3600,
            value=int(config["poll_interval_seconds"]),
            step=30,
            help=(
                "How often an agent reports in. Cron ticks every minute, so "
                "values below 60 do not increase frequency; they only relax "
                "the online/stale thresholds, which are multiples of this."
            ),
        )
        inventory_hours = st.number_input(
            "Collect inventory every (hours)",
            min_value=1,
            max_value=168,
            value=max(1, int(config["inventory_interval_seconds"]) // 3600),
            step=1,
            help="Full software inventory. The default of 12 hours means twice daily.",
        )
        spool_mb = st.number_input(
            "Offline spool cap (MB)",
            min_value=1,
            max_value=10240,
            value=max(1, int(config["spool_max_bytes"]) // (1024 * 1024)),
            step=16,
            help=(
                "Disk an agent may use for reports it could not deliver. "
                "At the cap, the oldest reports are dropped first."
            ),
        )

        if st.form_submit_button("Save", type="primary"):
            try:
                api.put(
                    "/config",
                    {
                        "poll_interval_seconds": int(poll),
                        "inventory_interval_seconds": int(inventory_hours) * 3600,
                        "spool_max_bytes": int(spool_mb) * 1024 * 1024,
                    },
                )
                st.success("Saved. Agents pick this up on their next heartbeat.")
            except ApiError as exc:
                st.error(exc.message)

    st.divider()
    st.subheader("Device status thresholds")
    st.markdown(
        f"""
Status is derived at read time from the last heartbeat, as a multiple of the
heartbeat interval above — so changing that interval cannot silently break it.

- **Online** — seen within 3 intervals ({int(poll) * 3}s)
- **Stale** — seen within 15 intervals ({int(poll) * 15}s)
- **Offline** — longer than that, or never seen

Three missed heartbeats before a device goes stale, so one dropped beat never
marks a healthy device offline.
"""
    )
