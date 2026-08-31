"""Shared test fixtures — a fake Provider so judge-dependent tests never
touch the network."""
from __future__ import annotations

import pytest

from eval_agents.agents import Agent
from eval_agents.providers.base import ChatMessage, ModelResponse, Provider


class FakeProvider(Provider):
    """A Provider stub that returns a scripted response (or raises a
    scripted exception) instead of calling a real vendor."""

    def __init__(self, model: str = "fake", response_text: str = "{}", raises: Exception | None = None):
        super().__init__(model)
        self.response_text = response_text
        self.raises = raises
        self.calls: list[list[ChatMessage]] = []

    def complete(self, messages, system=None, max_tokens=4096):
        self.calls.append(messages)
        if self.raises:
            raise self.raises
        return ModelResponse(text=self.response_text, model=self.model)


@pytest.fixture
def fake_judge():
    """Factory: fake_judge(response_text=...) or fake_judge(raises=...)."""

    def _make(**kwargs):
        return Agent(name="judge", provider=FakeProvider(**kwargs))

    return _make
