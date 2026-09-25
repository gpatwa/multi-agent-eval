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


# ---------------------------------------------------------------- tool use
# A provider-neutral tool-calling transcript. History items are, in order:
#   ChatMessage          — the user's opening message
#   ToolTurn             — an assistant turn (echoed back via its `native` form)
#   list[ToolResult]     — results for every call in the preceding ToolTurn
# Each adapter converts this to its vendor's wire format. `native` is the
# adapter's own assistant message, echoed back unchanged: vendors require it
# (Anthropic thinking blocks, Gemini thought signatures, OpenAI tool_calls).


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON Schema for the arguments object


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass
class ToolTurn:
    text: str
    model: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    native: object = field(default=None, repr=False)


ToolHistory = list  # list[ChatMessage | ToolTurn | list[ToolResult]]


class Provider(ABC):
    """One instance = one (provider, model) pair."""

    # Adapters that implement complete_with_tools() set this True. Use cases
    # that need tools skip candidates without it (e.g. the vendor CLIs,
    # which run their own agent loop and can't call our sandbox tools).
    supports_tools: bool = False

    def __init__(self, model: str):
        self.model = model

    def complete_with_tools(
        self,
        history: ToolHistory,
        tools: list[ToolSpec],
        system: str | None = None,
        max_tokens: int = 4096,
    ) -> ToolTurn:
        """Run one assistant turn that may request tool calls."""
        raise NotImplementedError(f"{type(self).__name__} does not support tool use")

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
