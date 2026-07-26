"""Narrow Model Context Protocol boundary for EcoLoop control."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ecoloop.mcp.server import create_mcp_server

__all__ = ["create_mcp_server"]


def __getattr__(name: str) -> object:
    """Lazily expose the server factory without preloading the module entry point."""

    if name == "create_mcp_server":
        from ecoloop.mcp.server import create_mcp_server

        return create_mcp_server
    raise AttributeError(name)
