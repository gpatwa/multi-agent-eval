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
    cli = any("CLI" in _label(c["provider"]) for c in cands.values())
    tail = " Latency for CLI candidates includes agent-CLI startup, so read it as pipeline time, not model speed." if cli else ""
    if best == ranking[0]:
        b = cands[best]
        return (f"Composite blends quality, latency, and cost — <b>{esc(best)} leads on both</b> raw reply quality "
                f"({b['quality_mean']:.2f}/5) and the composite.{tail}")
    others = [c["latency_p95"] for n, c in cands.items() if n != best]
    b = cands[best]
    return (f"Composite blends quality, latency, and cost — <b>{esc(best)} has the highest raw reply quality</b> "
            f"({b['quality_mean']:.2f}/5 vs {', '.join(f'{esc(n)}’s {c['quality_mean']:.2f}' for n, c in cands.items() if n != best)}) "
            f"but ranks {ordinal(ranking.index(best) + 1)} on the composite: its p95 latency is {b['latency_p95']:.1f}s vs "
            f"{min(others):.1f}–{max(others):.1f}s for the others.{tail}")


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
             f'      <p class="quality-note">{_quality_note(public)}</p>\n    </div>\n')
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


RENDERERS = {"stats": stats, "walkthrough": walkthrough, "proof": proof, "method_judges": method_judges}


def render(page: str, data: dict) -> str:
    """Replace every AUTO region in `page` with its rendering of `data`."""
    seen = set()

    def sub(m):
        name = m.group(2)
        if name not in RENDERERS:
            raise KeyError(f"unknown AUTO region {name!r}")
        seen.add(name)
        return f"{m.group(1)}{RENDERERS[name](data)}{m.group(4)}"

    out = REGION.sub(sub, page)
    missing = set(RENDERERS) - seen
    if missing:
        raise KeyError(f"page is missing AUTO region(s): {sorted(missing)}")
    return out
