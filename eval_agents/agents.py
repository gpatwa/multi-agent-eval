"""Agents — a role (system prompt + behavior) bound to a swappable provider.

The agent doesn't know or care which vendor is behind it; that binding
happens in config. This is what "multi-agent across providers" means here:
each role in the pipeline can run on a different platform.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .providers.base import ChatMessage, ModelResponse, Provider

WORKER_SYSTEM = (
    "You are a careful assistant completing an evaluation task. "
    "Follow the task instructions exactly. Be accurate and complete, "
    "but do not pad your answer with unrequested material."
)


@dataclass
class Agent:
    name: str  # e.g. "claude", "gpt", "gemini", "glm"
    provider: Provider
    system: str = WORKER_SYSTEM

    def run(self, prompt: str, max_tokens: int = 4096) -> ModelResponse:
        start = time.perf_counter()
        resp = self.provider.complete(
            [ChatMessage(role="user", content=prompt)],
            system=self.system,
            max_tokens=max_tokens,
        )
        resp.latency_s = time.perf_counter() - start
        return resp
