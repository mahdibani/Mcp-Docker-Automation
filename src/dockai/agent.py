"""Agent loop: prompt -> LLM -> tool call(s) -> results -> LLM ... -> final answer."""

from __future__ import annotations

from typing import Any, Protocol

from mcp.types import Tool

from dockai.llm.base import LLMProvider, ToolCall, ToolOutcome, ToolSpec
from dockai.mcp_client import MCPClient, ToolResult, is_destructive

SYSTEM_PROMPT = """\
You are dockai, an assistant that manages Docker containers on the user's Windows machine
through the tools you are given. You never ask the user to run docker commands themselves.

Rules:
- Use tools to act and to check state; never guess whether something is running.
- Resolve follow-ups ("it", "its logs", "everything you created") from the conversation so far.
- You may chain several tool calls to finish one request.
- Postgres databases are created as stacks with compose_up. Name the stack after the database
  unless the user names it. Only call a database "ready" once compose_up reports it healthy.
- When a tool says a port is in use and suggests a free one, retry with the suggested port and
  tell the user clearly which port you used instead.
- "Everything you created" means resources managed by dockai: take down each stack with
  compose_down and remove standalone dockai containers. Delete volumes only when the user asks
  to remove everything or delete the data. Never try to stop or remove other containers.
- The user already sees every tool result. Do not repeat long output such as logs; summarize
  what matters in it (errors, warnings, whether startup finished).
- Never print or invent passwords. Tool results deliberately hide them.
- If a tool fails, explain the cause in one or two sentences and what to do next.
- Answer concisely in plain text.
"""


class AgentUI(Protocol):
    def show_call(self, call: ToolCall, destructive: bool) -> None: ...
    def show_result(self, call: ToolCall, result: ToolResult) -> None: ...
    def confirm(self, call: ToolCall) -> bool: ...


def tool_spec(tool: Tool) -> ToolSpec:
    return ToolSpec(name=tool.name, description=(tool.description or "").strip(), parameters=tool.input_schema or {})


def _payload(result: ToolResult) -> Any:
    return result.data if result.data is not None else result.text


class Agent:
    def __init__(
        self,
        mcp: MCPClient,
        provider: LLMProvider,
        ui: AgentUI,
        *,
        max_iterations: int = 8,
        auto_confirm: bool = False,
    ):
        self.mcp = mcp
        self.ui = ui
        self.max_iterations = max_iterations
        self.auto_confirm = auto_confirm
        self.chat = provider.start_chat(SYSTEM_PROMPT, [tool_spec(t) for t in mcp.tools])

    async def _execute(self, call: ToolCall) -> ToolOutcome:
        tool = self.mcp.get_tool(call.name)
        if tool is None:
            return ToolOutcome(call, ok=False, payload=f"Unknown tool {call.name!r}.")
        destructive = is_destructive(tool)
        self.ui.show_call(call, destructive)
        if destructive and not self.auto_confirm and not self.ui.confirm(call):
            return ToolOutcome(call, ok=False, payload="The user declined this destructive action. Do not retry it.")
        result = await self.mcp.call(call.name, call.args)
        self.ui.show_result(call, result)
        return ToolOutcome(call, ok=result.ok, payload=_payload(result))

    async def ask(self, prompt: str) -> str:
        reply = await self.chat.send(prompt)
        for _ in range(self.max_iterations):
            if not reply.tool_calls:
                return reply.text or "(no answer)"
            outcomes = [await self._execute(call) for call in reply.tool_calls]
            reply = await self.chat.send_tool_results(outcomes)
        if not reply.tool_calls:
            return reply.text or "(no answer)"
        return f"Stopped after {self.max_iterations} tool rounds without a final answer (raise --max-iterations)."
