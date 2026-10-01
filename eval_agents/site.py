"""Render the landing page's data-bearing regions from the published data document.

The page (docs/index.html) is hand-written prose and design, except for four
regions fenced by `<!-- AUTO:name -->` … `<!-- /AUTO:name -->` markers: the stat
tiles, the one-ticket walkthrough, the results section, and the method item that
describes judge cross-checking. Those are a pure function of docs/data/results.json.

Claims policy: a sentence is rendered only if the data supports it.
  * no valid results (status "pending")  -> no numbers, no rankings, no claims;
  * "zero violations" only when the count is zero, otherwise the count and flag types;
  * the two-judge claim only when a valid second-judge comparison exists.
CI re-renders the page from the data and fails if the committed page differs, so
nothing on the page can be edited by hand or go stale.
"""
from __future__ import annotations

import html
import re

REGION = re.compile(r"(<!-- AUTO:(\w+) -->)(.*?)(<!-- /AUTO:\2 -->)", re.S)

PROVIDER_LABEL = {
    "ClaudeCodeProvider": "Claude Code CLI", "CodexProvider": "Codex CLI", "GeminiCliProvider": "Gemini CLI",
    "AnthropicProvider": "API", "OpenAIProvider": "API", "GeminiProvider": "API",
}
DIM_LABEL = {"policy_adherence": "policy"}
MAX_HELDOUT_GAP_OK = 0.25  # quality gap between suites that still reads as "consistent"
RESULTS_URL = "https://github.com/gpatwa/multi-agent-eval/blob/main/docs/results"  # rendered reports behind the numbers


def esc(text) -> str:
    return html.escape(str(text), quote=False)


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _label(provider: str | None) -> str:
    return PROVIDER_LABEL.get(provider or "", "API")


def _judge_name(j: dict | None) -> str:
    j = j or {}
    label, model = _label(j.get("provider")), j.get("model", "?")
    return model if label == "API" else f"{label} ({model})"


def _suites(data: dict):
    names = list(data["suites"])
    return names[0], names[1:]  # public first, then held-out


# ---------------------------------------------------------------- stat tiles


def stats(data: dict) -> str:
    suites = data["suites"]
    total = sum(s["n_tasks"] for s in suites.values())
    guard = sum(s["n_guardrail"] for s in suites.values())
    held = sum(s["n_tasks"] for n, s in suites.items() if n != next(iter(suites)))
    tiles = [
        (str(data["providers"]), "providers supported", ""),
        (str(total), f"reference tickets{f' ({held} held-out)' if held else ''}, {guard} adversarial", ""),
    ]
    if data["status"] == "published":
        flags = sum(c["critical_violations"] for s in suites.values() for c in s["candidates"].values())
        answers = sum(c["n_samples"] for s in suites.values() for c in s["candidates"].values())
        tiles.append((str(flags), f"guardrail flags in {answers} graded answers", "dot-good" if flags == 0 else "dot-warn"))
        public = data["agreement"] and data["agreement"].get(next(iter(suites)))
        if public and public["rank_rho"] is not None:
            tiles.append((f"{public['rank_rho']:.2f}", "judge rank correlation (ρ)", ""))
        else:
            efforts = {c["effort"] for c in suites[next(iter(suites))]["candidates"].values()}
            if len(efforts) == 1:
                tiles.append((next(iter(efforts)), "reasoning effort, matched across candidates", ""))
    return "\n      " + "\n      ".join(
        f'<div class="stat-tile"><div class="num tab-nums{" " + cls if cls else ""}">{esc(num)}</div>'
        f'<div class="label">{esc(label)}</div></div>' for num, label, cls in tiles) + "\n      "


# ---------------------------------------------------------------- tested models

VENDOR_BY_PREFIX = (("claude", "Anthropic"), ("gpt", "OpenAI"), ("o1", "OpenAI"), ("gemini", "Google"), ("grok", "xAI"),
                    ("glm", "Z.ai"), ("llama", "Meta"), ("deepseek", "DeepSeek"), ("qwen", "Alibaba"), ("kimi", "Moonshot"))


def vendor_of(model: str, fallback: str) -> str:
    return next((v for prefix, v in VENDOR_BY_PREFIX if model.lower().startswith(prefix)), fallback)


def tested(data: dict) -> str:
    """The exact models in the latest published run, with version, access path and effort, above the fold."""
    if data["status"] != "published":
        return ""
    first, rest = _suites(data)
    public = data["suites"][first]
    cands = public["candidates"]
    held = data["suites"][rest[0]]["n_tasks"] if rest else 0
    efforts = sorted({c["effort"] for c in cands.values()})
    items = []
    for n in public["ranking"]:
        c = cands[n]
        version = re.sub(r"\s*\(.*?\)|^codex-cli\s+", "", c.get("cli_version") or "").strip()
        via = _label(c["provider"]) + (f" {version}" if version else "")
        items.append(f'<li class="model"><span class="vendor">{esc(vendor_of(c["model"], n))}</span>'
                     f'<b class="mono">{esc(c["model"])}</b><span class="via">{esc(via)}</span></li>')
    ag = (data.get("agreement") or {}).get(first)
    judges = f"Judged by <b>{esc(_judge_name(public['judge']))}</b>" + (
        f", cross-checked by <b>{esc(_judge_name(ag['judge_second']))}</b>" if ag else "")
    effort = efforts[0] if len(efforts) == 1 else "mixed"
    meta = (f"Latest run {esc(data['generated'][:10])} · {public['n_tasks'] + held} tickets"
            + (f" ({held} held-out)" if held else "") + f" · reasoning effort {esc(effort)}")
    return (f'\n      <p class="tested-label">Models tested in the latest run</p>\n      <ul class="tested-models">\n        '
            + "\n        ".join(items) + f'\n      </ul>\n      <p class="tested-meta">{meta}. {judges}.</p>\n    ')


# ---------------------------------------------------------------- sources

# Primary pages for each vendor's model names and versions, checked 2026-09-29. Only vendors' own pages
# (plus one methodology write-up); a vendor group is marked "tested" when its model is in the latest run.
SOURCES = {
    "Anthropic": [("Claude Opus 5.5 announcement", "https://www.anthropic.com/claude-opus-5-5"),
                  ("Claude Opus 5 announcement", "https://www.anthropic.com/news/claude-opus-5"),
                  ("Claude Sonnet 5.5 announcement", "https://www.anthropic.com/claude-sonnet-5-5"),
                  ("Claude models overview", "https://platform.claude.com/docs/en/models/overview")],
    "OpenAI": [("Introducing GPT-6 Sol and Luna", "https://openai.com/index/introducing-gpt-6-sol-and-luna/"),
               ("Latest model guidance", "https://developers.openai.com/api/docs/guides/latest-model"),
               ("API model list", "https://developers.openai.com/api/docs/models")],
    "Google": [("Gemini API models", "https://ai.google.dev/gemini-api/docs/models"),
               ("Gemini API release notes", "https://ai.google.dev/gemini-api/docs/changelog")],
    "Meta": [("Introducing Muse", "https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/"),
             ("Muse Spark for developers", "https://developer.meta.com/ai/models/muse-spark/"),
             ("Meta Model API", "https://developer.meta.com/ai/products/meta-model-api/")],
}
FURTHER_READING = [("How Databricks rolls out frontier models to its employees on day 1",
                    "https://www.databricks.com/blog/how-databricks-rolls-out-frontier-models-14000-employees-day-1")]


def sources(data: dict) -> str:
    tested_vendors = set()
    if data["status"] == "published":
        public = data["suites"][next(iter(data["suites"]))]
        tested_vendors = {vendor_of(c["model"], n) for n, c in public["candidates"].items()}
    head = ('\n    <div class="sec-head reveal">\n      <p class="eyebrow">Sources</p>\n'
            '      <h2>Where the model names and versions come from</h2>\n'
            '      <p class="dek">Each vendor\'s own pages, so you can check what is current. Names and versions were last checked '
            'in September 2026 and will drift as vendors ship.</p>\n    </div>\n')
    groups = []
    for vendor, links in SOURCES.items():
        badge = ('<span class="pill ok">tested in the latest run</span>' if vendor in tested_vendors
                 else '<span class="pill">not tested here</span>')
        items = "".join(f'<li><a href="{esc(u)}" target="_blank" rel="noopener">{esc(t)}</a></li>' for t, u in links)
        groups.append(f'<div class="src-group"><h3>{esc(vendor)} {badge}</h3><ul>{items}</ul></div>')
    further = "".join(f'<li><a href="{esc(u)}" target="_blank" rel="noopener">{esc(t)}</a></li>' for t, u in FURTHER_READING)
    return (head + '    <div class="src-grid reveal">\n      ' + "\n      ".join(groups)
            + f'\n      <div class="src-group"><h3>Further reading</h3><ul>{further}</ul></div>\n    </div>\n  ')


# ---------------------------------------------------------------- history

HISTORY_URL = "https://github.com/gpatwa/multi-agent-eval/blob/main/docs/data/history.json"
CHANGE_TEXT = {
    "added": "added", "removed": "removed", "judge_changed": "judge changed",
    "leader_changed": lambda c: f"leader {c['from']} → {c['to']}",
    "model_changed": lambda c: f"model {c['from']} → {c['to']}",
    "quality_up": lambda c: f"quality +{c['delta']:.2f}", "quality_down": lambda c: f"quality {c['delta']:.2f}",
    "flags_up": lambda c: f"flags {c['from']} → {c['to']}", "flags_down": lambda c: f"flags {c['from']} → {c['to']}",
}


def _change(c: dict) -> str:
    text = CHANGE_TEXT[c["kind"]]
    text = text(c) if callable(text) else text
    who = "" if c["candidate"] == "*" else f"{c['candidate']}: "
    bad = c["kind"] in ("quality_down", "flags_up")
    return f'<span class="chg{" bad" if bad else ""}">{esc(who + text)}</span>'


def history(data: dict) -> str:
    entries = (data.get("history") or {}).get("entries") or []
    head = ('\n    <div class="sec-head reveal">\n      <p class="eyebrow">Published history</p>\n'
            '      <h2>Every published run stays on the record</h2>\n    </div>\n')
    if not entries:
        return head + '    <p class="proof-note">The first row appears here when a run is published.</p>\n  '
    rows = []
    for e in reversed(entries):
        pub = e["suites"][next(iter(e["suites"]))]
        lead = pub["ranking"][0]
        cands = pub["candidates"]
        quality = " · ".join(f"{esc(n)} {cands[n]['quality_mean']:.2f}" for n in pub["ranking"])
        flags = sum(v["critical_violations"] for s in e["suites"].values() for v in s["candidates"].values())
        models = " · ".join(esc(cands[n]["model"]) for n in pub["ranking"])
        changes = " ".join(_change(c) for c in e["changes"]) or ('<span class="chg">first published run</span>'
                                                                    if e is entries[0] else '<span class="chg">no change</span>')
        rows.append(f'<tr><th scope="row">{esc(e["published"][:10])}</th><td>{esc(lead)}</td><td>{quality}</td>'
                    f'<td>{flags}</td><td class="mod">{models}</td><td>{changes}</td></tr>')
    return (head + '    <div class="chart-card reveal">\n      <div class="topic-table"><table class="history-table">\n        <thead><tr>'
            '<th>Published</th><th>Leader</th><th>Quality</th><th>Flags</th><th>Models</th><th>Changes</th></tr></thead>\n        <tbody>\n          '
            + "\n          ".join(rows) + '\n        </tbody>\n      </table></div>\n'
            f'      <p class="quality-note">A row is added only when a model, score, judge or flag count changes. '
            f'<a href="{HISTORY_URL}">Raw history</a>.</p>\n    </div>\n  ')


# ---------------------------------------------------------------- hero panels


def score_basis(suite: dict) -> str:
    """What the composite is made of, from the run's own weights (zero-weight parts are not named)."""
    w = {k: v for k, v in (suite.get("weights") or {}).items() if v}
    total = sum(w.values()) or 1
    return ", ".join(f"{v / total:.0%} {k}" for k, v in sorted(w.items(), key=lambda kv: -kv[1]))


def panels(data: dict) -> str:
    """Three instrument panels under the hero. Pending runs show empty tracks and no figures."""

    def panel(title: str, body: str) -> str:
        return f'\n      <div class="panel"><h3>{esc(title)}</h3>{body}</div>'

    if data["status"] != "published":
        rows = "".join('<div class="prow wait"><span class="nm">awaiting run</span><div class="tr"><i></i></div><span>&nbsp;</span></div>'
                       for _ in range(3))
        return (panel("Scoreboard", rows + '<p class="sub">Figures appear when a run passes validation.</p>')
                + panel("Judge agreement", '<div class="big">&mdash;</div><div><span class="pill">pending</span></div>')
                + panel("Guardrail flags", '<div class="big">&mdash;</div><div><span class="pill">pending</span></div>') + "\n    ")
    first, _ = _suites(data)
    public = data["suites"][first]
    def flag_mark(n: str) -> str:
        k = public["candidates"][n]["critical_violations"]
        return f' <span class="fl">{k} flag{"s" if k != 1 else ""}</span>' if k else ""

    rows = "".join(
        f'<div class="prow"><span class="nm">{esc(n)}{flag_mark(n)}</span><div class="tr"><i style="--w:{public["candidates"][n]["composite"] * 100:.0f}%"></i></div>'
        f'<span>{public["candidates"][n]["composite"]:.2f}</span></div>' for n in public["ranking"])
    ag = (data.get("agreement") or {}).get(first)
    if ag and ag["rank_rho"] is not None:
        agree = (f'<div class="big">&rho; {ag["rank_rho"]:.2f}</div><div><span class="pill ok">two vendors</span></div>'
                 f'<p class="sub">Two different-vendor judges, {ag["n_answers"]} answers.</p>')
    else:
        agree = '<div class="big">&mdash;</div><div><span class="pill">one judge</span></div>'
    all_c = [c for s in data["suites"].values() for c in s["candidates"].values()]
    flags = sum(c["critical_violations"] for c in all_c)
    answers = sum(c["n_samples"] for c in all_c)
    flag = (f'<div class="big">{flags}</div><div><span class="pill {"ok" if flags == 0 else "bad"}">'
            f'{"none raised" if flags == 0 else "review"}</span></div><p class="sub">Across {answers} graded answers.</p>')
    return (panel("Composite score", rows + f'<p class="sub">{esc(score_basis(public).capitalize())}. Any guardrail flag ranks a model below every clean one. '
                                          f'{esc(_judge_name(public["judge"]))} judging.</p>')
            + panel("Judge agreement", agree) + panel("Guardrail flags", flag) + "\n    ")


# ---------------------------------------------------------------- walkthrough


def walkthrough(data: dict) -> str:
    head = ('\n    <div class="sec-head reveal">\n      <p class="eyebrow">Real run output, not a mockup</p>\n'
            '      <h2>Watch the evaluation work, on one real ticket</h2>\n')
    w = data.get("walkthrough")
    if data["status"] != "published" or not w:
        return head + ('      <p class="dek">Reference results are being refreshed at matched reasoning effort. This section fills in '
                       'automatically, with one real ticket and every candidate\'s scored reply, when the run completes and '
                       'passes validation.</p>\n    </div>\n  ')
    public = data["suites"][next(iter(data["suites"]))]
    n_dims = len(w["cards"][0]["scores"]) if w["cards"] else 0
    effort = next(iter({c["effort"] for c in public["candidates"].values()}), "default")
    gold = w["gold"]
    dek = (f'Every row in the scorecard further down comes from {public["n_tasks"]} public tickets scored like this one — '
           f'“{esc(w["subject"])}”, from the reference workload — run at matched <b>{esc(effort)}</b> reasoning effort '
           f'and graded on the same {n_dims} dimensions by the same judge.')
    cards = []
    for c in w["cards"]:
        ok = c["category"].lower().replace("_", "") == str(gold.get("category", "")).lower().replace("_", "")
        route = (f'<div class="case-route match">✓ Routed: {esc(c["category"])} — matches gold</div>' if ok else
                 f'<div class="case-route mismatch">✗ Routed: {esc(c["category"])} — gold is {esc(gold.get("category"))}</div>')
        rows = "\n".join(
            f'          <div class="dim-row"><span class="dim-label">{esc(DIM_LABEL.get(d, d))}</span>'
            f'<span class="dim-bar"><i style="--w:{score * 20}%"></i></span><span class="dim-val">{score}</span></div>'
            for d, score in c["scores"].items())
        says = f'\n        <p class="case-rationale">{esc(c["judge_says"])}</p>' if c["judge_says"] else ""
        cards.append(
            f'      <div class="case-card reveal">\n'
            f'        <div class="case-head"><span class="case-model">{esc(c["candidate"])}</span>'
            f'<span class="case-overall">{c["overall"]:.2f}</span></div>\n        {route}\n'
            f'        <blockquote class="case-reply">“{esc(c["reply_excerpt"])}”</blockquote>\n'
            f'        <div class="case-dims">\n{rows}\n        </div>{says}\n      </div>')
    return (head + f'      <p class="dek">{dek}</p>\n    </div>\n\n'
            f'    <div class="ticket-card reveal">\n      <div class="ticket-meta">\n        <span>Subject: {esc(w["subject"])}</span>\n'
            f'        <span class="gold">Gold routing: <b>{esc(gold.get("category"))} / {esc(gold.get("priority"))}</b></span>\n      </div>\n'
            f'      <p class="ticket-body">“{esc(w["body"])}”</p>\n    </div>\n\n'
            f'    <div class="case-grid">\n' + "\n".join(cards) + "\n    </div>\n  ")


# ---------------------------------------------------------------- results (proof) section


def _stamp(color: str, glyph: str, bold: str, text: str) -> str:
    return (f'      <div class="stamp">\n        <svg width="52" height="52" viewBox="0 0 52 52" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">\n'
            f'          <circle cx="26" cy="26" r="24" stroke="var(--{color})" stroke-width="1.4" stroke-dasharray="2 3"/>\n'
            f'          <circle cx="26" cy="26" r="18.5" stroke="var(--{color})" stroke-width="1.4"/>\n          {glyph}\n        </svg>\n'
            f'        <span class="label"><b>{esc(bold).replace("ρ", "<span class=\"rho\">ρ</span>")}</b>{esc(text)}</span>\n      </div>')


def _glyph(color: str, text: str) -> str:
    return (f'<text x="26" y="31" text-anchor="middle" font-family="IBM Plex Mono, monospace" font-size="15" '
            f'font-weight="600" fill="var(--{color})">{esc(text)}</text>')


CHECK = '<path d="M18 26.5l5.4 5.4L35 20" stroke="var(--brass)" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>'


def _chart(rows: list[dict], two_judges: bool) -> str:
    n = len(rows)
    height = 35 + 70 * n
    parts = [f'<svg class="bc-anim-target" data-reveal-class="bc-anim" viewBox="0 0 880 {height}" role="img"\n'
             f'             aria-label="{esc(_aria(rows, two_judges))}">',
             f'          <line class="bc-grid" x1="190" y1="40" x2="190" y2="{height - 7}"/>',
             f'          <line class="bc-grid" x1="525" y1="40" x2="525" y2="{height - 7}"/>',
             f'          <line class="bc-grid" x1="860" y1="40" x2="860" y2="{height - 7}"/>',
             '          <text class="bc-tick" x="190" y="30" text-anchor="start">0</text>',
             '          <text class="bc-tick" x="525" y="30" text-anchor="middle">0.5 composite</text>',
             '          <text class="bc-tick" x="860" y="30" text-anchor="end">1.0</text>']
    for i, r in enumerate(rows):
        y = 70 * i
        wa = max(2, round(r["a"] * 670))
        g = [f'          <g class="bc-g{min(i + 1, 3)}">',
             f'            <text class="bc-name" x="182" y="{66 + y}" text-anchor="end">{esc(r["name"])}</text>',
             f'            <text class="bc-model" x="182" y="{86 + y}" text-anchor="end">{esc(r["model"])}</text>',
             f'            <rect class="bc-bar-a" x="190" y="{54 + y}" width="{wa}" height="16" rx="3"/>']
        if two_judges:
            wb = max(2, round(r["b"] * 670))
            g.append(f'            <rect class="bc-bar-b" x="190" y="{74 + y}" width="{wb}" height="16" rx="3"/>')
        g.append(_value(wa, 66 + y, r["a"], "a"))
        if two_judges:
            g.append(_value(wb, 86 + y, r["b"], "b"))
        g.append("          </g>")
        parts += ["", *g]
    parts.append("        </svg>")
    return "\n".join(parts)


CHART_RIGHT = 878  # a value label needs ~40px; past this it goes inside the bar end instead


def _value(width: int, y: int, value: float, which: str) -> str:
    if 190 + width + 6 + 40 > CHART_RIGHT:
        return (f'            <text class="bc-value bc-value-{which} bc-value-in" x="{190 + width - 8}" y="{y}" '
                f'text-anchor="end">{value:.3f}</text>')
    return f'            <text class="bc-value bc-value-{which}" x="{190 + width + 6}" y="{y}">{value:.3f}</text>'


def _aria(rows: list[dict], two_judges: bool) -> str:
    if two_judges:
        body = ", ".join(f"{r['name']} scores {r['a']:.3f} and {r['b']:.3f}" for r in rows)
        return f"Composite score by candidate, scored by two judges. {body}."
    return "Composite score by candidate. " + ", ".join(f"{r['name']} scores {r['a']:.3f}" for r in rows) + "."


def _quality_note(public: dict) -> str:
    cands, ranking = public["candidates"], public["ranking"]
    best = max(cands, key=lambda n: cands[n]["quality_mean"])
    by_composite = sorted(cands, key=lambda n: -cands[n]["composite"])
    cli = any("CLI" in _label(c["provider"]) for c in cands.values())
    tail = " Latency for CLI candidates includes agent-CLI startup, so read it as pipeline time, not model speed." if cli else ""
    if not (public.get("weights") or {}).get("cost"):
        tail += (" Cost is not scored in this run: the subscription CLIs report no usable token counts."
                 if cli else " Cost is not scored in this run.")
    b = cands[best]
    out = f"Composite is {score_basis(public)}"
    if best == by_composite[0]:
        out += f" — <b>{esc(best)} leads on both</b> raw reply quality ({b['quality_mean']:.2f}/5) and the composite."
    else:
        others = [c["latency_p95"] for n, c in cands.items() if n != best]
        out += (f" — <b>{esc(best)} has the highest raw reply quality</b> "
                f"({b['quality_mean']:.2f}/5 vs {', '.join(f'{esc(n)}’s {c['quality_mean']:.2f}' for n, c in cands.items() if n != best)}) "
                f"but ranks {ordinal(by_composite.index(best) + 1)} on the composite: its p95 latency is {b['latency_p95']:.1f}s vs "
                f"{min(others):.1f}–{max(others):.1f}s for the others.")
    if ranking != by_composite:
        flagged = [f"{esc(n)} ({cands[n]['critical_violations']})" for n in by_composite if cands[n]["critical_violations"]]
        out += (f" Ranking puts any candidate with a guardrail flag below every clean one, so the order above differs from the "
                f"composite order. Flagged: {', '.join(flagged)}.")
    return out + tail


def _topics(public: dict) -> str:
    """Quality per topic per candidate: the composite's average can hide a topic where the leader is weak."""
    topics = public.get("topics") or {}
    names = [n for n in public["ranking"]]
    if len(topics) < 2:
        return ""
    head = "".join(f"<th>{esc(n)}</th>" for n in names)
    rows = []
    for topic, t in topics.items():
        cells = {n: t["candidates"].get(n, {}).get("quality_mean") for n in names}
        best = max((v for v in cells.values() if v is not None), default=None)
        tds = "".join(f'<td class="{"best" if v is not None and v == best else ""}">{"" if v is None else f"{v:.2f}"}</td>'
                      for v in cells.values())
        rows.append(f'<tr><th scope="row">{esc(topic.replace("_", " "))}</th><td>{t["n_tasks"]}</td>{tds}</tr>')
    return ('\n    <div class="chart-card reveal">\n      <p class="quality-note"><b>Quality by topic</b> (1 to 5, judge score). '
            f'The composite averages these; a leader can still be the weakest on one topic. Scored on {esc(score_basis(public))}.</p>\n      <div class="topic-table"><table>\n        <thead><tr><th>Topic</th><th>Tasks</th>'
            f'{head}</tr></thead>\n        <tbody>\n          ' + "\n          ".join(rows) + '\n        </tbody>\n      </table></div>\n    </div>\n')


def proof(data: dict) -> str:
    head = '\n    <div class="sec-head reveal">\n      <p class="eyebrow">Case study</p>\n      <h2>The reference workload: support-ticket triage</h2>\n    </div>\n'
    if data["status"] != "published":
        return head + ('    <p class="proof-note">Ships with a support inbox for <b>Northwind Cloud</b> — a synthetic reference company '
                       'and a real refund/priority policy. Results are being refreshed at matched reasoning effort; the scorecard '
                       'appears here automatically once a run completes and passes validation.</p>\n  ')
    first, rest = _suites(data)
    public = data["suites"][first]
    held = data["suites"][rest[0]] if rest else None
    ag = (data.get("agreement") or {}).get(first)
    effort = next(iter({c["effort"] for c in public["candidates"].values()}))
    judge1 = _judge_name(public["judge"])
    judged = f"judged by {esc(judge1)}" + (f" and cross-checked by {esc(_judge_name(ag['judge_second']))}" if ag else "")
    note = (f'    <p class="proof-note">\n      Ships with a support inbox for <b>Northwind Cloud</b> — a synthetic reference company, a real '
            f'refund/priority policy, {public["n_tasks"]} public tickets ({public["n_guardrail"]} adversarial)'
            + (f' plus {held["n_tasks"]} held-out tickets kept out of this repo' if held else "")
            + f' — every candidate at matched <b>{esc(effort)}</b> reasoning effort, {judged}.\n    </p>\n')
    rows = [{"name": n, "model": f'{public["candidates"][n]["model"]} · {_label(public["candidates"][n]["provider"])}',
             "a": public["candidates"][n]["composite"], "b": (ag or {}).get("composite_second_judge", {}).get(n, 0)}
            for n in public["ranking"]]
    legend = f'<span class="sw a"><i></i>Judge: {esc(_judge_name(public["judge"]))}</span>' + (
        f'\n        <span class="sw b"><i></i>Judge: {esc(_judge_name(ag["judge_second"]))}</span>' if ag else "")
    chart = (f'\n    <div class="chart-card reveal">\n      <div class="chart-legend">\n        {legend}\n      </div>\n'
             f'      <div class="bar-chart">\n        {_chart(rows, bool(ag))}\n      </div>\n'
             f'      <p class="quality-note">{_quality_note(public)}</p>\n'
             f'      <p class="quality-note">Check the numbers: <a href="{RESULTS_URL}/{first}/report.md">full per-ticket report and run settings</a>'
             + "".join(f' · <a href="{RESULTS_URL}/{n}/summary.json">{esc(n)} suite aggregates</a>' for n in rest)
             + '.</p>\n    </div>\n')
    chart += _topics(public)
    stamps = []
    if ag and ag["rank_rho"] is not None:
        same = ag["rank_rho"] == 1.0
        within = min((d["within1"] for d in ag["dimensions"].values()), default=None)
        level = ("" if within is None else
                 f" and land within one point of each other on every one of {ag['n_answers']} answers" if within == 1.0 else
                 f" and land within one point of each other on at least {within:.0%} of {ag['n_answers']} answers")
        text = f"Two different-vendor judges rank the candidates {'identically' if same else 'with strong but imperfect agreement'}{level}."
        stamps.append(_stamp("brass", CHECK, f"Spearman ρ = {ag['rank_rho']:.2f}", text))
    if held:
        gaps = {n: abs(public["candidates"][n]["quality_mean"] - held["candidates"][n]["quality_mean"])
                for n in public["candidates"] if n in held["candidates"]}
        gap = max(gaps.values()) if gaps else None
        if gap is not None:
            verdict = (" — no sign of overfitting to the public tickets." if gap <= MAX_HELDOUT_GAP_OK
                       else " — a gap worth investigating before trusting the public scores.")
            stamps.append(_stamp("brass", _glyph("brass", "Δ"), f"Held-out gap ≤ {gap:.2f}",
                                 f"Every candidate's quality on the {held['n_tasks']} held-out tickets is within {gap:.2f} of its public-suite score{verdict}"))
    all_c = [c for s in data["suites"].values() for c in s["candidates"].values()]
    flags = sum(c["critical_violations"] for c in all_c)
    answers = sum(c["n_samples"] for c in all_c)
    if flags == 0:
        stamps.append(_stamp("good", _glyph("good", "0"), "Zero violations",
                             f"No candidate promised a forbidden refund, leaked the policy, or followed an injected instruction in {answers} graded answers."))
    else:
        kinds = sorted({k for c in all_c for k in c["flag_counts"]})
        stamps.append(_stamp("critical", _glyph("critical", str(flags)), f"{flags} guardrail flag{'s' if flags != 1 else ''}",
                             f"Raised across {answers} graded answers ({', '.join(kinds)}). Flags are launch-gate events, reported as counts and never averaged away."))
    return head + note + chart + '\n    <div class="verdict-row reveal">\n' + "\n".join(stamps) + "\n    </div>\n  "


# ---------------------------------------------------------------- method item iii


def method_judges(data: dict) -> str:
    ag = data["status"] == "published" and any((data.get("agreement") or {}).values())
    if ag:
        h, p = ("Every ranking is cross-checked by a second, different-vendor judge",
                "We report the rank correlation between the two — a ranking that's only defensible to its own vendor's model gets caught.")
    else:
        h, p = ("Every run reports how it was judged",
                "Judge coverage, reasoning effort, model IDs and CLI versions are recorded with every run, and results with too many unjudged answers are never published.")
    return (f'\n      <div class="method-item reveal">\n        <span class="mk">iii.</span>\n'
            f'        <div><h3>{esc(h)}</h3>\n        <p>{esc(p)}</p></div>\n      </div>\n      ')


def readme_results(data: dict) -> str:
    """The README's results section, as markdown (same data and claims policy as the page)."""
    when = data.get("generated", "")[:10]
    if data["status"] != "published":
        return ("\n## Latest result\n\nReference results are being refreshed at matched reasoning effort across all "
                "candidates. This section is generated from [`docs/data/results.json`](docs/data/results.json) and fills in "
                "automatically when a run completes and passes validation (`python scripts/pipeline.py tick`).\n")
    first, rest = _suites(data)
    public = data["suites"][first]
    held = data["suites"][rest[0]] if rest else None
    ag = (data.get("agreement") or {}).get(first)
    effort = next(iter({c["effort"] for c in public["candidates"].values()}))
    judged = f"judged by {_judge_name(public['judge'])}" + (f" and cross-checked by {_judge_name(ag['judge_second'])}" if ag else "")
    lines = [f"\n## Latest result ({when})\n",
             f"Support-triage benchmark on {public['n_tasks']} public tickets ({public['n_guardrail']} adversarial)"
             + (f" plus {held['n_tasks']} held-out tickets kept out of this repo" if held else "")
             + f", every candidate at matched **{effort}** reasoning effort, {judged}. Generated from "
             "[`docs/data/results.json`](docs/data/results.json); reports: "
             f"[public suite](docs/results/{first}/report.md)"
             + "".join(f", [{n} aggregates](docs/results/{n}/summary.json)" for n in rest) + ".\n",
             "| Candidate | Model | Composite | Quality (±95% CI) | Latency p50 / p95 | Guardrail flags |", "|---|---|---|---|---|---|"]
    for n in public["ranking"]:
        c = public["candidates"][n]
        lines.append(f"| {n} | `{c['model']}` ({_label(c['provider'])}) | {c['composite']:.3f} | {c['quality_mean']:.2f} ± {c['quality_ci95']:.2f} "
                     f"| {c['latency_p50']:.1f}s / {c['latency_p95']:.1f}s | {c['critical_violations']} |")
    lines.append("")
    if held:
        lines.append("Held-out suite (same policy, different wording): " + "; ".join(
            f"{n} {held['candidates'][n]['quality_mean']:.2f} (public {public['candidates'][n]['quality_mean']:.2f})"
            for n in public["ranking"] if n in held["candidates"]) + " quality.\n")
    bullets = []
    if ag and ag["rank_rho"] is not None:
        bullets.append(f"Two different-vendor judges agree on the ranking (Spearman ρ = {ag['rank_rho']:.2f}) across {ag['n_answers']} answers.")
    all_c = [c for s_ in data["suites"].values() for c in s_["candidates"].values()]
    flags = sum(c["critical_violations"] for c in all_c)
    answers = sum(c["n_samples"] for c in all_c)
    if flags == 0:
        bullets.append(f"**Zero guardrail flags** in {answers} graded answers.")
    else:
        kinds = sorted({k for c in all_c for k in c["flag_counts"]})
        bullets.append(f"**{flags} guardrail flag{'s' if flags != 1 else ''}** in {answers} graded answers ({', '.join(kinds)}); flags are reported as counts, never averaged away.")
    bullets.append(html.unescape(re.sub(r"</?b>", "**", _quality_note(public))))
    return "\n".join(lines + [f"* {b}" for b in bullets]) + "\n"


# ---------------------------------------------------------------- <head>, sitemap, robots: everything that names the site's URL

TITLE = "LLM Vendor Evaluation on Your Own Tickets | The Model Ledger"  # must equal the page's static <title>
DESCRIPTION = ("Compare Claude, GPT and Gemini on your own support tickets and policy. Get a ranked scorecard of quality, "
               "latency, cost and guardrail violations. Open source.")
KEYWORDS = ("LLM evaluation, model comparison, LLM-as-judge, support ticket triage, AI guardrails, "
            "prompt injection testing, Claude, GPT, Gemini")
REPO_URL = "https://github.com/gpatwa/multi-agent-eval"
OG_ALT = "The Model Ledger: compare LLM vendors on your own tickets and policy"


def site_base(data: dict) -> str:
    return (data.get("site_url") or "").rstrip("/")


def head(data: dict) -> str:
    """The SEO / social block of <head>. URLs come from `site_url`, so moving the site to another domain is one
    config line; without a site URL the URL-bearing tags are simply left out (never a wrong domain)."""
    import json

    base = site_base(data)
    a = lambda text: html.escape(str(text), quote=True)  # noqa: E731
    lines = [f'<meta name="description" content="{a(DESCRIPTION)}">']
    if base:
        lines.append(f'<link rel="canonical" href="{a(base)}/">')
    lines.append('<meta property="og:type" content="website">')
    if base:
        lines.append(f'<meta property="og:url" content="{a(base)}/">')
    lines += [f'<meta property="og:title" content="{a(TITLE)}">', f'<meta property="og:description" content="{a(DESCRIPTION)}">',
              '<meta name="twitter:card" content="summary_large_image">', '<meta property="og:site_name" content="The Model Ledger">',
              '<meta property="og:locale" content="en_US">']
    if base:
        lines.append(f'<meta property="og:image" content="{a(base)}/og-image.png">')
    lines += ['<meta property="og:image:width" content="1200">', '<meta property="og:image:height" content="630">',
              f'<meta property="og:image:alt" content="{a(OG_ALT)}">']
    if base:
        lines.append(f'<meta name="twitter:image" content="{a(base)}/og-image.png">')
    if base and (data.get("history") or {}).get("entries"):
        lines.append(f'<link rel="alternate" type="application/atom+xml" title="The Model Ledger: published runs" href="{a(base)}/feed.xml">')
    lines += ['<meta name="theme-color" content="#171B22">', '<meta name="robots" content="index, follow">',
              '<link rel="icon" href="/favicon.svg" type="image/svg+xml">']
    site = {"@type": "WebSite", **({"@id": f"{base}/#website", "url": f"{base}/"} if base else {}), "name": "The Model Ledger", "inLanguage": "en"}
    software = {"@type": "SoftwareSourceCode", **({"@id": f"{base}/#software"} if base else {}), "name": "The Model Ledger (multi-agent-eval)",
                "description": DESCRIPTION, **({"url": f"{base}/"} if base else {}), "codeRepository": REPO_URL,
                "license": "https://opensource.org/licenses/MIT", "programmingLanguage": "Python", "runtimePlatform": "Python 3.13", "keywords": KEYWORDS}
    ld = json.dumps({"@context": "https://schema.org", "@graph": [site, software]}, indent=1)
    lines.append(f'<script type="application/ld+json">\n{ld}\n</script>')
    return "\n" + "\n".join(lines) + "\n"


def sitemap(data: dict) -> str:
    base = site_base(data)
    urls = f"  <url>\n    <loc>{html.escape(base)}/</loc>\n  </url>\n" if base else ""
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{urls}</urlset>\n'


def robots(data: dict) -> str:
    base = site_base(data)
    return "User-agent: *\nAllow: /\n" + (f"\nSitemap: {base}/sitemap.xml\n" if base else "")


RENDERERS = {"head": head, "stats": stats, "panels": panels, "tested": tested, "sources": sources, "history": history, "walkthrough": walkthrough, "proof": proof, "method_judges": method_judges}
README_RENDERERS = {"readme_results": readme_results}


def render(page: str, data: dict, renderers: dict = RENDERERS) -> str:
    """Replace every AUTO region in `page` with its rendering of `data`."""
    seen = set()

    def sub(m):
        name = m.group(2)
        if name not in renderers:
            raise KeyError(f"unknown AUTO region {name!r}")
        seen.add(name)
        return f"{m.group(1)}{renderers[name](data)}{m.group(4)}"

    out = REGION.sub(sub, page)
    missing = set(renderers) - seen
    if missing:
        raise KeyError(f"page is missing AUTO region(s): {sorted(missing)}")
    return out


def render_readme(text: str, data: dict) -> str:
    return render(text, data, README_RENDERERS)


def generated_files(root, data: dict, history: dict | None = None) -> list[tuple]:
    """Every generated file under `root` and the text it should contain: AUTO regions spliced into the
    hand-written page and README, plus the wholly generated sitemap.xml and robots.txt. The single list
    shared by scripts/build_site.py (and CI's freshness check) and the release pipeline."""
    import pathlib

    from .history import load as load_history

    root = pathlib.Path(root)
    docs, page, readme = root / "docs", root / "docs" / "index.html", root / "README.md"
    data = {**data, "history": history if history is not None else load_history(docs / "data" / "history.json")}
    files = [(page, render(page.read_text(), data))]
    if readme.exists():
        files.append((readme, render_readme(readme.read_text(), data)))
    from .launch import feed

    extra = [(docs / "sitemap.xml", sitemap(data)), (docs / "robots.txt", robots(data))]
    atom = feed(data, data["history"])
    if atom:
        extra.append((docs / "feed.xml", atom))
    return files + extra
