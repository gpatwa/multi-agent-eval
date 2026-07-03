"""Z.ai (GLM) adapter.

Z.ai exposes an OpenAI-compatible endpoint, so this is the thinnest
possible adapter: reuse the OpenAI provider with a different base URL,
API-key env var, and token parameter. This is the pattern to copy for
any other OpenAI-compatible vendor.
"""
from __future__ import annotations

from .openai_provider import OpenAIProvider


class ZaiProvider(OpenAIProvider):
    api_key_env = "ZAI_API_KEY"
    base_url = "https://api.z.ai/api/paas/v4"
    token_param = "max_tokens"

    def __init__(self, model: str = "glm-4.6"):
        super().__init__(model)
