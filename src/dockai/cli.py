"""Command-line entry point: `uv run dockai`."""

from __future__ import annotations

import argparse
import asyncio
import sys

from rich.console import Console
from rich.table import Table

from dockai.config import load_settings
from dockai.mcp_client import MCPClient, MCPConnectionError, is_destructive

console = Console()


def _print_tools(client: MCPClient) -> None:
    table = Table(title=f"{len(client.tools)} MCP tools discovered", title_justify="left")
    table.add_column("tool", style="cyan", no_wrap=True)
    table.add_column("kind")
    table.add_column("parameters", style="magenta")
    table.add_column("description")
    for tool in client.tools:
        kind = "[red]destructive[/]" if is_destructive(tool) else "[green]read-only[/]" if (
            tool.annotations and tool.annotations.read_only_hint
        ) else "write"
        params = ", ".join((tool.input_schema or {}).get("properties", {}).keys())
        table.add_row(tool.name, kind, params, (tool.description or "").strip().splitlines()[0])
    console.print(table)


async def _run(args: argparse.Namespace) -> int:
    settings = load_settings()
    async with MCPClient(settings.mcp_command) as client:
        if args.list_tools:
            _print_tools(client)
            return 0
        console.print("[yellow]Agent mode is not implemented yet. Use --list-tools.[/]")
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="dockai", description="Manage Docker in plain English through MCP.")
    parser.add_argument("--list-tools", action="store_true", help="print the MCP tools and exit")
    args = parser.parse_args()
    try:
        code = asyncio.run(_run(args))
    except MCPConnectionError as exc:
        console.print(f"[bold red]MCP connection error:[/] {exc}")
        code = 2
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)
