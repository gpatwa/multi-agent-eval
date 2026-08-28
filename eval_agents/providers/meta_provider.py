"""Meta (Muse Spark) adapter — Meta's own paid API, OpenAI-compatible.

Distinct from Llama: Meta wound down the original Llama API, but as of
July 2026 re-entered the paid-API business with the "Meta Model API" and a
new model family, Muse Spark, from Meta Superintelligence Labs. Llama itself
remains open-weight-only (aggregators / self-host — see
aggregator_providers.py and local_provider.py); this adapter is for Muse
Spark specifically.

Key: MODEL_API_KEY from developer.meta.com (Meta's own env var name, not
META_API_KEY — kept as-is here so it matches Meta's docs and any other
tooling you copy-paste from them).
"""
from __future__ import annotations

from .openai_provider import OpenAIProvider


class MetaProvider(OpenAIProvider):
    api_key_env = "MODEL_API_KEY"
    base_url = "https://api.meta.ai/v1"
    token_param = "max_tokens"

    def __init__(self, model: str = "muse-spark-1.2"):
        super().__init__(model)
