"""Provider registry — the factory that turns config into adapter instances.

Adding a new provider platform = write one adapter file + register it here.
Providers are imported lazily so you only need the SDKs for the providers
you actually configure.
"""
from __future__ import annotations

import os

from .providers.base import MissingCredentials, Provider

__all__ = ["MissingCredentials", "create_provider"]

# provider key -> (module path, class name, required env var or None)
_PROVIDERS: dict[str, tuple[str, str, str | None]] = {
    # API-key providers (pay per token)
    "anthropic": ("eval_agents.providers.anthropic_provider", "AnthropicProvider", "ANTHROPIC_API_KEY"),
    "openai": ("eval_agents.providers.openai_provider", "OpenAIProvider", "OPENAI_API_KEY"),
    "gemini": ("eval_agents.providers.gemini_provider", "GeminiProvider", "GEMINI_API_KEY"),
    "zai": ("eval_agents.providers.zai_provider", "ZaiProvider", "ZAI_API_KEY"),
    "openrouter": ("eval_agents.providers.openrouter_provider", "OpenRouterProvider", "OPENROUTER_API_KEY"),
    # Subscription providers (vendor CLIs, no API key — see cli_providers.py)
    "claude-code": ("eval_agents.providers.cli_providers", "ClaudeCodeProvider", None),
    "codex-cli": ("eval_agents.providers.cli_providers", "CodexProvider", None),
    "gemini-cli": ("eval_agents.providers.cli_providers", "GeminiCliProvider", None),
    # Offline testing
    "mock": ("eval_agents.providers.mock_provider", "MockProvider", None),
}


def create_provider(provider: str, model: str) -> Provider:
    try:
        module_path, class_name, env_var = _PROVIDERS[provider]
    except KeyError:
        raise ValueError(
            f"Unknown provider {provider!r}. Available: {sorted(_PROVIDERS)}"
        ) from None

    # Anthropic can also authenticate via an `ant auth login` profile, so a
    # missing env var is only fatal for the other providers.
    if env_var and not os.environ.get(env_var) and provider != "anthropic":
        raise MissingCredentials(f"{provider}: set {env_var} to use this provider")

    import importlib

    cls = getattr(importlib.import_module(module_path), class_name)
    return cls(model=model)
