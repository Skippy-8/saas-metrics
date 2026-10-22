"""Period arithmetic: month ends and fiscal quarter ends for any fiscal year-end month."""
from __future__ import annotations

import calendar as _cal
from datetime import date

ANNUALIZE = {"month": 12, "quarter": 4}


def month_end(y: int, m: int) -> date:
    return date(y, m, _cal.monthrange(y, m)[1])


def is_quarter_end(d: date, fy_end_month: int = 12) -> bool:
    return d == month_end(d.year, d.month) and (d.month - fy_end_month) % 3 == 0


def period_end(d: date, grain: str = "month", fy_end_month: int = 12) -> date:
    """The end of the month or fiscal quarter that contains `d`."""
    me = month_end(d.year, d.month)
    if grain == "month":
        return me
    while not is_quarter_end(me, fy_end_month):
        me = month_end(me.year + (me.month == 12), me.month % 12 + 1)
    return me


def periods_between(first: date, last: date, grain: str = "month") -> list[date]:
    """Every period end from `first` to `last` inclusive (both period ends)."""
    out, d = [], first
    step = 1 if grain == "month" else 3
    while d <= last:
        out.append(d)
        m = d.month + step
        d = month_end(d.year + (m - 1) // 12, (m - 1) % 12 + 1)
    return out
