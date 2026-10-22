"""Bring-your-own billing data: QC, waterfall, retention, concentration, tier migration.

The SQL engine is checked against an independent plain-Python implementation of the same
definitions, and the waterfall identity is checked to the cent.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from saas_metrics import cli
from saas_metrics import cohort_engine as ce

SAMPLE = Path(__file__).parent / "fixtures" / "sample_billing.csv.gz"
TODAY = date(2026, 10, 16)


@pytest.fixture(scope="module")
def monthly():
    return ce.analyze(SAMPLE, today=TODAY)


def reference_waterfall(path, grain="month"):
    """Plain-Python waterfall, written separately from the SQL, from the raw lines."""
    raw = ce.load(path)
    cal_fn = ce.FiscalCalendar(12)
    rev = defaultdict(Decimal)
    for r in raw.itertuples():
        d = pd.to_datetime(r.period).date()
        rev[(r.customer_id.strip(), ce.period_end(d, grain, cal_fn))] += Decimal(r.revenue)
    periods = ce.calendar_between(min(p for _, p in rev), max(p for _, p in rev), grain)
    customers = sorted({c for c, _ in rev})
    out = []
    seen = {c: False for c in customers}
    prev = {c: Decimal(0) for c in customers}
    for p in periods:
        row = dict.fromkeys(("beginning", "new_logo", "reactivation", "expansion", "contraction", "churn",
                             "ending"), Decimal(0))
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
    res = ce.analyze(SAMPLE, grain=grain, today=TODAY)
    sql = res.tables["waterfall"]
    ref = reference_waterfall(SAMPLE, grain)
    assert len(sql) == len(ref)
    for col in ("beginning", "new_logo", "reactivation", "expansion", "contraction", "churn", "ending"):
        assert [Decimal(str(v)) for v in sql[col]] == list(ref[col]), col


def test_waterfall_identity_to_the_cent_and_continuity(monthly):
    w = monthly.tables["waterfall"]
    assert (w.check_diff == 0).all()
    assert (w.beginning.iloc[1:].values == w.ending.iloc[:-1].values).all()
    assert w.beginning.iloc[0] == 0
    assert w.reactivation.sum() > 0 and w.churn.sum() > 0 and w.contraction.sum() > 0   # every path exercised


def test_ending_equals_net_billed_revenue_per_period(monthly):
    raw = ce.load(SAMPLE)
    raw["m"] = pd.to_datetime(raw.period).dt.to_period("M")
    billed = raw.assign(r=raw.revenue.astype(float)).groupby("m").r.sum().round(2)
    assert list(monthly.tables["waterfall"].ending.astype(float).round(2)) == list(billed)


def test_segment_waterfalls_sum_to_total(monthly):
    seg = monthly.tables["waterfall_by_segment"]
    assert (seg.check_diff == 0).all()
    tot = seg.groupby("period_end")[["new_logo", "churn", "ending"]].sum().reset_index()
    w = monthly.tables["waterfall"]
    for col in ("new_logo", "churn", "ending"):   # pandas holds the DECIMAL results as floats: compare to the cent
        assert list(tot[col].round(2)) == list(w[col].round(2))


def test_retention_triangle(monthly):
    r = monthly.tables["retention"]
    first = r[r.age == 0]
    assert (first.logo_retention == 1).all() and (first.ndr == 1).all()
    assert first.logos_start.sum() == r.groupby("vintage").logos_start.first().sum()
    w = monthly.tables["waterfall"]
    assert first.logos_start.sum() == w.new_logos.sum()          # every customer has exactly one vintage
    assert r.logo_retention.between(0, 1.0001).all() or (r.logo_retention > 1).sum() == 0


def test_concentration(monthly):
    c = monthly.tables["concentration"]
    assert c.top_decile_share.between(0, 1).all() and c.top10_share.between(0, 1).all()
    assert (c.hhi > 0).all() and (c.hhi <= 1).all()
    assert list(c.customers) == list(monthly.tables["waterfall"].active_logos)


def test_tier_migration_reconciles(monthly):
    tm = monthly.tables["tier_migration"]
    w = monthly.tables["waterfall"].set_index("period_end")
    by = tm.groupby("period_end")[["revenue_from", "revenue_to"]].sum().round(2)
    assert list(by.revenue_to) == list(w.loc[by.index, "ending"].round(2))
    assert list(by.revenue_from) == list(w.loc[by.index, "beginning"].round(2))
    assert set(tm.from_tier) | set(tm.to_tier) >= {"Inactive", "<$10k", "$10k-$100k", "$100k-$1M"}


def _write(tmp_path, rows, name="t.csv"):
    p = tmp_path / name
    pd.DataFrame(rows).to_csv(p, index=False)
    return p


def test_qc_blocks_bad_inputs(tmp_path):
    ok = {"customer_id": "A", "period": "2025-01-15", "revenue": "100"}
    cases = {
        "schema": [{"customer": "A", "period": "2025-01-15", "revenue": "1"}],
        "nulls": [ok, {**ok, "customer_id": ""}],
        "types": [ok, {**ok, "revenue": "abc"}],
    }
    for name, rows in cases.items():
        res = ce.analyze(_write(tmp_path, rows, f"{name}.csv"), today=TODAY)
        assert not res.tables and any(c.level == "error" and c.name == name for c in res.checks), name


def test_qc_warnings_block_unless_allowed(tmp_path):
    rows = [{"customer_id": "A", "period": "2025-01-15", "revenue": "100"},
            {"customer_id": "A", "period": "2025-03-15", "revenue": "120"},     # February missing entirely
            {"customer_id": "B", "period": "2025-03-15", "revenue": "-30"}]     # net negative
    p = _write(tmp_path, rows)
    blocked = ce.analyze(p, today=TODAY)
    assert not blocked.tables and {c.name for c in blocked.blocking} == {"missing periods", "negative revenue"}
    allowed = ce.analyze(p, allow_warnings=True, today=TODAY)
    assert (allowed.tables["waterfall"].check_diff == 0).all()
    assert any("net-negative" in n for n in allowed.notes)


def test_future_periods_are_flagged(tmp_path):
    p = _write(tmp_path, [{"customer_id": "A", "period": "2025-01-31", "revenue": "1"},
                          {"customer_id": "A", "period": "2027-01-31", "revenue": "1"}])
    res = ce.analyze(p, today=TODAY, allow_warnings=True)
    assert any(c.name == "future periods" for c in res.checks)


def test_parquet_input_gives_same_waterfall(monthly, tmp_path):
    import duckdb
    p = tmp_path / "billing.parquet"
    duckdb.connect().execute(f"COPY (SELECT * FROM read_csv('{SAMPLE}', all_varchar=true)) TO '{p}' (FORMAT parquet)")
    pq = ce.analyze(p, today=TODAY).tables["waterfall"]
    assert list(pq.ending) == list(monthly.tables["waterfall"].ending)


def test_ledger_mode_has_no_waterfall(db, tmp_path):
    db_file = tmp_path / "mdb.duckdb"
    db.execute(f"ATTACH '{db_file}' AS f")
    for t in ("cohort_arr", "cohort_logos"):
        db.execute(f"CREATE TABLE f.{t} AS SELECT * FROM {t}")
    db.execute("DETACH f")
    res = ce.ledger("MDB", db_file)
    assert "waterfall" not in res.tables and "retention" in res.tables
    r = res.tables["retention"]
    start = r[r.age == 0]
    assert (start[start.arr > 0].ndr == 1).all()
    assert start[start.arr == 0].ndr.isna().all() and (start.arr == 0).any()   # floored cohorts: NDR undefined
    assert any("no customer-level waterfall" in n for n in res.notes)
    assert any("start with $0" in n for n in res.notes)


def test_cli_writes_outputs_and_live_waterfall_formulas(tmp_path, capsys):
    cli.main(["cohorts", "--input", str(SAMPLE), "--out", str(tmp_path / "out")])
    out = capsys.readouterr().out
    assert "identity holds in every period: True" in out
    names = {p.name for p in (tmp_path / "out").iterdir()}
    assert {"waterfall.csv", "retention.csv", "concentration.csv", "tier_migration.csv", "cohorts.xlsx"} <= names
    formulas = pytest.importorskip("formulas")
    import re
    sol = {}
    for k, v in formulas.ExcelModel().loads(str(tmp_path / "out" / "cohorts.xlsx")).finish().calculate().items():
        m = re.match(r"'\[[^\]]+\]WATERFALL'!(J\d+)$", k)
        if m and m.group(1) != "J1":   # J1 is the header
            sol[m.group(1)] = v.value[0][0]
    assert sol and max(abs(v) for v in sol.values()) < 1e-6
