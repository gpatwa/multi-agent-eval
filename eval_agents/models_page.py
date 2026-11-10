"""The Models page: what each vendor has released, and what vendors report about it, in one place.

docs/data/models.json holds the facts, each with the vendor page it came from and when it was checked.
This module renders them to docs/models.html, which is wholly generated: it reuses the stylesheet, header,
footer and script of docs/index.html, so the two pages never drift apart in design.

Claims policy, same as the rest of the site:
- Every row names its source. A model with no source is rejected, not shown.
- Vendor-reported benchmark numbers are labelled as reported by the vendor and not reproduced here, with
  the vendor's own caveats beside them. They are never mixed into the measured ranking.
- "Tested here" is derived from the published run, not typed in.
"""
from __future__ import annotations

import html
import json
import pathlib
import re
from datetime import date

SCHEMA = 1
TITLE = "Model Releases and Vendor-Reported Benchmarks | The Model Ledger"
DESCRIPTION = ("Current Claude, GPT and Gemini models with IDs, prices and release dates from the vendors' own pages, "
               "plus the benchmark numbers vendors report, kept separate from our measured results.")
STATUS_LABEL = {"latest": "latest", "current": "current", "earlier": "earlier", "legacy": "legacy", "restricted": "restricted access"}
STATUS_ORDER = {"latest": 0, "current": 1, "restricted": 2, "earlier": 3, "legacy": 4}

esc = html.escape


class ModelsDataError(ValueError):
    pass


def load(path) -> dict:
    path = pathlib.Path(path)
    return json.loads(path.read_text()) if path.exists() else {}


def validate(m: dict) -> None:
    """Refuse data that would put an unsourced or malformed claim on the page."""
    if not m:
        return
    if m.get("schema") != SCHEMA:
        raise ModelsDataError(f"models.json schema must be {SCHEMA}")
    date.fromisoformat(m["checked"])
    for e in m.get("models", []):
        for k in ("vendor", "name", "id", "status", "source"):
            if not e.get(k):
                raise ModelsDataError(f"model entry missing {k!r}: {e}")
        if e["status"] not in STATUS_LABEL:
            raise ModelsDataError(f"{e['id']}: unknown status {e['status']!r}")
        if not e["source"].startswith("https://"):
            raise ModelsDataError(f"{e['id']}: source must be an https URL")
        if e.get("released"):
            date.fromisoformat(e["released"])
    for c in m.get("claims", []):
        for k in ("title", "source", "date", "vendor", "models", "rows"):
            if not c.get(k):
                raise ModelsDataError(f"claims entry missing {k!r}: {c.get('title')}")
        date.fromisoformat(c["date"])
        unknown = set(c.get("model_ids", {})) - set(c["models"])
        if unknown:
            raise ModelsDataError(f"model_ids names models not in the table: {sorted(unknown)}")
        for r in c["rows"]:
            extra = set(r["scores"]) - set(c["models"])
            if extra:
                raise ModelsDataError(f"{r['benchmark']}: scores for models not in the table: {sorted(extra)}")


def _link(url: str, text: str) -> str:
    return f'<a href="{esc(url, quote=True)}" target="_blank" rel="noopener">{esc(text)}</a>'


def _tested(data: dict) -> set[str]:
    if data.get("status") != "published":
        return set()
    return {c["model"] for s in data["suites"].values() for c in s["candidates"].values()}


def _vendor_table(vendor: str, entries: list[dict], tested: set[str]) -> str:
    rows = []
    for e in sorted(entries, key=lambda e: (STATUS_ORDER[e["status"]], e["name"])):
        badge = ' <span class="pill ok">tested here</span>' if e["id"] in tested else ""
        status = f'<span class="pill{" ok" if e["status"] == "latest" else ""}">{esc(STATUS_LABEL[e["status"]])}</span>'
        note = f'<br><span class="mnote">{esc(e["note"])}</span>' if e.get("note") else ""
        rows.append(
            f'<tr><th scope="row">{esc(e["name"])}{badge}{note}</th><td class="l"><code>{esc(e["id"])}</code></td>'
            f'<td>{status}</td><td>{esc(e.get("released") or "not stated")}</td><td>{esc(e.get("price") or "not stated")}</td>'
            f'<td class="l">{_link(e["source"], "source")}</td></tr>')
    return (f'<div class="topic-table models reveal"><h3>{esc(vendor)}</h3><table><thead><tr><th>Model</th><th class="l">API ID</th>'
            f'<th>Status</th><th>Released</th><th>Price per 1M tokens (in / out)</th><th class="l">Vendor page</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def models_section(m: dict, data: dict) -> str:
    tested = _tested(data)
    by_vendor: dict[str, list[dict]] = {}
    for e in m.get("models", []):
        by_vendor.setdefault(e["vendor"], []).append(e)
    head = ('<div class="sec-head reveal"><p class="eyebrow">Model releases</p>'
            '<h2>What each vendor lists as current</h2>'
            f'<p class="dek">Read from each vendor\'s own pages, last checked {esc(m["checked"])}. "Tested here" means the model is a '
            'candidate in the latest published run. A blank release date means the vendor page does not give one.</p></div>')
    unrev = m.get("unreviewed") or {}
    seen = "".join(f'<p class="mnote">Listed on {esc(v)}\'s page but not yet reviewed here: {esc(", ".join(ids))}.</p>' for v, ids in unrev.items())
    return head + "".join(_vendor_table(v, es, tested) for v, es in by_vendor.items()) + seen


def _claim_table(c: dict) -> str:
    th = "".join(f"<th>{esc(n)}</th>" for n in c["models"])
    body = []
    for r in c["rows"]:
        cells = []
        for n in c["models"]:
            v = r["scores"].get(n)
            note = (r.get("notes") or {}).get(n)
            cells.append(f'<td>{esc(v) if v else "—"}' + (f'<br><span class="mnote">{esc(note)}</span>' if v and note else "") + "</td>")
        body.append(f'<tr><th scope="row">{esc(r["benchmark"])}<br><span class="mnote">{esc(r.get("area", ""))}</span></th>{"".join(cells)}</tr>')
    caveats = "".join(f"<li>{esc(x)}</li>" for x in c.get("caveats", []))
    return (f'<div class="claim reveal"><h3>{esc(c["title"])} <span class="pill">reported by {esc(c["vendor"])}</span></h3>'
            f'<p class="mnote">{esc(c["date"])} · {_link(c["source"], "source")}</p>'
            f'<div class="topic-table"><table><thead><tr><th>Benchmark</th>{th}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'
            f'<ul class="caveats">{caveats}</ul></div>')


def claims_section(m: dict) -> str:
    claims = m.get("claims", [])
    if not claims:
        return ""
    head = ('<div class="sec-head reveal"><p class="eyebrow">Vendor-reported benchmarks</p>'
            '<h2>What vendors say, kept apart from what we measured</h2>'
            '<p class="dek">These numbers come from the vendors\' own announcements. We did not run them and cannot vouch for the '
            'setups, so they are not part of our ranking. Each table keeps the vendor\'s own footnotes. Where one of these '
            'benchmarks is something this project can run, we say whether it has.</p></div>')
    return head + "".join(_claim_table(c) for c in claims)


def _num(text: str) -> float | None:
    m = re.match(r"\s*([\d.]+)", text or "")
    return float(m.group(1)) if m else None


def head_to_head(claim: dict, a: str, b: str) -> tuple[int, int, int]:
    """(a ahead, b ahead, tied) over the benchmarks where the claim reports both. Higher is better on every row."""
    wa = wb = tie = 0
    for r in claim["rows"]:
        x, y = _num(r["scores"].get(a)), _num(r["scores"].get(b))
        if x is None or y is None:
            continue
        wa, wb, tie = wa + (x > y), wb + (y > x), tie + (x == y)
    return wa, wb, tie


def combined_section(m: dict, data: dict) -> str:
    """Public numbers beside our own, pair by pair. Raw scores are never averaged across benchmarks (different
    scales, effort settings and model sets); the comparison is who leads whom, counted per benchmark, then set
    against who leads in our run. Only models both sides cover can be compared, and the section says which cannot."""
    if data.get("status") != "published":
        return ""
    suite = data["suites"][next(iter(data["suites"]))]
    ours = {c["model"]: c for c in suite["candidates"].values()}
    lines, covered = [], set()
    for claim in m.get("claims", []):
        ids = claim.get("model_ids", {})
        tested = [(n, i) for n, i in ids.items() if i in ours]
        covered |= {i for _, i in tested}
        for k, (na, ia) in enumerate(tested):
            for nb, ib in tested[k + 1:]:
                wa, wb, tie = head_to_head(claim, na, nb)
                n = wa + wb + tie
                if n < 3:
                    continue
                pub = (f"{na} ahead on {wa} of {n}, {nb} on {wb}" + (f", {tie} tied" if tie else ""))
                qa, qb = ours[ia]["quality_mean"], ours[ib]["quality_mean"]
                ca, cb = ours[ia]["composite"], ours[ib]["composite"]
                lead_q = na if qa > qb else nb
                lead_c = na if ca > cb else nb
                lead_pub = na if wa > wb else nb if wb > wa else None
                verdict = ("agrees with" if lead_pub == lead_q else "differs from") if lead_pub else "cannot be compared with"
                comp = (f" Overall, our composite puts {lead_c} first because it also scores latency, not just quality."
                        if lead_c != lead_q else "")
                lines.append(
                    f'<div class="src-group"><h3>{esc(na)} vs {esc(nb)}</h3>'
                    f'<p><strong>Vendor-reported ({esc(claim["vendor"])}), {n} benchmarks:</strong> {esc(pub)}.</p>'
                    f'<p><strong>Our run, {suite["n_tasks"]} support tickets:</strong> {esc(lead_q)} scores higher on reply quality '
                    f'({max(qa, qb):.2f} vs {min(qa, qb):.2f} out of 5), which {verdict} the public tally.{esc(comp)}</p></div>')
    missing = sorted(n for n, c in ((c["model"], c) for c in ours.values()) if n not in covered)
    gap = (f'<p class="mnote">Not comparable yet: {esc(", ".join(missing))} {"appears" if len(missing) == 1 else "appear"} in none of the public tables above, '
           'so there is nothing to set our result against.</p>') if missing else ""
    if not lines:
        return ""
    head = ('<div class="sec-head reveal"><p class="eyebrow">Public numbers beside ours</p>'
            '<h2>Does the public picture match what we measured?</h2>'
            '<p class="dek">We do not average scores across benchmarks: they use different scales, effort settings and model sets. '
            'Instead we count who leads whom on each public benchmark, then set that beside who leads on our own tickets. '
            'The public side is vendor-reported, so it can lean toward its author. Independent benchmarks are not included yet.</p></div>')
    return head + '<div class="src-grid reveal">' + "".join(lines) + "</div>" + gap


def ours_section(data: dict) -> str:
    if data.get("status") != "published":
        return ""
    return ('<div class="sec-head reveal"><p class="eyebrow">Our own numbers</p><h2>Measured on the same tickets, same effort</h2>'
            '<p class="dek">The ranking on the <a href="/#results">front page</a> is what this project measured: one workload '
            '(support-ticket triage), matched reasoning effort, two judges from different vendors. It is a different kind of '
            'evidence from the tables above.</p></div>')


def trackers_section(m: dict) -> str:
    items = m.get("trackers", [])
    if not items:
        return ""
    lis = "".join(f"<li>{_link(t['url'], t['name'])}</li>" for t in items)
    return ('<div class="sec-head reveal"><p class="eyebrow">Other trackers</p><h2>Secondary sources</h2>'
            '<p class="dek">Aggregators list releases faster than vendors confirm them, and they disagree with each other and with '
            'the vendor pages. Use them to spot a new name, then check the vendor page. Nothing on this site relies on them.</p></div>'
            f'<div class="src-group reveal"><ul>{lis}</ul></div>')


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i: text.index(end, i) + len(end)]


EXTRA_CSS = """
  .models h3, .claim h3{font-size:17px; font-weight:600; margin:28px 0 10px; display:flex; gap:10px; align-items:center; flex-wrap:wrap;}
  .models td, .models th{vertical-align:top; text-align:left;}
  .models td:nth-child(5), .models th:nth-child(5){text-align:right;}
  .models th[scope=row]{text-transform:none;}
  .models code{font-family:"Geist Mono",monospace; font-size:12.5px; white-space:nowrap;}
  .claim td, .claim th{vertical-align:top;}
  .mnote{font-size:12px; color:var(--slate); font-family:"Geist",sans-serif; font-weight:400;}
  .claim{margin-top:28px;} .claim .topic-table table th:not(:first-child){white-space:nowrap;}
  .claim th[scope=row]{text-transform:none;}
  .caveats{margin:10px 0 0; padding-left:18px; font-size:13.5px; color:var(--slate); display:grid; gap:4px;}
  .page-top{padding-top:56px;}
"""


def render_page(index_html: str, m: dict, data: dict, base: str) -> str:
    """The whole docs/models.html, built from the shared pieces of index.html plus the sections above."""
    style = _between(index_html, "<style>", "</style>").replace("</style>", EXTRA_CSS + "</style>")
    header = _between(index_html, "<header>", "</header>")
    header = re.sub(r'href="#(\w+)"', r'href="/#\1"', header)
    header = header.replace('href="/#top"', 'href="/"').replace('<a href="/models">', '<a href="/models" aria-current="page">')
    footer = _between(index_html, "<footer", "</footer>")
    script = _between(index_html, "<script>\n(function", "</script>")
    fonts = "\n".join(re.findall(r'<link (?:rel="preconnect"|href="https://fonts)[^>]*>', index_html))
    url = f"{base}/models" if base else ""
    meta = [f'<meta name="description" content="{esc(DESCRIPTION, quote=True)}">', '<meta name="robots" content="index, follow">',
            '<meta name="theme-color" content="#171B22">', '<link rel="icon" href="/favicon.svg" type="image/svg+xml">']
    if url:
        meta += [f'<link rel="canonical" href="{esc(url)}">', f'<meta property="og:url" content="{esc(url)}">',
                 f'<meta property="og:image" content="{esc(base)}/og-image.png">']
    meta += ['<meta property="og:type" content="website">', f'<meta property="og:title" content="{esc(TITLE, quote=True)}">',
             f'<meta property="og:description" content="{esc(DESCRIPTION, quote=True)}">', '<meta name="twitter:card" content="summary_large_image">']
    intro = ('<section class="wrap page-top"><div class="sec-head"><p class="eyebrow">Models</p>'
             '<h1>Models and releases, in one place</h1>'
             '<p class="dek">What Anthropic, OpenAI and Google currently list, what they report about it, and which of it we have '
             'measured ourselves. Each row links to the page it came from.</p></div></section>')
    sections = [models_section(m, data), claims_section(m), combined_section(m, data), ours_section(data), trackers_section(m)]
    body = "\n".join(f'  <section class="wrap-wide">{s}</section>' for s in sections if s)
    return (f'<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            + "\n".join(meta) + f'\n<title>{esc(TITLE)}</title>\n{fonts}\n{style}\n</head>\n<body>\n\n{header}\n\n<main id="top">\n{intro}\n{body}\n</main>\n\n'
            f'{footer}\n\n{script}\n</body>\n</html>\n')
