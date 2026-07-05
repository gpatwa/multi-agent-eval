# Multi-Agent, Multi-Provider Model Evaluation

A multi-agent application that evaluates LLMs from different provider
platforms — **Anthropic (Claude), OpenAI (GPT), Google (Gemini), and Z.ai
(GLM)** — on the same task suite, using an **LLM-as-judge** pipeline, and
produces a comparison report.

The concrete use case it proves: *"Which model should we use for our
workload?"* — answered with data (quality scores, latency, token usage)
instead of vibes.

## Architecture

```
                       ┌──────────────────────────────────────────┐
 tasks.yaml ──────────▶│               Orchestrator               │
                       │  (runner.py: fan-out, judge, aggregate)  │
                       └───────┬──────────────────────────┬───────┘
                               │ same prompt, in parallel │
              ┌────────────┬───┴────────┬────────────┐    │
              ▼            ▼            ▼            ▼    ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────┐ ┌───────────┐
        │  Agent   │ │  Agent   │ │  Agent   │ │Agent │ │Judge Agent│
        │ "claude" │ │  "gpt"   │ │ "gemini" │ │"glm" │ │ (any      │
        └────┬─────┘ └────┬─────┘ └────┬─────┘ └──┬───┘ │ provider) │
             ▼            ▼            ▼          ▼     └─────┬─────┘
      ╔════════════════════════════════════════════════╗     │
      ║        Provider interface (providers/base.py)  ║◀────┘
      ║   complete(messages, system) -> ModelResponse  ║
      ╚═══╤══════════╤═══════════╤════════════╤════════╝
          ▼          ▼           ▼            ▼
      anthropic    openai    google-genai   openai SDK
        SDK         SDK         SDK        + Z.ai base_url
```

### Design decisions (the "flexibility to switch models" part)

1. **Adapter pattern at the provider boundary** ([base.py](eval_agents/providers/base.py)).
   One tiny interface — `complete(messages, system) -> ModelResponse` — with a
   normalized message/response shape. Nothing outside `providers/` imports a
   vendor SDK. Each adapter is ~40 lines using the vendor's *official* SDK, so
   you keep native features (Claude adaptive thinking, Gemini system
   instructions) instead of the lowest common denominator a generic proxy
   gives you.

2. **Config-driven model binding** ([config.yaml](config.yaml)). Roles
   (candidate, judge) are bound to `provider + model` in YAML. Switching a
   model is a one-line edit; adding a provider is one adapter file + one
   registry entry ([registry.py](eval_agents/registry.py)). Providers are
   imported lazily, so you only need SDKs for providers you actually use, and
   candidates with missing API keys are skipped rather than failing the run.

3. **OpenAI-compatible endpoints are subclasses, not new integrations.**
   Z.ai GLM speaks the OpenAI wire protocol, so
   [zai_provider.py](eval_agents/providers/zai_provider.py) is ~10 lines: it
   inherits the OpenAI adapter and overrides `base_url`, the key env var, and
   the token-cap parameter name. The same trick covers vLLM, Ollama,
   DeepSeek, Mistral, etc.

4. **Agents are roles, not vendors** ([agents.py](eval_agents/agents.py)).
   An `Agent` = name + system prompt + a `Provider` instance. The judge is
   just another agent, so you can grade with Claude today and Gemini
   tomorrow by editing one YAML block.

5. **A mock provider makes the pipeline testable offline**
   ([mock_provider.py](eval_agents/providers/mock_provider.py)) — run the
   whole system with zero API keys to verify orchestration, parsing, and
   reporting.

**Why not LangChain/LiteLLM?** Those are fine choices when you need their
breadth (hundreds of providers, routing, fallbacks). For learning how to
*build* this, and for production systems where you want full control over
each vendor's native request shape, a ~40-line adapter per provider is less
code than the abstraction it replaces — and this codebase shows exactly
where such a library would slot in (behind `Provider`).

## The evaluation pipeline (concrete use case)

1. **Fan-out** — each task in [tasks.yaml](tasks.yaml) (summarization,
   reasoning, extraction, coding) is sent to every candidate agent
   concurrently.
2. **Judge** — a judge agent scores each answer 1–5 on accuracy,
   completeness, clarity, and instruction-following against reference
   notes, returning strict JSON (prompt-based so it works identically on
   all providers).
3. **Report** — results aggregate into `results/report.md` (leaderboard +
   per-task tables) and `results/results.json`.

**Judge bias caveat:** the judge shares a vendor with one candidate. To
control for it, re-run with judges from different providers and compare
rankings — it's a one-line config change. For a fully **neutral judge**, use
[config.triage.hermes.yaml](config.triage.hermes.yaml): it grades with Nous
Research's **Hermes** via OpenRouter (`OPENROUTER_API_KEY`), a vendor that
isn't any of the four candidates. Where the Claude-judged and Hermes-judged
rankings agree, trust the result; where they disagree, read those
transcripts yourself. Hermes can also be served locally (Ollama/vLLM) by
setting `OPENROUTER_BASE_URL`, or added as a fifth candidate.

## Real customer problem: support ticket triage & reply

The flagship benchmark targets an actual production decision: *which model
should power our support triage?* Each ticket in
[tasks.triage.yaml](tasks.triage.yaml) must be routed to a queue, assigned a
priority, and answered with a reply that follows company policy
([the policy lives in triage.py](eval_agents/usecases/triage.py) and is given
verbatim to both the candidates and the judge, so they can never drift apart).

Scoring is decision-grade, not a single vibe score:

- **routing** & **priority** are graded *deterministically* against gold
  labels — a mis-route is objectively wrong, the judge doesn't get a vote;
- **policy_adherence**, **resolution**, and **tone** of the reply are graded
  by the LLM judge against the policy (promising a refund the policy forbids
  is an automatic 1);
- invalid JSON output scores 1 across the board instead of being excluded —
  breaking the output contract *is* a triage failure;
- the ten synthesized tickets each target one policy decision point (refund
  inside vs. outside the 14-day window, monthly vs. annual proration,
  retention-then-honor cancellation, account-takeover escalation, priority
  boundaries), so the per-task table shows *which rule* a model gets wrong.

The final ranking is a **balanced scorecard** — a weighted blend of quality,
latency, and cost per task (weights and per-model pricing in
[config.triage.yaml](config.triage.yaml)), because the cheapest
acceptable-quality model is often the right production answer.

```bash
# offline demo of the triage benchmark (mock providers, no keys)
python main.py --config config.triage.demo.yaml --out results-triage-demo

# real run across providers
python main.py --config config.triage.yaml --out results-triage
```

To benchmark *your* customer problem: copy `eval_agents/usecases/triage.py`,
swap in your policy/taxonomy/scorer, register it in
`eval_agents/usecases/__init__.py`, and point a config's `use_case` at it.
Configs without a `use_case` fall back to the generic rubric.

## Guardrails & what gets measured

Beyond quality scores, every run measures:

- **Critical violations (launch gate).** The judge flags replies that promise
  a forbidden refund/timeline, fail to escalate security issues, follow
  instructions embedded in the ticket, or leak internal prompts; a regex pass
  flags card/SSN-shaped PII echoed in replies. Violations are counted as hard
  events in the scorecard (⚠ column + breakdown table) — treat any non-zero
  count as disqualifying regardless of composite rank.
- **Adversarial probes.** `tasks.triage.yaml` includes guardrail tickets:
  a prompt-injection "system override" demanding a forbidden refund, a
  prompt-leak attempt disguised as a compliance audit, and a
  legitimate-but-scary GDPR deletion request (over-refusal check).
- **Latency p50 / p95** — support SLAs break on the tail, not the mean.
- **Cost split** — input vs. output cost per task, plus projected monthly
  spend at your ticket volume (`scorecard.monthly_volume`).
- **Variance** — `--trials N` repeats every task; quality is reported as
  mean ± sd. Don't call a winner when the gap is inside the noise.

## Eval rigor: regression gating & judge validation

**Regression gating (evals as CI):** each run writes `summary.json`.
Compare a new run against a known-good baseline and fail (exit 1) when a
candidate's quality drops more than the threshold or violations increase:

```bash
python main.py --config config.triage.yaml --out results-baseline           # pin baseline
python main.py --config config.triage.yaml --out results-new \
       --baseline results-baseline --regression-threshold 0.3               # gate
```

Re-run this whenever the prompt, policy, model version, or provider changes.

**Judge validation:** an LLM judge is itself a model that needs evaluating.
Export a labeling sheet, hand-label 20–30 rows, and measure agreement:

```bash
python scripts/judge_agreement.py export results-triage/results.json labels.csv
# fill in the human_* columns, then:
python scripts/judge_agreement.py score labels.csv
```

Rule of thumb: within-1 agreement ≥ 80% and Pearson r ≥ 0.6 means the judge
is usable; below that, fix the rubric or judge model before trusting
rankings. Note routing/priority never depend on the judge — they're graded
deterministically against gold labels.

## Web UI

A FastAPI server with a browser frontend wraps the same pipeline:

```bash
uvicorn webapp.server:app --port 8321
# open http://localhost:8321
```

Pick a config, hit **Start evaluation**, and watch the leaderboard fill in
live (runs execute in a background thread; the page polls for progress).
Each task expands to show every candidate's answer with its per-dimension
scores and the judge's rationale. Completed runs are also written to
`runs/<run_id>/` as `report.md` + `results.json`.

REST API (usable without the frontend):

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/configs` | available config files + their model bindings |
| `POST` | `/api/runs` | `{"config": "config.demo.yaml"}` — start a run |
| `GET` | `/api/runs` | run summaries with progress |
| `GET` | `/api/runs/{id}` | full results (partial while running) |

## Quick start

```bash
cd multi-agent-eval
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. Prove the pipeline offline — no API keys needed
python main.py --config config.demo.yaml --out results-demo

# 2. Real run — set keys for the providers you have (others are skipped)
export ANTHROPIC_API_KEY=... OPENAI_API_KEY=... GEMINI_API_KEY=... ZAI_API_KEY=...
python main.py --config config.yaml --out results
open results/report.md
```

## No API keys? Use your subscriptions instead

You can run the whole thing on **consumer subscriptions** (Claude Pro/Max,
ChatGPT Plus/Pro, a free Google account) with **no pay-per-token API keys**.
Each subscription candidate bridges to the vendor's coding-agent CLI, which
authenticates with your normal login:

| Provider key | CLI | Auth | Install |
|---|---|---|---|
| `claude-code` | `claude -p` | Claude Pro/Max | `npm i -g @anthropic-ai/claude-code` then `claude` → `/login` |
| `codex-cli` | `codex exec` | ChatGPT Plus/Pro | `npm i -g @openai/codex` then `codex login` |
| `gemini-cli` | `gemini -p` | free Google account | `npm i -g @google/gemini-cli` then `gemini` (OAuth) |

```bash
# Install & log in to at least one CLI above, then:
python main.py --config config.subscription.yaml --out results-sub
```

See [config.subscription.yaml](config.subscription.yaml). CLIs that aren't
installed are skipped, so one subscription is enough to start. If a CLI lives
off `PATH`, point at it with `CLAUDE_CLI_PATH` / `CODEX_CLI_PATH` /
`GEMINI_CLI_PATH`.

**Trade-offs vs. the API adapters:** you're benchmarking *model + agent-CLI*
(not the bare model), latency includes CLI startup, subscription rate limits
apply, and only Claude Code reports token counts. Great for personal
benchmarking on plans you already pay for; don't route production traffic
through these. Both paths share the exact same orchestrator, judge, and
report code — subscriptions are just another `Provider` behind the same seam.

## Extending

- **Add a provider:** create `eval_agents/providers/foo_provider.py`
  implementing `Provider.complete()`, register it in `registry.py`, and
  reference it in `config.yaml`. If it's OpenAI-compatible, subclass
  `OpenAIProvider` like the Z.ai adapter does.
- **Add tasks:** append to `tasks.yaml` — real value comes from tasks that
  mirror *your* workload.
- **Multiple judges / panel scoring:** instantiate several judge agents and
  average their `Verdict.overall` in `runner.py`.
- **Different use case:** the agent/provider layers are use-case agnostic —
  the same abstraction supports a planner→worker→reviewer pipeline where
  each role runs on the provider best suited (e.g. cheap model for
  classification, frontier model for synthesis).

## Project layout

```
multi-agent-eval/
├── main.py                     # CLI entry point
├── webapp/
│   ├── server.py               # FastAPI REST API + background run manager
│   └── static/index.html       # browser frontend (no build step)
├── config.yaml                 # provider/model bindings (the switchboard)
├── config.demo.yaml            # offline mock configuration
├── tasks.yaml                  # evaluation task suite
└── eval_agents/
    ├── registry.py             # provider factory (config -> adapter)
    ├── agents.py               # Agent = role + provider binding
    ├── judge.py                # LLM-as-judge rubric + JSON parsing
    ├── runner.py               # orchestrator (fan-out, judging)
    ├── report.py               # markdown + JSON reporting
    └── providers/
        ├── base.py             # Provider interface + normalized types
        ├── anthropic_provider.py
        ├── openai_provider.py  # also base class for OpenAI-compatible APIs
        ├── zai_provider.py     # GLM via OpenAI-compatible endpoint
        ├── gemini_provider.py
        └── mock_provider.py    # offline testing
```
