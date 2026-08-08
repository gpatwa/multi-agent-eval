"""Local / self-hosted models — Ollama, LM Studio, vLLM, llama.cpp.

No account, no API key, no per-token cost: the model runs on your machine.
All of these expose an OpenAI-compatible endpoint, so this is the same
~10-line adapter as every other vendor, with the key requirement dropped.

Defaults to Ollama (`ollama serve`, port 11434). Point elsewhere with
LOCAL_BASE_URL:

    LM Studio   http://localhost:1234/v1
    vLLM        http://localhost:8000/v1
    llama.cpp   http://localhost:8080/v1

Honest caveat for benchmarking: a model you can run on a laptop (7-14B) is
not the same class as the hosted open-weight flagships (Kimi K3, Qwen3.8-Max
are trillion-parameter). Expect lower scores. The question a local run
answers is a real one though — "is a free, private, on-device model good
enough for this job?" — and at $0/ticket the bar it has to clear is lower.
"""
from __future__ import annotations

import os

from .base import MissingCredentials
from .openai_provider import OpenAIProvider


class LocalProvider(OpenAIProvider):
    token_param = "max_tokens"

    def __init__(self, model: str = "qwen3"):
        self.base_url = os.environ.get("LOCAL_BASE_URL", "http://localhost:11434/v1")
        # Local servers ignore auth, but the OpenAI client requires something.
        os.environ.setdefault("LOCAL_API_KEY", "local")
        self.api_key_env = "LOCAL_API_KEY"
        try:
            super().__init__(model)
        except Exception as exc:  # server not running / unreachable
            raise MissingCredentials(
                f"local model server unreachable at {self.base_url} "
                f"({exc}) — start it (e.g. `ollama serve`) or set LOCAL_BASE_URL"
            ) from None
