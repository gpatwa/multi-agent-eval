"""Use cases — domain-specific task schema + scoring for a real customer
problem. Each module supplies a worker system prompt and a Scorer.

Registered use cases are keyed by the `use_case` field in a config file.
"""
from __future__ import annotations

from . import triage

# use_case key -> (worker_system_prompt, scorer)
REGISTRY = {
    "support_triage": (triage.TRIAGE_SYSTEM, triage.triage_scorer),
}
