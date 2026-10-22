"""The memo is generated from the model, and its prose claims hold in the data.

1. The template contains no figures: every number is a placeholder (fiscal labels and form
   names excepted), so no number in the memo can drift from the model.
2. The committed memo equals a fresh render from the fixture store.
3. Each directional claim in the prose is checked against the numbers behind it.
"""
from __future__ import annotations

import re

import pandas as pd
import pytest

from saas_metrics import backtest, memo, roi

ALLOWED = [
    r"\{\{[a-z0-9_]+\}\}",          # placeholders
    r"\bQ[1-4] FY\d\d\b", r"\bFY\d\d\b",
    r"\b10-[QK]\b", r"\b8-K\b",
    r"Rule of 40", r"Section \d:",
]


def test_template_has_no_hard_coded_numbers():
    text = memo.TEMPLATE.read_text()
    for pat in ALLOWED:
        text = re.sub(pat, "", text)
    stray = re.findall(r".{0,30}\d.{0,30}", text)
    assert not stray, stray[:5]


def test_every_placeholder_is_known(db):
    f = memo.facts(db)
    used = set(re.findall(r"\{\{([a-z0-9_]+)\}\}", memo.TEMPLATE.read_text()))
    assert used <= set(f), sorted(used - set(f))


def test_committed_memo_is_in_sync_with_the_model(db):
    assert memo.OUT_MD.read_text() == memo.render(db), "run `saas_metrics memo` to regenerate the memo"


def test_html_tearsheet_is_self_contained(db):
    html = memo.to_html(memo.render(db), db)
    assert html.count("<svg") == 2
    assert not re.search(r"(src|href)=['\"]https?://", html)


@pytest.fixture(scope="module")
def numbers(db):
    long, _ = backtest.bridge(db)
    four = long.groupby(["metric", "component"])["value"].sum()
    q = long.pivot_table(index=["metric", "component"], columns="period_end", values="value")
    return four, q, backtest.scorecard(db)


def test_claim_revenue_beat_and_profit_beat_by_more(numbers):
    four, _, _ = numbers
    rev = four[("revenue", "actual")] / four[("revenue", "plan")] - 1
    ng = four[("ng_op_inc", "actual")] / four[("ng_op_inc", "plan")] - 1
    fcf = four[("fcf", "actual")] / four[("fcf", "plan")] - 1
    assert 0 < rev < ng < fcf


def test_claim_cash_beat_mostly_conversion(numbers):
    four, _, _ = numbers
    assert four[("fcf", "fcf_conversion")] > 0.5 * (four[("fcf", "actual")] - four[("fcf", "plan")])


def test_claim_total_error_beats_baselines_and_guidance(numbers):
    _, _, sc = numbers
    def tot(col):
        k = sc[col].notna()
        return abs(sc.loc[k, col].sum() / sc.loc[k, "actual_revenue"].sum() - 1)
    assert tot("model_base") < min(tot("trend_naive"), tot("seasonal_naive")) < tot("guidance")


def test_claim_trend_naive_beats_base_per_quarter(numbers):
    _, _, sc = numbers
    mape = lambda c: (sc[c] / sc.actual_revenue - 1).abs().mean()  # noqa: E731
    assert mape("trend_naive") < mape("model_base")


def test_claim_ea_timing_is_the_largest_quarterly_revenue_error(numbers):
    _, q, _ = numbers
    comps = list(backtest.GROUPS[:backtest.EXPLAINS["revenue"]])
    biggest = q.loc["revenue"].loc[comps].abs().max(axis=1).idxmax()
    assert biggest == "ea_mix_timing"
    first, second = q.loc[("revenue", "ea_mix_timing")].iloc[:2]
    assert first < 0 < second


def test_claim_more_logos_at_smaller_spend_and_fewer_costlier_heads(numbers):
    four, _, _ = numbers
    assert four[("revenue", "new_logo_volume")] > 0 > four[("revenue", "new_logo_arr")]
    assert four[("ng_op_inc", "headcount_pace")] > 0 > four[("ng_op_inc", "cost_per_head")]


def test_claim_q3_below_even_the_downside(numbers):
    _, _, sc = numbers
    assert sc.actual_revenue.iloc[0] < sc.model_downside.iloc[0]
    assert (sc.guidance.dropna() < sc.actual_revenue[sc.guidance.notna()]).all()   # guidance was conservative


def test_claim_hiring_case_depends_on_attainment(db):
    inp = {i.key: i.value for i in roi.roi_inputs(db)}
    s = roi.summary(roi.gtm_cases(inp), inp)
    assert s.at["A", "npv"] > s.at["B", "npv"] > 0
    f = memo.facts(db)
    co = float(f["co_magic"])
    assert roi.ae_efficiency(inp) > 2 * co
    low = {**inp, "roi_attain": roi.attainment_at_efficiency(inp, co)}
    assert pd.isna(roi.summary(roi.gtm_cases(low), low).at["A", "payback_month"])
    assert roi.attainment_at_efficiency(inp, co) < roi.breakeven_attainment(inp) < inp["roi_attain"]
