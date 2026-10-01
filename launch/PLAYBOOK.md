# Distribution playbook: sharing the results

The goal is to put the published results in front of the people who choose LLM vendors, with claims that survive their scrutiny. Reach comes from being accurate and easy to check, so the playbook is built around that.

## Audience and message

| Who | Cares about | One-line message |
|---|---|---|
| Platform and AI engineers | Whether a model holds up on *their* workload, how scoring works | A self-hosted harness that scores models on your own tickets and policy, with a second judge and a guardrail gate |
| Engineering and product leads | Defensible vendor choice, risk | The ranking, what it can't tell you, and how a model swap is tracked in history |
| Evaluation practitioners | Method, judge bias, leakage | Exact checks where possible, judge only for judgment, a different-vendor cross-check, a held-out set |

Lead with the method and the limits. The numbers are an example of what the tool produces, not the product.

## Claims policy (applies to every post and reply)

1. Every number and model name comes from `docs/data/results.json` or `history.json`. `scripts/launch_kit.py` writes the drafts from them; do not retype figures.
2. State the limits in the post itself, not only on the site: sample size, one workload, matched effort, cost not scored, CLI latency includes startup.
3. Never write "best model". Say which model leads on which measure and what the ranking rule is.
4. Disclose authorship on every community post.
5. If a result changes after you post, reply in the same place with the correction, then let the pipeline add its history row. Do not edit or delete to hide it.

## Channels

| Channel | Why | Draft | Who posts | Notes |
|---|---|---|---|---|
| Atom feed (`/feed.xml`) | Subscribers, aggregators and newsletters pick it up with no effort | generated | **Automatic** | One entry per history row, deployed with the site |
| Search and discovery | Ongoing, free traffic | sitemap, IndexNow, Search Console | **Automatic**, plus one-time Search Console setup (done) | Check `site:eval.aveto.dev` weekly |
| GitHub release | Gives the repo a dated, linkable results page | `release_notes.md` | Automatic command, **your go-ahead first** | `python scripts/launch_kit.py --github-release` |
| Hacker News (Show HN) | Largest developer audience for self-hosted tools | `show_hn.md` | **You** | Needs something people can try; do not ask for upvotes; post the first comment yourself |
| Reddit | r/MachineLearning ([P] tag), r/LLMDevs, r/ClaudeAI, r/OpenAI | `reddit.md` | **You** | Each sub has its own self-promotion rules: read them first, and post to one or two, not all |
| X and LinkedIn | Reach into practitioner and lead networks | `x_thread.md`, `linkedin.md` | **You** | Attach the social card; LinkedIn suits the lead audience |
| dev.to or Hashnode | Search-indexed write-up that links back | `blog_outline.md` | **You** (outline is generated) | Set the canonical link to the landing page |
| Curated lists and newsletters | Slow, steady discovery | one-line description | **You**, as pull requests or submissions | Public pull requests to others' repos: ask before I open any |

Channel rules change. Read each channel's current posting rules the day you post.

## When to announce

Not every publish. Announce when the history shows a reason:
- the first published run (launch);
- a **leader change**, a **model swap**, a **quality drop** or **new guardrail flags**, which are the history's recorded changes;
- a new vendor or model added to the run.

Routine re-runs with no headline change only update the feed.

## Launch sequence

1. **Gate.** `python scripts/launch_kit.py` exits 2 if results aren't published, cross-checked and linked. Fix the reason before going further.
2. **Pre-flight checklist** (below).
3. **Day 0, morning:** GitHub release, then Show HN with your first comment ready.
4. **Day 0, same day:** X thread and LinkedIn post. One Reddit post where the rules fit.
5. **Day 1 to 3:** the blog write-up, then list and newsletter submissions.
6. Reply to every substantive comment for the first day. Answer questions with a link to the specific section or file, not a restatement.

## Pre-flight checklist

- [ ] The tested models match what you'd call current. The Claude candidate must be re-run on the latest Opus before a broad launch, or the first comment will be "why not the latest?". See the Sources section on the site.
- [x] A **LICENSE** file exists in the repo (MIT, added 2026-09-30), so "Open source" on the page is true.
- [ ] `https://eval.aveto.dev` loads, the Sources links resolve, and `launch/kit/` was regenerated after the last publish.
- [ ] README results match the site (they are generated from the same data).
- [ ] You can run the quickstart from a clean clone.

## After posting

- **Watch:** GitHub traffic and referrers for the repo, Search Console impressions, and the feed's subscriber count if your host reports it. Record the numbers in a line in this file with the date so the next launch has a baseline.
- **Respond:** corrections and method questions first. If someone finds a real flaw, add it to the history or limits, then reply with the fix.
- **Learn:** note which framing drew the questions. Update the drafts' templates in `eval_agents/launch.py`, not the posted copy.

## What this playbook does not do

It never posts for you. Posting needs your accounts and your judgment about tone, so the drafts stop at a file you copy from. Everything that can be automated without an account (feed, sitemap, IndexNow, repo metadata, the drafts themselves) already is.
