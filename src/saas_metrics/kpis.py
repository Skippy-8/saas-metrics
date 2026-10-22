"""Operating KPIs parsed from 10-Q / 10-K text (they are not tagged in XBRL).

Each parse keeps the sentence it came from, so every KPI can be traced to the filing.
Patterns match the wording MongoDB used across fiscal 2023-2027; a filing where a
pattern finds nothing returns no row for that KPI (never a guess).
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass


def text_of(raw_html: str) -> str:
    t = re.sub(r"<[^>]+>", " ", raw_html)
    return re.sub(r"\s+", " ", html.unescape(t))


@dataclass
class Kpi:
    kpi: str
    value: float
    unit: str
    qualifier: str  # e.g. "over", "approximately", "" (exact)
    snippet: str


# (kpi, unit, regex) from the active company's `kpis:` section. The first group is the
# qualifier ("over", "approximately", or empty), the second the number.
PATTERNS: list[tuple[str, str, re.Pattern]] = []


def refresh() -> None:
    """Re-read the active company's KPI patterns (called by config.activate)."""
    from saas_metrics import config
    PATTERNS[:] = [(k["name"], k["unit"], re.compile(k["pattern"], re.I)) for k in config.ACTIVE.raw.get("kpis", [])]


def parse(text: str) -> list[Kpi]:
    out = []
    for kpi, unit, pat in PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        qual, num = m.group(1) or "", m.group(2)
        s0 = text.rfind(". ", 0, m.start()) + 2
        s1 = text.find(". ", m.end())
        out.append(Kpi(kpi, float(num.replace(",", "")), unit, qual.lower(), text[s0:s1 + 1][:400]))
    return out
