"""The SQL engine against an independent plain-Python waterfall; QC; mapping; segment and channel views."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

import saas_metrics as sm
from saas_metrics.calendar import period_end, periods_between
from saas_metrics.inputs import read

SAMPLE = Path(__file__).parent / "fixtures" / "sample_billing.csv.gz"
TODAY = date(2026, 10, 16)


@pytest.fixture(scope="module")
def monthly():
    return sm.analyze(SAMPLE, today=TODAY)


def reference_waterfall(path, grain="month"):
    """Plain-Python waterfall, written separately from the SQL, from the raw lines."""
    raw = read(path)
    rev = defaultdict(Decimal)
    for r in raw.itertuples():
        d = pd.to_datetime(r.period).date()
        rev[(r.customer_id.strip(), period_end(d, grain))] += Decimal(r.revenue)
    periods = periods_between(min(p for _, p in rev), max(p for _, p in rev), grain)
    customers = sorted({c for c, _ in rev})
    seen, prev, out = dict.fromkeys(customers, False), dict.fromkeys(customers, Decimal(0)), []
    for p in periods:
        row = dict.fromkeys(("beginning", "new_logo", "reactivation", "expansion", "contraction", "churn", "ending"), Decimal(0))
        for c in customers:
            cur, pv = max(rev.get((c, p), Decimal(0)), Decimal(0)), prev[c]
            row["beginning"] += pv
            row["ending"] += cur
            if pv == 0 and cur > 0:
                row["reactivation" if seen[c] else "new_logo"] += cur
            elif pv > 0 and cur == 0:
                row["churn"] += pv
            elif pv > 0 and cur > pv:
                row["expansion"] += cur - pv
            elif pv > 0 and cur < pv:
                row["contraction"] += pv - cur
            seen[c] |= cur > 0
            prev[c] = cur
        out.append({"period_end": p, **row})
    return pd.DataFrame(out)


@pytest.mark.parametrize("grain", ["month", "quarter"])
def test_waterfall_matches_independent_reference(grain):
    sql = sm.analyze(SAMPLE, grain=grain, today=TODAY).waterfall
    ref = reference_waterfall(SAMPLE, grain)
    assert len(sql) == len(ref)
    for col in ("beginning", "new_logo", "reactivation", "expansion", "contraction", "churn", "ending"):
        assert [Decimal(str(v)) for v in sql[col]] == list(ref[col]), col


def test_identity_continuity_and_paths(monthly):
    w = monthly.waterfall
    assert (w.check_diff == 0).all()
    assert (w.beginning.iloc[1:].values == w.ending.iloc[:-1].values).all()
    assert w.reactivation.sum() > 0 and w.churn.sum() > 0 and w.contraction.sum() > 0


def test_group_waterfalls_sum_to_total(monthly):
    w = monthly.waterfall
    for name, key in (("waterfall_by_segment", "segment"), ("waterfall_by_channel", "channel")):
        g = monthly.tables[name]
        assert (g.check_diff == 0).all() and g[key].nunique() == 3
        tot = g.groupby("period_end")[["new_logo", "churn", "ending"]].sum().reset_index()
        for col in ("new_logo", "churn", "ending"):
            assert list(tot[col].round(2)) == list(w[col].round(2))


def test_metrics_consistent_with_waterfall_and_retention(monthly):
    m, w = monthly.metrics, monthly.waterfall
    assert list(m.revenue.round(2)) == list(w.ending.astype(float).round(2))
    assert (m.grr_12m.dropna() <= m.nrr_12m.dropna() + 1e-12).all() and (m.grr_12m.dropna() <= 1).all()
    assert m.nrr_12m.notna().sum() == len(m) - 12
    cust = monthly.tables["customers"]
    assert list(cust.drop(columns="customer_id").sum().round(2)) == list(w.ending.astype(float).round(2))


def _write(tmp_path, rows, name="t.csv"):
    p = tmp_path / name
    pd.DataFrame(rows).to_csv(p, index=False)
    return p


def test_qc_blocks_bad_inputs(tmp_path):
    ok = {"customer_id": "A", "period": "2025-01-15", "revenue": "100"}
    for name, rows in {"schema": [{"customer": "A", "period": "2025-01-15", "revenue": "1"}],
                       "nulls": [ok, {**ok, "customer_id": ""}],
                       "types": [ok, {**ok, "revenue": "abc"}]}.items():
        r = sm.analyze(_write(tmp_path, rows, f"{name}.csv"), today=TODAY)
        assert not r.ok and any(c.level == "error" and c.name == name for c in r.checks), name


def test_qc_warnings_block_unless_allowed(tmp_path):
    p = _write(tmp_path, [{"customer_id": "A", "period": "2025-01-15", "revenue": "100"},
                          {"customer_id": "A", "period": "2025-03-15", "revenue": "120"},
                          {"customer_id": "B", "period": "2025-03-15", "revenue": "-30"}])
    blocked = sm.analyze(p, today=TODAY)
    assert not blocked.ok and {c.name for c in blocked.blocking} == {"missing periods", "negative revenue"}
    allowed = sm.analyze(p, allow_warnings=True, today=TODAY)
    assert (allowed.waterfall.check_diff == 0).all() and any("net-negative" in n for n in allowed.notes)


def test_mapping_and_formats(tmp_path, monthly):
    raw = read(SAMPLE).rename(columns={"customer_id": "Customer", "period": "Date", "revenue": "Amount ($)"})
    raw["Amount ($)"] = "$" + raw["Amount ($)"]
    mapped = sm.analyze(raw, mapping={"customer_id": "customer", "period": "DATE", "revenue": "Amount ($)"}, today=TODAY)
    assert list(mapped.waterfall.ending) == list(monthly.waterfall.ending)
    with pytest.raises(sm.InputError, match="not found"):
        sm.analyze(raw, mapping={"customer_id": "Client"})
    with pytest.raises(sm.InputError, match="unknown target"):
        sm.analyze(raw, mapping={"mrr": "Amount ($)"})
    pq = tmp_path / "b.parquet"
    read(SAMPLE).to_parquet(pq)
    assert list(sm.analyze(pq, today=TODAY).waterfall.ending) == list(monthly.waterfall.ending)


@pytest.mark.parametrize("grain", ["month", "quarter"])
def test_headline_current_mrr_is_the_last_period_run_rate(grain):
    """The workbench's tiles: current MRR is ending ARR / 12, not the sum of MRR over every row of history."""
    r = sm.analyze(SAMPLE, grain=grain, today=TODAY)
    h = r.headline()
    last = r.metrics.iloc[-1]
    assert h["ending_arr"] == pytest.approx(last["arr"])
    assert h["current_mrr"] == pytest.approx(last["arr"] / 12)
    k = 12 if grain == "month" else 4
    assert h["current_mrr"] == pytest.approx(r.waterfall.iloc[-1]["ending"] * k / 12)    # last period's revenue, monthly
    assert h["starting_arr"] == pytest.approx(r.waterfall.iloc[-1]["beginning"] * k)
    assert h["current_mrr"] < read(SAMPLE)["revenue"].astype(float).sum() / 12           # far below the historical total
