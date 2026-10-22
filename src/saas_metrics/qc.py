"""Pre-flight checks on the built store. Each returns (name, passed, detail).

`run(con)` is called by `saas_metrics qc` and asserted check-by-check in tests/test_qc.py.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pandas as pd

from saas_metrics import config, periods
from saas_metrics import lines as _lines

TOL = 0.5  # dollars; XBRL values are whole dollars (MongoDB reports in thousands)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


def _wide(con, vintage="latest") -> pd.DataFrame:
    return con.execute("SELECT * FROM q_wide WHERE vintage = ? AND in_window ORDER BY period_end",
                       [vintage]).df().set_index("fiscal_label")


def completeness(con) -> Check:
    n = con.execute("""SELECT line, vintage, count(*) n FROM quarterly_financials WHERE in_window
                       GROUP BY ALL""").df()
    want = {"latest": len(periods.quarter_ends(config.FIRST_QUARTER_END, config.LAST_QUARTER_END)),
            "as_of_cutoff": len(periods.quarter_ends(config.FIRST_QUARTER_END, config.T0_QUARTER_END))}
    bad = [f"{r.line}/{r.vintage}={r.n}" for r in n.itertuples() if r.n != want[r.vintage]]
    missing = {ln.name for ln in _lines.LINES} - set(n.line)
    ok = not bad and not missing
    return Check(f"every line has {want['latest']} quarters ({want['as_of_cutoff']} as of cutoff)", ok,
                 "ok" if ok else f"short: {bad[:8]} missing: {sorted(missing)}")


def identity_specs() -> dict[str, tuple[str, str]]:
    """name -> (left, right) expressions over line names (checks.identities in the config)."""
    out = {}
    for e in config.ACTIVE.raw.get("checks", {}).get("identities", []):
        left, right = (x.strip() for x in e["expr"].split("="))
        out[e.get("name", e["expr"])] = (left, right)
    return out


def identities(con) -> list[Check]:
    out = []
    for vintage in ("latest", "as_of_cutoff"):
        w = _wide(con, vintage)
        for name, (left, right) in identity_specs().items():
            d = (w.eval(left) - w.eval(right)).abs()
            out.append(Check(f"[{vintage}] {name}", bool((d <= TOL).all()),
                             f"max |diff| ${d.max():,.0f}" + ("" if (d <= TOL).all() else f" in {list(d[d > TOL].index)}")))
    return out


def quarters_sum_to_year(con) -> Check:
    """For each complete fiscal year in the window, Q1..Q4 (as derived) = the 10-K annual figure.

    Q4 is FY - 9M YTD, so this holds only if Q1 + Q2 + Q3 equal the 9-month YTD the filings report.
    """
    q = con.execute("""SELECT line, fiscal_year, fiscal_quarter, period_end, value FROM quarterly_financials
                       WHERE vintage = 'latest' AND in_window""").df()
    facts = con.execute("""SELECT concept, axis, member, start, "end", value FROM facts
                           WHERE form = '10-K' AND start IS NOT NULL""").df()
    diffs, n = [], 0
    for ln in (ln for ln in _lines.LINES if ln.kind == "duration"):
        lq = q[q.line == ln.name]
        for fy, g in lq.groupby("fiscal_year"):
            if set(g.fiscal_quarter) != {1, 2, 3, 4}:
                continue
            fy_end = g.period_end.max()
            fy_start = periods.fy_start(fy_end).isoformat()
            for axis, member in ln.keys():
                hit = facts[(facts.concept == ln.concept) & (facts.axis == axis) & (facts.member == member)
                            & (facts.start == fy_start) & (facts["end"] == fy_end)]
                if len(hit):
                    annual = hit.value.iloc[0]  # identical across 10-Ks unless restated
                    diffs.append((ln.name, fy, g.value.sum() - annual))
                    n += 1
                    break
    worst = max(diffs, key=lambda d: abs(d[2])) if diffs else None
    ok = n > 0 and all(abs(d[2]) <= TOL for d in diffs)
    return Check("derived quarters sum to the 10-K fiscal year", ok,
                 f"{n} line-years checked, worst {worst[0]} FY{worst[1]} ${worst[2]:,.0f}" if worst else "nothing checked")


def companyfacts_match(con, companyfacts: dict) -> Check:
    """Every undimensioned fact parsed from the filings equals SEC's companyfacts value."""
    rows = [("us-gaap:" + tag, f.get("start"), f["end"], f["accn"], f["val"])
            for tag, d in companyfacts["facts"]["us-gaap"].items()
            for fs in d["units"].values() for f in fs]
    cf = pd.DataFrame(rows, columns=["concept", "start", "end", "accn", "cf_val"])
    x = con.execute("""SELECT concept, start, "end", accn, value FROM facts
                       WHERE axis = '' AND concept LIKE 'us-gaap:%'""").df()
    m = x.merge(cf, on=["concept", "start", "end", "accn"], how="left")
    unmatched = int(m.cf_val.isna().sum())
    mismatched = int((m.cf_val.notna() & ((m.value - m.cf_val).abs() > TOL)).sum())
    ok = unmatched == 0 and mismatched == 0
    return Check("parsed facts match SEC companyfacts", ok,
                 f"{len(x)} facts, {unmatched} not in companyfacts, {mismatched} value mismatches")


def fact_conflicts(con) -> Check:
    """In-scope facts for the same period must agree across filings (or it is a restatement to explain)."""
    df = con.execute("""SELECT concept, axis, member, start, "end", count(DISTINCT value) AS n
                        FROM facts WHERE in_scope GROUP BY ALL HAVING n > 1""").df()
    out = con.execute("SELECT count(*) FROM facts WHERE NOT in_scope").fetchone()[0]
    return Check("in-scope facts agree across filings", len(df) == 0,
                 f"{len(df)} conflicting periods; {out} out-of-scope facts ignored")


def point_in_time(con) -> Check:
    leaks = con.execute("""SELECT count(*) FROM quarterly_financials
                           WHERE vintage = 'as_of_cutoff' AND filed > ?""", [config.CUTOFF_DATE]).fetchone()[0]
    after_t0 = con.execute("""SELECT count(*) FROM quarterly_financials
                              WHERE vintage = 'as_of_cutoff' AND period_end > ?""", [config.T0_QUARTER_END]).fetchone()[0]
    ok = leaks == 0 and after_t0 == 0
    return Check("no post-cutoff information in the as-of-cutoff vintage", ok,
                 f"{leaks} rows filed after {config.CUTOFF_DATE}, {after_t0} quarters after T0")


def restatements(con) -> Check:
    r = con.execute("""SELECT count(*) FROM quarterly_financials a JOIN quarterly_financials b USING (period_end, line)
                       WHERE a.vintage = 'as_of_cutoff' AND b.vintage = 'latest' AND abs(a.value - b.value) > ?""",
                    [TOL]).fetchone()[0]
    return Check("restatements between cutoff and latest (informational)", True,
                 f"{r} line-quarters changed after the cutoff")


def cohort_ledger(con) -> list[Check]:
    out = []
    arr = con.execute("""SELECT c.vintage, c.period_end, c.arr_proxy, sum(a.arr) AS ledger
                         FROM cohort_calibration c JOIN cohort_arr a USING (vintage, period_end)
                         GROUP BY ALL""").df()
    d = (arr.ledger - arr.arr_proxy).abs()
    out.append(Check("cohort ARR sums to the ARR proxy", bool((d <= 1).all()), f"max |diff| ${d.max():,.2f}"))
    logos = con.execute("""SELECT c.vintage, c.period_end, c.total_customers, sum(l.customers) AS ledger
                           FROM cohort_calibration c JOIN cohort_logos l USING (vintage, period_end)
                           GROUP BY ALL""").df()
    d = (logos.ledger - logos.total_customers).abs()
    out.append(Check("cohort logos sum to reported customers", bool((d <= 1e-6).all()), f"max |diff| {d.max():.2e}"))
    neg = con.execute("""SELECT count(*) FROM cohort_calibration
                         WHERE new_cohort_arr < -1 OR new_logos < 0""").fetchone()[0]
    out.append(Check("no negative new-cohort ARR or new logos", neg == 0, f"{neg} quarters negative"))
    gaps = con.execute("""
        WITH c AS (SELECT *, bool_or(coalesce(constrained, false)) OVER
                     (PARTITION BY vintage ORDER BY period_end ROWS 3 PRECEDING) AS window_constrained
                   FROM cohort_calibration)
        SELECT fiscal_label, vintage, nrr_ledger_t4q - nrr_disclosed AS gap
        FROM c WHERE nrr_ledger_t4q IS NOT NULL AND NOT window_constrained""").df()
    worst = gaps.gap.abs().max() if len(gaps) else float("nan")
    out.append(Check("ledger NRR within 1.5pp of disclosed (windows without constrained quarters)",
                     len(gaps) > 0 and bool((gaps.gap.abs() <= 0.015).all()),
                     f"{len(gaps)} quarters, worst {worst * 100:.2f}pp"))
    return out


def run(con: duckdb.DuckDBPyConnection, companyfacts_path: Path | None = None) -> list[Check]:
    path = companyfacts_path or (config.RAW / "companyfacts.json")
    opener = __import__("gzip").open if str(path).endswith(".gz") else open
    with opener(path, "rt") as fh:
        cf = json.load(fh)
    return [completeness(con), *identities(con), quarters_sum_to_year(con), companyfacts_match(con, cf),
            fact_conflicts(con), point_in_time(con), restatements(con), *cohort_ledger(con)]
