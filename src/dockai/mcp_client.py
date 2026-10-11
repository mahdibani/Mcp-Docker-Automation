"""Async MCP client: launches the server over stdio, discovers tools, calls them."""

from __future__ import annotations

import json
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client
from mcp.types import CallToolResult, TextContent, Tool


class MCPConnectionError(RuntimeError):
    pass


@dataclass
class ToolResult:
    ok: bool
    text: str
    data: Any = None


def _server_env() -> dict[str, str]:
    # The SDK only forwards a minimal safe set of variables; Docker on Windows also needs these.
    env = get_default_environment()
    for key, value in os.environ.items():
        if key.startswith(("DOCKER_", "DOCKAI_", "POSTGRES_")) or key in ("ProgramFiles", "ProgramData", "PROGRAMFILES"):
            env[key] = value
    return env


def is_destructive(tool: Tool) -> bool:
    annotations = tool.annotations
    return bool(annotations and annotations.destructive_hint and not annotations.read_only_hint)


def _to_result(result: CallToolResult) -> ToolResult:
    text = "\n".join(c.text for c in result.content if isinstance(c, TextContent))
    data = result.structured_content
    if data is None:
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            data = None
    return ToolResult(ok=not result.is_error, text=text, data=data)


class MCPClient:
    """`async with MCPClient(cmd) as client:` starts the server and lists its tools."""

    def __init__(self, command: list[str]):
        if not command:
            raise ValueError("MCP server command is empty")
        self.command = command
        self.tools: list[Tool] = []
        self._session: ClientSession | None = None
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> MCPClient:
        params = StdioServerParameters(command=self.command[0], args=self.command[1:], env=_server_env())
        try:
            read, write = await self._stack.enter_async_context(stdio_client(params))
            self._session = await self._stack.enter_async_context(ClientSession(read, write))
            await self._session.initialize()
            self.tools = (await self._session.list_tools()).tools
        except Exception as exc:
            await self._stack.aclose()
            raise MCPConnectionError(
                f"Could not connect to the MCP server `{' '.join(self.command)}`: {exc or type(exc).__name__}"
            ) from exc
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self._stack.aclose()

    def get_tool(self, name: str) -> Tool | None:
        return next((t for t in self.tools if t.name == name), None)

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        if self._session is None:
            raise MCPConnectionError("MCP session is not open")
        try:
            result = await self._session.call_tool(name, arguments or {})
        except Exception as exc:
            return ToolResult(ok=False, text=f"MCP call failed: {exc}")
        return _to_result(result)
