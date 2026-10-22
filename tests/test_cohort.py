"""Cohort ledger math on small synthetic inputs."""
from __future__ import annotations

import pandas as pd
import pytest

from saas_metrics import cohort


def _inp(arr, customers, nrr):
    idx = [f"2025-{i:02d}-28" for i in range(1, len(arr) + 1)]
    return pd.DataFrame({"fiscal_label": idx, "days": 92, "atlas_run_rate": arr, "other_sub_ttm": 0.0,
                         "arr_proxy": arr, "total_customers": customers, "atlas_customers": customers,
                         "customers_100k_arr": 0.0, "nrr": nrr, "nrr_is_lower_bound": False}, index=idx)


def test_identities_and_nrr_when_unconstrained():
    nrr = 1.20
    g = nrr ** 0.25 - 1
    arr = [100.0]
    for _ in range(7):
        arr.append(arr[-1] * (1 + g) + 5.0)  # existing book grows at g, plus 5 of new ARR each quarter
    inp = _inp(arr, [1000.0 + 50 * i for i in range(8)], [nrr] * 8)
    a, logos, cal = cohort.build(inp, cohort.Assumptions(churn_q=0.03))
    assert a.groupby("period_end").arr.sum().values == pytest.approx(arr)
    assert logos.groupby("period_end").customers.sum().values == pytest.approx(inp.total_customers.values)
    assert cal.new_cohort_arr.dropna().values == pytest.approx([5.0] * 7)
    assert cal.nrr_ledger_t4q.dropna().values == pytest.approx([nrr] * 4)
    assert not cal.constrained.dropna().any()


def test_constrained_quarter_floors_new_arr_at_zero():
    arr = [100.0, 101.0, 106.0, 111.0, 117.0]   # quarter 2 grows 1%, less than 120% NRR implies
    cal = cohort.build(_inp(arr, [1000.0] * 5, [1.20] * 5))[2]
    q1 = cal.iloc[1]
    assert bool(q1.constrained) and q1.new_cohort_arr == pytest.approx(0.0)
    assert q1.g_quarterly == pytest.approx(0.01)
    assert (cal.new_cohort_arr.dropna() >= -1e-9).all()


def test_missing_inputs_raise():
    qw = pd.DataFrame({"period_end": ["2025-07-31"], "fiscal_label": ["Q2 FY26"], "in_window": [True],
                       "revenue_atlas": [1.0], "revenue_other_subscription": [1.0]})
    kp = pd.DataFrame(columns=["period_end", "kpi", "value", "qualifier"])
    with pytest.raises(ValueError):
        cohort.inputs(qw, kp)
