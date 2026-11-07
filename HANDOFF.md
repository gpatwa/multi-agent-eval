# Handoff (updated 2026-10-27)

Read this first. It is the state of **The Model Ledger** (repo `gpatwa/multi-agent-eval`) as of the end of the last session. Delete it once you have absorbed it.

## What this is

A self-hosted, multi-provider LLM evaluation harness with a fully automated results pipeline and a landing page at **https://eval.aveto.dev**. Reference workload: support-ticket triage (40 public tickets, 20 held-out). Candidates run on subscriptions (Claude Code CLI, Codex CLI) and the Gemini API. An LLM judge scores answers, a second judge from a different vendor cross-checks, and the page is generated from the published data.

## Where things stand

- Branch `claude/handoff-review-planning-b8ecc2` is in step with `main`. Push to both: `git push origin HEAD:main HEAD:claude/handoff-review-planning-b8ecc2`. Check `gh run list` after every push.
- **Published run (2026-10-17, effort `low` for all):** rank 1 `gpt-6-astra` (composite 0.97, 0 flags), 2 `claude-opus-5-5` (0.70, 0 flags), 3 `gemini-4-argon` (0.68; 1 flag on the public suite, 3 on held-out). Two judges agree (Spearman rho 1.00). Cost is **not scored** (weight 0, nothing priced). Published history has 3 entries (the third records the Claude and Gemini model swaps).
- **Ranking rule (user decision):** any candidate with a guardrail flag ranks below every clean candidate; within each group by composite. Implemented in `eval_agents/report.py:rank`, recomputed in `publish.py`.
- Page, README results, sitemap, robots, `feed.xml`, `history.json` and `docs/results/` are generated and live. MIT license added. Launch playbook and drafts exist (`launch/PLAYBOOK.md`, `launch/kit/`); **nothing has been posted anywhere.**
- 2026-10-27: CI on the publish commit had failed because AutomationBench's unpinned `verifiers` resolved to 0.4.0, which removed `verifiers.types`. `requirements-automationbench.txt` now pins `verifiers==0.3.1`. Confirm the next CI run is green.

## What is blocking: nothing

The Claude Code login works again (checked 2026-10-27: `claude -p ... --model claude-opus-5-5` returns `ok`), and `pipeline.py status` shows every candidate ready for both suites. To refresh results after a model change: edit the candidate line in `config.triage.mixed.yaml` and `config.triage.mixed.codex-judge.yaml` by hand (the pipeline then reports `stale-model` for that candidate only), then run `python scripts/pipeline.py tick --publish --loop --interval 900` in the background. It re-runs only the stale candidate, re-scores with Codex, gates, publishes, adds a history row (with any regression), and refreshes `launch/kit/`. Probe a new model on its CLI first; Codex rejects most non-Astra GPT models on a ChatGPT login.

## Decisions the user has made (do not relitigate)

- **No API keys for now.** AutomationBench needs tool calling, so API providers only (about $27 for one trial each, about $80 for three). The user said it is not worth it without a clear return. A priced cost lane for triage is also parked. Only `GEMINI_API_KEY` exists in `.env`.
- Site is `eval.aveto.dev`, everything on Cloudflare. `release.yaml` `site.url` is the single source for the domain.
- Flagged models rank below clean ones. MIT license. Dark "instrument panel" design.
- No manual steps for the user other than credentials. Verify vendor and model claims from primary sources. Never post publicly or open pull requests on other people's repos without asking.

## Model landscape (rechecked 2026-11-04; vendor pages for Anthropic, OpenAI, Google; nothing changed for the tested vendors since 2026-10-27)

- Anthropic: Fable 5.1 (`claude-fable-5-1`, $10/$50), Opus 5.5 (`claude-opus-5-5`, $4/$20, the published Claude), Sonnet 5.5 (`claude-sonnet-5-5`, $2/$10), Haiku 5.5 (`claude-haiku-5-5`, from $0.10/$0.50, for routing and classification). Opus 5, Fable 5, Sonnet 5 and Haiku 4.5 are legacy. Fable 5.1, Sonnet 5.5 and Haiku 5.5 have not been tried on the Claude Code CLI.
- OpenAI: `gpt-6-astra` ($10/$50, the published GPT), `gpt-6.1-sol` ($2/$10), **`gpt-6.1-terra` ($1/$5, new)**, `gpt-6-luna` ($0.10/$0.50). **Only `gpt-6-astra` runs on the ChatGPT-account Codex lane**: Sol, Terra, `gpt-6-sol` and Luna all return "not supported ... with a ChatGPT account" (probed 2026-10-27). The rest need an OpenAI API key (Sol about $1 for the triage suites, an estimate).
- Google: **`gemini-4-argon` is GA (2026-10-13)** and is the published Gemini. Per Google's pricing page: $2 in / $10 out (thinking billed as output) / $0.10 cached through 2026-11-30, then $4 / $20 / $0.20 from 2026-12-01; no prompt-length tiers. The Pro-tier `gemini-3.1-pro-preview` is still listed. `gemini-3.8-flash` is the newest Flash ($0.75/$3.75 until 2026-12-31).
- Google's docs moved to `aistudio.google.com/docs/...` (the old `ai.google.dev/gemini-api/docs/...` URLs 301 there). Gemini 4 Argon is Google's only Gemini 4 model and its "flagship reasoning model"; a tracker's "Gemini 4 Carbon" is NOT on Google's models page or changelog. `gemini-2.5-pro` / `2.5-flash` are deprecated (reroute to 3.8-flash / 3.5-flash-lite on 2026-12-01). The OpenAI model page's "View more" list was not expanded, so a newer OpenAI model can't be ruled out from it.
- Others (trackers, not vendor pages; less certain; add Tencent Hy4 2026-11-04, Kimi K3.1 Pro open weights promised by 2026-11-04 but unconfirmed, Google/Meta/DeepSeek releases forecast for 5 and 23 Nov): Grok 4.7, DeepSeek V4.1 Flash (refreshed 2026-10-17), Kimi K3.1 Pro (2026-10-23), GLM 5.3, Qwen3.8-Omni-Flash, Meta Muse Spark 1.3. Trackers also list "GPT-6 Pro" and a Claude "Duet" preview; neither appears on the vendors' model pages, so treat them as unverified.
- The `SOURCES` map in `eval_agents/site.py` drives the page's Sources section (updated 2026-10-27). The README's "Keeping model IDs and pricing current" table and the non-published configs were updated the same day (Gemini candidate to Argon with its pricing); judges were left as they were.

## How the system works (what you need to know)

- `release.yaml` declares the release (configs, suites, site, repo metadata). `scripts/pipeline.py tick [--publish] [--loop]` runs missing candidates, merges, re-scores with the second judge, gates (`eval_agents/publish.py`), writes `docs/data/results.json` and `history.json`, re-renders generated files and commits. State is in `.pipeline/` (gitignored). Exit codes 0/1/2/75. Cooldowns are per provider (`run:<candidate>`, `judge:primary`, `judge:second`) and 20 minutes after a quota stop.
- **Generated files:** never hand-edit `<!-- AUTO:name -->` regions in `docs/index.html` or README, nor `docs/sitemap.xml`, `robots.txt`, `feed.xml`. Run `python scripts/build_site.py` (`--check` verifies freshness). CI fails if they are stale. To rewrite files without committing, run `pipeline.py tick` with no `--publish`, then commit code and docs together.
- Page sections generated from data (AUTO regions in `docs/index.html`): `scoreboard` (the hero's ranked table and verdict), `readout` (findings and limits side by side), `proof` (chart and per-topic table), `walkthrough`, `history`, `sources`, `method_judges`, plus `head`. Section order is results first (scoreboard, read-out, proof, walkthrough, history, method, sources), then the generic problem, use-case and coverage sections. Redesigned 2026-11-06; the older hero panels, stat tiles and tested-models block are gone.
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
- Tests: `.venv/bin/python -m pytest -q` (378 passed, 19 skipped). AutomationBench tests only run with `requirements-automationbench.txt` installed (CI does this).

## Open follow-ups (none blocking)

1. Decide whether to add more tiers as candidates: Claude Fable 5.1 (same price tier as Astra) and Sonnet or Haiku 5.5 via Claude Code (untested on the CLI), and GPT-6.1 Sol or Terra (needs an `OPENAI_API_KEY`).
2. A priced cost lane for triage (about $5 for all three vendors, an estimate) needs Anthropic and OpenAI API keys. The user chose to skip API keys for now.
3. AutomationBench lane: needs full runs for all three vendors at matched effort with API keys (about $27 for one trial each, about $80 for three). The user judged it not worth it without a clear return.
4. Composite ranking bug for a candidate whose answers all errored; a judge-parse retry; a Codex `service_tier` override (untested).
5. The data side of the launch is ready (published, cross-checked, licensed). The user decides when to post; follow `launch/PLAYBOOK.md`. Re-run `python scripts/launch_kit.py` first.
6. Argon's price doubles on 2026-12-01; the page does not show cost, but note it if cost scoring is added.

## User preferences

Concise recommendations. Commit and push each discrete unit. Verify claims from primary sources. Never fabricate logos, testimonials or outputs. Commit trailer: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Memory notes live in `~/.claude/projects/-Users-gopalpatwa-opt-multi-agent-eval/memory/`.
