"""Score the frozen forecast against what the company actually reported (reference implementation).

Budget-vs-actual bridge by sequential substitution
-------------------------------------------------
Start from the frozen plan (all drivers as set at the cutoff). Replace one group of drivers at a
time with its *actual-implied* value -- the value that, fed through the same model, reproduces
what was reported -- and attribute each step's change to that group. With every group replaced
the model lands exactly on the reported figures, so the components sum to actual - plan.

Order (volume before rate, revenue before cost): the attribution depends on the order, which is
fixed here and stated wherever the bridge is shown. Groups 4.. come from the config's yoy revenue
segments, one each (saas_metrics/segments.py); MongoDB's are listed below.

   1 expansion          net expansion of the existing ARR book (disclosed NRR, constrained as in the ledger)
   2 new_logo_volume    gross new logos
   3 new_logo_arr       ARR per new logo
   4 ea_mix_timing      Enterprise Advanced revenue (term-license timing; also moves Atlas via the TTM)
   5 services           services revenue
   6 gross_margin       subscription and services gross margin, incl. SBC in cost of revenue (non-GAAP)
   7 headcount_pace     employees
   8 cost_per_head      non-GAAP spend per employee (R&D, S&M, G&A)
   9 amortization       amortization of intangibles
  10 fcf_conversion     FCF / non-GAAP operating income

Revenue is fully explained after step 5, non-GAAP operating income after step 9, FCF after 10
(in general: after 3 + the number of yoy segments, then 4 more, then 1).

Actual-implied drivers (quarter t, from reported data)
  g_t        = min(NRR_t^(1/4) - 1, ARR_t / ARR_{t-1} - 1)     (the cohort ledger's rule)
  new logos  = C_t - C_{t-1} (1 - churn)                        (same [Illustrative] churn as the plan)
  ARR / logo = (ARR_t - ARR_{t-1} (1 + g_t)) / new logos
  EA, services (each yoy segment) growth = value_t / value_{t-4} - 1
  gross margins, headcount, spend per employee, amortization, FCF conversion = as reported
Headcount after the last 10-K (January 2026) is [Estimated]; so the split between headcount pace
and cost per head is uncertain for Q1-Q2 FY27, though their sum is exact.
"""
from __future__ import annotations

import duckdb
import pandas as pd

from saas_metrics import config, drivers, model, segments

ARR_GROUPS = {
    "expansion": "Expansion of existing customers (NRR / usage)",
    "new_logo_volume": "New-logo volume",
    "new_logo_arr": "ARR per new logo",
}
COST_GROUPS = {
    "gross_margin": "Gross margin rate",
    "headcount_pace": "Headcount pace",
    "cost_per_head": "Cost per head",
    "amortization": "Amortization",
}
CASH_GROUPS = {"fcf_conversion": "FCF conversion"}


def _segment_groups() -> dict[str, str]:
    return {s.bridge_group: s.bridge_label or f"{s.short} revenue" for s in segments.yoy()}


def group_labels() -> dict[str, str]:
    """Bridge groups of the active company, in substitution order, with their labels."""
    out = {**ARR_GROUPS, **_segment_groups(), **COST_GROUPS, **CASH_GROUPS}
    assert list(out) == segments.bridge_order(segments.active())
    return out


def groups() -> tuple[str, ...]:
    return tuple(group_labels())


def explains() -> dict[str, int]:
    """Number of groups that fully explain each metric."""
    n_rev = len(ARR_GROUPS) + len(_segment_groups())
    return {"revenue": n_rev, "ng_op_inc": n_rev + len(COST_GROUPS), "fcf": n_rev + len(COST_GROUPS) + len(CASH_GROUPS)}


def __getattr__(name: str):  # GROUPS, GROUP_LABELS, EXPLAINS follow the active company
    if name == "GROUPS":
        return groups()
    if name == "GROUP_LABELS":
        return group_labels()
    if name == "EXPLAINS":
        return explains()
    raise AttributeError(name)


def actual_rows(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Every quarter as finally reported (latest filings), in the model's row layout."""
    hist = con.execute("SELECT * FROM q_wide WHERE vintage = 'latest' ORDER BY period_end").df()
    cal = con.execute("SELECT * FROM cohort_calibration WHERE vintage = 'latest'").df().set_index("period_end")
    hc = drivers.headcount_history(con, hist, as_of=None).set_index("period_end")
    rows = pd.DataFrame(model._history_rows(hist, cal, hc["headcount"])).set_index("period_end")
    rows["hc_basis"] = hc["hc_tag"]
    nrr = con.execute("""SELECT period_end, value / 100 AS nrr FROM kpi_disclosures WHERE kpi = ?""",
                      [config.ACTIVE.raw["cohort_ledger"]["nrr_kpi"]]).df().set_index("period_end")["nrr"]
    rows["nrr"] = nrr.reindex(rows.index)
    out = []
    recs = rows.reset_index().to_dict("records")
    for i, r in enumerate(recs):
        model._derived(r, recs[max(0, i - 3):i] if i >= 3 else [])
        r["fcf"] = r["cfo"] - r["capex"]
        out.append(r)
    return pd.DataFrame(out).set_index("period_end")


def implied_drivers(act: pd.DataFrame, plan: dict[str, float]) -> pd.DataFrame:
    """Actual-implied driver values for each forecast quarter."""
    out = {}
    churn = plan["churn_q"]
    svc = segments.nonrecurring().key
    for e in model.forecast_quarters():
        i = act.index.get_loc(e)
        r, p, y = act.iloc[i], act.iloc[i - 1], act.iloc[i - 4]
        g = min(r.nrr ** (1 / model.QUARTERS_PER_YEAR) - 1, r.arr / p.arr - 1)
        nl = r.customers - p.customers * (1 - churn)
        new_arr = r.arr - p.arr * (1 + g)
        out[e] = {
            "g": g, "new_logos": nl, "arr_per_new_logo": new_arr / nl,
            **{f"{s.key}_yoy": r[s.key] / y[s.key] - 1 for s in segments.yoy()},
            "sub_gm": 1 - r.cost_sub / r["sub"], "svc_gm": 1 - r.cost_svc / r[svc],
            "sbc_cogs_pct": (r.sbc_cost_subscription + r.sbc_cost_services) / r.revenue,
            "headcount": r.headcount, **{f"cph_{d}": r[f"cph_{d}"] for d in drivers.DEPTS},
            "amort": r.amort, "fcf_conv": r.fcf / r.ng_op_inc,
        }
    return pd.DataFrame(out).T


def simulate(hist: pd.DataFrame, plan: dict[str, float], implied: pd.DataFrame, n_actual: int) -> pd.DataFrame:
    """Run the 4 forecast quarters with the first `n_actual` driver groups set to actual-implied values.

    hist: as-of-cutoff history rows from saas_metrics.model (for t-1 / t-4 references and T0 opening values).
    """
    use = set(groups()[:n_actual])
    res, svc, ttm = segments.residual(), segments.nonrecurring().key, segments.ttm()
    rows = hist.to_dict("records")
    t0 = rows[-1]
    hc_prev, cph_prev = t0["headcount"], {d: t0[f"cph_{d}"] for d in drivers.DEPTS}
    out = []
    for e in model.forecast_quarters():
        a = implied.loc[e]
        p, y = rows[-1], rows[-4]
        r = {"period_end": e, "days": model.days_in(e), "fiscal_q": model.periods.fiscal(e)[1]}
        g = a.g if "expansion" in use else plan["nrr"] ** (1 / model.QUARTERS_PER_YEAR) - 1
        nl = a.new_logos if "new_logo_volume" in use else plan["new_logos"]
        arpl = a.arr_per_new_logo if "new_logo_arr" in use else plan["arr_per_new_logo"]
        r["arr"] = p["arr"] * (1 + g) + nl * arpl
        for s in segments.yoy():
            k = f"{s.key}_yoy"
            r[s.key] = y[s.key] * (1 + (a[k] if s.bridge_group in use else plan[k]))
        for s in ttm:
            r[f"{s.key}_ttm"] = sum(q[s.key] for q in rows[-3:]) + r[s.key]
        r[res.key] = (r["arr"] - sum(r[f"{s.key}_ttm"] for s in ttm)) * r["days"] / model.DAYS_PER_YEAR
        r["sub"] = sum(r[s.key] for s in segments.recurring())
        r["revenue"] = r["sub"] + r[svc]
        sub_gm = a.sub_gm if "gross_margin" in use else plan["sub_gm"]
        svc_gm = a.svc_gm if "gross_margin" in use else plan["svc_gm"]
        r["gp"] = r["revenue"] - r["sub"] * (1 - sub_gm) - r[svc] * (1 - svc_gm)
        # non-GAAP adds back the SBC inside cost of revenue (part of the non-GAAP gross margin rate)
        sbc_cogs = (a.sbc_cogs_pct if "gross_margin" in use
                    else plan["sbc_pct_cost_subscription"] + plan["sbc_pct_cost_services"])
        r["ng_gp"] = r["gp"] + r["revenue"] * sbc_cogs
        hc_prev = a.headcount if "headcount_pace" in use else hc_prev + plan["net_hires_q"]
        r["headcount"] = hc_prev
        ng = 0.0
        for d in drivers.DEPTS:
            cph_prev[d] = cph_prev[d] * (1 + plan[f"cph_growth_{d}"])   # the plan path, always advanced
            cph = a[f"cph_{d}"] if "cost_per_head" in use else cph_prev[d]
            ng += r["headcount"] * cph
        r["ng_opex"] = ng
        r["amort"] = a.amort if "amortization" in use else plan["amort_q"]
        r["ng_op_inc"] = r["ng_gp"] - r["ng_opex"] + r["amort"]
        conv = a.fcf_conv if "fcf_conversion" in use else plan[f"fcf_conv_q{r['fiscal_q']}"]
        r["fcf"] = r["ng_op_inc"] * conv
        rows.append(r)
        out.append(r)
    return pd.DataFrame(out).set_index("period_end")


def bridge(con: duckdb.DuckDBPyConnection, scenario: str = "Base") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (bridge, steps). bridge: metric x group x quarter contribution (plus 'plan', 'actual')."""
    hist = drivers.history(con)
    cal = con.execute("SELECT * FROM cohort_calibration WHERE vintage='as_of_cutoff'").df().set_index("period_end")
    hc = drivers.headcount_history(con, hist).set_index("period_end")["headcount"]
    drv = drivers.derive(con)
    plan = {d.key: d.value(scenario) for d in drv}
    base = model.run(hist, cal, hc, drv, scenario)
    hist_rows = base[base.kind == "Actual"].reset_index()
    act = actual_rows(con)
    implied = implied_drivers(act, plan)
    gs = groups()
    steps = {k: simulate(hist_rows, plan, implied, k) for k in range(len(gs) + 1)}
    recs = []
    for metric, n in explains().items():
        for e in model.forecast_quarters():
            recs.append({"metric": metric, "component": "plan", "period_end": e, "value": steps[0].at[e, metric]})
            for k in range(1, n + 1):
                recs.append({"metric": metric, "component": gs[k - 1], "period_end": e,
                             "value": steps[k].at[e, metric] - steps[k - 1].at[e, metric]})
            recs.append({"metric": metric, "component": "actual", "period_end": e, "value": act.at[e, metric]})
    long = pd.DataFrame(recs)
    step_df = pd.concat({k: v for k, v in steps.items()}, names=["step"])
    return long, step_df


# ---- damped-trend baseline ---------------------------------------------------------------------
# Holt's linear trend with a damped slope (Gardner & McKenzie), on quarterly revenue to T0:
#   level_t = a * y_t + (1 - a) * (level_{t-1} + phi * trend_{t-1})
#   trend_t = b * (level_t - level_{t-1}) + (1 - b) * phi * trend_{t-1}
#   forecast h quarters ahead = level_T + (phi + phi^2 + ... + phi^h) * trend_T
# Starts from level_0 = y_0, trend_0 = y_1 - y_0. (a, b, phi) minimise one-step-ahead squared error
# over the history on a fixed grid, so the baseline uses nothing after the cutoff.
DAMPED_GRID = {"alpha": [k / 20 for k in range(1, 20)], "beta": [k / 20 for k in range(1, 20)],
               "phi": [k / 100 for k in range(80, 100, 2)]}


def damped_path(y: list[float], alpha: float, beta: float, phi: float) -> tuple[list[float], list[float], float]:
    """(levels, trends, one-step-ahead SSE) for the series y."""
    lv, tr, sse = [y[0]], [y[1] - y[0]], 0.0
    for t in range(1, len(y)):
        pred = lv[-1] + phi * tr[-1]
        if t >= 2:
            sse += (y[t] - pred) ** 2
        lv.append(alpha * y[t] + (1 - alpha) * pred)
        tr.append(beta * (lv[-1] - lv[-2]) + (1 - beta) * phi * tr[-1])
    return lv, tr, sse


def damped_fit(y: list[float]) -> dict[str, float]:
    best = None
    for a in DAMPED_GRID["alpha"]:
        for b in DAMPED_GRID["beta"]:
            for phi in DAMPED_GRID["phi"]:
                sse = damped_path(y, a, b, phi)[2]
                if best is None or sse < best[0]:
                    best = (sse, a, b, phi)
    return {"alpha": best[1], "beta": best[2], "phi": best[3]}


def damped_forecast(y: list[float], n: int) -> tuple[dict[str, float], list[float]]:
    p = damped_fit(y)
    lv, tr, _ = damped_path(y, p["alpha"], p["beta"], p["phi"])
    out, f = [], lv[-1]
    for h in range(1, n + 1):
        f += p["phi"] ** h * tr[-1]
        out.append(f)
    return p, out


def scorecard(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Revenue and non-GAAP operating income: driver model vs naive baselines vs guidance vs actual."""
    hist = drivers.history(con)
    cal = con.execute("SELECT * FROM cohort_calibration WHERE vintage='as_of_cutoff'").df().set_index("period_end")
    hc = drivers.headcount_history(con, hist).set_index("period_end")["headcount"]
    drv = drivers.derive(con)
    runs = {sc: model.run(hist, cal, hc, drv, sc) for sc in drivers.SCENARIOS}
    act = actual_rows(con)
    h = runs["Base"][runs["Base"].kind == "Actual"]
    rev = list(h.revenue)
    g_ttm = sum(rev[-4:]) / sum(rev[-8:-4]) - 1
    g_q = (rev[-1] / rev[-5]) ** (1 / model.QUARTERS_PER_YEAR) - 1
    fq = model.forecast_quarters()
    guide = con.execute("SELECT period, metric, mid FROM guidance WHERE known_at_cutoff AND filed = "
                        "(SELECT max(filed) FROM guidance WHERE known_at_cutoff)").df()
    gmid = {(r.period, r.metric): r.mid for r in guide.itertuples()}
    rep = con.execute("SELECT period, metric, value FROM reported_non_gaap").df()
    reported = {(r.period, r.metric): r.value for r in rep.itertuples()}
    lab = {e: model.periods.label(e) for e in fq}
    fy = lab[fq[0]].split()[1]                                 # fiscal year of the first test quarter
    q_before = [x for x in h.index if model.periods.label(x).endswith(fy)]   # its quarters already reported

    def implied_q4(metric: str, actual_of) -> float | None:
        """Guidance for the fiscal year's last quarter = FY guide - quarters reported - next-quarter guide."""
        fy_mid, q_mid = gmid.get((fy, metric)), gmid.get((lab[fq[0]], metric))
        if fy_mid is None or q_mid is None:
            return None
        return fy_mid - sum(actual_of(x) for x in q_before) - q_mid

    damped_p, damped = damped_forecast(rev, len(fq))
    rows, trend = [], rev[-1]
    for k, e in enumerate(fq):
        trend *= 1 + g_q
        r = {"period_end": e, "fiscal_label": lab[e], "actual_revenue": act.at[e, "revenue"],
             # same quarter a year earlier, from the as-of-cutoff history (every t-4 is on or before T0)
             "seasonal_naive": h.at[model.periods.prev_quarter_end(model.periods.prev_quarter_end(
                 model.periods.prev_quarter_end(model.periods.prev_quarter_end(e)))).isoformat(), "revenue"]
             * (1 + g_ttm),
             "trend_naive": trend}
        for sc in drivers.SCENARIOS:
            r[f"model_{sc.lower()}"] = runs[sc].at[e, "revenue"]
        r["damped_trend"] = damped[k]
        if k == 0:
            r["guidance"] = gmid.get((lab[e], "revenue"))
        elif lab[e].startswith("Q4") and lab[e].endswith(fy):
            r["guidance"] = implied_q4("revenue", lambda x: h.at[x, "revenue"])
        else:
            r["guidance"] = None
        r["actual_ng_op_model_def"] = act.at[e, "ng_op_inc"]
        r["model_base_ng_op"] = runs["Base"].at[e, "ng_op_inc"]
        r["actual_ng_op_company_def"] = reported.get((lab[e], "non_gaap_operating_income"))
        if k == 0:
            r["guidance_ng_op"] = gmid.get((lab[e], "non_gaap_operating_income"))
        elif lab[e].startswith("Q4") and lab[e].endswith(fy):
            r["guidance_ng_op"] = implied_q4("non_gaap_operating_income",
                                             lambda x: reported[(model.periods.label(x), "non_gaap_operating_income")])
        else:
            r["guidance_ng_op"] = None
        rows.append(r)
    df = pd.DataFrame(rows).set_index("period_end")
    df.attrs.update({"g_ttm": g_ttm, "g_q": g_q, "damped": damped_p})
    return df

