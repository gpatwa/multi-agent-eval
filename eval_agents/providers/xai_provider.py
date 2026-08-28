"""xAI (Grok) adapter — OpenAI-compatible endpoint.

Not to be confused with Groq (aggregator_providers.py) — different company,
same-sounding name, easy to mix up. xAI is the model vendor (Grok); Groq is
fast-inference hosting for other vendors' open-weight models.

Key: XAI_API_KEY from console.x.ai.
"""
from __future__ import annotations

from .openai_provider import OpenAIProvider


class XAIProvider(OpenAIProvider):
    api_key_env = "XAI_API_KEY"
    base_url = "https://api.x.ai/v1"
    token_param = "max_tokens"

    def __init__(self, model: str = "grok-4.6"):
        super().__init__(model)
