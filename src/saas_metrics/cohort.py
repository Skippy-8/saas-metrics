"""[Calibrated Synthetic Cohort Panel]: a cohort ledger that reconciles exactly to disclosures.

MongoDB does not publish cohorts. This module builds the simplest panel consistent with
what it does publish, and states every assumption it needs.

The general shape is set by the config (`arr_proxy`, `cohort_ledger`, see saas_metrics/segments.py);
the description below is MongoDB's (configs/MDB.yaml).

Disclosed inputs (per quarter t)
  ARR proxy_t = Atlas revenue_t x 365 / days in quarter      (usage-based, ratable; Q1 has 89-90
                                                              days vs 92, so plain x4 understates it)
              + trailing-12-month Other subscription revenue  (Enterprise Advanced term licenses are
                                                              recognised up front under ASC 606, so
                                                              one quarter is lumpy)
  C_t         = total customers ("over N" in the filings)
  NRR_t       = net ARR expansion rate, a trailing-12-month measure rounded to a whole percent.
                Before Q2 FY25 the filings only say "over 120%"; used as 120, flagged lower bound.

Assumptions (labelled in the output)
  1. All existing cohorts expand at one net rate g_t in quarter t, set from the disclosed
     annual rate: g_t = NRR_t^(1/4) - 1.
  2. Existing customers cannot account for more than the whole quarter's ARR growth: if
     assumption 1 implies negative new-cohort ARR, g_t is lowered so new-cohort ARR is 0 and
     the quarter is flagged `constrained`. (Q1-Q2 FY25 hit this: the quarters MongoDB
     described as weaker Atlas consumption.)
  3. Gross logo churn per quarter is an illustrative rate (not disclosed). New logos are the
     plug that makes surviving logos sum to the reported customer count.
  Logos are kept at total-customer level. The Atlas / non-Atlas split is reported as disclosed
  but not split by cohort: customers migrate from Enterprise Advanced to Atlas and that flow
  is not disclosed, so a tier split would be invented.

Identities that hold by construction (tested)
  sum_cohorts ARR_t = ARR proxy_t;  sum_cohorts logos_t = reported customers_t.
Diagnostic (tested within a band, reported, not forced): ledger trailing-4Q NRR vs disclosed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from saas_metrics import config, periods

LABEL = "[Calibrated Synthetic Cohort Panel]"


@dataclass(frozen=True)
class Assumptions:
    churn_q: float = 0.035  # [Illustrative] quarterly gross logo churn, all customers


def inputs(qwide: pd.DataFrame, kpi: pd.DataFrame) -> pd.DataFrame:
    """One row per in-window quarter with the disclosed series the ledger is calibrated to.

    qwide: q_wide rows for ONE data vintage, including pre-window quarters (for the TTM)
    kpi:   kpi_disclosures rows already limited to that vintage's information set
    """
    run, ttm, led = _spec()
    q = qwide.sort_values("period_end").set_index("period_end")
    days = pd.Series({e: (date.fromisoformat(e) - periods.quarter_start(e)).days + 1 for e in q.index})
    df = pd.DataFrame({
        "fiscal_label": q["fiscal_label"],
        "in_window": q["in_window"],
        "days": days,
        run["column"]: q[run["line"]] * 365 / days,
        **{t["column"]: q[t["line"]].rolling(4).sum() for t in ttm},
    })
    df["arr_proxy"] = df[run["column"]] + sum(df[t["column"]] for t in ttm)
    k = kpi.pivot_table(index="period_end", columns="kpi", values="value", aggfunc="first")
    qual = kpi[kpi.kpi == led["nrr_kpi"]].set_index("period_end")["qualifier"]
    report = list(led.get("report_kpis", []))
    k = k.reindex(columns=[led["customers_kpi"], *report, led["nrr_kpi"]])
    df = df.join(k.set_axis(["total_customers", *report, "nrr"], axis=1), how="left")
    df["nrr_is_lower_bound"] = qual.reindex(df.index).isin(["over", "above"])
    df = df[df["in_window"]].drop(columns="in_window")
    df["nrr"] = df.pop("nrr") / 100
    missing = df[["arr_proxy", "total_customers", "nrr"]].isna().any(axis=1)
    if missing.any():
        raise ValueError(f"ledger inputs missing for {list(df.index[missing])}")
    return df


DEFAULT = Assumptions()


def assumptions() -> Assumptions:
    """The active company's ledger assumptions (cohort_ledger in its config)."""
    return Assumptions(churn_q=float(config.ACTIVE.raw["cohort_ledger"]["churn_q"]))


def _spec() -> tuple[dict, list[dict], dict]:
    raw = config.ACTIVE.raw
    return raw["arr_proxy"]["run_rate"], raw["arr_proxy"].get("trailing_12m", []), raw["cohort_ledger"]


def build(inp: pd.DataFrame, a: Assumptions | None = None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Returns (cohort_arr, cohort_logos, calibration), all labelled synthetic."""
    a = a or assumptions()
    run, ttm, led = _spec()
    parts = [run["column"], *(t["column"] for t in ttm)]
    report = list(led.get("report_kpis", []))
    arr_c: dict[str, float] = {}
    logo_c: dict[str, float] = {}
    arr_rows, logo_rows, cal = [], [], []
    prev_arr = None
    for t, p in enumerate(inp.index):
        arr_t, cust_t, nrr_t = inp.at[p, "arr_proxy"], inp.at[p, "total_customers"], inp.at[p, "nrr"]
        g = constrained = new_arr = new_logos = churned = None
        if t == 0:
            arr_c["opening"], logo_c["opening"] = arr_t, cust_t
        else:
            g = nrr_t ** 0.25 - 1
            constrained = (1 + g) * prev_arr > arr_t
            if constrained:
                g = arr_t / prev_arr - 1
            for c in arr_c:
                arr_c[c] *= 1 + g
            new_arr = arr_t - sum(arr_c.values())
            arr_c[p] = new_arr
            churned = sum(logo_c.values()) * a.churn_q
            for c in logo_c:
                logo_c[c] *= 1 - a.churn_q
            new_logos = cust_t - sum(logo_c.values())
            logo_c[p] = new_logos
        prev_arr = arr_t
        arr_rows += [{"cohort": c, "period_end": p, "arr": v, "label": LABEL} for c, v in arr_c.items()]
        logo_rows += [{"cohort": c, "period_end": p, "customers": v, "label": LABEL} for c, v in logo_c.items()]
        cal.append({
            "period_end": p, "fiscal_label": inp.at[p, "fiscal_label"], "days": int(inp.at[p, "days"]),
            **{c: inp.at[p, c] for c in parts},
            "arr_proxy": arr_t, "g_quarterly": g, "constrained": constrained, "new_cohort_arr": new_arr,
            "total_customers": cust_t, **{c: inp.at[p, c] for c in report}, "new_logos": new_logos,
            "churned_logos": churned, "churn_q_assumption": a.churn_q,
            "nrr_disclosed": nrr_t, "nrr_is_lower_bound": bool(inp.at[p, "nrr_is_lower_bound"]),
        })
    cal_df = pd.DataFrame(cal)
    # the opening quarter has no flow values; keep the columns typed so DuckDB does not store text
    cal_df["constrained"] = cal_df["constrained"].astype("boolean")
    for col in ("g_quarterly", "new_cohort_arr", "new_logos", "churned_logos"):
        cal_df[col] = pd.to_numeric(cal_df[col])
    arr_df = pd.DataFrame(arr_rows)
    cal_df["nrr_ledger_t4q"] = cal_df["period_end"].map(ledger_nrr(arr_df))
    return arr_df, pd.DataFrame(logo_rows), cal_df


def ledger_nrr(cohort_arr: pd.DataFrame) -> pd.Series:
    """Trailing-4Q NRR measured from the ledger: ARR now from cohorts that existed 4 quarters ago."""
    arr = cohort_arr.pivot(index="period_end", columns="cohort", values="arr")
    idx = list(arr.index)
    out = {}
    for t in range(4, len(idx)):
        base = arr.loc[idx[t - 4]].dropna()
        out[idx[t]] = arr.loc[idx[t], base.index].sum() / base.sum()
    return pd.Series(out, dtype=float)
