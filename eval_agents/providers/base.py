"""Provider abstraction — the seam that makes models swappable.

Every provider adapter implements `Provider.complete()` against a tiny
normalized message format. The rest of the application never imports a
vendor SDK, so switching a model (or an entire provider) is a config edit.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class MissingCredentials(Exception):
    """Raised when a provider can't run (no API key, CLI not installed).

    Candidates that raise this at construction time are skipped, not fatal.
    """


@dataclass
class ChatMessage:
    role: str  # "user" | "assistant"
    content: str


@dataclass
class ModelResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0
    raw: object = field(default=None, repr=False)


class Provider(ABC):
    """One instance = one (provider, model) pair."""

    def __init__(self, model: str):
        self.model = model

    @abstractmethod
    def complete(
        self,
        messages: list[ChatMessage],
        system: str | None = None,
        max_tokens: int = 4096,
    ) -> ModelResponse:
        """Run a chat completion and return a normalized response."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self.model!r})"
