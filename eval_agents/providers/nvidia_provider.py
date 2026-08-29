"""NVIDIA (Nemotron) adapter — NVIDIA's own hosted API, OpenAI-compatible.

NVIDIA both trains its own open-weight model family (Nemotron) and hosts it
directly (build.nvidia.com / NIM), alongside many other vendors' open models
served the same way — so this is a genuine first-party provider, not just
another aggregator entry.

Key: NVIDIA_API_KEY (starts with "nvapi-") from build.nvidia.com — free tier
available, no credit card required to start.
"""
from __future__ import annotations

from .openai_provider import OpenAIProvider


class NvidiaProvider(OpenAIProvider):
    api_key_env = "NVIDIA_API_KEY"
    base_url = "https://integrate.api.nvidia.com/v1"
    token_param = "max_tokens"

    def __init__(self, model: str = "nvidia/nemotron-3-ultra-550b-a55b"):
        super().__init__(model)
