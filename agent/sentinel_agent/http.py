"""Minimal HTTPS client over urllib, so the agent needs no third-party package."""

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from sentinel_agent import __version__
from sentinel_agent.config import Config

log = logging.getLogger("sentinel.http")

DEFAULT_TIMEOUT = 30


class HttpError(Exception):
    """A request that reached the server and was refused."""

    def __init__(self, status: int, code: str | None, message: str) -> None:
        super().__init__(f"HTTP {status}: {code or message}")
        self.status = status
        self.code = code
        self.message = message

    @property
    def fatal(self) -> bool:
        """True when retrying cannot help.

        A revoked key or a disabled device will refuse every future beat too,
        so the report should not be spooled forever.
        """
        return self.status in (400, 401, 403, 404, 409, 413, 422)


class TransportError(Exception):
    """The server could not be reached. Retrying later may succeed."""


class InsecureUrlRefused(Exception):
    pass


def _check_url(config: Config) -> None:
    if config.base_url.startswith("https://"):
        return
    if config.insecure:
        # Logged on every single request, so an insecure deployment is loud
        # rather than quietly forgotten.
        log.warning(
            "sending credentials over plain HTTP to %s because the agent was "
            "enrolled with --insecure",
            config.base_url,
        )
        return
    raise InsecureUrlRefused(
        f"refusing to send the API key to {config.base_url!r} over plain HTTP; "
        "re-enroll with --insecure only for local testing"
    )


def _extract_error(status: int, raw: bytes) -> HttpError:
    code: str | None = None
    message = raw.decode("utf-8", "replace")[:500]
    try:
        payload = json.loads(raw)
        detail = payload.get("detail", payload)
        if isinstance(detail, dict):
            code = detail.get("code")
            message = detail.get("message", message)
        elif isinstance(detail, (str, list)):
            message = str(detail)[:500]
    except (json.JSONDecodeError, AttributeError):
        pass
    return HttpError(status, code, message)


def _request(
    config: Config, path: str, method: str, payload: dict | None, timeout: int
) -> dict[str, Any]:
    _check_url(config)

    url = f"{config.base_url}{path}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "User-Agent": f"sentinel-agent/{__version__}",
        "Accept": "application/json",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, method=method, headers=headers)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise _extract_error(exc.code, exc.read()) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransportError(f"{url} unreachable: {exc}") from exc

    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TransportError(f"{url} returned a non-JSON body: {exc}") from exc
    return decoded if isinstance(decoded, dict) else {"data": decoded}


def post(config: Config, path: str, payload: dict, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """POST JSON and return the decoded response, or {} for an empty body."""
    return _request(config, path, "POST", payload, timeout)


def get(config: Config, path: str, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """GET JSON. Used to fetch an update bundle, which is larger than a
    heartbeat, so it gets a longer default timeout."""
    return _request(config, path, "GET", None, max(timeout, 60))
