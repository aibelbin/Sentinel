"""The tool server: a real MCP server, listening on localhost only.

Runs as its own process so a fault in a tool cannot take the chat service down.
It forwards the caller's bearer token to Sentinel, so a tool can never do more
than the signed-in administrator can.
"""

import json
from typing import Annotated, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import Annotations, TextContent
from pydantic import Field

from . import tools
from .config import get_settings
from .sentinel import Sentinel, SentinelError

mcp = FastMCP(
    "liva-tools",
    host="127.0.0.1",
    port=get_settings().mcp_port,
    stateless_http=True,
    json_response=True,
)

Device = Annotated[str, Field(min_length=1, max_length=128, description="Device name (e.g. web-01) or UUID")]
Limit = Annotated[int, Field(ge=1, le=100, description="Page size")]
Offset = Annotated[int, Field(ge=0, description="Rows to skip")]


def _content(out: tools.ToolOutput) -> list[TextContent]:
    """The assistant block is for the model; the user block is for the screen only."""
    blocks = [TextContent(type="text", text=out.text, annotations=Annotations(audience=["assistant"]))]
    if out.ui is not None:
        blocks.append(
            TextContent(type="text", text=json.dumps(out.ui), annotations=Annotations(audience=["user"]))
        )
    return blocks


async def _run(ctx: Context, fn, **kwargs) -> list[TextContent]:
    header = ctx.request_context.request.headers.get("authorization", "")
    token = header.removeprefix("Bearer ").strip()
    if not token:
        raise ValueError("missing Sentinel session token")
    client = Sentinel(token, get_settings().sentinel_url)
    try:
        return _content(await fn(client, **kwargs))
    except (tools.ToolError, SentinelError) as exc:
        # Becomes an error result the model can read and explain.
        raise ValueError(str(exc)) from exc
    finally:
        await client.aclose()


@mcp.tool()
async def list_devices(
    ctx: Context,
    status: Annotated[
        Literal["online", "stale", "offline", "disabled"] | None,
        Field(description="Only devices in this state"),
    ] = None,
    q: Annotated[str | None, Field(max_length=128, description="Substring of name or hostname")] = None,
) -> list[TextContent]:
    """List managed devices with status, OS, package count and live CPU/memory."""
    return await _run(ctx, tools.list_devices, status=status, q=q)


@mcp.tool()
async def get_device(ctx: Context, device: Device) -> list[TextContent]:
    """Full detail for one device: host facts, resources, config, watchdog update state."""
    return await _run(ctx, tools.get_device, device=device)


@mcp.tool()
async def list_packages(
    ctx: Context,
    device: Device,
    ecosystem: Literal["os", "python", "npm", "java"] | None = None,
    q: Annotated[str | None, Field(max_length=128)] = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[TextContent]:
    """Installed software on a device, optionally filtered by ecosystem or name."""
    return await _run(
        ctx, tools.list_packages, device=device, ecosystem=ecosystem, q=q, limit=limit, offset=offset
    )


@mcp.tool()
async def list_changes(ctx: Context, device: Device, limit: Limit = 50, offset: Offset = 0) -> list[TextContent]:
    """Recent package changes (added, removed, upgraded) detected on a device."""
    return await _run(ctx, tools.list_changes, device=device, limit=limit, offset=offset)


@mcp.tool()
async def list_reports(ctx: Context, device: Device, limit: Limit = 20, offset: Offset = 0) -> list[TextContent]:
    """Inventory reports a device has submitted."""
    return await _run(ctx, tools.list_reports, device=device, limit=limit, offset=offset)


@mcp.tool()
async def list_tasks(ctx: Context, device: Device, limit: Limit = 20, offset: Offset = 0) -> list[TextContent]:
    """Task history for a device."""
    return await _run(ctx, tools.list_tasks, device=device, limit=limit, offset=offset)


@mcp.tool()
async def get_config(ctx: Context) -> list[TextContent]:
    """The global watchdog configuration (poll and inventory intervals, spool size)."""
    return await _run(ctx, tools.get_config)


@mcp.tool()
async def get_release_status(ctx: Context) -> list[TextContent]:
    """The watchdog version being served, how many devices run it, and whether it is quarantined."""
    return await _run(ctx, tools.get_release_status)


@mcp.tool()
async def create_device(
    ctx: Context, name: Annotated[str, Field(min_length=1, max_length=128, description="Unique device name")]
) -> list[TextContent]:
    """Register a new device. The key, .deb and install commands appear on the administrator's screen, not in your result."""
    return await _run(ctx, tools.create_device, name=name)


@mcp.tool()
async def build_package(ctx: Context) -> list[TextContent]:
    """Build the watchdog .deb and offer it to the administrator as a download."""
    return await _run(ctx, tools.build_package)


@mcp.tool()
async def update_device(
    ctx: Context,
    device: Device,
    name: Annotated[str | None, Field(min_length=1, max_length=128)] = None,
    enabled: bool | None = None,
) -> list[TextContent]:
    """Rename a device or enable/disable it."""
    return await _run(ctx, tools.update_device, device=device, name=name, enabled=enabled)


@mcp.tool()
async def queue_collect_inventory(ctx: Context, device: Device) -> list[TextContent]:
    """Ask a device to re-scan its installed software on its next heartbeat."""
    return await _run(ctx, tools.queue_collect_inventory, device=device)


@mcp.tool()
async def delete_device(ctx: Context, device: Device) -> list[TextContent]:
    """Permanently delete a device and its history. Requires the administrator's approval."""
    return await _run(ctx, tools.delete_device, device=device)


@mcp.tool()
async def rotate_device_key(ctx: Context, device: Device) -> list[TextContent]:
    """Issue a new API key for a device; the old one stops working. Requires approval."""
    return await _run(ctx, tools.rotate_device_key, device=device)


@mcp.tool()
async def update_config(
    ctx: Context,
    poll_interval_seconds: Annotated[int | None, Field(ge=30, le=3600)] = None,
    inventory_interval_seconds: Annotated[int | None, Field(ge=300, le=604800)] = None,
    spool_max_bytes: Annotated[int | None, Field(ge=1048576, le=10737418240)] = None,
) -> list[TextContent]:
    """Change the global watchdog configuration for every device. Requires approval."""
    return await _run(
        ctx,
        tools.update_config,
        poll_interval_seconds=poll_interval_seconds,
        inventory_interval_seconds=inventory_interval_seconds,
        spool_max_bytes=spool_max_bytes,
    )


@mcp.tool()
async def clear_quarantine(ctx: Context) -> list[TextContent]:
    """Let a quarantined watchdog version be handed out again. Requires approval."""
    return await _run(ctx, tools.clear_quarantine)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
