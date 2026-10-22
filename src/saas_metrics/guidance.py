"""Management guidance from earnings press releases (8-K Item 2.02, exhibit 99.1).

Two layouts appear in fiscal 2023-2027:

A (to FY26): one two-column table, next quarter then full year
    Third Quarter Fiscal 2026  Full Year Fiscal 2026
    Revenue  $587.0 million to $592.0 million  $2.34 billion to $2.36 billion
    Non-GAAP Income from Operations  $66.0 million to $70.0 million  ...
    (losses are printed in parentheses: "($10.0) million")

B (FY27 on): one block per period, GAAP and non-GAAP side by side
    First Quarter Fiscal Year 2027 Guidance ...
    Revenues are expected to be in the range of: $659 million to $664 million
    ... Income (Loss) from Operations are expected to be in the range of:
        $(48.0) million to $(44.0) million $105.0 million to $109.0 million

Guidance is a comparator for the backtest only; it is never a model input.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

ORD = {"first": 1, "second": 2, "third": 3, "fourth": 4}
# a signed dollar amount: $587.0 million | ($10.0) million | $(48.0) million | $2.34 billion | $(0.34)
_AMT = r"(\(?\$\s?\(?[\d,.]+\)?)\s?(million|billion)?"
ISSUED = re.compile(r"as of today, (\w+ \d{1,2}, \d{4})", re.I)


@dataclass
class Guide:
    issued: str        # date as printed, e.g. "August 26, 2025"
    period: str        # "Q3 FY26" or "FY26"
    metric: str        # revenue | non_gaap_operating_income | non_gaap_eps
    low: float
    high: float
    snippet: str


def _num(tok: str, scale: str | None) -> float:
    neg = "(" in tok
    v = float(re.sub(r"[^\d.]", "", tok))
    mult = {"billion": 1e9, "million": 1e6}.get((scale or "").lower(), 1.0)
    return (-v if neg else v) * mult


def _pairs(s: str) -> list[tuple[float, float]]:
    vals = [_num(a, b) for a, b in re.findall(_AMT, s)]
    return [(vals[i], vals[i + 1]) for i in range(0, len(vals) - 1, 2)]


def _label(q: int | None, fy: int) -> str:
    return f"Q{q} FY{fy % 100:02d}" if q else f"FY{fy % 100:02d}"


def _layout_a(text: str, issued: str) -> list[Guide]:
    h = re.search(r"(First|Second|Third|Fourth) Quarter Fiscal (\d{4}) Full Year Fiscal (\d{4})", text, re.I)
    if not h:
        return []
    q, qfy, fy = ORD[h.group(1).lower()], int(h.group(2)), int(h.group(3))
    win = text[h.end(): h.end() + 900]
    rows = {
        "revenue": r"Revenue (.*?)(?=Non-GAAP)",
        "non_gaap_operating_income": r"Non-GAAP (?:Income|Loss) from Operations (.*?)(?=Non-GAAP Net)",
        "non_gaap_eps": r"Non-GAAP Net (?:Income|Loss) per Share (.*?)(?=Reconciliation|The guidance|$)",
    }
    out = []
    for metric, pat in rows.items():
        m = re.search(pat, win, re.I | re.S)
        if not m:
            continue
        pr = _pairs(m.group(1)[:200])
        if len(pr) >= 2:
            out.append(Guide(issued, _label(q, qfy), metric, *pr[0], m.group(0)[:200]))
            out.append(Guide(issued, _label(None, fy), metric, *pr[1], m.group(0)[:200]))
    return out


def _layout_b(text: str, issued: str) -> list[Guide]:
    out = []
    blocks = list(re.finditer(r"(?:(First|Second|Third|Fourth) Quarter|Full Year) Fiscal (?:Year )?(\d{4}) Guidance", text, re.I))
    for i, b in enumerate(blocks):
        q = ORD[b.group(1).lower()] if b.group(1) else None
        end = blocks[i + 1].start() if i + 1 < len(blocks) else b.end() + 1500
        win = text[b.end(): end]
        period = _label(q, int(b.group(2)))
        m = re.search(r"Revenues? (?:is|are) expected to be in the range of:? (.*?)(?=GAAP|$)", win, re.I | re.S)
        if m and _pairs(m.group(1)[:120]):
            out.append(Guide(issued, period, "revenue", *_pairs(m.group(1)[:120])[0], m.group(0)[:200]))
        m = re.search(r"from Operations (?:is|are) expected to be in the range of:? (.*?)(?=Net Income|$)", win, re.I | re.S)
        if m and len(_pairs(m.group(1)[:200])) >= 2:  # GAAP pair first, then non-GAAP
            out.append(Guide(issued, period, "non_gaap_operating_income", *_pairs(m.group(1)[:200])[1], m.group(0)[:200]))
        m = re.search(r"per Share (?:is|are) expected to be in the range of:? (.*?)(?=Full Year|Conference|$)", win, re.I | re.S)
        if m and len(_pairs(m.group(1)[:120])) >= 2:
            out.append(Guide(issued, period, "non_gaap_eps", *_pairs(m.group(1)[:120])[1], m.group(0)[:200]))
    return out


def parse(text: str) -> list[Guide]:
    iss = ISSUED.search(text)
    issued = iss.group(1) if iss else ""
    return _layout_a(text, issued) or _layout_b(text, issued)


@dataclass
class Actual:
    period: str        # "Q2 FY26"
    metric: str        # non_gaap_operating_income | non_gaap_eps
    value: float
    snippet: str


_QOF = re.compile(r"(first|second|third|fourth) quarter of fiscal (?:year )?(\d{4})", re.I)


def parse_actuals(text: str) -> list[Actual]:
    """Company-defined non-GAAP results for the quarter the release reports.

    e.g. "Non-GAAP income from operations was $86.8 million" and
         "Non-GAAP net income was $87.2 million, or $1.00 per share"
    """
    q = _QOF.search(text)
    if not q:
        return []
    period = _label(ORD[q.group(1).lower()], int(q.group(2)))
    out = []
    m = re.search(r"Non-GAAP (income|loss) from operations was \$\s?([\d,.]+) million", text, re.I)
    if m:
        v = float(m.group(2).replace(",", "")) * 1e6 * (-1 if m.group(1).lower() == "loss" else 1)
        out.append(Actual(period, "non_gaap_operating_income", v, m.group(0)))
    m = re.search(r"Non-GAAP net (income|loss) was \$\s?[\d,.]+ million, or \$\s?\(?([\d.]+)\)? per share", text, re.I)
    if m:
        v = float(m.group(2)) * (-1 if m.group(1).lower() == "loss" else 1)
        out.append(Actual(period, "non_gaap_eps", v, m.group(0)))
    return out
