import asyncio

import httpx
import pytest

from app.config import Settings
from app.groq import GroqError, chat_completion

SETTINGS = Settings(
    groq_api_key="gsk_test", groq_base_url="https://groq.test/openai/v1", model="m",
    sentinel_url="http://api", public_url="http://api", db_path=":memory:",
    mcp_port=1, mcp_url="http://x", max_steps=8, retention_days=30,
)


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def call(handler, settings=SETTINGS):
    async def go():
        async with client_for(handler) as client:
            return await chat_completion([{"role": "user", "content": "hi"}], [], settings=settings, client=client)
    return asyncio.run(go())


def test_returns_the_assistant_message_and_sends_auth_and_model():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.read()
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "hello"}}]})

    msg = call(handler)
    assert msg == {"role": "assistant", "content": "hello"}
    assert seen["auth"] == "Bearer gsk_test"
    assert b'"model":"m"' in seen["body"].replace(b" ", b"")


def test_tool_calls_are_passed_through():
    calls = [{"id": "c1", "type": "function", "function": {"name": "get_config", "arguments": "{}"}}]

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": calls}}]})

    assert call(handler)["tool_calls"] == calls


def test_rate_limit_carries_retry_after():
    def handler(request):
        return httpx.Response(429, headers={"retry-after": "7"}, json={"error": {"message": "slow down"}})

    with pytest.raises(GroqError) as err:
        call(handler)
    assert err.value.status == 429 and err.value.retry_after == 7
    assert "7" in err.value.message


def test_malformed_tool_call_is_identifiable():
    def handler(request):
        return httpx.Response(400, json={"error": {"message": "bad", "code": "tool_use_failed"}})

    with pytest.raises(GroqError) as err:
        call(handler)
    assert err.value.code == "tool_use_failed"


def test_missing_key_is_a_clear_error_without_a_request():
    unset = Settings(**{**SETTINGS.__dict__, "groq_api_key": None})
    with pytest.raises(GroqError, match="GROQ_API_KEY"):
        call(lambda r: pytest.fail("no request expected"), unset)
