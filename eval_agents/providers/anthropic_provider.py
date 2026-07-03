"""Anthropic (Claude) adapter — official `anthropic` SDK."""
from __future__ import annotations

from .base import ChatMessage, ModelResponse, Provider


class AnthropicProvider(Provider):
    def __init__(self, model: str = "claude-opus-4-8"):
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
