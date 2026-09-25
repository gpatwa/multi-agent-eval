"""Anthropic (Claude) adapter — official `anthropic` SDK."""
from __future__ import annotations

from .base import ChatMessage, ModelResponse, Provider, ToolCall, ToolHistory, ToolSpec, ToolTurn


class AnthropicProvider(Provider):
    supports_tools = True

    def __init__(self, model: str = "claude-opus-5"):
        super().__init__(model)
        import anthropic

        # Zero-arg client resolves ANTHROPIC_API_KEY or an `ant auth login` profile.
        self.client = anthropic.Anthropic()

    def complete(
        self,
        messages: list[ChatMessage],
        system: str | None = None,
        max_tokens: int = 4096,
    ) -> ModelResponse:
        kwargs: dict = {}
        if system:
            kwargs["system"] = system

        resp = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            # Adaptive thinking requires Claude 4.6+ models (opus-4-6 and later,
            # sonnet-4-6/5). Remove this line if you configure an older model.
            thinking={"type": "adaptive"},
            messages=[{"role": m.role, "content": m.content} for m in messages],
            **kwargs,
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        return ModelResponse(
            text=text,
            model=resp.model,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            raw=resp,
        )

    def complete_with_tools(
        self,
        history: ToolHistory,
        tools: list[ToolSpec],
        system: str | None = None,
        max_tokens: int = 4096,
    ) -> ToolTurn:
        messages: list[dict] = []
        for item in history:
            if isinstance(item, ChatMessage):
                messages.append({"role": item.role, "content": item.content})
            elif isinstance(item, ToolTurn):
                # Echo the full content list unchanged — with adaptive thinking
                # the thinking blocks must be passed back alongside tool_use.
                messages.append({"role": "assistant", "content": item.native})
            else:
                # All results for one assistant turn go in a single user message.
                messages.append({
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": r.call_id, "content": r.content, "is_error": r.is_error}
                        for r in item
                    ],
                })

        kwargs: dict = {"system": system} if system else {}
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            thinking={"type": "adaptive"},
            tools=[{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools],
            messages=messages,
            **kwargs,
        )
        # A "refusal" stop (or max_tokens) simply yields no tool calls, which
        # ends the loop; the scorer then grades whatever state was reached.
        return ToolTurn(
            text="".join(b.text for b in resp.content if b.type == "text"),
            model=resp.model,
            tool_calls=[
                ToolCall(id=b.id, name=b.name, arguments=b.input if isinstance(b.input, dict) else {})
                for b in resp.content if b.type == "tool_use"
            ],
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            native=resp.content,
        )
