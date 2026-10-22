"""Reference calculator: the operating model in plain Python.

The Excel workbook (saas_metrics.workbook) implements the same arithmetic with live formulas;
tests recalculate the workbook and require both to agree. Keep the two in step: any change
to a formula here needs the same change in workbook.py.

Forecast mechanics (quarter t, starting at T0 + 1). Revenue segments come from the config
(saas_metrics/segments.py); MongoDB's names in brackets.
  customers  = customers[t-1] * (1 - churn_q) + new_logos
  ARR        = ARR[t-1] * nrr^(1/4) + new_logos * arr_per_new_logo
  each yoy segment [EA, Services] = same quarter last year * (1 + <key>_yoy)
  residual segment [Atlas] = (ARR - trailing-4-quarter sum of the trailing_12m segments [EA]) * days / 365
  subscription = the recurring segments;  revenue = subscription + the non-recurring segment [Services]
  cost       = subscription * (1 - sub_gm) + services * (1 - svc_gm)
  headcount  = headcount[t-1] + net_hires_q
  dept non-GAAP spend = headcount * spend per employee (grows at cph_growth per quarter)
  dept GAAP  = non-GAAP + revenue * SBC % (by department)
  non-GAAP operating income = GAAP operating income + SBC + amortization
  FCF        = non-GAAP operating income * FCF conversion for that fiscal quarter
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from saas_metrics import config, periods, segments
from saas_metrics.drivers import DEPTS, Driver

DAYS_PER_YEAR = 365
QUARTERS_PER_YEAR = 4
SBC_LINES = ("cost_subscription", "cost_services", *DEPTS)


def quarters_after(last_quarter_end: str, n: int = 4) -> list[str]:
    """The `n` quarter ends that follow `last_quarter_end` (a quarter end, YYYY-MM-DD)."""
    out, e = [], date.fromisoformat(last_quarter_end)
    for _ in range(n):
        m, y = e.month + 3, e.year
        if m > 12:
            m, y = m - 12, y + 1
        e = periods.month_end(y, m)
        out.append(e.isoformat())
    return out


def forecast_quarters(n: int = 4) -> list[str]:
    return quarters_after(config.T0_QUARTER_END, n)


def days_in(end: str) -> int:
    return (date.fromisoformat(end) - periods.quarter_start(end)).days + 1


def _history_rows(hist: pd.DataFrame, cal: pd.DataFrame, hc: pd.Series) -> list[dict]:
    rows = []
    for r in hist.itertuples():
        rows.append({
            "period_end": r.period_end, "fiscal_label": r.fiscal_label, "kind": "Actual",
            "days": days_in(r.period_end), "fiscal_q": int(r.fiscal_label[1]),
            **{s.key: getattr(r, s.line) for s in segments.active()},
            "cost_sub": r.cost_subscription, "cost_svc": r.cost_services,
            **{d: getattr(r, d) for d in DEPTS},
            **{f"sbc_{s}": getattr(r, f"sbc_{s}") for s in SBC_LINES},
            "amort": r.amort_intangibles, "op_inc_reported": r.operating_income,
            "cfo": r.cfo, "capex": r.capex,
            "customers": cal["total_customers"].get(r.period_end, float("nan")),
            "headcount": hc.get(r.period_end, float("nan")),
        })
    return rows


def _derived(row: dict, prev4: list[dict]) -> None:
    """Lines that are formulas in both history and forecast columns."""
    row["sub"] = sum(row[s.key] for s in segments.recurring())
    row["revenue"] = row["sub"] + row[segments.nonrecurring().key]
    row["cost"] = row["cost_sub"] + row["cost_svc"]
    row["gp"] = row["revenue"] - row["cost"]
    row["opex"] = sum(row[d] for d in DEPTS)
    row["op_inc"] = row["gp"] - row["opex"]
    row["sbc_total"] = sum(row[f"sbc_{s}"] for s in SBC_LINES)
    row["ng_op_inc"] = row["op_inc"] + row["sbc_total"] + row["amort"]
    for d in DEPTS:
        row[f"ng_{d}"] = row[d] - row[f"sbc_{d}"]
        row[f"cph_{d}"] = row[f"ng_{d}"] / row["headcount"] if row["headcount"] == row["headcount"] else float("nan")
    res, ttm = segments.residual(), segments.ttm()
    if len(prev4) == 3:
        for s in ttm:
            row[f"{s.key}_ttm"] = sum(p[s.key] for p in prev4) + row[s.key]
        row["arr"] = row[res.key] * DAYS_PER_YEAR / row["days"] + sum(row[f"{s.key}_ttm"] for s in ttm)
    else:
        for s in ttm:
            row[f"{s.key}_ttm"] = float("nan")
        row["arr"] = float("nan")


def forecast_rows(rows: list[dict], plan: dict[str, float], quarters: list[str] | None = None) -> list[dict]:
    """Append one forecast row per quarter to a copy of `rows`.

    `rows` must end at the last known quarter and already carry the derived history lines
    (model._derived). `plan` is the driver values keyed as in saas_metrics.drivers.Driver.
    One recursion serves both the frozen model and backtest.DefaultSaaSForecaster, so a
    forecast cannot drift between the two.
    """
    out = list(rows)
    for e in (quarters if quarters is not None else forecast_quarters()):
        p, y = out[-1], out[-4]            # previous quarter, same quarter last year
        r = {"period_end": e, "fiscal_label": periods.label(e), "kind": "Forecast", "days": days_in(e),
             "fiscal_q": periods.fiscal(e)[1]}
        r["customers"] = p["customers"] * (1 - plan["churn_q"]) + plan["new_logos"]
        r["arr_existing"] = p["arr"] * plan["nrr"] ** (1 / QUARTERS_PER_YEAR)
        r["arr_new"] = plan["new_logos"] * plan["arr_per_new_logo"]
        r["arr"] = r["arr_existing"] + r["arr_new"]
        for s in segments.yoy():
            r[s.key] = y[s.key] * (1 + plan[f"{s.key}_yoy"])
        for s in segments.ttm():
            r[f"{s.key}_ttm"] = sum(q[s.key] for q in out[-3:]) + r[s.key]
        res, svc = segments.residual(), segments.nonrecurring()
        r[res.key] = (r["arr"] - sum(r[f"{s.key}_ttm"] for s in segments.ttm())) * r["days"] / DAYS_PER_YEAR
        r["sub"] = sum(r[s.key] for s in segments.recurring())
        r["revenue"] = r["sub"] + r[svc.key]
        r["cost_sub"] = r["sub"] * (1 - plan["sub_gm"])
        r["cost_svc"] = r[svc.key] * (1 - plan["svc_gm"])
        r["cost"] = r["cost_sub"] + r["cost_svc"]
        r["gp"] = r["revenue"] - r["cost"]
        r["headcount"] = p["headcount"] + plan["net_hires_q"]
        for s in SBC_LINES:
            r[f"sbc_{s}"] = r["revenue"] * plan[f"sbc_pct_{s}"]
        for d in DEPTS:
            r[f"cph_{d}"] = p[f"cph_{d}"] * (1 + plan[f"cph_growth_{d}"])
            r[f"ng_{d}"] = r["headcount"] * r[f"cph_{d}"]
            r[d] = r[f"ng_{d}"] + r[f"sbc_{d}"]
        r["opex"] = sum(r[d] for d in DEPTS)
        r["op_inc"] = r["gp"] - r["opex"]
        r["amort"] = plan["amort_q"]
        r["sbc_total"] = sum(r[f"sbc_{s}"] for s in SBC_LINES)
        r["ng_op_inc"] = r["op_inc"] + r["sbc_total"] + r["amort"]
        r["fcf"] = r["ng_op_inc"] * plan[f"fcf_conv_q{r['fiscal_q']}"]
        out.append(r)
    return out


def run(hist: pd.DataFrame, cal: pd.DataFrame, hc: pd.Series, drv: list[Driver], scenario: str) -> pd.DataFrame:
    """Return one row per quarter (history then forecast) with every model line."""
    v = {d.key: d.value(scenario) for d in drv}
    rows = _history_rows(hist, cal, hc)
    for i, r in enumerate(rows):
        _derived(r, rows[max(0, i - 3):i] if i >= 3 else [])
        r["fcf"] = r["cfo"] - r["capex"]
    rows = forecast_rows(rows, v)
    return pd.DataFrame(rows).set_index("period_end")
