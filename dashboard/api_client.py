"""Thin HTTP client for the Sentinel API.

The dashboard holds a session bearer token and never touches PostgreSQL, so
replacing this frontend later means writing a new client against an API that
already exists and is already exercised.
"""

import os
from typing import Any

import requests

DEFAULT_TIMEOUT = 20


class ApiError(Exception):
    def __init__(self, status: int, code: str | None, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class Unauthorized(ApiError):
    """The session is missing, expired or revoked."""


def base_url() -> str:
    return os.environ.get("API_URL", "http://api:8000").rstrip("/")


def public_server_url() -> str:
    """The URL devices should be pointed at.

    Distinct from base_url(): the dashboard reaches the API over the Compose
    network, but a managed device needs the externally reachable address.
    """
    return os.environ.get("PUBLIC_API_URL", base_url()).rstrip("/")


def _raise_for_status(response: requests.Response) -> None:
    if response.ok:
        return

    code: str | None = None
    message = response.text[:500]
    try:
        detail = response.json().get("detail", {})
        if isinstance(detail, dict):
            code = detail.get("code")
            message = detail.get("message") or code or message
        elif isinstance(detail, list) and detail:
            # FastAPI validation errors arrive as a list of field problems.
            first = detail[0]
            message = f"{'.'.join(str(p) for p in first.get('loc', []))}: {first.get('msg', '')}"
        elif isinstance(detail, str):
            message = detail
    except (ValueError, AttributeError):
        # A proxy or non-FastAPI error page can return a JSON array or scalar,
        # where .get would raise. The message falls back to the raw body.
        pass

    if response.status_code == 401:
        raise Unauthorized(401, code, message or "Session expired.")
    raise ApiError(response.status_code, code, message or f"Request failed ({response.status_code}).")


class Api:
    def __init__(self, token: str | None = None) -> None:
        self.token = token
        self._session = requests.Session()

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        try:
            response = self._session.request(
                method,
                f"{base_url()}/api/v1{path}",
                headers=self._headers(),
                timeout=DEFAULT_TIMEOUT,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise ApiError(0, "unreachable", f"Cannot reach the API: {exc}") from exc
        _raise_for_status(response)
        return response

    def get(self, path: str, **params) -> Any:
        clean = {key: value for key, value in params.items() if value not in (None, "")}
        return self._request("GET", path, params=clean).json()

    def post(self, path: str, payload: dict | None = None) -> Any:
        response = self._request("POST", path, json=payload or {})
        return response.json() if response.content else None

    def patch(self, path: str, payload: dict) -> Any:
        return self._request("PATCH", path, json=payload).json()

    def put(self, path: str, payload: dict) -> Any:
        return self._request("PUT", path, json=payload).json()

    def delete(self, path: str) -> None:
        self._request("DELETE", path)

    def get_raw(self, path: str) -> bytes:
        """For endpoints that return a file rather than JSON."""
        return self._request("GET", path).content

    def post_raw(self, path: str, payload: dict) -> bytes:
        """For endpoints that return a file rather than JSON."""
        return self._request("POST", path, json=payload).content

    # ---- auth -------------------------------------------------------------- #

    def login(self, username: str, password: str) -> dict:
        response = self._session.post(
            f"{base_url()}/api/v1/auth/login",
            json={"username": username, "password": password},
            timeout=DEFAULT_TIMEOUT,
        )
        _raise_for_status(response)
        return response.json()

    def logout(self) -> None:
        try:
            self._request("POST", "/auth/logout", json={})
        except ApiError:
            # A failed logout still clears the local session; nothing to report.
            pass
