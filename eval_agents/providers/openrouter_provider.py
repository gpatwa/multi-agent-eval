"""OpenRouter adapter — access to open-weight models (Nous Hermes, Llama,
Qwen, ...) through one OpenAI-compatible endpoint.

Main use here: a NEUTRAL judge. Nous Research's Hermes models aren't made by
any of the four candidate vendors (Anthropic/OpenAI/Google/Z.ai), so judging
with Hermes removes same-vendor grading bias entirely.

To run Hermes locally instead (Ollama, vLLM), set OPENROUTER_BASE_URL to your
server (e.g. http://localhost:11434/v1) and OPENROUTER_API_KEY to any
non-empty string, then use the local model tag as the model name.
"""
from __future__ import annotations

import os

from .openai_provider import OpenAIProvider


class OpenRouterProvider(OpenAIProvider):
    api_key_env = "OPENROUTER_API_KEY"
    token_param = "max_tokens"

    def __init__(self, model: str = "nousresearch/hermes-4-405b"):
        self.base_url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        super().__init__(model)
