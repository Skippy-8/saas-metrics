"""Fiscal calendar of the active company (configs/<TICKER>.yaml company.fiscal_year_end_month).

Thin wrappers over saas_metrics.fiscal.FiscalCalendar, kept so existing code calls `periods.label(...)`.
For MongoDB (January year end) the quarter ended 2025-07-31 is "Q2 FY26".
"""
from __future__ import annotations

from datetime import date

from saas_metrics.fiscal import FiscalCalendar, month_end  # noqa: F401  (month_end re-exported)


def _cal() -> FiscalCalendar:
    from saas_metrics import config
    return FiscalCalendar(config.ACTIVE.fiscal_year_end_month)


def fiscal(end: str | date) -> tuple[int, int]:
    """(fiscal_year, quarter) for a quarter-end date."""
    return _cal().fiscal(end)


def label(end: str | date) -> str:
    return _cal().label(end)


def fy_start(end: str | date) -> date:
    return _cal().fy_start(end)


def prev_quarter_end(end: str | date) -> date:
    return FiscalCalendar.prev_quarter_end(end)


def quarter_start(end: str | date) -> date:
    return _cal().quarter_start(end)


def quarter_ends(first: str, last: str) -> list[date]:
    return _cal().quarter_ends(first, last)
