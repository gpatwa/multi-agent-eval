"""Every registered provider must: (a) construct given its env var set, and
(b) raise MissingCredentials cleanly without it — with no network calls in
either case. This is the automated version of the one-off "is provider X
ready" scripts written by hand throughout the project's early history."""
from __future__ import annotations

import os

import pytest

from eval_agents.providers.base import MissingCredentials
from eval_agents.registry import _PROVIDERS, create_provider

# CLI-subscription and local providers do real filesystem/network probes at
# construction time (shutil.which, a live server check) — not appropriate
# for a fast unit test. Covered by their own docstrings + manual smoke use.
_SKIP_CONSTRUCT = {"claude-code", "codex-cli", "gemini-cli", "local", "mock"}

_SAMPLE_MODEL = {
    "anthropic": "claude-opus-5",
    "openai": "gpt-5.6-sol",
    "gemini": "gemini-3.1-pro-preview",
    "zai": "glm-5.3",
    "xai": "grok-4.6",
    "meta": "muse-spark-1.2",
    "nvidia": "nvidia/nemotron-3-ultra-550b-a55b",
    "openrouter": "meta-llama/llama-4-maverick",
    "together": "meta-llama/Llama-4-Maverick-17B-128E-Instruct",
    "groq": "llama-4-maverick-17b-128e-instruct",
    "fireworks": "accounts/fireworks/models/llama4-maverick-instruct-basic",
    "deepinfra": "meta-llama/Llama-4-Maverick-17B-128E-Instruct",
    "moonshot": "kimi-k3",
    "qwen": "qwen3.8-max",
    "deepseek": "deepseek-v4-pro",
}


@pytest.mark.parametrize("provider", sorted(_PROVIDERS))
def test_skips_cleanly_without_credentials(provider, monkeypatch):
    _, _, env_var = _PROVIDERS[provider]
    if not env_var or provider == "anthropic":
        pytest.skip(f"{provider} has no required env var (or has a CLI-profile fallback)")
    monkeypatch.delenv(env_var, raising=False)
    with pytest.raises(MissingCredentials):
        create_provider(provider, _SAMPLE_MODEL.get(provider, "x"))


@pytest.mark.parametrize("provider", sorted(set(_PROVIDERS) - _SKIP_CONSTRUCT))
def test_constructs_with_credentials(provider, monkeypatch):
    _, _, env_var = _PROVIDERS[provider]
    if env_var:
        monkeypatch.setenv(env_var, "test-key")
    model = _SAMPLE_MODEL.get(provider)
    assert model, f"add a sample model for {provider!r} to _SAMPLE_MODEL"
    p = create_provider(provider, model)
    assert p.model == model


def test_unknown_provider_raises_value_error():
    with pytest.raises(ValueError, match="Unknown provider"):
        create_provider("not-a-real-provider", "some-model")
