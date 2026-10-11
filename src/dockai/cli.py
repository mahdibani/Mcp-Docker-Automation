"""Command-line entry point.

    uv run dockai                 interactive REPL
    uv run dockai "is it up?"     one-shot
    uv run dockai --list-tools    show discovered MCP tools
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from dockai.agent import Agent
from dockai.config import load_settings
from dockai.llm.base import LLMUnavailable, ToolCall
from dockai.mcp_client import MCPClient, MCPConnectionError, ToolResult, is_destructive
from dockai.redact import redact

console = Console()
MAX_RESULT_CHARS = 1500


class RichUI:
    def show_call(self, call: ToolCall, destructive: bool) -> None:
        args = json.dumps(redact(call.args), ensure_ascii=False)
        style = "red" if destructive else "cyan"
        console.print(f"[bold {style}]▶ MCP tool[/] [bold]{call.name}[/] [dim]{args}[/]")

    def show_result(self, call: ToolCall, result: ToolResult) -> None:
        data = redact(result.data)
        if isinstance(data, dict):
            # Multi-line text (logs) reads better raw than as an escaped JSON string; show the end of it.
            text_fields = {k: v for k, v in data.items() if isinstance(v, str) and "\n" in v}
            rest = {k: v for k, v in data.items() if k not in text_fields}
            body = json.dumps(rest, indent=2, ensure_ascii=False) if rest else ""
            for text in text_fields.values():
                text = text.strip("\n")
                if len(text) > MAX_RESULT_CHARS * 3:
                    text = "… (earlier lines truncated)\n" + text[-MAX_RESULT_CHARS * 3:]
                body += "\n" + text
        else:
            body = json.dumps(data, indent=2, ensure_ascii=False) if data is not None else result.text
            if len(body) > MAX_RESULT_CHARS:
                body = body[:MAX_RESULT_CHARS] + "\n… (truncated)"
        body = escape(body)
        title = f"{call.name} → {'ok' if result.ok else 'error'}"
        console.print(Panel(body, title=title, title_align="left", border_style="green" if result.ok else "red"))

    def confirm(self, call: ToolCall) -> bool:
        answer = console.input(f"[bold red]Destructive action[/] [bold]{call.name}[/]. Proceed? [y/N] ")
        return answer.strip().lower() in ("y", "yes")


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


async def _ask(agent: Agent, prompt: str) -> None:
    answer = await agent.ask(prompt)
    console.print(Panel(Markdown(answer), title="dockai", title_align="left", border_style="blue"))


async def _repl(agent: Agent) -> None:
    console.print("[bold blue]dockai[/] ready. Type a request in plain English, or [bold]exit[/] to quit.")
    while True:
        try:
            prompt = console.input("[bold green]dockai>[/] ").strip()
        except EOFError:
            return
        if not prompt:
            continue
        if prompt.lower() in ("exit", "quit", "q"):
            return
        try:
            await _ask(agent, prompt)
        except Exception as exc:  # keep the session alive after one failed request
            console.print(f"[bold red]Error:[/] {exc}")


async def _run(args: argparse.Namespace) -> int:
    settings = load_settings()
    async with MCPClient(settings.mcp_command) as client:
        if args.list_tools:
            _print_tools(client)
            return 0
        if not settings.gemini_api_key:
            console.print("[bold red]GEMINI_API_KEY is not set.[/] Copy .env.example to .env and add your key.")
            return 2
        if client.get_tool("docker_status"):
            status = await client.call("docker_status")
            if not status.ok:
                console.print(f"[bold red]Docker is not reachable.[/] {status.text.removeprefix('Error executing tool docker_status: ')}")
                if args.prompt:
                    return 4
                console.print("[yellow]Starting anyway; requests will fail until Docker is running.[/]")
        from dockai.llm.gemini import GeminiProvider

        provider = GeminiProvider(settings.gemini_api_key, settings.gemini_model, settings.gemini_fallback_models)
        agent = Agent(
            client,
            provider,
            RichUI(),
            max_iterations=args.max_iterations or settings.max_iterations,
            auto_confirm=args.yes,
        )
        if args.prompt:
            await _ask(agent, " ".join(args.prompt))
        else:
            await _repl(agent)
        return 0


def main() -> None:
    # Windows consoles default to cp1252, which cannot print the symbols rich uses.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="dockai", description="Manage Docker in plain English through MCP.")
    parser.add_argument("prompt", nargs="*", help="one-shot request; omit for the interactive REPL")
    parser.add_argument("-y", "--yes", action="store_true", help="skip y/n confirmation for destructive actions")
    parser.add_argument("--max-iterations", type=int, help="cap on LLM/tool round trips per request")
    parser.add_argument("--list-tools", action="store_true", help="print the MCP tools and exit")
    args = parser.parse_args()
    try:
        code = asyncio.run(_run(args))
    except MCPConnectionError as exc:
        console.print(f"[bold red]MCP connection error:[/] {exc}")
        code = 2
    except LLMUnavailable as exc:
        console.print(f"[bold red]LLM error:[/] {exc}")
        code = 3
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)
