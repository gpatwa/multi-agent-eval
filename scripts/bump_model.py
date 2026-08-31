#!/usr/bin/env python3
"""Bump a model ID across every config file (and its provider adapter's
default) in one command, instead of hand-editing N files.

    python scripts/bump_model.py glm-5.2 glm-5.3
    python scripts/bump_model.py glm-5.2 glm-5.3 --price glm=1.40,4.40

Scans config*.yaml and eval_agents/providers/*.py for the literal old model
string and replaces it. Safe by construction: model IDs are specific enough
strings that a literal replace across these two file classes doesn't risk
collateral edits (verified after every run by re-parsing every config and
re-importing every provider module).

--price PROVIDER_KEY=IN,OUT additionally updates that provider's entry in
every scorecard.pricing map that has one (matches the existing `key: [a, b]`
line regardless of spacing, preserves any trailing comment). Note: it does
not preserve the original file's column-alignment whitespace (some configs
hand-align decimal points across rows with extra spaces) -- the numbers are
always correct, but the diff may include a cosmetic re-spacing of that one
line. Re-align by hand afterward if you care about the visual column.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def bump_model(old: str, new: str) -> list[pathlib.Path]:
    targets = sorted(ROOT.glob("config*.yaml")) + sorted((ROOT / "eval_agents" / "providers").glob("*.py"))
    changed = []
    for path in targets:
        text = path.read_text()
        if old not in text:
            continue
        path.write_text(text.replace(old, new))
        changed.append(path)
    return changed


def bump_price(provider_key: str, in_price: float, out_price: float) -> list[pathlib.Path]:
    pattern = re.compile(
        rf"^(\s*{re.escape(provider_key)}:\s*)\[\s*[\d.]+\s*,\s*[\d.]+\s*\](.*)$", re.MULTILINE
    )
    changed = []
    for path in sorted(ROOT.glob("config*.yaml")):
        text = path.read_text()
        new_text, n = pattern.subn(rf"\g<1>[{in_price:.2f}, {out_price:.2f}]\g<2>", text)
        if n:
            path.write_text(new_text)
            changed.append(path)
    return changed


def verify() -> list[str]:
    """Re-parse every config and re-import every provider module. Returns a
    list of problems (empty = all clear)."""
    import importlib

    import yaml

    problems = []
    for path in sorted(ROOT.glob("config*.yaml")):
        try:
            yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            problems.append(f"{path.name}: YAML parse error: {exc}")

    sys.path.insert(0, str(ROOT))
    from eval_agents.registry import _PROVIDERS  # noqa: E402

    for _key, (module_path, class_name, _env) in _PROVIDERS.items():
        try:
            mod = importlib.import_module(module_path)
            getattr(mod, class_name)
        except Exception as exc:
            problems.append(f"{module_path}.{class_name}: import error: {exc}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("old_model", help="exact current model ID string")
    parser.add_argument("new_model", help="exact new model ID string")
    parser.add_argument("--price", metavar="KEY=IN,OUT", help="also update this provider's pricing entry")
    parser.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    args = parser.parse_args()

    if args.dry_run:
        matches = [
            p
            for p in sorted(ROOT.glob("config*.yaml")) + sorted((ROOT / "eval_agents" / "providers").glob("*.py"))
            if args.old_model in p.read_text()
        ]
        print(f"Would update {len(matches)} file(s):")
        for p in matches:
            print(f"  {p.relative_to(ROOT)}")
        return 0

    changed = bump_model(args.old_model, args.new_model)
    if not changed:
        print(f"No files contained {args.old_model!r} — nothing to do.", file=sys.stderr)
        return 1
    print(f"Updated {len(changed)} file(s):")
    for p in changed:
        print(f"  {p.relative_to(ROOT)}")

    if args.price:
        key, prices = args.price.split("=", 1)
        in_price, out_price = (float(x) for x in prices.split(","))
        priced = bump_price(key, in_price, out_price)
        print(f"\nUpdated pricing for {key!r} in {len(priced)} file(s):")
        for p in priced:
            print(f"  {p.relative_to(ROOT)}")

    problems = verify()
    if problems:
        print("\nVERIFICATION FAILED:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(f"\nVerified: every config*.yaml parses, every provider module imports cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
