"""Source tables for SynthCo (configs/examples/SYNTHCO.yaml): a FICTIONAL company. Not real data.

`source_tables()` returns the same five tables `saas_metrics build` parses from SEC files (filings, facts,
kpi_disclosures, guidance, reported_non_gaap), generated from a seeded random process, so the full
pipeline (quarterly derivation, cohort ledger, model, workbook, backtest) runs on a second,
differently shaped company. Deterministic: same seed, same tables. Used by
tests/test_synthetic_company.py; run directly to print a summary.

How the "filings" are laid out (the derivation paths they exercise)
  10-Q (fiscal Q1-Q3): income-statement lines for the quarter and year to date; cash-flow lines
                       (CFO, capex, SBC) only year to date, so their Q2 and Q3 come from YTD differencing
  10-K (fiscal Q4):    fiscal-year facts only, so Q4 = FY - 9M YTD
  KPIs: customers, >$50k customers and net revenue retention every quarter; employees at year end.
  Earnings 8-K: next-quarter and full-year guidance, and company-defined non-GAAP operating income.
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from saas_metrics import company, lines
from saas_metrics.fiscal import FiscalCalendar

CONFIG = Path(__file__).resolve().parents[1] / "configs" / "examples" / "SYNTHCO.yaml"
SEED = 20261016
CASH_FLOW = ("cfo", "capex", "sbc_total")       # reported year-to-date only in 10-Qs
FIRST, LAST = "2021-03-31", "2026-06-30"        # quarters generated (the first filing is Q3 FY21)


def _series(cal: FiscalCalendar, rnd: random.Random) -> pd.DataFrame:
    """One row per quarter, in dollars (whole thousands), with every line and KPI."""
    rows = []
    run_rate = 400e6
    for i, e in enumerate(cal.quarter_ends(FIRST, LAST)):
        q = cal.fiscal(e)[1]
        days = (e - cal.quarter_start(e)).days + 1
        k = lambda x: round(x / 1000) * 1000  # noqa: E731  whole thousands, like a filing
        run_rate *= 1 + 0.055 - 0.0008 * i + rnd.gauss(0, 0.004)
        platform = k(run_rate * days / 365)
        addons = k(20e6 * 1.30 ** (i / 4) * (0.90, 1.00, 1.00, 1.15)[q - 1] * (1 + rnd.gauss(0, 0.02)))
        licenses = k(30e6 * 1.12 ** (i / 4) * (0.70, 0.90, 0.80, 1.60)[q - 1] * (1 + rnd.gauss(0, 0.05)))
        sub = platform + addons + licenses
        prosvc = k(sub * 0.035 * (1 + rnd.gauss(0, 0.05)))
        rev = sub + prosvc
        cost_sub = k(sub * (0.24 - 0.0005 * i) * (1 + rnd.gauss(0, 0.01)))
        cost_svc = k(prosvc * 1.15 * (1 + rnd.gauss(0, 0.03)))
        sbc = {"sbc_cost_subscription": k(rev * 0.012), "sbc_cost_services": k(rev * 0.004),
               "sbc_rnd": k(rev * (0.075 - 0.0006 * i)), "sbc_snm": k(rev * (0.055 - 0.0005 * i)),
               "sbc_gna": k(rev * 0.03)}
        heads = 3000 + 90 * i + rnd.randint(-15, 15)
        ng = {"rnd": rev * (0.21 - 0.0012 * i), "snm": rev * (0.33 - 0.0025 * i), "gna": rev * (0.10 - 0.0006 * i)}
        opex = {d: k(ng[d] * (1 + rnd.gauss(0, 0.01))) + sbc[f"sbc_{d}"] for d in ng}
        amort = 1_500_000 if e.year < 2024 else 2_000_000
        gp = rev - cost_sub - cost_svc
        op_inc = gp - sum(opex.values())
        sbc_total = sum(sbc.values())
        ng_op = op_inc + sbc_total + amort
        cfo = k(ng_op * (1.6, 0.9, 0.8, 0.95)[q - 1] * (1 + rnd.gauss(0, 0.03)) + 0.05 * rev)
        rows.append({
            "period_end": e, "fiscal_q": q,
            "revenue_platform": platform, "revenue_addons": addons, "revenue_licenses": licenses,
            "revenue_subscription": sub, "revenue_prosvc": prosvc, "revenue": rev,
            "cost_subscription": cost_sub, "cost_services": cost_svc, "cost_of_revenue": cost_sub + cost_svc,
            "gross_profit": gp, **opex, "opex_total": sum(opex.values()), "operating_income": op_inc,
            **sbc, "sbc_total": sbc_total, "amort_intangibles": amort, "cfo": cfo, "capex": k(rev * 0.015),
            # KPIs
            "total_customers": 5000 + 160 * i + rnd.randint(-20, 20),
            "customers_50k_arr": 700 + 22 * i + rnd.randint(-5, 5),
            "net_revenue_retention": round(118 - 0.25 * i),
            "employees": heads,
            "ng_company": ng_op + k(0.004 * rev),       # company definition excludes a little more
        })
    return pd.DataFrame(rows).set_index("period_end")


def source_tables(seed: int = SEED) -> dict[str, pd.DataFrame]:
    co = company.load(CONFIG)
    cal = FiscalCalendar(co.fiscal_year_end_month)
    cutoff = co.raw["backtest"]["cutoff_date"]
    s = _series(cal, random.Random(seed))
    lns = {ln.name: ln for ln in lines.from_config(co.raw)}
    filings, facts, kpis, guide, ng_rep = [], [], [], [], []
    ends = [e for e in s.index if e >= date.fromisoformat(co.raw["sec"]["filings_from"])]
    for n, e in enumerate(ends):
        q = cal.fiscal(e)[1]
        form = "10-K" if q == 4 else "10-Q"
        filed = (e + timedelta(days=60 if form == "10-K" else 38)).isoformat()
        accn = f"0000000000-{e.year % 100:02d}-{2 * n + 1:06d}"
        filings.append({"accn": accn, "form": form, "filed": filed, "period_end": e.isoformat(),
                        "document": f"synthco-{e:%Y%m%d}.htm"})
        fy_start, q_start = cal.fy_start(e), cal.quarter_start(e)
        in_fy = [x for x in s.index if fy_start <= x <= e]
        for name, ln in lns.items():
            spans = [(fy_start, s.loc[in_fy, name].sum())]              # FY or year-to-date
            if form == "10-Q" and name not in CASH_FLOW and q > 1:
                spans.append((q_start, s.at[e, name]))                  # and the quarter
            for start, v in spans:
                facts.append({"accn": accn, "filed": filed, "form": form, "concept": ln.concept, "axis": ln.axis,
                              "member": ln.member, "start": start.isoformat(), "end": e.isoformat(),
                              "value": float(v), "unit": "usd", "decimals": "-3", "in_scope": True})
        kpi_names = ["total_customers", "customers_50k_arr", "net_revenue_retention"] + (["employees"] if q == 4 else [])
        for kp in kpi_names:
            kpis.append({"accn": accn, "form": form, "filed": filed, "period_end": e.isoformat(), "kpi": kp,
                         "value": float(s.at[e, kp]), "unit": "percent" if kp == "net_revenue_retention" else "count",
                         "qualifier": "", "snippet": "synthetic", "method": "synthetic"})
        # earnings 8-K two days before the 10-Q/10-K: this quarter's non-GAAP result and guidance
        e8k = (e + timedelta(days=(58 if form == "10-K" else 36))).isoformat()
        a8k = f"0000000000-{e.year % 100:02d}-{2 * n + 2:06d}"
        ng_rep.append({"accn": a8k, "filed": e8k, "period": cal.label(e), "metric": "non_gaap_operating_income",
                       "value": float(s.at[e, "ng_company"]), "snippet": "synthetic", "known_at_cutoff": e8k <= cutoff})
        nxt = [x for x in s.index if x > e][:1]
        if not nxt:
            continue
        nq = nxt[0]
        nfy = cal.fiscal(nq)[0]
        fy_q = [x for x in s.index if cal.fiscal(x)[0] == nfy]
        for metric, col in (("revenue", "revenue"), ("non_gaap_operating_income", "ng_company")):
            targets = {cal.label(nq): s.at[nq, col]}
            if len(fy_q) == 4:
                targets[f"FY{nfy % 100:02d}"] = s.loc[fy_q, col].sum()
            for period, actual in targets.items():
                mid = round(actual * (0.98 if metric == "revenue" else 0.90) / 1e6, 1) * 1e6   # guided conservatively
                guide.append({"accn": a8k, "filed": e8k, "issued": e8k, "period": period, "metric": metric,
                              "low": mid - 2e6, "high": mid + 2e6, "mid": mid, "snippet": "synthetic",
                              "known_at_cutoff": e8k <= cutoff})
    return {"filings": pd.DataFrame(filings), "facts": pd.DataFrame(facts), "kpi_disclosures": pd.DataFrame(kpis),
            "guidance": pd.DataFrame(guide), "reported_non_gaap": pd.DataFrame(ng_rep)}


if __name__ == "__main__":
    t = source_tables()
    print("SynthCo (fictional): " + ", ".join(f"{k} {len(v)}" for k, v in t.items()))
