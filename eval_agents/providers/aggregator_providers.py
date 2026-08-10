"""Aggregators — one account, most models.

The problem this solves: benchmarking across vendors normally means an
account, a key, and a billing relationship per vendor. An aggregator hosts
many vendors' models behind a single OpenAI-compatible endpoint, so **one
key reaches most of the field**. Meta is the clearest case — Meta no longer
runs a first-party Llama API, so an aggregator (or self-hosting) is the
only way to call Llama at all.

Every aggregator below speaks the same protocol, so they are all the same
adapter with a different base URL and key env var:

    provider        key env var           notes
    --------        -----------           -----
    openrouter      OPENROUTER_API_KEY    widest catalog (incl. closed models)
    together        TOGETHER_API_KEY      open-weight focus, good throughput
    groq            GROQ_API_KEY          fastest inference, generous free tier
    fireworks       FIREWORKS_API_KEY     open-weight focus, tuning support
    deepinfra       DEEPINFRA_API_KEY     low prices on open weights

Model ids are namespaced per aggregator — check its model list. Examples:
    openrouter   meta-llama/llama-4-maverick, moonshotai/kimi-k3, qwen/qwen3.8-max
    groq         llama-4-maverick-17b-128e-instruct
    together     meta-llama/Llama-4-Maverick-17B-128E-Instruct

Trade-offs vs. going direct: you inherit the aggregator's routing, uptime,
and margin, and its per-token price is usually a little above the vendor's
own. In exchange you get one bill, one key, and instant access to new models
without opening another account. For a benchmark harness that's the right
trade; for high-volume production, go direct to the winner you pick.
"""
from __future__ import annotations

import os

from .openai_provider import OpenAIProvider


class _Aggregator(OpenAIProvider):
    """Base: an OpenAI-compatible multi-vendor endpoint.

    `{PREFIX}_BASE_URL` overrides the endpoint (useful for self-hosted
    gateways or regional endpoints).
    """

    token_param = "max_tokens"
    default_base_url = ""

    def __init__(self, model: str):
        env_prefix = self.api_key_env.rsplit("_API_KEY", 1)[0]
        self.base_url = os.environ.get(f"{env_prefix}_BASE_URL", self.default_base_url)
        super().__init__(model)


class OpenRouterProvider(_Aggregator):
    api_key_env = "OPENROUTER_API_KEY"
    default_base_url = "https://openrouter.ai/api/v1"

    def __init__(self, model: str = "meta-llama/llama-4-maverick"):
        super().__init__(model)


class TogetherProvider(_Aggregator):
    api_key_env = "TOGETHER_API_KEY"
    default_base_url = "https://api.together.xyz/v1"

    def __init__(self, model: str = "meta-llama/Llama-4-Maverick-17B-128E-Instruct"):
        super().__init__(model)


class GroqProvider(_Aggregator):
    api_key_env = "GROQ_API_KEY"
    default_base_url = "https://api.groq.com/openai/v1"

    def __init__(self, model: str = "llama-4-maverick-17b-128e-instruct"):
        super().__init__(model)


class FireworksProvider(_Aggregator):
    api_key_env = "FIREWORKS_API_KEY"
    default_base_url = "https://api.fireworks.ai/inference/v1"

    def __init__(self, model: str = "accounts/fireworks/models/llama4-maverick-instruct-basic"):
        super().__init__(model)


class DeepInfraProvider(_Aggregator):
    api_key_env = "DEEPINFRA_API_KEY"
    default_base_url = "https://api.deepinfra.com/v1/openai"

    def __init__(self, model: str = "meta-llama/Llama-4-Maverick-17B-128E-Instruct"):
        super().__init__(model)
