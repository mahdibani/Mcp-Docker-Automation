"""Agent loop tests: scripted LLM, real MCPClient with a mocked MCP ClientSession."""

from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

from dockai.agent import Agent
from dockai.llm.base import ChatSession, LLMProvider, LLMReply, ToolCall, ToolOutcome
from dockai.mcp_client import MCPClient

SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}}}
TOOLS = [
    Tool(name="list_containers", description="List", input_schema={"type": "object", "properties": {}},
         annotations=ToolAnnotations(read_only_hint=True)),
    Tool(name="get_logs", description="Logs", input_schema=SCHEMA, annotations=ToolAnnotations(read_only_hint=True)),
    Tool(name="compose_down", description="Down", input_schema=SCHEMA,
         annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True)),
]


class ScriptedChat(ChatSession):
    def __init__(self, replies):
        self.replies = list(replies)
        self.sent_prompts: list[str] = []
        self.sent_outcomes: list[list[ToolOutcome]] = []

    async def send(self, user_text):
        self.sent_prompts.append(user_text)
        return self.replies.pop(0)

    async def send_tool_results(self, outcomes):
        self.sent_outcomes.append(outcomes)
        return self.replies.pop(0)


class ScriptedProvider(LLMProvider):
    def __init__(self, replies):
        self.chat = ScriptedChat(replies)
        self.tools = None

    def start_chat(self, system_prompt, tools):
        self.tools = tools
        return self.chat


class RecordingUI:
    def __init__(self, confirm_answer=True):
        self.calls, self.results, self.confirm_asked = [], [], []
        self.confirm_answer = confirm_answer

    def show_call(self, call, destructive):
        self.calls.append((call.name, destructive))

    def show_result(self, call, result):
        self.results.append((call.name, result.ok))

    def confirm(self, call):
        self.confirm_asked.append(call.name)
        return self.confirm_answer


def make_client(structured=None, is_error=False, text='{"ok": true}'):
    client = MCPClient(["unused"])
    client.tools = TOOLS
    session = AsyncMock()
    session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text=text)], structured_content=structured, is_error=is_error
    )
    client._session = session
    return client, session


def call(tool, **args):
    return ToolCall(id=f"id-{tool}", name=tool, args=args)


def make_agent(replies, *, ui=None, auto_confirm=False, max_iterations=8, **client_kwargs):
    client, session = make_client(**client_kwargs)
    provider = ScriptedProvider(replies)
    ui = ui or RecordingUI()
    agent = Agent(client, provider, ui, max_iterations=max_iterations, auto_confirm=auto_confirm)
    return agent, provider, session, ui


async def test_tools_come_from_the_mcp_session():
    agent, provider, *_ = make_agent([LLMReply(text="hi")])
    assert [t.name for t in provider.tools] == ["list_containers", "get_logs", "compose_down"]
    assert provider.tools[1].parameters == SCHEMA


async def test_answer_without_tools():
    agent, provider, session, ui = make_agent([LLMReply(text="Hello")])
    assert await agent.ask("hi") == "Hello"
    session.call_tool.assert_not_called()


async def test_single_tool_call_then_answer():
    agent, provider, session, ui = make_agent(
        [LLMReply(text=None, tool_calls=[call("list_containers")]), LLMReply(text="none running")],
        structured={"containers": []},
    )
    assert await agent.ask("what is running?") == "none running"
    session.call_tool.assert_awaited_once_with("list_containers", {})
    assert ui.calls == [("list_containers", False)]
    [[outcome]] = provider.chat.sent_outcomes
    assert outcome.ok and outcome.payload == {"containers": []} and outcome.call.id == "id-list_containers"


async def test_multi_step_chain_in_one_prompt():
    agent, provider, session, ui = make_agent([
        LLMReply(text=None, tool_calls=[call("list_containers")]),
        LLMReply(text=None, tool_calls=[call("get_logs", name="shop_db-postgres")]),
        LLMReply(text="logs look fine"),
    ])
    assert await agent.ask("find the db and check its logs") == "logs look fine"
    assert [c.args[0] for c in session.call_tool.await_args_list] == ["list_containers", "get_logs"]


async def test_parallel_calls_in_one_reply_all_run():
    agent, provider, session, ui = make_agent([
        LLMReply(text=None, tool_calls=[call("get_logs", name="a"), call("get_logs", name="b")]),
        LLMReply(text="done"),
    ])
    await agent.ask("logs of a and b")
    assert len(provider.chat.sent_outcomes[0]) == 2


async def test_destructive_call_needs_confirmation():
    ui = RecordingUI(confirm_answer=True)
    agent, provider, session, ui = make_agent(
        [LLMReply(text=None, tool_calls=[call("compose_down", name="shop_db")]), LLMReply(text="removed")], ui=ui
    )
    await agent.ask("remove it")
    assert ui.confirm_asked == ["compose_down"]
    session.call_tool.assert_awaited_once()


async def test_declined_destructive_call_is_not_executed():
    ui = RecordingUI(confirm_answer=False)
    agent, provider, session, ui = make_agent(
        [LLMReply(text=None, tool_calls=[call("compose_down", name="shop_db")]), LLMReply(text="ok, kept it")], ui=ui
    )
    assert await agent.ask("remove it") == "ok, kept it"
    session.call_tool.assert_not_called()
    [[outcome]] = provider.chat.sent_outcomes
    assert not outcome.ok and "declined" in outcome.payload


async def test_yes_flag_skips_confirmation():
    ui = RecordingUI(confirm_answer=False)
    agent, provider, session, ui = make_agent(
        [LLMReply(text=None, tool_calls=[call("compose_down", name="x")]), LLMReply(text="gone")],
        ui=ui, auto_confirm=True,
    )
    await agent.ask("remove it")
    assert ui.confirm_asked == []
    session.call_tool.assert_awaited_once()


async def test_read_only_tools_never_ask_for_confirmation():
    ui = RecordingUI(confirm_answer=False)
    agent, *_ = make_agent([LLMReply(text=None, tool_calls=[call("get_logs", name="x")]), LLMReply(text="ok")], ui=ui)
    await agent.ask("logs")
    assert ui.confirm_asked == []


async def test_tool_error_is_reported_back_to_the_llm():
    agent, provider, session, ui = make_agent(
        [LLMReply(text=None, tool_calls=[call("get_logs", name="nope")]), LLMReply(text="no such container")],
        is_error=True, text="No container named 'nope'.",
    )
    assert await agent.ask("logs of nope") == "no such container"
    [[outcome]] = provider.chat.sent_outcomes
    assert not outcome.ok and outcome.payload == "No container named 'nope'."
    assert ui.results == [("get_logs", False)]


async def test_unknown_tool_is_not_sent_to_mcp():
    agent, provider, session, ui = make_agent(
        [LLMReply(text=None, tool_calls=[call("rm_rf")]), LLMReply(text="cannot")]
    )
    await agent.ask("do something odd")
    session.call_tool.assert_not_called()
    assert "Unknown tool" in provider.chat.sent_outcomes[0][0].payload


async def test_max_iterations_cap_stops_runaway_loops():
    loop_forever = [LLMReply(text=None, tool_calls=[call("list_containers")]) for _ in range(10)]
    agent, provider, session, ui = make_agent(loop_forever, max_iterations=3)
    answer = await agent.ask("loop")
    assert "Stopped after 3 tool rounds" in answer
    assert session.call_tool.await_count == 3


async def test_mcp_transport_failure_becomes_a_tool_error():
    agent, provider, session, ui = make_agent(
        [LLMReply(text=None, tool_calls=[call("list_containers")]), LLMReply(text="the server is down")]
    )
    session.call_tool.side_effect = ConnectionError("pipe closed")
    assert await agent.ask("list") == "the server is down"
    assert "pipe closed" in provider.chat.sent_outcomes[0][0].payload


@pytest.mark.parametrize("text,expected", [('{"a": 1}', {"a": 1}), ("plain text", None)])
async def test_text_results_are_parsed_as_json_when_possible(text, expected):
    client, session = make_client(text=text)
    result = await client.call("list_containers")
    assert result.data == expected and result.text == text
