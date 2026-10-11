"""Gemini function-calling provider (google-genai SDK)."""

from __future__ import annotations

import logging
from typing import Any

from google import genai
from google.genai import errors, types

from dockai.llm.base import ChatSession, LLMProvider, LLMReply, LLMUnavailable, ToolCall, ToolOutcome, ToolSpec
from dockai.llm.gemini_schema import to_gemini_parameters
from dockai.retry import aretry

log = logging.getLogger(__name__)


class TransientLLMError(RuntimeError):
    pass


def _declaration(spec: ToolSpec) -> types.FunctionDeclaration:
    return types.FunctionDeclaration(
        name=spec.name,
        description=spec.description,
        parameters=to_gemini_parameters(spec.parameters),
    )


def _reply(response: types.GenerateContentResponse) -> LLMReply:
    calls = [ToolCall(id=fc.id, name=fc.name or "", args=dict(fc.args or {})) for fc in response.function_calls or []]
    text = None
    if response.candidates and response.candidates[0].content:
        text = "".join(p.text for p in response.candidates[0].content.parts or [] if p.text and not p.thought) or None
    return LLMReply(text=text, tool_calls=calls)


class GeminiChat(ChatSession):
    """Wraps a google-genai async chat. On a sustained outage it moves to the next model, keeping history."""

    def __init__(self, client: genai.Client, models: list[str], config: types.GenerateContentConfig):
        self._client = client
        self._models = models
        self._config = config
        self._index = 0
        self._chat = client.aio.chats.create(model=models[0], config=config)

    @property
    def model(self) -> str:
        return self._models[self._index]

    def _switch_to_next_model(self) -> bool:
        if self._index + 1 >= len(self._models):
            return False
        history = self._chat.get_history()
        self._index += 1
        log.warning("model overloaded, falling back to %s", self.model)
        self._chat = self._client.aio.chats.create(model=self.model, config=self._config, history=history)
        return True

    async def _send(self, message: Any) -> LLMReply:
        async def attempt() -> types.GenerateContentResponse:
            try:
                return await self._chat.send_message(message)
            except errors.ServerError as exc:
                raise TransientLLMError(f"{exc.code} {exc.status}") from exc
            except errors.ClientError as exc:
                if exc.code == 429:
                    raise TransientLLMError(f"{exc.code} {exc.status}") from exc
                raise

        while True:
            try:
                response = await aretry(attempt, attempts=3, base_delay=2.0, retry_on=(TransientLLMError,))
                return _reply(response)
            except TransientLLMError as exc:
                if self._switch_to_next_model():
                    continue
                raise LLMUnavailable(
                    f"Gemini is unavailable or rate-limited on every configured model ({exc}). "
                    "Wait a minute and try again, or change GEMINI_MODEL / GEMINI_FALLBACK_MODELS in .env."
                ) from exc
            except errors.ClientError as exc:
                raise LLMUnavailable(f"Gemini rejected the request: {exc}") from exc

    async def send(self, user_text: str) -> LLMReply:
        return await self._send(user_text)

    async def send_tool_results(self, outcomes: list[ToolOutcome]) -> LLMReply:
        parts = []
        for o in outcomes:
            response = {"result": o.payload} if o.ok else {"error": o.payload}
            part = types.Part.from_function_response(name=o.call.name, response=response)
            part.function_response.id = o.call.id
            parts.append(part)
        return await self._send(parts)


class GeminiProvider(LLMProvider):
    def __init__(self, api_key: str, model: str, fallback_models: list[str] | None = None):
        self._client = genai.Client(api_key=api_key)
        self.models = [model, *[m for m in fallback_models or [] if m != model]]

    def start_chat(self, system_prompt: str, tools: list[ToolSpec]) -> ChatSession:
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=[types.Tool(function_declarations=[_declaration(t) for t in tools])],
            # We run the tools ourselves so every call is shown (and confirmed) before it executes.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        return GeminiChat(self._client, self.models, config)
