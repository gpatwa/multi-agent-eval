"""OpenAI adapter — official `openai` SDK (Chat Completions).

Also serves as the base class for any OpenAI-compatible endpoint
(Z.ai GLM, local vLLM/Ollama gateways, etc.) — subclasses only change
the base URL, API-key env var, and token-cap parameter name.
"""
from __future__ import annotations

import json
import os

from .base import ChatMessage, ModelResponse, Provider, ToolCall, ToolHistory, ToolSpec, ToolTurn


class OpenAIProvider(Provider):
    # Chat Completions function calling. OpenAI-compatible subclasses inherit
    # this; an endpoint that doesn't implement `tools` fails that candidate's
    # run with the endpoint's error rather than silently answering in text.
    supports_tools = True
    api_key_env = "OPENAI_API_KEY"
    base_url: str | None = None
    # gpt-5-family models reject `max_tokens`; compatible endpoints (GLM)
    # often only accept `max_tokens`. Subclasses override as needed.
    token_param = "max_completion_tokens"

    def __init__(self, model: str = "gpt-6-astra"):
        super().__init__(model)
        import openai

        self.client = openai.OpenAI(
            api_key=os.environ[self.api_key_env],
            base_url=self.base_url,
        )

    def complete(
        self,
        messages: list[ChatMessage],
        system: str | None = None,
        max_tokens: int = 4096,
    ) -> ModelResponse:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend({"role": m.role, "content": m.content} for m in messages)

        resp = self.client.chat.completions.create(
            model=self.model,
            messages=msgs,
            **{self.token_param: max_tokens},
        )
        usage = resp.usage
        return ModelResponse(
            text=resp.choices[0].message.content or "",
            model=resp.model,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            raw=resp,
        )

    def complete_with_tools(
        self,
        history: ToolHistory,
        tools: list[ToolSpec],
        system: str | None = None,
        max_tokens: int = 4096,
    ) -> ToolTurn:
        msgs: list[dict] = [{"role": "system", "content": system}] if system else []
        for item in history:
            if isinstance(item, ChatMessage):
                msgs.append({"role": item.role, "content": item.content})
            elif isinstance(item, ToolTurn):
                msgs.append(item.native)
            else:  # list[ToolResult]
                msgs.extend(
                    {"role": "tool", "tool_call_id": r.call_id, "content": r.content} for r in item
                )

        resp = self.client.chat.completions.create(
            model=self.model,
            messages=msgs,
            tools=[
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools
            ],
            **{self.token_param: max_tokens},
        )
        message = resp.choices[0].message
        raw_calls = message.tool_calls or []
        native: dict = {"role": "assistant", "content": message.content or ""}
        if raw_calls:
            native["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in raw_calls
            ]
        usage = resp.usage
        return ToolTurn(
            text=message.content or "",
            model=resp.model,
            tool_calls=[ToolCall(id=c.id, name=c.function.name, arguments=_parse_args(c.function.arguments)) for c in raw_calls],
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            native=native,
        )


def _parse_args(raw: str | None) -> dict:
    """Tool arguments arrive as a JSON string; malformed JSON becomes an
    arguments dict the sandbox rejects (reported back to the model as an
    error result), rather than crashing the run."""
    try:
        args = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"_invalid_json": raw}
    return args if isinstance(args, dict) else {"_invalid_json": raw}
