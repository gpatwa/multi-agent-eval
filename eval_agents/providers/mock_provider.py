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
            # Match the rubric the prompt asks for (triage reply rubric vs generic).
            if "policy_adherence" in prompt:
                dims = ("policy_adherence", "resolution", "tone")
            else:
                dims = ("accuracy", "completeness", "clarity", "instruction_following")
            rng = seed
            scores = {}
            for dim in dims:
                rng, score = divmod(rng, 3)
                scores[dim] = 3 + score  # 3-5
            payload = {
                "scores": scores,
                "overall": round(sum(scores.values()) / len(scores), 2),
                "rationale": f"Mock evaluation by {self.model}.",
            }
            if "policy_adherence" in prompt:
                payload["critical_violation"] = seed % 9 == 0  # occasional flag for demo
            text = json.dumps(payload)
        elif system and '"category"' in system:
            # Triage-shaped worker output: pick pseudorandom labels so the
            # deterministic routing/priority grading exercises both outcomes.
            categories = ["billing", "technical", "account_access", "feature_request", "cancellation"]
            priorities = ["urgent", "high", "normal", "low"]
            text = json.dumps(
                {
                    "category": categories[seed % len(categories)],
                    "priority": priorities[(seed // 7) % len(priorities)],
                    "reply": f"[{self.model}] Thanks for reaching out — here's what we'll do next.",
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
