"""Provider-neutral types. A new model only needs an LLMProvider and a ChatSession."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class LLMUnavailable(RuntimeError):
    """The model could not be reached or refused the request; the message is user-facing."""


@dataclass(frozen=True)
class ToolSpec:
    """A tool as the LLM sees it: name, description and a JSON Schema for its arguments."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str | None
    name: str
    args: dict[str, Any]


@dataclass
class LLMReply:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass(frozen=True)
class ToolOutcome:
    """Result of executing a ToolCall, sent back to the model."""

    call: ToolCall
    ok: bool
    payload: Any


class ChatSession(ABC):
    """One conversation. Keeps its own history so follow-ups like "is it running?" work."""

    @abstractmethod
    async def send(self, user_text: str) -> LLMReply: ...

    @abstractmethod
    async def send_tool_results(self, outcomes: list[ToolOutcome]) -> LLMReply: ...


class LLMProvider(ABC):
    @abstractmethod
    def start_chat(self, system_prompt: str, tools: list[ToolSpec]) -> ChatSession: ...
