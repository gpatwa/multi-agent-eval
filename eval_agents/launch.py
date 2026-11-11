"""Channel-ready announcements, written from the published data and nothing else.

A launch post is a claim about results, so it is generated the same way the landing page is:
every number and every model name comes from docs/data/results.json and history.json, and the
limits (sample size, matched effort, unscored cost, unequal latency) are appended by code, not
by whoever is writing the post. If the data changes, the drafts change. Nothing here posts
anywhere; a person with the channel's account does that (see launch/PLAYBOOK.md).

Pure functions of the release data.
"""
from __future__ import annotations

from .site import CHANGE_TEXT, REPO_URL, _judge_name, _label, _suites, flag_kinds, judge_flag_split, site_base, vendor_of

HN_TITLE_MAX, X_POST_MAX = 80, 280


def _public(data: dict) -> dict:
    return data["suites"][_suites(data)[0]]


def _by_composite(public: dict) -> list[str]:
    return sorted(public["candidates"], key=lambda n: -public["candidates"][n]["composite"])


def model_names(data: dict) -> list[str]:
    public = _public(data)
    return [public["candidates"][n]["model"] for n in public["ranking"]]


def readiness(data: dict, history: dict) -> list[str]:
    """Reasons not to announce yet (empty = fine to post)."""
    problems = []
    if data.get("status") != "published":
        problems.append("results are not published yet")
        return problems
    if not site_base(data):
        problems.append("no site URL in release.yaml: nothing to link to")
    first = _suites(data)[0]
    if not (data.get("agreement") or {}).get(first):
        problems.append("no second-judge cross-check: do not claim the ranking is cross-checked")
    if not (history.get("entries") or []):
        problems.append("history is empty")
    return problems


def findings(data: dict) -> list[str]:
    """What the data supports saying, most newsworthy first. Each line stands alone."""
    first, rest = _suites(data)
    public = _public(data)
    cands, ranking = public["candidates"], public["ranking"]
    comp = _by_composite(public)
    out = []
    if ranking != comp:
        top = comp[0]
        flags = cands[top]["critical_violations"]
        out.append(f"{cands[top]['model']} has the highest composite ({cands[top]['composite']:.2f}) but {flags} confirmed "
                   f"flag{'s' if flags != 1 else ''}, so {cands[ranking[0]]['model']} ranks first: a flag ranks a model below every clean one.")
    else:
        out.append(f"{cands[ranking[0]]['model']} ranks first on the composite ({cands[ranking[0]]['composite']:.2f}).")
    best = max(cands, key=lambda n: cands[n]["quality_mean"])
    slowest = max(cands, key=lambda n: cands[n]["latency_p50"])
    others = [c["latency_p50"] for n, c in cands.items() if n != best]
    line = f"{cands[best]['model']} has the highest raw reply quality ({cands[best]['quality_mean']:.2f}/5)"
    if best == slowest and others:
        line += f" but the slowest median latency ({cands[best]['latency_p50']:.1f}s vs {min(others):.1f}-{max(others):.1f}s)"
    out.append(line + ".")
    topics = public.get("topics") or {}
    if len(topics) >= 2:
        wins = sum(1 for t in topics.values()
                   if max(t["candidates"], key=lambda n: t["candidates"][n]["quality_mean"] or 0) == best)
        out.append(f"{cands[best]['model']} has the best quality on {wins} of {len(topics)} ticket topics.")
    ag = (data.get("agreement") or {}).get(first)
    if ag and ag["rank_rho"] is not None:
        out.append(f"Two judges from different vendors agree on the order (Spearman rho {ag['rank_rho']:.2f}, {ag['n_answers']} answers).")
    if rest:
        held = data["suites"][rest[0]]
        gaps = [abs(cands[n]["quality_mean"] - held["candidates"][n]["quality_mean"]) for n in cands if n in held["candidates"]]
        if gaps:
            out.append(f"Quality on {held['n_tasks']} held-out tickets stays within {max(gaps):.2f} of the public suite.")
    allc = [c for s in data["suites"].values() for c in s["candidates"].values()]
    flags, answers = sum(c["critical_violations"] for c in allc), sum(c["n_samples"] for c in allc)
    if flags:
        by_model: dict[str, int] = {}
        for suite in data["suites"].values():
            for c in suite["candidates"].values():
                if c["critical_violations"]:
                    by_model[c["model"]] = by_model.get(c["model"], 0) + c["critical_violations"]
        who = ", ".join(f"{m}: {k}" for m, k in sorted(by_model.items(), key=lambda kv: -kv[1]))
        out.append(f"{flags} confirmed flag{'s' if flags != 1 else ''} across {answers} graded answers ({who}); {flag_kinds(data)}.")
    else:
        out.append(f"No confirmed flags across {answers} graded answers.")
    return out


def limitations(data: dict) -> list[str]:
    first, rest = _suites(data)
    public = _public(data)
    held = data["suites"][rest[0]]["n_tasks"] if rest else 0
    efforts = sorted({c["effort"] for c in public["candidates"].values()})
    out = [f"{public['n_tasks']} public tickets plus {held} held-out, from one reference workload (support triage). "
           "It may not transfer to your tickets, which is why the tool is self-hosted."]
    if len(efforts) == 1:
        out.append(f"Every candidate ran at reasoning effort {efforts[0]}.")
    if not (public.get("weights") or {}).get("cost"):
        out.append("Cost is not scored in this run.")
    if any("CLI" in _label(c["provider"]) for c in public["candidates"].values()):
        out.append("Latency for CLI candidates includes agent-CLI startup, so it is pipeline time, not model speed.")
    ag = (data.get("agreement") or {}).get(first)
    out.append(f"Judged by {_judge_name(public['judge'])}" + (f", cross-checked by {_judge_name(ag['judge_second'])}." if ag else "."))
    split = judge_flag_split(data)
    if split and split[:2] != (split[2], split[2]):
        out.append(f"A flag counts only when both judges raise it. The primary judge flagged {split[0]} answers, the second {split[1]}, "
                   f"and {split[2]} were flagged by both, so flag counts depend on which judge you ask.")
    overlap = _judge_overlap(data)
    if overlap:
        out.append(f"{overlap} Self-preference is possible, which is why a second vendor cross-checks the first judge.")
    return out


def _judge_overlap(data: dict) -> str:
    """Where a judge is also a candidate, or from a candidate's vendor. Empty when judges and candidates don't overlap."""
    first = _suites(data)[0]
    public = _public(data)
    cands = {c["model"]: n for n, c in public["candidates"].items()}
    vendors = {vendor_of(c["model"], n): c["model"] for n, c in public["candidates"].items()}
    ag = (data.get("agreement") or {}).get(first)
    parts = []
    for role, j in (("primary judge", public.get("judge")), ("second judge", (ag or {}).get("judge_second"))):
        model = (j or {}).get("model")
        if not model:
            continue
        if model in cands:
            parts.append(f"The {role} ({model}) is also a candidate.")
        elif vendor_of(model, "") in vendors:
            parts.append(f"The {role} ({model}) is from the same vendor as candidate {vendors[vendor_of(model, '')]}.")
    return " ".join(parts)


def _models_sentence(data: dict) -> str:
    public = _public(data)
    return ", ".join(f"{vendor_of(public['candidates'][n]['model'], n)} {public['candidates'][n]['model']}" for n in public["ranking"])


def kit(data: dict, history: dict) -> dict[str, str]:
    """File name -> text for every channel."""
    url, repo = site_base(data), REPO_URL
    f, lim, models = findings(data), limitations(data), _models_sentence(data)
    run_date = data["generated"][:10]
    bullets = "\n".join(f"- {x}" for x in f)
    limits = "\n".join(f"- {x}" for x in lim)
    stamp = f"<!-- generated from docs/data/results.json (run {run_date}); regenerate with scripts/launch_kit.py, do not edit numbers by hand -->\n"

    hn_title = "Show HN: Self-hosted harness to compare LLM vendors on your own tickets"
    show_hn = (f"{stamp}# Show HN\n\n**Where:** https://news.ycombinator.com/submit  (link: {url})\n"
               f"**Title ({len(hn_title)} chars, limit {HN_TITLE_MAX}):** {hn_title}\n\n**First comment (post it yourself right after submitting):**\n\n"
               f"I built this because vendor benchmarks don't say how a model does on my own policy and tickets. It runs candidates through "
               f"your tasks, grades the exact parts exactly (routing, priority, declared actions), uses an LLM judge only for judgment calls, "
               f"cross-checks that judge with a second vendor, and treats a confirmed flag as a gate instead of an averaged score.\n\n"
               f"The first reference run ({run_date}) covers {models}:\n\n{bullets}\n\nLimits, up front:\n\n{limits}\n\n"
               f"Repo: {repo}\nResults and method: {url}\n\nI'd most like feedback on the scoring rules and on what a fair cost comparison looks like "
               f"when two of the candidates run on subscriptions.\n")

    reddit_title = f"I built a self-hosted harness to compare LLM vendors on your own tickets; first results: {', '.join(model_names(data))}"
    reddit = (f"{stamp}# Reddit\n\n**Where (check each sub's self-promotion rules first):** r/MachineLearning (tag [P]), r/LLMDevs, "
              f"r/ClaudeAI, r/OpenAI, r/LocalLLaMA only if it fits their rules\n**Title:** {reddit_title[:300]}\n\n**Body:**\n\n"
              f"Disclosure: I'm the author. This is an open-source harness that scores models on your own tickets and policy.\n\n{bullets}\n\n"
              f"Limits:\n\n{limits}\n\nRepo: {repo}\nResults: {url}\n\nHappy to run another model if you tell me which and how you'd want it scored.\n")

    cli = any("CLI" in _label(c["provider"]) for c in _public(data)["candidates"].values())
    latency = " Latency through the CLIs includes startup." if cli and "slowest" in f[1] else ""
    topic_post = [f[2]] if "ticket topics" in f[2] else []
    judged = next((x for x in lim if x.startswith("Judged by")), "")
    overlap = next((x for x in lim if x.startswith(("The primary judge", "The second judge"))), "")
    thread = [
        f"I built a self-hosted harness to compare LLM vendors on your own tickets and policy. First run: {models}. Results, method and repo: {url}",
        f[0],
        f[1] + latency,
        *topic_post,
        f[-1] + " A flag ranks a model below every clean one.",
        "Limits: " + " ".join(lim[:3]),
        *[x for x in (judged, overlap) if x],
        f"Open source: {repo}",
    ]
    x_thread = f"{stamp}# X thread\n\n**Where:** your X account. Post as a thread; add the social card from {url}/og-image.png to post 1.\n\n" + "\n\n".join(
        f"**{i}/{len(thread)}** ({len(t)} chars)\n{t}" for i, t in enumerate(thread, 1))

    linkedin = (f"{stamp}# LinkedIn\n\n**Where:** your profile; tag no one you haven't asked.\n\n"
                f"Choosing an LLM vendor usually comes down to a benchmark the vendor chose. I built an open-source, self-hosted harness that "
                f"scores models on your own tickets and policy instead.\n\nFirst reference run ({run_date}), {models}:\n\n{bullets}\n\n"
                f"What I'd flag before you rely on it:\n\n{limits}\n\nMethod and numbers: {url}\nCode: {repo}\n")

    blog = (f"{stamp}# Blog post outline (dev.to or Hashnode)\n\n**Where:** dev.to / Hashnode with a canonical link to {url}. Search-indexed, so use the real "
            f"model names in the title.\n\n**Working title:** Comparing {', '.join(model_names(data))} on the same support tickets\n\n"
            f"1. Why a vendor's benchmark isn't your benchmark\n2. How scoring works: exact checks, a judge for judgment, a second judge to cross-check\n"
            f"3. The result and what surprised me\n{bullets}\n4. Why a confirmed flag ranks a model down instead of averaging out\n"
            f"5. What this run can't tell you\n{limits}\n6. Run it on your own tickets: {repo}\n")

    # Public text only (it becomes the release body): no stamp, no instructions to the person posting it.
    release = (f"## Results run {run_date}\n\nModels: {models}.\n\n{bullets}\n\n### Limits\n\n{limits}\n\n"
               f"Full results and method: {url}\n")
    return {"show_hn.md": show_hn, "reddit.md": reddit, "x_thread.md": x_thread, "linkedin.md": linkedin, "blog_outline.md": blog,
            "release_notes.md": release}


def x_posts(data: dict, history: dict) -> list[str]:
    """The thread's individual posts, for length checks."""
    import re

    return re.findall(r"\*\*\d+/\d+\*\* \(\d+ chars\)\n(.*?)(?=\n\n\*\*\d+/|\Z)", kit(data, history)["x_thread.md"], re.S)


# ---------------------------------------------------------------- feed


def change_text(c: dict) -> str:
    """One history change as plain text, e.g. 'claude: model claude-opus-5 -> claude-opus-5-5'."""
    text = CHANGE_TEXT[c["kind"]]
    text = text(c) if callable(text) else text
    return text if c["candidate"] == "*" else f"{c['candidate']}: {text}"


def feed(data: dict, history: dict) -> str:
    """Atom feed of published runs, newest first. A distribution channel that needs no account: feed readers,
    newsletters and aggregators can subscribe, and every entry says what changed."""
    import html

    base = site_base(data)
    if not base or not (history.get("entries") or []):
        return ""
    e = html.escape
    items = []
    for entry in reversed(history["entries"]):
        pub = entry["suites"][next(iter(entry["suites"]))]
        lead = pub["candidates"][pub["ranking"][0]]["model"]
        what = [c for c in entry["changes"] if c["kind"] in ("model_changed", "leader_changed", "quality_down", "flags_up")] or []
        if what:
            tail = "Changes: " + "; ".join(change_text(c) for c in what) + "."
        else:
            tail = "First published run." if not entry["changes"] else "No headline change."
        summary = f"{lead} ranks first. {tail}"
        items.append(f"  <entry>\n    <title>Run {e(entry['published'][:10])}: {e(lead)} ranks first</title>\n"
                     f"    <id>{e(base)}/#run-{e(entry['id'])}</id>\n    <updated>{e(entry['published'])}</updated>\n"
                     f"    <link href=\"{e(base)}/#history\"/>\n    <summary>{e(summary)}</summary>\n  </entry>")
    updated = history["entries"][-1]["published"]
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<feed xmlns="http://www.w3.org/2005/Atom">\n  <title>The Model Ledger: published runs</title>\n'
            f'  <id>{e(base)}/</id>\n  <link href="{e(base)}/feed.xml" rel="self"/>\n  <link href="{e(base)}/"/>\n  <updated>{e(updated)}</updated>\n'
            + "\n".join(items) + "\n</feed>\n")
