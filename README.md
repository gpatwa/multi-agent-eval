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

# real run across providers (API keys)
python main.py --config config.triage.yaml --out results-triage

# real run on SUBSCRIPTIONS instead of API keys (Claude Pro/Max, ChatGPT
# Plus/Pro, free Google account — see "No API keys?" section below):
python main.py --config config.triage.subscription.yaml --out results-triage-sub
# cross-vendor judge check, still subscription-only:
python main.py --config config.triage.subscription.gemini-judge.yaml --out results-triage-sub-b
python scripts/compare_judges.py results-triage-sub/summary.json results-triage-sub-b/summary.json
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
scores, guardrail flags, and the judge's rationale.

**Run history is persistent:** every run writes `runs/<run_id>/` (`run.json`
metadata, `results.json` saved incrementally per task, plus `report.md` and
`summary.json` on completion) and is rehydrated into the sidebar on server
restart. A run interrupted by a restart shows as failed with its partial
results still viewable.

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

## Keeping model IDs and pricing current

Model IDs and per-token prices live **only** in the config files — no code
change is needed to adopt a new release. Candidate defaults as of
**July 2026**:

| Provider | Model ID | $/MTok in | $/MTok out |
|---|---|---|---|
| Anthropic | `claude-opus-5` | 5.00 | 25.00 |
| OpenAI | `gpt-5.6-sol` | 5.00 | 30.00 |
| Google | `gemini-3.1-pro-preview` | 2.00 | 12.00 |
| Z.ai | `glm-5.2` | 1.40 | 4.40 |
| xAI | `grok-4.6` | 2.00 | 6.00 |

Cheaper tiers worth benchmarking against the flagships: `claude-sonnet-5`,
`gpt-5.6-terra` / `gpt-5.6-luna`, `gemini-3.5-flash`, `glm-5`.

### Open-weight models

[config.triage.open.yaml](config.triage.open.yaml) runs the open-weight tier
against a flagship anchor — the comparison that actually decides deployments,
since these sit well below flagship pricing:

| Provider key | Model ID | $/MTok in | $/MTok out | Key |
|---|---|---|---|---|
| `moonshot` | `kimi-k3` | 3.00 | 15.00 | `MOONSHOT_API_KEY` |
| `qwen` | `qwen3.8-max` | 2.00 | 6.00 | `DASHSCOPE_API_KEY` |
| `deepseek` | `deepseek-v4-pro` | 0.435 | 0.87 | `DEEPSEEK_API_KEY` |
| `zai` | `glm-5.2` | 1.40 | 4.40 | `ZAI_API_KEY` |

Value tiers go lower still: `kimi-k2.6` ($0.95/$4.00), `deepseek-v4-flash`
($0.14/$0.28), `glm-5`. All four vendors serve OpenAI-compatible endpoints,
so each adapter is ~8 lines
([open_weight_providers.py](eval_agents/providers/open_weight_providers.py)).
You can also reach all of them through a single `OPENROUTER_API_KEY` using
namespaced ids (`moonshotai/kimi-k3`, `qwen/qwen3.8-max`,
`deepseek/deepseek-v4-pro`), or self-host the weights and point
`OPENROUTER_BASE_URL` at a local vLLM/Ollama server.

**Read the result with the guardrail column, not just cost.** A model that's
10x cheaper but fails the prompt-injection probe isn't a saving — that's why
violations gate independently of the composite score.

### One account instead of many (aggregators)

Benchmarking across vendors normally means an account, a key, and a billing
relationship *per vendor*. An aggregator hosts many vendors behind one
OpenAI-compatible endpoint, so **a single key reaches most of the field** —
including **Meta Llama, which no longer has a first-party API** and is
reachable only via an aggregator or self-hosting.

| Provider key | Endpoint env var | Notes |
|---|---|---|
| `openrouter` | `OPENROUTER_API_KEY` | widest catalog, includes closed models |
| `together` | `TOGETHER_API_KEY` | open-weight focus, good throughput |
| `groq` | `GROQ_API_KEY` | fastest inference, generous free tier |
| `fireworks` | `FIREWORKS_API_KEY` | open-weight focus, tuning support |
| `deepinfra` | `DEEPINFRA_API_KEY` | low prices on open weights |

[config.triage.oneaccount.yaml](config.triage.oneaccount.yaml) runs six
candidates — Llama, Kimi, Qwen, DeepSeek, GLM, and Claude as a closed-model
anchor — plus a neutral Hermes judge, all through **one key**. Switching
aggregators is a one-word change (`provider:`) plus the model-id namespacing
that aggregator uses.

Trade-off: you inherit the aggregator's routing, uptime, and margin, and its
per-token price sits a little above going direct. For a benchmark harness
that's the right trade; for production volume, go direct to whichever model
wins.

### Local / self-hosted (no account at all)

`provider: local` talks to Ollama, LM Studio, vLLM, or llama.cpp — no key,
no per-token cost, data never leaves the machine
([local_provider.py](eval_agents/providers/local_provider.py)). Point
elsewhere with `LOCAL_BASE_URL`. Verified working with `llama3` and `qwen3`
via Ollama. Laptop-scale models (7–14B) won't match the trillion-parameter
hosted flagships, but at $0/ticket the bar they must clear is lower.

### Where to get open models (aggregators, direct, local)

Every option below is OpenAI-compatible, so switching between them is a
base-URL change, not an integration:

| Route | Examples | Trade-off |
|---|---|---|
| **Aggregator** | OpenRouter, Together, Fireworks, Groq, DeepInfra, Novita | One key, many models; small markup. Groq is fastest, has a free tier |
| **Direct from vendor** | `moonshot`, `qwen`, `deepseek`, `zai` providers | Cheapest per token, one account each |
| **Cloud platform** | Bedrock, Vertex AI, Azure AI Foundry | Enterprise billing/compliance; heavier setup |
| **Local** | `local` provider — Ollama, LM Studio, vLLM | **No account, no key, $0/token**, fully private |

For local, [config.triage.local.yaml](config.triage.local.yaml) runs
candidates through Ollama with a hosted judge:

```bash
ollama serve && ollama pull qwen3
python main.py --config config.triage.local.yaml --out results-local
```

Set `LOCAL_BASE_URL` to use LM Studio (`:1234/v1`), vLLM (`:8000/v1`), or
llama.cpp (`:8080/v1`) instead. Any aggregator works through the
`openrouter` provider by overriding `OPENROUTER_BASE_URL` — e.g. Groq's
`https://api.groq.com/openai/v1`.

When a vendor ships a new model, edit the `model:` line and its `pricing:`
entry in the config, then re-run against your pinned baseline
(`--baseline results-triage/`) to see whether the upgrade actually helps
*your* use case. Prices change more often than IDs — the `pricing:` map is
what makes the cost column and monthly projection meaningful, so re-check it
against the vendor's pricing page when a run informs a real decision.

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
