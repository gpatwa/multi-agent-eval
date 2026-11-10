"""Keep docs/data/models.json current from the vendors' own pages.

Anthropic publishes one machine-readable page per model (…/models/<slug>/overview.md), so its rows are rewritten
from those pages: ID, status, release date and price come straight from the vendor, never from a tracker.
OpenAI and Google publish no such feed, so their rows stay curated, and this module only *watches* their model
pages: a model ID that appears on the page but is not in models.json is reported as "unreviewed", and a page
that cannot be read is reported as unreadable instead of as "no change". Nothing here invents a row.

Pure parsing is separate from fetching so the parsers are tested without a network.
"""
from __future__ import annotations

import json
import re
import urllib.request
from datetime import date, datetime

from .models_page import SCHEMA, validate

UA = "model-ledger-refresh (+https://github.com/gpatwa/multi-agent-eval)"  # Cloudflare-style filters reject Python's default UA
ANTHROPIC_DOCS = "https://platform.claude.com/docs/en/models/{slug}/overview"
ANTHROPIC_SEEDS = ("fable-5-2", "fable-5-1", "fable-5")
MAX_PAGES = 16
WATCH = {  # vendor -> (page, regex for model IDs on it)
    "OpenAI": ("https://developers.openai.com/api/docs/models", r"\bgpt-\d+(?:\.\d+)?-[a-z]+(?![\w.-])"),
    "Google": ("https://aistudio.google.com/docs/models", r"\bgemini-\d+(?:\.\d+)?-[a-z]+(?![\w.-])"),
}
# Same-family IDs that are not text models people would compare (images, audio, realtime, page assets).
NOT_TEXT = ("image", "live", "realtime", "transcribe", "texture", "block", "family", "models", "tts", "embedding",
            "robotics", "omni", "oss", "rosalind", "whisper", "translate", "flare", "sunburst")


def parse_anthropic(md: str, slug: str) -> dict | None:
    """One models.json row from a model's overview.md, or None if the page is not a model page we can read."""
    title = re.search(r"^title:\s*(.+)$", md, re.M)
    mid = re.search(r"Model ID:\s*`([^`]+)`", md)
    rel = re.search(r"Released\s+([A-Z][a-z]+ \d{1,2}, \d{4})", md)
    price = re.search(r"Input pricing:\s*(From )?(\$[\d.]+) / MTok\s*·\s*Output pricing:\s*(From )?(\$[\d.]+) / MTok", md)
    if not (title and mid):
        return None
    flag = re.search(r"^\*\*([A-Za-z ]+)\.\*\*", md, re.M)
    label = flag.group(1).strip().lower() if flag else ""
    status = {"latest": "latest", "legacy": "legacy", "verification required": "restricted"}.get(label)
    if status is None:
        return None  # an unknown lifecycle label is a page change to look at, not something to guess
    return {
        "vendor": "Anthropic", "name": title.group(1).strip(), "id": mid.group(1), "status": status,
        "released": datetime.strptime(rel.group(1), "%B %d, %Y").date().isoformat() if rel else None,
        "price": (f"{'from ' if price.group(1) else ''}{price.group(2)} / {'from ' if price.group(3) else ''}{price.group(4)}"
                  if price else None),
        "source": ANTHROPIC_DOCS.format(slug=slug),
    }


def linked_slugs(md: str) -> list[str]:
    return sorted(set(re.findall(r"/docs/en/models/([a-z0-9-]+)/overview", md)))


def _major(model_id: str) -> int:
    m = re.search(r"-(\d+)", model_id)
    return int(m.group(1)) if m else 0


def _newest_major(ids) -> int:
    return max((_major(i) for i in ids), default=0)


def model_ids(text: str, pattern: str) -> set[str]:
    return {i for i in re.findall(pattern, text.lower()) if i.rsplit("-", 1)[-1] not in NOT_TEXT}


AA_URL = "https://artificialanalysis.ai/leaderboards/models"
# column -> (our model ID, Artificial Analysis slug per effort tier). Argon has one listed variant, "High".
AA_MODELS = {
    "Opus 5.5": ("claude-opus-5-5", {"low": "claude-opus-5-5-low", "high": "claude-opus-5-5-high", "highest": "claude-opus-5-5"}),
    "GPT-6 Astra": ("gpt-6-astra", {"low": "gpt-6-astra-low", "high": "gpt-6-astra-high", "highest": "gpt-6-astra"}),
    "Gemini 4 Argon": ("gemini-4-argon", {"high": "gemini-4-argon", "highest": "gemini-4-argon"}),
}
AA_TIERS = {"low": "Low effort", "high": "High effort", "highest": "Highest effort listed"}
# row label, area, how to read it from an AA row, how to print it
AA_ROWS = (
    ("Intelligence Index", "Composite of AA's evaluations", lambda r: r.get("intelligenceIndex"), lambda v: f"{v:.1f}"),
    ("AutomationBench-AA", "Business workflows", lambda r: (r.get("scores") or {}).get("automationbench-aa"), lambda v: f"{v * 100:.1f}%"),
    ("AA-Briefcase", "Knowledge work (Elo-style)", lambda r: (r.get("scores") or {}).get("aa-briefcase"), lambda v: f"{v:.0f}"),
    ("Terminal-Bench-Science 0.1", "Agentic scientific research", lambda r: (r.get("scores") or {}).get("terminal-bench-science"), lambda v: f"{v * 100:.1f}%"),
    ("Humanity's Last Exam", "Multidisciplinary reasoning", lambda r: (r.get("scores") or {}).get("humanitys-last-exam"), lambda v: f"{v * 100:.1f}%"),
)


def parse_artificial_analysis(page: str) -> dict[str, dict]:
    """slug -> richest model object embedded in the leaderboard page (the page repeats some models as stubs)."""
    text = page.replace('\\"', '"').replace("\\\\", "\\")
    dec, rows = json.JSONDecoder(), {}
    for m in re.finditer(r'\{"slug":"([^"]+)","name":', text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
        except ValueError:
            continue
        k = obj["slug"]
        if k not in rows or len(json.dumps(obj)) > len(json.dumps(rows[k])):
            rows[k] = obj
    return rows


def aa_claims(rows: dict[str, dict], today: date) -> list[dict]:
    """One independent table per effort tier, only from cells the page actually has; a model with no variant at a
    tier is left out of that table rather than filled with a neighbour's number."""
    claims = []
    for tier, label in AA_TIERS.items():
        present = {col: rows.get(slugs.get(tier, "")) for col, (_, slugs) in AA_MODELS.items()}
        present = {c: r for c, r in present.items() if r and r.get("intelligenceIndex") is not None}
        if len(present) < 2:
            continue
        out_rows = []
        for name, area, get_, fmt in AA_ROWS:
            scores = {c: fmt(v) for c, r in present.items() if (v := get_(r)) is not None}
            if len(scores) >= 2:
                out_rows.append({"benchmark": name, "area": area, "scores": scores})
        notes = [f"{c}: {r['name']}" for c, r in present.items()]
        claims.append({
            "kind": "independent", "title": f"Artificial Analysis, {label.lower()}", "vendor": "Artificial Analysis",
            "date": today.isoformat(), "source": AA_URL, "effort": tier, "models": list(present),
            "model_ids": {c: AA_MODELS[c][0] for c in present}, "rows": out_rows,
            "caveats": ["Independent: Artificial Analysis runs these evaluations itself and names the variants (" + "; ".join(notes) + ").",
                        "Effort labels are Artificial Analysis's. Our own run used low effort; compare only against the low-effort table."
                        if tier != "low" else "Our own run used low effort, so this is the table to compare against."]})
    return claims


def merge_anthropic(models: list[dict], fresh: list[dict]) -> list[dict]:
    """Fresh Anthropic rows replace the same IDs; Anthropic rows no longer reachable are kept as they were."""
    by_id = {m["id"]: m for m in fresh}
    out = [by_id.pop(m["id"], m) if m["vendor"] == "Anthropic" else m for m in models]
    return out + list(by_id.values())


def _get(url: str, timeout: int = 60) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (fixed https vendor URLs)
        return r.read().decode("utf-8", "replace")


def fetch_anthropic(get=_get) -> tuple[list[dict], list[str]]:
    """(rows, problems): breadth-first over the model pages linked from the seeds."""
    queue, seen, rows, problems = list(ANTHROPIC_SEEDS), set(), [], []
    while queue and len(seen) < MAX_PAGES:
        slug = queue.pop(0)
        if slug in seen:
            continue
        seen.add(slug)
        try:
            md = get(ANTHROPIC_DOCS.format(slug=slug) + ".md")
        except Exception as e:  # noqa: BLE001 (any fetch failure is reported, not fatal)
            problems.append(f"anthropic {slug}: {type(e).__name__}")
            continue
        row = parse_anthropic(md, slug)
        if row is None:
            problems.append(f"anthropic {slug}: page not recognised")
            continue
        rows.append(row)
        queue += [s for s in linked_slugs(md) if s not in seen]
    return rows, problems


def watch(models: list[dict], get=_get) -> tuple[dict[str, list[str]], list[str]]:
    """({vendor: unreviewed IDs}, problems) for the vendors whose pages we only watch."""
    known = {m["id"].lower() for m in models}
    unreviewed, problems = {}, []
    for vendor, (url, pattern) in WATCH.items():
        try:
            ids = model_ids(get(url), pattern)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{vendor.lower()} page unreadable: {type(e).__name__}")
            continue
        if not ids:
            problems.append(f"{vendor.lower()} page unreadable: no model IDs found")
            continue
        new = sorted(i for i in ids - known if _major(i) >= _newest_major(ids & known) - 1)  # skip long-deprecated generations
        if new:
            unreviewed[vendor] = new
    return unreviewed, problems


def refresh(models_json: dict, today: date | None = None, get=_get) -> tuple[dict, dict]:
    """(new models.json, report). Never writes a row it could not read."""
    rows, problems = fetch_anthropic(get)
    models = merge_anthropic(models_json.get("models", []), rows)
    unreviewed, wproblems = watch(models, get)
    claims, aproblems = list(models_json.get("claims", [])), []
    try:
        fresh = aa_claims(parse_artificial_analysis(get(AA_URL)), today or date.today())
        if fresh:
            claims = [c for c in claims if c.get("kind") != "independent"] + fresh
        else:
            aproblems.append("artificial analysis: no readable rows for our models")
    except Exception as e:  # noqa: BLE001
        aproblems.append(f"artificial analysis: {type(e).__name__}")
    out = {**models_json, "schema": SCHEMA, "models": models, "claims": claims}
    if rows and not problems:  # `checked` only advances when the Anthropic read was clean
        out["checked"] = (today or date.today()).isoformat()
    out["unreviewed"] = unreviewed
    validate(out)
    changed = [m["id"] for m in models if m not in models_json.get("models", [])]
    return out, {"changed": changed, "unreviewed": unreviewed, "problems": problems + wproblems + aproblems, "anthropic_rows": len(rows)}


def dump(models_json: dict) -> str:
    return json.dumps(models_json, indent=2, ensure_ascii=False) + "\n"
