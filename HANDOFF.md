# Handoff (written 2026-10-03)

Read this first. It is the state of **The Model Ledger** (repo `gpatwa/multi-agent-eval`) as of the end of the last session. Delete it once you have absorbed it.

## What this is

A self-hosted, multi-provider LLM evaluation harness with a fully automated results pipeline and a landing page at **https://eval.aveto.dev**. Reference workload: support-ticket triage (40 public tickets, 20 held-out). Candidates run on subscriptions (Claude Code CLI, Codex CLI) and the Gemini API. An LLM judge scores answers, a second judge from a different vendor cross-checks, and the page is generated from the published data.

## Where things stand

- Branch `claude/handoff-review-planning-b8ecc2` is in step with `main`. Push to both: `git push origin HEAD:main HEAD:claude/handoff-review-planning-b8ecc2`. CI and the Cloudflare deploy are green on the last commit.
- **Published run (2026-09-29, effort `low` for all):** rank 1 `gpt-6-astra`, 2 `gemini-3.1-pro-preview`, 3 `claude-opus-5`. Two judges agree (Spearman rho 1.00). Gemini has 2 guardrail flags and Claude 1 on the public suite. Cost is **not scored** (weight 0, nothing priced).
- **Ranking rule (user decision):** any candidate with a guardrail flag ranks below every clean candidate; within each group by composite. Implemented in `eval_agents/report.py:rank`, recomputed in `publish.py`.
- `docs/data/history.json` is append-only (2 entries so far). Page, README, sitemap, robots, `feed.xml` and the results files under `docs/results/` are all generated and live.
- MIT license added. Launch playbook and drafts exist; **nothing has been posted anywhere.**

## The one blocker: the Claude Code login has expired

`claude -p ...` fails with "OAuth session expired and could not be refreshed". The Claude CLI is already updated (2.1.286). The user must run `claude` in a terminal and sign in; that is the only manual step. Until then nothing can run, because the **primary judge is Claude Code too**.

After sign-in, in this order:
1. Confirm the CLI accepts the model: `env -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_BASE_URL claude -p "ok" --output-format json --model claude-opus-5-5 --effort low`. If it is rejected, say so and stop; do not fall back silently.
2. `config.triage.mixed.yaml` and `config.triage.mixed.codex-judge.yaml` are **already** staged to `claude-opus-5-5` (candidate line only; the judge stays `claude-opus-5`). `python scripts/pipeline.py status` shows Claude "needs run — stale-model" for both suites; GPT and Gemini are kept.
3. Run `python scripts/pipeline.py tick --publish --loop --interval 900` in the background. It re-runs Claude on both suites, has Codex re-score, applies the gates, publishes, adds a history row (`model claude-opus-5 -> claude-opus-5-5`, plus any quality drop or new flags), and refreshes `launch/kit/`.
4. Check the live site and CI, then tell the user.

## Decisions the user has made (do not relitigate)

- **No API keys for now.** AutomationBench needs tool calling, so API providers only (about $27 for one trial each, about $80 for three). The user said it is not worth it without a clear return. A priced cost lane for triage is also parked. Only `GEMINI_API_KEY` exists in `.env`.
- Site is `eval.aveto.dev`, everything on Cloudflare. `release.yaml` `site.url` is the single source for the domain.
- Flagged models rank below clean ones. MIT license. Dark "instrument panel" design.
- No manual steps for the user other than credentials. Verify vendor and model claims from primary sources. Never post publicly or open pull requests on other people's repos without asking.

## Model landscape (checked 2026-09-30, mostly primary sources)

- Anthropic: Fable 5.1 (`claude-fable-5-1`, $10/$50), Opus 5.5 (`claude-opus-5-5`, $4/$20), Sonnet 5.5, Haiku 4.5. Opus 5 is legacy.
- OpenAI: `gpt-6-astra` ($10/$50), `gpt-6.1-sol` ($2/$10), `gpt-6-luna`. **Only `gpt-6-astra` runs on the ChatGPT-account Codex lane**; Sol, `gpt-6-sol` and Luna return "not supported ... with a ChatGPT account". Sol needs an OpenAI API key (about $1 for the triage suites, an estimate).
- Google: Pro tier is still `gemini-3.1-pro-preview`. **Gemini 4 Argon** was announced 2026-09-30 as limited preview ("rolling out soon", no API ID). Retry when it reaches the API.
- Others (news sites, less certain): Grok 4.7, DeepSeek V4.1 Flash, Qwen3.8-Omni-Flash, GLM 5.3, Kimi K3, Meta Muse Spark 1.3 (via Meta Model API or OpenRouter).
- The `SOURCES` map in `eval_agents/site.py` drives the page's Sources section. Add Fable 5.1 there when relevant.

## How the system works (what you need to know)

- `release.yaml` declares the release (configs, suites, site, repo metadata). `scripts/pipeline.py tick [--publish] [--loop]` runs missing candidates, merges, re-scores with the second judge, gates (`eval_agents/publish.py`), writes `docs/data/results.json` and `history.json`, re-renders generated files and commits. State is in `.pipeline/` (gitignored). Exit codes 0/1/2/75. Cooldowns are per provider (`run:<candidate>`, `judge:primary`, `judge:second`) and 20 minutes after a quota stop.
- **Generated files:** never hand-edit `<!-- AUTO:name -->` regions in `docs/index.html` or README, nor `docs/sitemap.xml`, `robots.txt`, `feed.xml`. Run `python scripts/build_site.py` (`--check` verifies freshness). CI fails if they are stale. To rewrite files without committing, run `pipeline.py tick` with no `--publish`, then commit code and docs together.
- Page sections generated from data: hero panels, tested models, proof chart, per-topic table, history, sources. The claims policy: the page and every draft say only what the data supports (for example, "Cost is not scored in this run" appears from the weights).
- `scripts/launch_kit.py` writes channel drafts into `launch/kit/` (gitignored); it exits 2 when results are not ready. `launch/PLAYBOOK.md` says where to post and who posts. **A person posts; do not post for the user.**
- Deploy: `.github/workflows/deploy-site.yml` on pushes touching `docs/**`, `release.yaml` or the script. It needs only `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` (already GitHub secrets; Cloudflare zone `aveto.dev`). It creates the Pages domain and proxied CNAME, verifies, and pings IndexNow. Google Search Console is set up by the user (sitemap submitted; status was still "Couldn't fetch", which is normal until Google's first crawl).

## Gotchas learned the hard way

- Cloudflare blocks Python's default User-Agent (403, error 1010): every HTTP call sets one.
- `.gitignore` has `results*/`; `docs/results/` is explicitly un-ignored. A test guards it. Do not delete any `results-*/` run directory.
- Pipeline runs now go stale when the config model or effort changes (`stale-model`, `stale-effort` in `Pipeline.health`).
- Do not use `scripts/bump_model.py` for IDs that are prefixes of other IDs (`claude-opus-5` inside `claude-opus-5-5`). A hardening task ran in another session; check whether it landed before using it, and otherwise edit configs by hand.
- `env_unset` in `release.yaml` strips `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL`; the desktop app injects them and they would route the CLI through its proxy.
- Claude CLI token counts (cached harness tokens) and Codex counts (about 16.7k input for a one-word prompt) are not model cost. Real cost needs an API-key lane.
- macOS: `sed -i` needs a backup suffix; use Python for edits. The Bash approval check has failed transiently before; waiting and retrying once worked.
- Tests: `.venv/bin/python -m pytest -q` (378 passed, 19 skipped).

## Open follow-ups (none blocking)

1. Re-run Claude on Opus 5.5 (blocked on login), then decide whether to add Fable 5.1 as a second Claude tier.
2. GPT-6.1 Sol and a priced cost lane, both waiting on an `OPENAI_API_KEY` decision.
3. Retry Gemini 4 Argon when it has an API ID.
4. AutomationBench lane: needs full runs for all three vendors at matched effort with API keys; the single existing Gemini sample cannot be published.
5. Composite ranking bug for a candidate whose answers all errored; a judge-parse retry; a Codex `service_tier` override (untested).
6. After the Opus 5.5 run, update the Sources section and README mentions, then offer the launch (blockers are only the Claude re-run now; the license is done).

## User preferences

Concise recommendations. Commit and push each discrete unit. Verify claims from primary sources. Never fabricate logos, testimonials or outputs. Commit trailer: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Memory notes live in `~/.claude/projects/-Users-gopalpatwa-opt-multi-agent-eval/memory/`.
