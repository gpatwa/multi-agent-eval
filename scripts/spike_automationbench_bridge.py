"""SPIKE (2026-09-25): run AutomationBench support tasks through this repo's
provider tool loop, scored by AutomationBench's own partial_credit.

Proof of the integration path, not production code, and not imported by the
package or the tests. AutomationBench (MIT, github.com/zapier/AutomationBench)
requires Python >= 3.13 and pulls in the verifiers and datasets packages, so
run it from a separate venv:

    uv venv -p python3.13 .abvenv
    git clone --depth 1 https://github.com/zapier/AutomationBench.git ../AutomationBench
    VIRTUAL_ENV=.abvenv uv pip install -e ../AutomationBench -r requirements.txt
    .abvenv/bin/python scripts/spike_automationbench_bridge.py 2   # N smallest support tasks, Gemini

Result on 2026-09-25 (gemini-3.1-pro-preview): helpcrunch_zoho_desk_bridge 17/17
(pass), zoho_desk_ticket_categorization 12/13 (partial 0.923), ~195k input tokens.
"""
import copy, json, pathlib, sys, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from automationbench.runner import AutomationBenchEnv, strip_none_values, compute_allowed_services
from automationbench.rubric import create_rubric, partial_credit
from automationbench.domains.support.tasks import get_support_dataset
from automationbench.schema.world import WorldState
from automationbench.tools import ALL_TOOLS

from eval_agents.providers.base import ChatMessage, ToolResult, ToolSpec
from eval_agents.registry import create_provider

ds = get_support_dataset()
env = AutomationBenchEnv(dataset=ds, rubric=create_rubric(), toolset="limited_zapier")
DEFS = {t.name: t for t in env._all_tool_defs}
FNS = {f.__name__: f for f in ALL_TOOLS}


class ABSandbox:
    def __init__(self, info):
        self.info = copy.deepcopy(info)
        init = strip_none_values(self.info["initial_state"])
        self.info["assertions"] = [strip_none_values(a) for a in self.info["assertions"]]
        self.world = WorldState(**init)
        self.world.meta.allowed_services = compute_allowed_services(init, self.info["assertions"], self.info["zapier_tools"])
        self.initial = copy.deepcopy(init)
        self.specs = [ToolSpec(n, DEFS[n].description, DEFS[n].parameters) for n in self.info["zapier_tools"]]
        self.calls = 0; self.errors = 0

    def execute(self, call):
        self.calls += 1
        args = {k: v for k, v in call.arguments.items() if not (isinstance(v, dict) and not v)}
        try:
            out = FNS[call.name](world=self.world, **args)
            return ToolResult(call.id, call.name, out if isinstance(out, str) else json.dumps(out, default=str))
        except Exception as exc:
            self.errors += 1
            return ToolResult(call.id, call.name, f"error: {type(exc).__name__}: {exc}", is_error=True)

    def score(self):
        state = {"info": self.info, "world": self.world, "initial_state": self.initial}
        pc = partial_credit(state)
        res = state.get("_assertion_results", [])
        return pc, sum(1 for r in res if not r["excluded"] and r["passed"]), sum(1 for r in res if not r["excluded"])


def run(provider, row, max_steps=50):
    info = json.loads(row["info"])
    box = ABSandbox(info)
    system, user = row["prompt"][0]["content"], row["prompt"][-1]["content"]
    history = [ChatMessage("user", user)]
    tin = tout = 0; t0 = time.perf_counter()
    for step in range(max_steps):
        turn = provider.complete_with_tools(history, box.specs, system=system, max_tokens=8192)
        tin += turn.input_tokens; tout += turn.output_tokens
        history.append(turn)
        if not turn.tool_calls:
            break
        history.append([box.execute(c) for c in turn.tool_calls])
    pc, passed, total = box.score()
    return dict(task=info["task_name"], steps=step + 1, tool_calls=box.calls, tool_errors=box.errors,
                partial_credit=round(pc, 3), passed=passed, scored=total, completed=pc == 1.0,
                tokens=(tin, tout), latency=round(time.perf_counter() - t0, 1))


if __name__ == "__main__":
    rows = sorted(ds, key=lambda r: len(json.loads(r["info"])["assertions"]))[: int(sys.argv[1])]
    provider = create_provider("gemini", "gemini-3.1-pro-preview")
    for row in rows:
        print(json.dumps(run(provider, row)), flush=True)
