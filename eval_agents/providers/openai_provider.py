"""OpenAI adapter — official `openai` SDK (Chat Completions).

Also serves as the base class for any OpenAI-compatible endpoint
(Z.ai GLM, local vLLM/Ollama gateways, etc.) — subclasses only change
the base URL, API-key env var, and token-cap parameter name.
"""
from __future__ import annotations

import os

from .base import ChatMessage, ModelResponse, Provider


class OpenAIProvider(Provider):
    api_key_env = "OPENAI_API_KEY"
    base_url: str | None = None
    # gpt-5-family models reject `max_tokens`; compatible endpoints (GLM)
    # often only accept `max_tokens`. Subclasses override as needed.
    token_param = "max_completion_tokens"

    def __init__(self, model: str = "gpt-5"):
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
