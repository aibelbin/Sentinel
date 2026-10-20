"""Groq chat completions (OpenAI-compatible) with tool calling."""

import httpx

from .config import Settings


class GroqError(Exception):
    def __init__(self, status: int, message: str, *, code: str | None = None,
                 retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.retry_after = retry_after


async def chat_completion(
    messages: list[dict], tools: list[dict], *, settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> dict:
    if not settings.groq_api_key:
        raise GroqError(
            0, "GROQ_API_KEY is not set, so Liva cannot talk to the model yet.", code="not_configured"
        )

    body: dict = {"model": settings.model, "messages": messages, "temperature": 0.2}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=60)
    try:
        response = await client.post(
            f"{settings.groq_base_url}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {settings.groq_api_key}"},
        )
    except httpx.HTTPError as exc:
        raise GroqError(0, f"Cannot reach Groq: {exc}", code="unreachable") from exc
    finally:
        if owns_client:
            await client.aclose()

    if response.status_code == 429:
        try:
            wait = float(response.headers.get("retry-after", ""))
        except ValueError:
            wait = None
        suffix = f" Retry in {int(wait)}s." if wait is not None else " Try again shortly."
        raise GroqError(429, f"Groq rate limit reached.{suffix}", code="rate_limited", retry_after=wait)
    if not response.is_success:
        try:
            error = response.json().get("error", {})
        except ValueError:
            error = {}
        raise GroqError(
            response.status_code, error.get("message") or response.text[:300], code=error.get("code")
        )

    message = response.json()["choices"][0]["message"]
    out: dict = {"role": "assistant", "content": message.get("content")}
    if message.get("tool_calls"):
        out["tool_calls"] = message["tool_calls"]
    return out
