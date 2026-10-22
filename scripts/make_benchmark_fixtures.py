"""Rebuild tests/fixtures/benchmark_<TICKER>.json.gz from the cached companyfacts.

The offline tests must be able to ask for *every* tag the pipeline reads, but a full companyfacts
document is ~1.5MB. So each fixture keeps only the concepts the config actually names — the
benchmark lines, the check tags, and the revenue-quality families — plus the fact fields the
pipeline uses. Re-run after adding a concept to a config:

    uv run python scripts/make_benchmark_fixtures.py
"""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from saas_metrics import company, quality  # noqa: E402

RAW = ROOT / "data" / "raw" / "benchmark"
OUT = ROOT / "tests" / "fixtures"
KEEP = ("start", "end", "val", "accn", "fy", "fp", "form", "filed")
FORMS = ("10-Q", "10-K")


def concepts_for(c: company.Company) -> set[str]:
    out = {x for cs in c.benchmark_lines().values() for x in cs} | set(c.check_tags().values())
    for families in quality.families_for(c).values():
        out |= {x for f in families for x in f.concepts}
    out |= set(quality.CASH_FLOW_CHANGE_TAGS)
    return out


def trim(cf: dict, concepts: set[str]) -> dict:
    wanted: dict[str, set[str]] = {}
    for concept in concepts:
        ns, _, tag = concept.partition(":")
        wanted.setdefault(ns, set()).add(tag)
    facts = {}
    for ns, tags in wanted.items():
        kept = {}
        for tag in sorted(tags):
            entry = cf["facts"].get(ns, {}).get(tag)
            if entry is None:
                continue
            units = {}
            for unit, fs in entry["units"].items():
                rows = [{k: f[k] for k in KEEP if k in f} for f in fs if f.get("form") in FORMS]
                if rows:
                    units[unit] = rows
            if units:
                kept[tag] = {**{k: v for k, v in entry.items() if k != "units"}, "units": units}
        if kept:
            facts[ns] = kept
    return {"cik": cf.get("cik"), "entityName": cf.get("entityName"), "facts": facts}


def main() -> None:
    written = []
    for ticker in company.available():
        c = company.load(ticker)
        if "benchmark" not in c.raw:
            continue
        src = RAW / f"{ticker}.json"
        if not src.exists():
            print(f"skip {ticker}: no cached {src}")
            continue
        trimmed = trim(json.loads(src.read_text()), concepts_for(c))
        dest = OUT / f"benchmark_{ticker}.json.gz"
        dest.write_bytes(gzip.compress(json.dumps(trimmed, separators=(",", ":")).encode(), 9))
        n = sum(len(v) for v in trimmed["facts"].values())
        written.append(f"{ticker}: {n} concept(s), {dest.stat().st_size / 1024:.0f} KB")
    print("\n".join(written))


if __name__ == "__main__":
    main()
