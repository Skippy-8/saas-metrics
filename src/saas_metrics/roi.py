"""Unit economics scorecard and the GTM capital-allocation case (reference implementation).

Scorecard (trailing four quarters, so seasonal quarters do not swing the ratios)
  CAC payback (months) = non-GAAP S&M, trailing 4Q to the prior quarter
                         / (net new ARR over 4 quarters x non-GAAP subscription gross margin) x 12
  Magic number         = (subscription revenue TTM - the same a year earlier) / non-GAAP S&M TTM (prior quarter)
  Rule of 40           = revenue growth YoY + FCF margin (TTM)
  Burn multiple        = -FCF TTM / net new ARR, only while FCF is negative ("n/m" otherwise)
  LTV : CAC            [Illustrative]: ARR per customer x gross margin / annual logo churn, over non-GAAP S&M
                         per new logo. Churn is not disclosed, so this ratio inherits the assumed rate.

GTM case: incremental Enterprise AEs vs a product-attach initiative, monthly cash flows over the horizon.
Inputs come from the config's `unit_econ` section (MongoDB: 30 AEs, 36 months). Most are [Illustrative]
(not disclosed); NRR, gross margin and the target customer count (MongoDB: >$100k ARR) are sourced.
"""
from __future__ import annotations

from dataclasses import dataclass

import duckdb
import pandas as pd

from saas_metrics import config, drivers, model, periods

MONTHS_PER_YEAR = 12


def months() -> int:
    """GTM case horizon of the active company (unit_econ.horizon_months)."""
    return int(config.ACTIVE.raw["unit_econ"]["horizon_months"])


def __getattr__(name: str):  # MONTHS follows the active company
    if name == "MONTHS":
        return months()
    raise AttributeError(name)


@dataclass
class RoiInput:
    key: str
    label: str
    value: float
    tag: str
    note: str
    fmt: str


def roi_inputs(con: duckdb.DuckDBPyConnection) -> list[RoiInput]:
    """Inputs for the GTM case, as known at the cutoff."""
    hist = drivers.history(con).set_index("period_end")
    t4 = hist[hist["in_window"]].tail(4)
    sub_gm_ng = 1 - (t4.cost_subscription.sum() - t4.sbc_cost_subscription.sum()) / t4.revenue_subscription.sum()
    kpi = con.execute("""SELECT kpi, value FROM kpi_disclosures WHERE period_end = ? AND filed <= ?""",
                      [config.T0_QUARTER_END, config.CUTOFF_DATE]).df().set_index("kpi")["value"]
    ue = config.ACTIVE.raw["unit_econ"]
    a, b = ue["case_a"], ue["case_b"]
    nrr_kpi = config.ACTIVE.raw["cohort_ledger"]["nrr_kpi"]
    t0 = config.T0_QUARTER_END
    t0_src = f"{periods.label(t0)} {'10-K' if periods.fiscal(t0)[1] == 4 else '10-Q'}."
    ill = "[Illustrative]"
    return [
        RoiInput("roi_n_ae", "A. Incremental Enterprise AEs hired", a["n_ae"], ill,
                 "Size of the hiring bet being evaluated.", "#,##0"),
        RoiInput("roi_ae_cost", "A. Fully loaded cost per AE, annual ($)", a["ae_cost"], ill,
                 "Salary, benefits, T&E; excludes commission.", "$#,##0"),
        RoiInput("roi_support_pct", "A. Support overhead (SEs, managers), % of AE cost", a["support_pct"], ill, "", "0%"),
        RoiInput("roi_quota", "A. Annual quota, new ARR per ramped AE ($)", a["quota"], ill, "", "$#,##0"),
        RoiInput("roi_attain", "A. Quota attainment", a["attainment"], ill, "", "0%"),
        RoiInput("roi_ramp", "A. Ramp to full productivity (months)", a["ramp_months"], ill, "Linear ramp.", "0"),
        RoiInput("roi_commission", "A. Commission, % of booked ARR", a["commission"], ill, "Paid in the month booked.",
                 "0%"),
        RoiInput("roi_target_customers", f"B. {b['target_label']} (T0)", kpi[b["target_kpi"]],
                 "[Sourced - SEC Filing]", t0_src, "#,##0"),
        RoiInput("roi_attach", "B. Attach rate at maturity", b["attach_rate"], ill,
                 f"Share of {b.get('target_short', 'target customers')} adopting.", "0%"),
        RoiInput("roi_attach_arr", "B. Incremental ARR per attaching customer ($)", b["arr_per_attach"], ill, "",
                 "$#,##0"),
        RoiInput("roi_adopt", "B. Months to full adoption", b["adoption_months"], ill, "Linear adoption.", "0"),
        RoiInput("roi_upfront", "B. Upfront build cost, month 1 ($)", b["upfront_cost"], ill, "", "$#,##0"),
        RoiInput("roi_run_cost", "B. Ongoing cost per month ($)", b["run_cost_monthly"], ill, "", "$#,##0"),
        RoiInput("roi_nrr", "Both: net expansion of the new ARR (annual)", kpi[nrr_kpi] / 100,
                 "[Sourced - SEC Filing]", "Disclosed net ARR expansion rate at T0.", "0.0%"),
        RoiInput("roi_gm", "Both: non-GAAP subscription gross margin", sub_gm_ng, "[Historical Derived]",
                 "Trailing 4 quarters to T0.", "0.0%"),
        RoiInput("roi_discount", "Both: discount rate (annual)", ue["discount_rate"], ill,
                 "Hurdle rate, not a WACC estimate.", "0.0%"),
    ]


SOURCED = {"roi_target_customers": "--target-customers", "roi_nrr": "--nrr", "roi_gm": "--gross-margin"}


def resolve_inputs(con: duckdb.DuckDBPyConnection | None, overrides: dict[str, float]
                   ) -> tuple[dict[str, float], dict[str, str], dict[str, str]]:
    """GTM inputs for `saas_metrics unit-econ`: config defaults, sourced values from the store (when built),
    then command-line overrides. Returns (values, labels, source of each value)."""
    ue = config.ACTIVE.raw["unit_econ"]
    if con is not None:
        items = roi_inputs(con)
        vals = {x.key: x.value for x in items}
        labels = {x.key: x.label for x in items}
        src = {x.key: "SEC filings (store)" if x.tag.startswith(("[Sourced", "[Historical")) else "config"
               for x in items}
    else:
        missing = [flag for key, flag in SOURCED.items() if key not in overrides]
        if missing:
            raise ValueError(f"no built store ({config.DB_PATH}); run `saas_metrics build` or give {' '.join(missing)}")
        a, b = ue["case_a"], ue["case_b"]
        vals = {"roi_n_ae": a["n_ae"], "roi_ae_cost": a["ae_cost"], "roi_support_pct": a["support_pct"],
                "roi_quota": a["quota"], "roi_attain": a["attainment"], "roi_ramp": a["ramp_months"],
                "roi_commission": a["commission"], "roi_attach": b["attach_rate"], "roi_attach_arr": b["arr_per_attach"],
                "roi_adopt": b["adoption_months"], "roi_upfront": b["upfront_cost"], "roi_run_cost": b["run_cost_monthly"],
                "roi_discount": ue["discount_rate"]}
        labels, src = {k: k for k in vals}, {k: "config" for k in vals}
    vals["roi_rep_attrition"] = ue["case_a"].get("rep_attrition_annual", 0.0)
    vals["roi_months"] = ue["horizon_months"]
    labels.update(roi_rep_attrition="A. Rep attrition, annual (backfilled; backfills ramp again)",
                  roi_months="Horizon (months)")
    src.update(roi_rep_attrition="config", roi_months="config")
    for k, v in overrides.items():
        vals[k], src[k] = v, "command line"
        labels.setdefault(k, k)
    return vals, labels, src


def gtm_cases(inp: dict[str, float], horizon: int | None = None) -> dict[str, pd.DataFrame]:
    """Monthly cash flows for case A (AE hiring) and case B (product attach).

    Optional `roi_rep_attrition` (annual, default 0): reps who leave are backfilled at once, so the
    team stays at roi_n_ae and the cost is unchanged, but each backfill starts its ramp again. The team
    is tracked as a tenure distribution; with no attrition it is one cohort and the result equals the
    workbook's case A exactly.
    """
    g_m = inp["roi_nrr"] ** (1 / MONTHS_PER_YEAR) - 1
    leave_m = 1 - (1 - inp.get("roi_rep_attrition", 0.0)) ** (1 / MONTHS_PER_YEAR)
    tenure = {1: float(inp["roi_n_ae"])}          # tenure in months -> reps
    a_rows, b_rows = [], []
    arr_a = arr_b = cum_a = cum_b = 0.0
    for m in range(1, int(horizon or inp.get("roi_months") or months()) + 1):
        ramp = (sum(n * min(1.0, t / inp["roi_ramp"]) for t, n in tenure.items()) / inp["roi_n_ae"]
                if leave_m else min(1.0, m / inp["roi_ramp"]))
        booked = inp["roi_n_ae"] * inp["roi_quota"] / MONTHS_PER_YEAR * inp["roi_attain"] * ramp
        if leave_m:
            left = sum(tenure.values()) * leave_m
            tenure = {t + 1: n * (1 - leave_m) for t, n in tenure.items()}
            tenure[1] = left
        arr_a = arr_a * (1 + g_m) + booked
        gp = arr_a / MONTHS_PER_YEAR * inp["roi_gm"]
        cost = (inp["roi_n_ae"] * inp["roi_ae_cost"] / MONTHS_PER_YEAR * (1 + inp["roi_support_pct"])
                + inp["roi_commission"] * booked)
        net = gp - cost
        cum_a += net
        a_rows.append({"month": m, "ramp": ramp, "booked_arr": booked, "arr": arr_a, "gross_profit": gp,
                       "cost": cost, "net": net, "cumulative": cum_a})

        adopt = min(1.0, m / inp["roi_adopt"])
        target_arr = inp["roi_target_customers"] * inp["roi_attach"] * inp["roi_attach_arr"] * adopt
        added = max(0.0, target_arr - (b_rows[-1]["target_arr"] if b_rows else 0.0))
        arr_b = arr_b * (1 + g_m) + added
        gp_b = arr_b / MONTHS_PER_YEAR * inp["roi_gm"]
        cost_b = inp["roi_run_cost"] + (inp["roi_upfront"] if m == 1 else 0.0)
        net_b = gp_b - cost_b
        cum_b += net_b
        b_rows.append({"month": m, "target_arr": target_arr, "booked_arr": added, "arr": arr_b,
                       "gross_profit": gp_b, "cost": cost_b, "net": net_b, "cumulative": cum_b})
    return {"A": pd.DataFrame(a_rows).set_index("month"), "B": pd.DataFrame(b_rows).set_index("month")}


def npv(rate_annual: float, flows: list[float]) -> float:
    """Excel NPV convention: the first flow is discounted one period."""
    r = (1 + rate_annual) ** (1 / MONTHS_PER_YEAR) - 1
    return sum(f / (1 + r) ** (i + 1) for i, f in enumerate(flows))


def irr_annual(flows: list[float]) -> float | None:
    lo, hi = -0.99, 1.0
    f = lambda r: sum(x / (1 + r) ** i for i, x in enumerate(flows))  # noqa: E731
    if f(lo) * f(hi) > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if f(lo) * f(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (1 + (lo + hi) / 2) ** MONTHS_PER_YEAR - 1


def summary(cases: dict[str, pd.DataFrame], inp: dict[str, float]) -> pd.DataFrame:
    out = {}
    for k, df in cases.items():
        neg = int((df.cumulative < 0).sum())
        out[k] = {"cumulative_36m": df.cumulative.iloc[-1], "trough": df.cumulative.min(),
                  "payback_month": neg + 1 if neg < len(df) else None,
                  "npv": npv(inp["roi_discount"], list(df.net)), "irr_annual": irr_annual(list(df.net)),
                  "arr_month_36": df.arr.iloc[-1]}
    return pd.DataFrame(out).T


def scorecard(rows: pd.DataFrame, new_logos: pd.Series, churn_q: float) -> pd.DataFrame:
    """Trailing-4Q efficiency metrics for every quarter with enough history (model row layout)."""
    r = rows.copy()
    r["snm_ng"] = r["snm"] - r["sbc_snm"]
    r["sub_gm_ng"] = (r["sub"] - r["cost_sub"] + r["sbc_cost_subscription"]) / r["sub"]
    for c in ("snm_ng", "sub", "revenue", "fcf"):
        r[f"{c}_ttm"] = r[c].rolling(4).sum()
    r["net_new_arr_ttm"] = r["arr"] - r["arr"].shift(4)
    r["cac_payback_months"] = r["snm_ng_ttm"].shift(1) / (r["net_new_arr_ttm"] * r["sub_gm_ng"]) * MONTHS_PER_YEAR
    r["magic_number"] = (r["sub_ttm"] - r["sub_ttm"].shift(4)) / r["snm_ng_ttm"].shift(1)
    r["rule_of_40"] = r["revenue"] / r["revenue"].shift(4) - 1 + r["fcf_ttm"] / r["revenue_ttm"]
    r["burn_multiple"] = (-r["fcf_ttm"] / r["net_new_arr_ttm"]).where(r["fcf_ttm"] < 0)
    r["new_logos"] = new_logos.reindex(r.index)
    r["new_logos_ttm"] = r["new_logos"].rolling(4).sum()
    r["cac_per_logo"] = r["snm_ng_ttm"].shift(1) / r["new_logos_ttm"]
    annual_churn = 1 - (1 - churn_q) ** model.QUARTERS_PER_YEAR
    r["ltv"] = r["arr"] / r["customers"] * r["sub_gm_ng"] / annual_churn
    r["ltv_cac"] = r["ltv"] / r["cac_per_logo"]
    return r


def ae_efficiency(inp: dict[str, float]) -> float:
    """Case A's ramped new ARR per dollar of AE cost (overhead and commission included)."""
    booked = inp["roi_quota"] * inp["roi_attain"]
    return booked / (inp["roi_ae_cost"] * (1 + inp["roi_support_pct"]) + inp["roi_commission"] * booked)


def attainment_at_efficiency(inp: dict[str, float], efficiency: float) -> float:
    """Quota attainment that makes ae_efficiency() equal `efficiency` (e.g. the company's magic number)."""
    return efficiency * inp["roi_ae_cost"] * (1 + inp["roi_support_pct"]) / (
        inp["roi_quota"] * (1 - efficiency * inp["roi_commission"]))


def attainment_for_payback(inp: dict[str, float], target_month: int, lo: float = 0.0, hi: float = 3.0
                           ) -> float | None:
    """Lowest quota attainment at which case A has paid back by `target_month` (cumulative net cash
    >= 0 that month), or None if even `hi` does not get there. Bisection on attainment."""
    def ok(a: float) -> bool:
        horizon = max(target_month, int(inp.get("roi_months") or months()))
        return gtm_cases({**inp, "roi_attain": a}, horizon)["A"].cumulative.iloc[target_month - 1] >= 0
    if not ok(hi):
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (lo, mid) if ok(mid) else (mid, hi)
    return hi


def breakeven_attainment(inp: dict[str, float], lo: float = 0.0, hi: float = 2.0) -> float:
    """Quota attainment at which case A's 36-month NPV is zero (bisection; NPV rises with attainment)."""
    def f(a: float) -> float:
        x = {**inp, "roi_attain": a}
        return npv(x["roi_discount"], list(gtm_cases(x)["A"].net))
    if f(lo) > 0 or f(hi) < 0:
        raise ValueError("breakeven attainment outside the search range")
    for _ in range(80):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if f(mid) < 0 else (lo, mid)
    return (lo + hi) / 2
