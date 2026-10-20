"""Async client for the Sentinel API, authenticated as the calling administrator."""

from typing import Any

import httpx


class SentinelError(Exception):
    def __init__(self, status: int, code: str | None, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _error_from(response: httpx.Response) -> SentinelError:
    code: str | None = None
    message = response.text[:500]
    try:
        detail = response.json().get("detail", {})
        if isinstance(detail, dict):
            code = detail.get("code")
            message = detail.get("message") or code or message
        elif isinstance(detail, list) and detail:
            first = detail[0]
            message = f"{'.'.join(str(p) for p in first.get('loc', []))}: {first.get('msg', '')}"
        elif isinstance(detail, str):
            message = detail
    except (ValueError, AttributeError):
        pass
    return SentinelError(response.status_code, code, message or f"Request failed ({response.status_code}).")


class Sentinel:
    def __init__(self, token: str, base_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/api/v1",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=20,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(self, method: str, path: str, *, params: dict | None = None, json: Any = None) -> httpx.Response:
        try:
            response = await self._client.request(method, path, params=params, json=json)
        except httpx.HTTPError as exc:
            raise SentinelError(0, "unreachable", f"Cannot reach the Sentinel API: {exc}") from exc
        if not response.is_success:
            raise _error_from(response)
        return response

    async def get(self, path: str, **params) -> Any:
        clean = {key: value for key, value in params.items() if value not in (None, "")}
        return (await self.request("GET", path, params=clean)).json()

    async def post(self, path: str, payload: dict | None = None) -> Any:
        response = await self.request("POST", path, json=payload or {})
        return response.json() if response.content else None

    async def patch(self, path: str, payload: dict) -> Any:
        return (await self.request("PATCH", path, json=payload)).json()

    async def put(self, path: str, payload: dict) -> Any:
        return (await self.request("PUT", path, json=payload)).json()

    async def delete(self, path: str) -> None:
        await self.request("DELETE", path)

    async def get_bytes(self, path: str) -> bytes:
        return (await self.request("GET", path)).content

    async def post_text(self, path: str, payload: dict) -> str:
        return (await self.request("POST", path, json=payload)).text
