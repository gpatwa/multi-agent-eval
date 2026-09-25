"""Use cases — domain-specific task schema + scoring for a real customer
problem. Each module supplies a worker system prompt and a Scorer.

Registered use cases are keyed by the `use_case` field in a config file.
"""
from __future__ import annotations

from . import automationbench, triage, triage_tools

# use_case key -> (worker_system_prompt, scorer)
REGISTRY = {
    "support_triage": (triage.TRIAGE_SYSTEM, triage.triage_scorer),
    "support_triage_tools": (triage_tools.TOOLS_SYSTEM, triage_tools.tools_scorer),
    "automationbench": (automationbench.AB_SYSTEM, automationbench.automationbench_scorer),
}

# Use cases whose candidates work the task through a tool loop rather than a
# single completion: use_case key -> executor(agent, task) -> ModelResponse.
# Candidates on providers without tool support are skipped for these.
EXECUTORS = {
    "support_triage_tools": triage_tools.run_candidate,
    "automationbench": automationbench.run_candidate,
}
