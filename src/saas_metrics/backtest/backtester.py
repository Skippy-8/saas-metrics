"""Point-in-time backtest harness: score any `BaseForecaster` at several historical origins.

At each origin T the harness:
  1. builds a history slice that contains nothing filed/ended after T (`sec_history` by default);
  2. calls `guard_no_future` on it anyway, so a separate loader cannot smuggle in the future;
  3. fits the forecaster, validates the forecast against the quarters after the last known one;
  4. scores Revenue and FCF against the latest reported actuals, with MAPE, signed bias and
     one-quarter / four-quarter variance bridges.

The old `bridge`/`scorecard`/workbook path is untouched: this class is additive.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable

import duckdb
import numpy as np
import pandas as pd

from saas_metrics import model
from saas_metrics.backtest.forecasters import DefaultSaaSForecaster
from saas_metrics.backtest.plan import normalise_history
from saas_metrics.backtest.point_in_time import evaluation_dates as default_evaluation_dates
from saas_metrics.backtest.point_in_time import last_known_quarter, latest_actuals, sec_history
from saas_metrics.backtest.protocol import (
    BaseForecaster,
    ForecastContractError,
    as_date,
    guard_no_future,
    validate_forecast,
)

METRICS = ("revenue", "fcf")
BIAS_TOLERANCE = 0.01   # |mean signed error| below 1% is reported as balanced


@dataclass
class BacktestResult:
    """Long-form evaluation detail plus the summary and variance bridges."""

    ticker: str
    forecaster: str
    horizon: int
    evaluations: pd.DataFrame
    summary: pd.DataFrame
    bridges: pd.DataFrame


class PointInTimeBacktester:
    """Run a forecaster at historical quarter ends and score it without look-ahead.

    ``history_loader`` defaults to the SEC slice (`sec_history`).  For a billing/ledger frame use
    ``billing_history_loader(ledger)``; whatever loader is supplied, the engine still runs
    `guard_no_future` on its output.
    """

    def __init__(self, ticker: str | None = None, forecaster: BaseForecaster | None = None, *,
                 con: duckdb.DuckDBPyConnection | None = None,
                 history_loader: Callable[[date], pd.DataFrame] | None = None,
                 horizon: int = 4, max_evaluations: int = 4):
        if horizon < 1:
            raise ForecastContractError("horizon must be at least 1")
        if max_evaluations < 1:
            raise ForecastContractError("max_evaluations must be at least 1")
        self.ticker = ticker
        self.forecaster = DefaultSaaSForecaster() if forecaster is None else forecaster
        if not isinstance(self.forecaster, BaseForecaster):
            raise TypeError(f"{type(self.forecaster).__name__} does not implement fit/predict "
                            f"(see saas_metrics.backtest.BaseForecaster)")
        self.con = con
        self.history_loader = history_loader
        self.horizon = horizon
        self.max_evaluations = max_evaluations

    def run(self, dates: list[date | str] | None = None) -> BacktestResult:
        from saas_metrics import company, config

        target = company.load(self.ticker) if self.ticker is not None else config.ACTIVE
        with config.using(target):
            con = self.con if self.con is not None else duckdb.connect(str(config.DB_PATH), read_only=True)
            try:
                origins = ([as_date(d) for d in dates] if dates is not None
                           else default_evaluation_dates(con, self.horizon, self.max_evaluations))
                if not origins:
                    raise ForecastContractError("no evaluation date has a full horizon of actuals")
                actuals = latest_actuals(con)
                loader = self.history_loader or (lambda day: sec_history(con, day))
                detail: list[dict] = []
                for origin in origins:
                    detail.extend(self._evaluate(origin, actuals, loader))
            finally:
                if self.con is None:
                    con.close()

        evaluations = pd.DataFrame(detail)
        return BacktestResult(
            ticker=target.ticker,
            forecaster=type(self.forecaster).__name__,
            horizon=self.horizon,
            evaluations=evaluations,
            summary=_summarise(evaluations),
            bridges=_bridges(evaluations, self.horizon),
        )

    def _evaluate(self, origin: date, actuals: pd.DataFrame,
                  loader: Callable[[date], pd.DataFrame]) -> list[dict]:
        history = loader(origin)
        if not isinstance(history, pd.DataFrame):
            raise ForecastContractError(f"history loader returned {type(history).__name__}, not a DataFrame")
        history = normalise_history(history)
        guard_no_future(history, origin)
        last_known = last_known_quarter(history, origin)
        expected = model.quarters_after(last_known, self.horizon)

        self.forecaster.fit(history, origin)
        forecast = validate_forecast(self.forecaster.predict(self.horizon), self.horizon, origin,
                                     expected_periods=expected)

        missing = [q for q in expected if q not in actuals.index]
        if missing:
            raise ForecastContractError(f"actuals are missing for {missing}; cannot score the "
                                        f"forecast made at {origin}")
        actual = actuals.loc[expected]

        out = []
        for step, period in enumerate(expected, start=1):
            row = forecast.loc[forecast["period_end"] == period].iloc[0]
            for metric in METRICS:
                f, a = float(row[metric]), float(actual.at[period, metric])
                error = f - a
                pct = error / abs(a) if a != 0 else np.nan
                out.append({
                    "as_of": origin.isoformat(),
                    "last_known": last_known,
                    "period_end": period,
                    "step": step,
                    "metric": metric,
                    "forecast": f,
                    "actual": a,
                    "error": error,
                    "ape": abs(pct) if not np.isnan(pct) else np.nan,
                    "signed_pct": pct,
                })
        return out


def _summarise(evaluations: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for metric in METRICS:
        sub = evaluations.loc[evaluations["metric"] == metric].dropna(subset=["ape"])
        n = len(sub)
        mape = float(sub["ape"].mean()) if n else np.nan
        bias = float(sub["signed_pct"].mean()) if n else np.nan
        if n == 0:
            direction = "not scorable"
        elif bias < -BIAS_TOLERANCE:
            direction = "under-forecasting"
        elif bias > BIAS_TOLERANCE:
            direction = "over-forecasting"
        else:
            direction = "balanced"
        rows.append({"metric": metric, "n": n, "mape": mape, "bias": bias, "direction": direction})
    return pd.DataFrame(rows)


def _bridges(evaluations: pd.DataFrame, horizon: int) -> pd.DataFrame:
    rows = []
    for (as_of, last_known), group in evaluations.groupby(["as_of", "last_known"], sort=False):
        for metric in METRICS:
            one = group.loc[group["metric"] == metric].sort_values("step")
            windows = [("1Q", one.iloc[:1])]
            if horizon > 1:
                windows.append((f"{horizon}Q", one))
            for label, part in windows:
                forecast, actual = float(part["forecast"].sum()), float(part["actual"].sum())
                variance = forecast - actual
                rows.append({
                    "as_of": as_of,
                    "last_known": last_known,
                    "metric": metric,
                    "window": label,
                    "forecast": forecast,
                    "actual": actual,
                    "variance": variance,
                    "variance_pct": variance / abs(actual) if actual != 0 else np.nan,
                })
    return pd.DataFrame(rows)


def format_result(result: BacktestResult) -> str:
    """Console tables: summary then the 1Q/4Q variance bridges, formatted for reading."""
    summary = result.summary.copy()
    summary["mape"] = summary["mape"].map(lambda v: "n/a" if pd.isna(v) else f"{v:.2%}")
    summary["bias"] = summary["bias"].map(lambda v: "n/a" if pd.isna(v) else f"{v:+.2%}")
    bridges = result.bridges.copy()
    for col in ("forecast", "actual", "variance"):
        bridges[col] = (bridges[col] / 1e6).map(lambda v: f"{v:,.1f}")
    bridges["variance_pct"] = bridges["variance_pct"].map(
        lambda v: "n/a" if pd.isna(v) else f"{v:+.2%}")
    bridges = bridges.rename(columns={"forecast": "forecast $M", "actual": "actual $M",
                                      "variance": "variance $M"})
    n_origins = result.evaluations["as_of"].nunique()
    return "\n".join([
        f"{result.ticker} point-in-time backtest | {result.forecaster} | horizon {result.horizon}Q "
        f"| {n_origins} origin(s)",
        "",
        "Forecast accuracy (MAPE) and directional bias (forecast - actual, % of |actual|):",
        summary.to_string(index=False),
        "",
        "Variance bridge (variance = forecast - actual; 1Q is the next quarter, "
        f"{result.horizon}Q is the full horizon):",
        bridges.to_string(index=False),
    ])
