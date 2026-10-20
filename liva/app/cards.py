"""Turns a tool's user-audience card into what the screen may show and what may be stored."""

import base64
import copy

from .downloads import Downloads
from .security import mask_key, redact


def split_card(card: dict, downloads: Downloads, owner_key: str) -> tuple[dict, dict]:
    """Returns (live, stored).

    live: shown once over the stream; keeps the secret, with file bytes swapped
    for one-time download ids.
    stored: written to SQLite; the secret is masked and files are names only.
    """
    live = copy.deepcopy(card)
    for file in live.get("files", []):
        data = base64.b64decode(file.pop("data_b64"))
        file["size"] = len(data)
        file["download_id"] = downloads.put(owner_key, file["name"], file["mime"], data)

    stored = copy.deepcopy(live)
    if stored.get("api_key"):
        stored["api_key"] = mask_key(stored["api_key"])
    stored["commands"] = [redact(command) for command in stored.get("commands", [])]
    stored["files"] = [{"name": f["name"], "size": f["size"]} for f in stored.get("files", [])]
    stored["secret_shown_once"] = True
    return live, stored
