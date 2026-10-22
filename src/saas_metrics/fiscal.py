"""Fiscal calendar for any fiscal-year-end month (periods.py is the January-only original).

Quarter labels follow the company's own fiscal year: for a January year end (MongoDB, Snowflake)
the quarter ended 2025-07-31 is "Q2 FY26"; for a December year end (Datadog, The Trade Desk) the
quarter ended 2026-06-30 is "Q2 FY26".
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta


def month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def _d(x: str | date) -> date:
    return date.fromisoformat(x) if isinstance(x, str) else x


@dataclass(frozen=True)
class FiscalCalendar:
    fy_end_month: int  # 1-12

    def fiscal(self, end: str | date) -> tuple[int, int]:
        """(fiscal_year, quarter) for a quarter-end date."""
        d = _d(end)
        fy = d.year + 1 if d.month > self.fy_end_month else d.year
        q = ((d.month - self.fy_end_month - 1) % 12) // 3 + 1
        if (d.month - self.fy_end_month) % 3:
            raise ValueError(f"{d} is not a quarter end for a fiscal year ending in month {self.fy_end_month}")
        return fy, q

    def label(self, end: str | date) -> str:
        fy, q = self.fiscal(end)
        return f"Q{q} FY{fy % 100:02d}"

    def fy_start(self, end: str | date) -> date:
        fy, _ = self.fiscal(end)
        return month_end(fy - 1, self.fy_end_month) + timedelta(days=1)

    @staticmethod
    def prev_quarter_end(end: str | date) -> date:
        d = _d(end)
        m, y = d.month - 3, d.year
        if m <= 0:
            m, y = m + 12, y - 1
        return month_end(y, m)

    def quarter_start(self, end: str | date) -> date:
        return self.prev_quarter_end(end) + timedelta(days=1)

    def quarter_ends(self, first: str | date, last: str | date) -> list[date]:
        out, d = [], _d(last)
        while d >= _d(first):
            out.append(d)
            d = self.prev_quarter_end(d)
        return out[::-1]

    def is_quarter_end(self, d: str | date) -> bool:
        d = _d(d)
        return d == month_end(d.year, d.month) and (d.month - self.fy_end_month) % 3 == 0
