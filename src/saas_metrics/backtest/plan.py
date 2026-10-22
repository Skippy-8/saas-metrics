"""Derive the operating model's drivers from a trailing window of history.

This mirrors ``saas_metrics.drivers.derive`` formula for formula, but reads a frame instead of
DuckDB.  That is what lets ``DefaultSaaSForecaster`` fit from the point-in-time frame alone: no
query can quietly reach past the evaluation date.

At the frozen cutoff the two derivations are identical (tested), so the default forecaster
reproduces the workbook's Base forecast exactly.
"""
from __future__ import annotations

from typing import Mapping

import numpy as np
import pandas as pd

from saas_metrics import config, drivers, model, segments
from saas_metrics.backtest.protocol import ForecastContractError


def normalise_history(frame: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise ForecastContractError(f"historical_df must be a DataFrame, got {type(frame).__name__}")
    df = frame.copy()
    if "period_end" not in df.columns:
        if df.index.name == "period_end":
            df = df.reset_index()
        else:
            raise ForecastContractError("historical_df must have a period_end column")
    parsed = pd.to_datetime(df["period_end"], errors="coerce")
    if parsed.isna().any():
        raise ForecastContractError("historical_df has an unparseable period_end")
    df["period_end"] = parsed.dt.strftime("%Y-%m-%d")
    if df["period_end"].duplicated().any():
        raise ForecastContractError("historical_df has duplicate period_end rows")
    return df.sort_values("period_end").reset_index(drop=True)


def _require(df: pd.DataFrame, columns: list[str]) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise ForecastContractError(
            f"DefaultSaaSForecaster needs the model history column(s) {missing}; pass plan=... "
            f"for a custom driver set, or use a different forecaster"
        )


def _finite(value: float, key: str) -> float:
    value = float(value)
    if not np.isfinite(value):
        raise ForecastContractError(f"cannot derive driver {key!r} from the history: value is not finite")
    return value


def _spec(key: str) -> dict:
    return dict(config.ACTIVE.raw.get("drivers", {}).get(key) or {})


def derive_plan(frame: pd.DataFrame, scenario: str = "Base") -> dict[str, float]:
    """Driver values for the operating model, from the last four in-window quarters.

    Base uses trailing means; Upside/Downside use the plan's config bands (maximum/minimum
    new-logo count, +/- percentage points on NRR and subscription gross margin, +/- per cent on
    new-cohort ARR per logo), exactly as ``drivers.derive`` does at the frozen cutoff.
    """
    from saas_metrics import segments as seg

    if scenario not in drivers.SCENARIOS:
        raise ForecastContractError(f"scenario must be one of {drivers.SCENARIOS}, got {scenario!r}")
    df = normalise_history(frame)
    active = seg.active()
    svc = seg.nonrecurring()
    needed = ["nrr", "new_logos", "new_cohort_arr", "churn_q_assumption", "revenue", "sub", "cost_sub",
              "cost_svc", "headcount", "amort", "cfo", "capex", "ng_op_inc", "fiscal_label",
              *[s.key for s in active], *[f"sbc_{s}" for s in model.SBC_LINES], *drivers.DEPTS]
    _require(df, needed)

    if "in_window" in df.columns:
        if df["in_window"].isna().any():
            raise ForecastContractError("in_window has missing values; cannot tell which quarters to use")
        win = df[df["in_window"].astype(bool)]
    else:
        win = df
    if len(win) < 5:
        raise ForecastContractError(f"need at least 5 in-window quarters to derive a plan, got {len(win)}")
    if seg.yoy() and len(df) < 8:
        raise ForecastContractError(f"need 8 quarters of history for year-on-year segments, got {len(df)}")
    t4, t0 = win.tail(4), win.iloc[-1]

    nrr = _finite(t0["nrr"], "nrr")
    churn = _finite(t0["churn_q_assumption"], "churn_q")
    if not 0 <= churn < 1:
        raise ForecastContractError(f"churn_q must be in [0, 1), got {churn}")
    logos = pd.to_numeric(t4["new_logos"], errors="coerce")
    new_arr = pd.to_numeric(t4["new_cohort_arr"], errors="coerce")
    if logos.isna().any() or new_arr.isna().any():
        raise ForecastContractError("new_logos/new_cohort_arr have missing values in the trailing window")
    if not np.isfinite(logos.to_numpy(dtype=float)).all() or not np.isfinite(new_arr.to_numpy(dtype=float)).all():
        raise ForecastContractError("new_logos/new_cohort_arr must be finite")
    total_logos = float(logos.sum())
    if total_logos <= 0:
        raise ForecastContractError("the trailing window has no new logos; cannot derive ARR per new logo")

    revenue4 = float(pd.to_numeric(t4["revenue"], errors="coerce").sum())
    if revenue4 <= 0:
        raise ForecastContractError("the trailing window has non-positive revenue")
    sub4 = float(pd.to_numeric(t4["sub"], errors="coerce").sum())
    svc4 = float(pd.to_numeric(t4[svc.key], errors="coerce").sum())
    if sub4 <= 0 or svc4 <= 0:
        raise ForecastContractError("the trailing window has non-positive subscription or services revenue")

    base = {
        "nrr": nrr,
        "new_logos": float(logos.mean()),
        "arr_per_new_logo": float(new_arr.sum()) / total_logos,
        "churn_q": churn,
        "sub_gm": 1 - float(pd.to_numeric(t4["cost_sub"], errors="coerce").sum()) / sub4,
        "svc_gm": 1 - float(pd.to_numeric(t4["cost_svc"], errors="coerce").sum()) / svc4,
        "net_hires_q": (float(t0["headcount"]) - float(win.iloc[-5]["headcount"])) / 4,
        "amort_q": float(t0["amort"]),
    }

    # Year-on-year segments: trailing-12-month growth at T0 over the TTM a year earlier.
    ttm_index = win.reset_index(drop=True)
    for s in seg.yoy():
        series = pd.to_numeric(ttm_index[s.key], errors="coerce")
        ttm = series.rolling(4).sum()
        current, prior = ttm.iloc[-1], ttm.iloc[-5]
        if not np.isfinite(current) or not np.isfinite(prior) or prior == 0:
            raise ForecastContractError(f"segment {s.key!r} has no year-earlier TTM to grow from")
        base[f"{s.key}_yoy"] = float(current / prior - 1)

    for d in drivers.DEPTS:
        ng = (pd.to_numeric(win[d], errors="coerce") - pd.to_numeric(win[f"sbc_{d}"], errors="coerce")) \
            / pd.to_numeric(win["headcount"], errors="coerce")
        current, prior = ng.iloc[-1], ng.iloc[-5]
        if not np.isfinite(current) or not np.isfinite(prior) or current <= 0 or prior <= 0:
            raise ForecastContractError(f"non-GAAP cost per head for {d!r} must be positive and finite")
        base[f"cph_growth_{d}"] = float((current / prior) ** 0.25 - 1)
        base[f"sbc_pct_{d}"] = float(pd.to_numeric(t4[f"sbc_{d}"], errors="coerce").sum() / revenue4)

    for line in ("cost_subscription", "cost_services"):
        base[f"sbc_pct_{line}"] = float(pd.to_numeric(t4[f"sbc_{line}"], errors="coerce").sum() / revenue4)

    conv = (pd.to_numeric(win["cfo"], errors="coerce") - pd.to_numeric(win["capex"], errors="coerce")) \
        / pd.to_numeric(win["ng_op_inc"], errors="coerce")
    for fq in (1, 2, 3, 4):
        mask = win["fiscal_label"].astype(str).str.startswith(f"Q{fq} ")
        if not mask.any():
            raise ForecastContractError(f"no fiscal Q{fq} quarter in the history to derive FCF conversion")
        value = conv[mask].iloc[-1]
        if not np.isfinite(value):
            raise ForecastContractError(f"FCF conversion for fiscal Q{fq} is undefined "
                                        f"(missing or zero non-GAAP operating income)")
        base[f"fcf_conv_q{fq}"] = float(value)

    for key, value in base.items():
        _finite(value, key)

    if scenario == "Base":
        return base
    # Scenario bands, mirroring drivers.derive.
    nrr_pp = _spec("nrr").get("band_pp", 2) / 100
    apl_pct = _spec("arr_per_new_logo").get("band_pct", 25) / 100
    gm_pp = _spec("sub_gm").get("band_pp", 1) / 100
    up = scenario == "Upside"
    out = dict(base)
    out["nrr"] = base["nrr"] + (nrr_pp if up else -nrr_pp)
    out["new_logos"] = float(logos.max() if up else logos.min())
    out["arr_per_new_logo"] = base["arr_per_new_logo"] * (1 + (apl_pct if up else -apl_pct))
    out["sub_gm"] = base["sub_gm"] + (gm_pp if up else -gm_pp)
    for s in seg.yoy():
        pp = _spec(f"{s.key}_yoy").get("band_pp", 5) / 100
        out[f"{s.key}_yoy"] = base[f"{s.key}_yoy"] + (pp if up else -pp)
    return {k: _finite(v, k) for k, v in out.items()}


def plan_from_history(frame: pd.DataFrame, scenario: str = "Base") -> dict[str, float]:
    """Deprecated alias kept for readability in notebooks."""
    return derive_plan(frame, scenario)


def require_plan(plan: Mapping[str, float]) -> dict[str, float]:
    """Validate an explicit plan: every value must be present and finite."""
    required = ["nrr", "new_logos", "arr_per_new_logo", "churn_q", "sub_gm", "svc_gm", "net_hires_q",
                "amort_q", *[f"{s.key}_yoy" for s in segments.yoy()],
                *[f"cph_growth_{d}" for d in drivers.DEPTS], *[f"sbc_pct_{d}" for d in drivers.DEPTS],
                "sbc_pct_cost_subscription", "sbc_pct_cost_services", *(f"fcf_conv_q{q}" for q in (1, 2, 3, 4))]
    missing = [k for k in required if k not in plan]
    if missing:
        raise ForecastContractError(f"plan is missing driver(s): {', '.join(missing)}")
    return {k: _finite(plan[k], k) for k in required}
