"""Mock adapter — lets the whole pipeline run offline, with no API keys.

Useful for demos, CI, and testing the orchestration logic in isolation.
Models whose name contains "judge" return a valid scoring JSON; everything
else returns a short canned answer derived from the prompt.
"""
from __future__ import annotations

import hashlib
import json
import time

from .base import ChatMessage, ModelResponse, Provider


class MockProvider(Provider):
    def complete(
        self,
        messages: list[ChatMessage],
        system: str | None = None,
        max_tokens: int = 4096,
    ) -> ModelResponse:
        prompt = messages[-1].content if messages else ""
        seed = int(hashlib.sha256((self.model + prompt).encode()).hexdigest(), 16)
        time.sleep(0.05)  # simulate a little latency

        if "judge" in self.model:
            rng = seed
            scores = {}
            for dim in ("accuracy", "completeness", "clarity", "instruction_following"):
                rng, score = divmod(rng, 3)
                scores[dim] = 3 + score  # 3-5
            text = json.dumps(
                {
                    "scores": scores,
                    "overall": round(sum(scores.values()) / len(scores), 2),
                    "rationale": f"Mock evaluation by {self.model}.",
                }
            )
        else:
            text = (
                f"[{self.model}] Mock answer to: "
                f"{prompt.splitlines()[0][:80]} ... "
                "(replace with a real provider in config.yaml)"
            )

        return ModelResponse(
            text=text,
            model=self.model,
            input_tokens=len(prompt) // 4,
            output_tokens=len(text) // 4,
        )
