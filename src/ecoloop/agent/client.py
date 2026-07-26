"""Genuine MCP stdio client and Ollama tool-schema conversion."""

from __future__ import annotations

import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ecoloop.agent.models import ToolSpec
from ecoloop.config import repository_root, sanitized_environment


class MCPClientError(RuntimeError):
    """Raised when MCP transport or a remote tool reports a failure."""


@runtime_checkable
class MCPClientPort(Protocol):
    """Small client surface consumed by the supervisory host."""

    async def discover_tools(self) -> list[ToolSpec]:
        """Discover currently available tools through MCP."""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call one tool through MCP and return its structured result."""


class MCPStdioClient:
    """Lifecycle-safe official MCP client over a child process's stdio."""

    def __init__(self, parameters: StdioServerParameters) -> None:
        self._parameters = parameters
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._tools: dict[str, ToolSpec] = {}

    async def __aenter__(self) -> MCPStdioClient:
        """Start the child server and initialize an MCP session."""

        if self._stack is not None:
            raise MCPClientError("MCP client is already connected")
        stack = AsyncExitStack()
        try:
            read_stream, write_stream = await stack.enter_async_context(
                stdio_client(self._parameters)
            )
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack
        self._session = session
        await self.discover_tools()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> None:
        """Close the MCP session and child process cleanly."""

        stack = self._stack
        self._stack = None
        self._session = None
        self._tools.clear()
        if stack is not None:
            await stack.aclose()

    async def discover_tools(self) -> list[ToolSpec]:
        """Discover FastMCP tools dynamically from the connected server."""

        session = self._require_session()
        response = await session.list_tools()
        tools = [
            ToolSpec(
                name=tool.name,
                description=tool.description or "",
                input_schema=dict(tool.inputSchema),
            )
            for tool in response.tools
        ]
        self._tools = {tool.name: tool for tool in tools}
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke a discovered tool through MCP and parse structured output."""

        session = self._require_session()
        if name not in self._tools:
            raise MCPClientError(f"tool was not discovered from MCP server: {name}")
        result = await session.call_tool(name, arguments)
        if result.isError:
            message = _text_result(result.content)
            raise MCPClientError(f"MCP tool {name} failed: {message[:500]}")
        if result.structuredContent is not None:
            structured = dict(result.structuredContent)
            if set(structured) == {"result"} and isinstance(structured["result"], dict):
                return dict(structured["result"])
            return structured
        text = _text_result(result.content)
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MCPClientError(f"MCP tool {name} returned non-JSON text") from exc
        if not isinstance(parsed, dict):
            raise MCPClientError(f"MCP tool {name} returned a non-object result")
        return parsed

    def ollama_tools(self) -> list[dict[str, Any]]:
        """Return Ollama function definitions for the latest discovery result."""

        return [tool.as_ollama_tool() for tool in self._tools.values()]

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise MCPClientError("MCP client is not connected")
        return self._session


def default_server_parameters(
    *,
    database_path: Path | None = None,
) -> StdioServerParameters:
    """Return allowlisted parameters for the packaged local stdio server."""

    environment = _safe_child_environment()
    if database_path is not None:
        environment["ECOLOOP_DATABASE_PATH"] = str(database_path.resolve())
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "ecoloop.mcp.server"],
        env=environment,
        cwd=repository_root(),
    )


def tools_for_ollama(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    """Convert dynamically discovered MCP tools to Ollama definitions."""

    return [tool.as_ollama_tool() for tool in tools]


def _safe_child_environment() -> dict[str, str]:
    values = sanitized_environment()
    for name in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH"):
        value = os.environ.get(name)
        if value:
            values[name] = value
    return values


def _text_result(content: list[Any]) -> str:
    return "\n".join(
        str(item.text)
        for item in content
        if getattr(item, "type", None) == "text" and hasattr(item, "text")
    )
