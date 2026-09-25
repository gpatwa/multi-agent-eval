"""Mock adapter — lets the whole pipeline run offline, with no API keys.

Useful for demos, CI, and testing the orchestration logic in isolation.
Models whose name contains "judge" return a valid scoring JSON; everything
else returns a short canned answer derived from the prompt.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from .base import ChatMessage, ModelResponse, Provider, ToolCall, ToolTurn


class MockProvider(Provider):
    supports_tools = True

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
                payload["contradicts_actions"] = seed % 13 == 0
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
                    "actions": {
                        "refund": ["none", "full", "prorated", "duplicate_charge"][(seed // 11) % 4],
                        "escalate": ["none", "security", "engineering"][(seed // 13) % 3],
                        "offer_pause": bool((seed // 17) % 2),
                    },
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

    def complete_with_tools(self, history, tools, system=None, max_tokens=4096) -> ToolTurn:
        """Scripted agent for the tool-use tier: look the customer up, set a
        pseudorandom triage, maybe take one action, reply, stop. Seeded by
        model + ticket, so runs are reproducible and candidates differ."""
        prompt = history[0].content
        seed = int(hashlib.sha256((self.model + prompt).encode()).hexdigest(), 16)
        step = sum(isinstance(h, ToolTurn) for h in history)
        time.sleep(0.02)

        def turn(*calls, text=""):
            tool_calls = [ToolCall(id=f"call-{step}-{i}", name=n, arguments=a) for i, (n, a) in enumerate(calls)]
            return ToolTurn(text=text, model=self.model, tool_calls=tool_calls,
                            input_tokens=len(prompt) // 4 + 50 * step, output_tokens=30, native=None)

        if step == 0:
            m = re.search(r"From:\s*(\S+@\S+)", prompt)
            return turn(("lookup_account", {"email": m.group(1) if m else "unknown@example.com"}))
        if step == 1:
            categories = ["billing", "technical", "account_access", "feature_request", "cancellation"]
            priorities = ["urgent", "high", "normal", "low"]
            calls = [("set_triage", {"category": categories[seed % 5], "priority": priorities[(seed // 7) % 4]})]
            charges = []
            last = history[-1]
            if isinstance(last, list) and last and not last[0].is_error:
                charges = json.loads(last[0].content).get("charges", [])
            action = (seed // 11) % 4
            if action == 1 and charges:
                calls.append(("issue_refund", {"charge_id": charges[0]["id"],
                                               "type": ["full", "prorated", "duplicate_charge"][(seed // 13) % 3]}))
            elif action == 2:
                calls.append(("escalate", {"team": ["security", "engineering"][(seed // 13) % 2]}))
            elif action == 3:
                calls.append(("offer_pause", {}))
            return turn(*calls)
        if step == 2:
            return turn(("send_reply", {"message": f"[{self.model}] Thanks for reaching out — we've taken care of it."}))
        return turn(text="Done.")
