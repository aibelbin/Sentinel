"""Which tools run on their own and which wait for the administrator.

A static table rather than text the model reads, so the model cannot argue its
way past it.
"""

import json

AUTO = "auto"
APPROVAL = "approval"

TIERS: dict[str, str] = {
    "list_devices": AUTO,
    "get_device": AUTO,
    "list_packages": AUTO,
    "list_changes": AUTO,
    "list_reports": AUTO,
    "list_tasks": AUTO,
    "get_config": AUTO,
    "get_release_status": AUTO,
    "create_device": AUTO,
    "build_package": AUTO,
    "update_device": AUTO,
    "queue_collect_inventory": AUTO,
    "delete_device": APPROVAL,
    "rotate_device_key": APPROVAL,
    "update_config": APPROVAL,
    "clear_quarantine": APPROVAL,
}


def tier_of(name: str) -> str | None:
    return TIERS.get(name)


def describe(name: str, args: dict) -> str:
    device = args.get("device", "?")
    if name == "delete_device":
        return f"Delete device '{device}' and all of its inventory history. This cannot be undone."
    if name == "rotate_device_key":
        return f"Rotate the API key of '{device}'. The device stops reporting until it is re-enrolled with the new key."
    if name == "update_config":
        changes = ", ".join(f"{key} = {value}" for key, value in args.items()) or "no changes"
        return f"Change the global watchdog configuration for every device: {changes}."
    if name == "clear_quarantine":
        return "Clear the quarantine on the current watchdog version so it is handed out to devices again."
    return f"{name} {json.dumps(args, default=str)}"
