"""Financial lines: which XBRL concept (and dimension member) each one reads, and how.

kind = "duration": a flow over a period (P&L, cash flow); quarterly values may need
       YTD differencing or FY - 9M.
kind = "instant":  a balance at a date (cash, deferred revenue, RPO).
"""
from __future__ import annotations

from dataclasses import dataclass

PSA = "srt:ProductOrServiceAxis"
ISL = "us-gaap:IncomeStatementLocationAxis"
SIL = "us-gaap:StatementOfIncomeLocationBalanceAxis"  # typed axis used from FY27


@dataclass(frozen=True)
class Line:
    name: str
    concept: str
    kind: str
    axis: str = ""
    member: str = ""
    label: str = ""
    # other (axis, member) spellings of the same line; filers changed tagging over time.
    # member "*" accepts any member on that axis, provided exactly one fact matches.
    alts: tuple[tuple[str, str], ...] = ()

    def keys(self) -> list[tuple[str, str]]:
        return [(self.axis, self.member), *self.alts]


def from_config(raw: dict) -> list[Line]:
    """Lines from a config's `lines:` section, in file order.

    `dims` and each entry of `alternates` map axis -> member; several axes on one fact are stored
    joined with "|" (dimensions sorted by axis, as the XBRL parser sorts them).
    """
    def keyed(d: dict) -> tuple[str, str]:
        items = sorted(d.items())
        return "|".join(a for a, _ in items), "|".join(str(m) for _, m in items)
    out = []
    for name, e in raw["lines"].items():
        axis, member = keyed(e.get("dims", {}))
        out.append(Line(name, e["concept"], e.get("kind", "duration"), axis, member, e.get("label", ""),
                        tuple(keyed(a) for a in e.get("alternates", []))))
    return out


LINES: list[Line] = []
CONCEPTS: set[str] = set()


def refresh() -> None:
    """Re-read the active company's lines (called by config.activate)."""
    from saas_metrics import config
    LINES[:] = from_config(config.ACTIVE.raw)
    CONCEPTS.clear()
    CONCEPTS.update(ln.concept for ln in LINES)
