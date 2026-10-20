"""Thin client for Liva, the chat assistant.

Liva is optional and isolated: every failure to reach it is a LivaUnavailable
that the view turns into a notice, never an exception the rest of the dashboard
sees.
"""

import json
import os
from typing import Iterator

import requests

CONNECT_TIMEOUT = 3
READ_TIMEOUT = 180


class LivaUnavailable(Exception):
    """Liva is not reachable."""


class LivaError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def base_url() -> str:
    return os.environ.get("LIVA_URL", "http://liva:8100").rstrip("/")


class Liva:
    def __init__(self, token: str, user: str) -> None:
        self._token = token
        self._user = user
        self._session = requests.Session()

    def _request(self, method: str, path: str, *, stream: bool = False, **kwargs):
        try:
            response = self._session.request(
                method, f"{base_url()}{path}",
                headers={"Authorization": f"Bearer {self._token}", "X-Liva-User": self._user},
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), stream=stream, **kwargs,
            )
        except requests.RequestException as exc:
            raise LivaUnavailable(str(exc)) from exc
        if response.status_code >= 400:
            message = response.text[:300]
            try:
                detail = response.json().get("detail")
                message = detail if isinstance(detail, str) else message
            except (ValueError, AttributeError):
                pass
            raise LivaError(response.status_code, message or f"Liva returned {response.status_code}.")
        return response

    def health(self) -> dict:
        return self._request("GET", "/healthz").json()

    def chats(self) -> list[dict]:
        return self._request("GET", "/chats").json()

    def new_chat(self) -> dict:
        return self._request("POST", "/chats").json()

    def chat(self, chat_id: str) -> dict:
        return self._request("GET", f"/chats/{chat_id}").json()

    def delete_chat(self, chat_id: str) -> None:
        self._request("DELETE", f"/chats/{chat_id}")

    def audit(self) -> list[dict]:
        return self._request("GET", "/audit").json()

    def download(self, download_id: str) -> bytes:
        return self._request("GET", f"/downloads/{download_id}").content

    def _events(self, response) -> Iterator[dict]:
        # The stream can die after the request succeeded (Liva restarts, a read
        # times out, a line is cut short); none of that may escape as a traceback.
        try:
            for line in response.iter_lines(decode_unicode=True):
                if line:
                    yield json.loads(line)
        except (requests.RequestException, ValueError) as exc:
            raise LivaUnavailable(f"the connection to Liva broke mid-answer: {exc}") from exc

    def send(self, chat_id: str, text: str, bypass: bool) -> Iterator[dict]:
        response = self._request(
            "POST", f"/chats/{chat_id}/messages", stream=True, json={"content": text, "bypass": bypass}
        )
        yield from self._events(response)

    def decide(self, chat_id: str, call_id: str, approve: bool, bypass: bool) -> Iterator[dict]:
        response = self._request(
            "POST", f"/chats/{chat_id}/approvals/{call_id}", stream=True,
            json={"approve": approve, "bypass": bypass},
        )
        yield from self._events(response)
