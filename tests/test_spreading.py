"""Spreading invoices over their service periods: exact to the cent, by day, and only through as_of."""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

import saas_metrics as sm
from saas_metrics.inputs import InputError, prepare, spread

TODAY = date(2026, 10, 20)


def lines(rows):
    return pd.DataFrame(rows, columns=["customer_id", "period", "revenue", "service_start", "service_end"]).astype("string")


def test_annual_invoice_spreads_by_day_and_sums_exactly():
    out, n = spread(lines([["A", "2025-01-01", "1200.00", "2025-01-01", "2025-12-31"]]))
    assert n == 1 and len(out) == 12
    cents = [round(float(v) * 100) for v in out.revenue]
    assert sum(cents) == 120000
    feb, jan = cents[1], cents[0]
    assert abs(feb - 120000 * 28 / 365) < 1 and abs(jan - 120000 * 31 / 365) < 1


def test_partial_months_and_credits():
    out, _ = spread(lines([["A", "2025-02-15", "300.00", "2025-02-15", "2025-05-14"],
                           ["A", "2025-02-15", "-30.00", "2025-02-15", "2025-05-14"]]))
    assert sorted(out.period.unique()) == ["2025-02-28", "2025-03-31", "2025-04-30", "2025-05-31"]
    assert round(out.revenue.astype(float).sum(), 2) == 270.00
    assert round(out[out.revenue.astype(float) < 0].revenue.astype(float).sum(), 2) == -30.00


def test_lines_without_service_dates_are_unchanged():
    out, n = spread(lines([["A", "2025-03-03", "50.00", None, None]]))
    assert n == 0 and out.iloc[0].period == "2025-03-03"


def test_bad_service_dates_raise():
    with pytest.raises(InputError, match="before service_start"):
        spread(lines([["A", "2025-03-03", "50", "2025-06-01", "2025-05-01"]]))


def test_service_after_as_of_is_deferred():
    df = lines([["A", "2025-12-01", "1200.00", "2025-12-01", "2026-11-30"],
                ["B", "2025-12-20", "100.00", None, None]])
    out, notes = prepare(df)
    assert set(out.period.str[:7]) == {"2025-12"}                  # only December is earned by the export date
    assert any("deferred" in n for n in notes)


def test_spreading_removes_false_churn_from_annual_plans():
    from saas_metrics import sample
    df = sample.generate()
    with_dates = sm.analyze(df, mapping=sample.MAPPING, today=TODAY)
    without = sm.analyze(df, mapping={k: v for k, v in sample.MAPPING.items()
                                      if k not in ("service_start", "service_end")}, today=TODAY)
    assert with_dates.ok and without.ok
    assert with_dates.waterfall.churned_logos.sum() < 0.7 * without.waterfall.churned_logos.sum()
