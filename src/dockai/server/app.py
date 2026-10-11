"""MCP server exposing typed Docker tools. Run with `uv run dockai-server` (stdio transport)."""

from __future__ import annotations

import logging
import sys

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from dockai import MANAGED_LABEL_KEY, MANAGED_LABEL_VALUE
from dockai.server import docker_ops
from dockai.server.docker_ops import DockerUnavailable

# stdout carries the MCP protocol, so logs must go to stderr.
logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="[dockai-server] %(levelname)s %(message)s")

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)

mcp = MCPServer(
    name="dockai",
    instructions=(
        "Docker tools for dockai. Everything created is labelled managed-by=dockai; "
        "stop/remove tools only touch labelled resources."
    ),
    log_level="WARNING",
)


def _docker():
    try:
        return docker_ops.client()
    except DockerUnavailable as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=READ_ONLY)
def docker_status() -> dict:
    """Check that the Docker daemon is reachable and report its version."""
    info = _docker().version()
    return {"reachable": True, "version": info.get("Version"), "os": info.get("Os"), "arch": info.get("Arch")}


@mcp.tool(annotations=READ_ONLY)
def list_containers(include_stopped: bool = True, only_managed: bool = False) -> dict:
    """List containers with status, health and published ports.

    Args:
        include_stopped: Also list containers that are not running.
        only_managed: Only list containers created by dockai (label managed-by=dockai).
    """
    filters = {"label": f"{MANAGED_LABEL_KEY}={MANAGED_LABEL_VALUE}"} if only_managed else None
    containers = _docker().containers.list(all=include_stopped, filters=filters)
    return {"containers": [docker_ops.summarize(c) for c in containers]}


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
