"""Open-weight model providers — Kimi, Qwen, DeepSeek.

All three serve OpenAI-compatible endpoints, so each adapter is just a base
URL, an API-key env var, and a default model. This is the payoff of the
Provider seam: adding a vendor costs ~8 lines, not an integration.

Why benchmark these at all: the interesting question for a production use
case is rarely "which flagship wins" but "can a model at 1/10th the price
clear my quality bar and guardrails?" These sit 5-30x below the flagships
per token, so a passing score here changes the economics of the whole
deployment.

Keys (one per vendor you want to evaluate):
    MOONSHOT_API_KEY   platform.moonshot.ai   (Kimi)
    DASHSCOPE_API_KEY  Alibaba Cloud Model Studio  (Qwen)
    DEEPSEEK_API_KEY   platform.deepseek.com  (DeepSeek)

Alternatively, reach all of them through one OpenRouter key using
`provider: openrouter` with a namespaced model id (e.g.
`moonshotai/kimi-k3`, `qwen/qwen3.8-max`, `deepseek/deepseek-v4-pro`).
Open weights also mean you can self-host and point OPENROUTER_BASE_URL at
a local vLLM/Ollama server instead.
"""
from __future__ import annotations

from .openai_provider import OpenAIProvider


class MoonshotProvider(OpenAIProvider):
    """Kimi — Moonshot AI."""

    api_key_env = "MOONSHOT_API_KEY"
    base_url = "https://api.moonshot.ai/v1"
    token_param = "max_tokens"

    def __init__(self, model: str = "kimi-k3"):
        super().__init__(model)


class QwenProvider(OpenAIProvider):
    """Qwen — Alibaba Cloud Model Studio (formerly DashScope).

    Use the region endpoint closest to you; `-intl` is the international one.
    """

    api_key_env = "DASHSCOPE_API_KEY"
    base_url = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    token_param = "max_tokens"

    def __init__(self, model: str = "qwen3.8-max"):
        super().__init__(model)


class DeepSeekProvider(OpenAIProvider):
    """DeepSeek."""

    api_key_env = "DEEPSEEK_API_KEY"
    base_url = "https://api.deepseek.com"
    token_param = "max_tokens"

    def __init__(self, model: str = "deepseek-v4-pro"):
        super().__init__(model)
