"""Every headline metric on a tape small enough to compute by hand (docs/definitions.md, worked example)."""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

import saas_metrics as sm

TODAY = date(2026, 10, 20)


def tape() -> pd.DataFrame:
    """14 months, Jan 2025 - Feb 2026.
    A: 100 every month.                 B: 100, then 150 from month 13 (expansion).
    C: 100 for months 1-5, gone 6-12, back at 80 in months 13-14 (churn, then reactivation).
    D: new in month 13 at 50.           E: 200 for months 1-12, then 120 (contraction)."""
    months = [date(2025 + (i // 12), i % 12 + 1, 15) for i in range(14)]
    rev = {"A": [100] * 14, "B": [100] * 12 + [150, 150], "C": [100] * 5 + [0] * 7 + [80, 80],
           "D": [0] * 12 + [50, 50], "E": [200] * 12 + [120, 120]}
    rows = [{"customer_id": c, "period": m.isoformat(), "revenue": v}
            for c, vals in rev.items() for m, v in zip(months, vals, strict=True) if v]
    return pd.DataFrame(rows)


@pytest.fixture(scope="module")
def rep():
    r = sm.analyze(tape(), today=TODAY)
    assert r.ok, r.checks
    return r


def row(df, i):
    return df.iloc[i]


def test_waterfall_month_13(rep):
    w = row(rep.waterfall, 12)                                     # January 2026
    assert (w.beginning, w.new_logo, w.reactivation, w.expansion, w.contraction, w.churn, w.ending) == (
        400, 50, 80, 50, 80, 0, 500)
    assert (rep.waterfall.check_diff == 0).all()


def test_churn_month_6(rep):
    w, m = row(rep.waterfall, 5), row(rep.metrics, 5)              # June 2025: C churns
    assert (w.beginning, w.churn, w.churned_logos) == (500, 100, 1)
    assert m.logo_churn_rate == pytest.approx(1 / 4)               # 1 of the 4 active in May
    assert m.gross_revenue_churn_rate == pytest.approx(100 / 500)


def test_retention_month_13(rep):
    m = row(rep.metrics, 12)                                       # base: January 2025 (A, B, C, E = 500)
    assert m.nrr_12m == pytest.approx((100 + 150 + 80 + 120) / 500)          # 0.9: D is new, excluded
    assert m.grr_12m == pytest.approx((100 + 100 + 80 + 120) / 500)          # 0.8: expansion capped at base
    assert m.logo_retention_12m == pytest.approx(4 / 4)                      # C is back, so retained
    assert pd.isna(row(rep.metrics, 11).nrr_12m)                             # no year-ago base yet


def test_quick_ratio_arr_arpa(rep):
    m = row(rep.metrics, 12)
    assert m.quick_ratio == pytest.approx((50 + 80 + 50) / (80 + 0))         # 2.25
    last = row(rep.metrics, 13)
    assert last.arr == 500 * 12 and last.arpa == pytest.approx(500 / 5)
    assert pd.isna(row(rep.metrics, 1).quick_ratio)                          # nothing lost: undefined


def test_quarterly_grain_looks_back_four_quarters():
    r = sm.analyze(tape(), grain="quarter", today=TODAY)
    assert r.ok and list(r.metrics.arr) == list(r.waterfall.ending.astype(float) * 4)
    assert r.metrics.nrr_12m.notna().sum() == len(r.metrics) - 4
