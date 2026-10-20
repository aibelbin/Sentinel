import asyncio
import json
from types import SimpleNamespace

import pytest

from app import mcp_server, tiers, tools


def test_every_tool_is_registered_and_has_a_tier():
    registered = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert registered == set(tiers.TIERS)


def test_context_parameter_is_not_exposed_to_the_model():
    for tool in asyncio.run(mcp_server.mcp.list_tools()):
        assert "ctx" not in tool.inputSchema.get("properties", {})
        assert tool.description


def test_limits_are_bounded_in_the_schema():
    by_name = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    limit = by_name["list_packages"].inputSchema["properties"]["limit"]
    assert limit["minimum"] == 1 and limit["maximum"] == 100


def test_content_splits_model_text_from_user_card():
    out = tools.ToolOutput("for the model", {"kind": "download", "files": []})
    blocks = mcp_server._content(out)
    assert blocks[0].annotations.audience == ["assistant"]
    assert blocks[1].annotations.audience == ["user"]
    assert json.loads(blocks[1].text)["kind"] == "download"
    assert len(mcp_server._content(tools.ToolOutput("only text"))) == 1


def test_missing_token_is_rejected_before_any_http_call():
    ctx = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers={})))
    with pytest.raises(ValueError, match="token"):
        asyncio.run(mcp_server._run(ctx, tools.get_config))
