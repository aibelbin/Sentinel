"""Files (the .deb, the install script) handed to the dashboard exactly once.

In memory only: nothing the model could ever see, and nothing written to disk.
"""

import secrets
import time
from typing import Callable


class Downloads:
    def __init__(self, ttl: float = 600, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        self._items: dict[str, tuple[float, str, str, str, bytes]] = {}

    def put(self, owner_key: str, name: str, mime: str, data: bytes) -> str:
        self._sweep()
        download_id = secrets.token_urlsafe(16)
        self._items[download_id] = (self._clock() + self._ttl, owner_key, name, mime, data)
        return download_id

    def take(self, download_id: str, owner_key: str) -> tuple[str, str, bytes] | None:
        self._sweep()
        item = self._items.get(download_id)
        if item is None or item[1] != owner_key:
            return None
        del self._items[download_id]
        return item[2], item[3], item[4]

    def _sweep(self) -> None:
        now = self._clock()
        for key in [k for k, v in self._items.items() if v[0] <= now]:
            del self._items[key]
