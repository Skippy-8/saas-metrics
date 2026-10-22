"""The built-in operating model as a `BaseForecaster` implementation.

The arithmetic is not re-implemented here: `predict` calls `model.forecast_rows`, the same
recursion the Excel model and the budget-vs-actual bridge use.  Only the inputs change: the
plan is derived from the trailing four quarters of the point-in-time frame (or supplied
explicitly), instead of from the frozen cutoff's driver list.
"""
from __future__ import annotations

from datetime import date
from typing import Mapping

import numpy as np
import pandas as pd

from saas_metrics import drivers, model
from saas_metrics.backtest.plan import derive_plan, normalise_history, require_plan
from saas_metrics.backtest.protocol import ForecastContractError, as_date, guard_no_future


class DefaultSaaSForecaster:
    """Plan-driven SaaS operating model (revenue, margins, FCF) over a fixed horizon.

    ``plan`` is a mapping keyed like ``saas_metrics.drivers.Driver``.  When omitted, the plan is
    derived from the trailing four in-window quarters of the frame passed to ``fit``.  An
    explicit plan is useful for what-if runs and for pinning the frozen cutoff's assumptions.

    ``predict`` reports GAAP gross margin (`gp / revenue`), **non-GAAP** operating margin
    (`ng_op_inc / revenue`, i.e. operating income plus stock-based compensation and amortization,
    the model's own definition) and FCF margin (`fcf / revenue`).
    """

    name = "default_saas_model"

    def __init__(self, plan: Mapping[str, float] | None = None, scenario: str = "Base"):
        if scenario not in drivers.SCENARIOS:
            raise ForecastContractError(f"scenario must be one of {drivers.SCENARIOS}, got {scenario!r}")
        self.plan = None if plan is None else dict(plan)
        self.scenario = scenario
        self.history_: pd.DataFrame | None = None
        self.plan_: dict[str, float] | None = None
        self.as_of_date_: date | None = None

    def fit(self, historical_df: pd.DataFrame, as_of_date: date) -> None:
        self.history_ = None
        self.plan_ = None
        guard_no_future(historical_df, as_of_date)
        frame = normalise_history(historical_df)
        plan = require_plan(self.plan) if self.plan is not None else derive_plan(frame, self.scenario)
        self.history_ = frame
        self.plan_ = plan
        self.as_of_date_ = as_date(as_of_date)

    def predict(self, horizon_quarters: int) -> pd.DataFrame:
        if self.history_ is None or self.plan_ is None:
            raise ForecastContractError("DefaultSaaSForecaster.predict called before fit")
        try:
            horizon = int(horizon_quarters)
        except (TypeError, ValueError) as exc:
            raise ForecastContractError(f"horizon_quarters must be an integer, got {horizon_quarters!r}") from exc
        if horizon < 1:
            raise ForecastContractError(f"horizon_quarters must be at least 1, got {horizon}")

        last_known = str(self.history_["period_end"].iloc[-1])
        quarters = model.quarters_after(last_known, horizon)
        rows = self.history_.to_dict("records")
        out = pd.DataFrame(model.forecast_rows(rows, self.plan_, quarters)[-horizon:])
        revenue = pd.to_numeric(out["revenue"], errors="coerce")
        if revenue.isna().any() or (revenue <= 0).any():
            raise ForecastContractError("the model produced non-positive or missing revenue; "
                                        "check the plan and history")
        margins = pd.DataFrame({
            "period_end": out["period_end"],
            "revenue": revenue.astype(float),
            "fcf": pd.to_numeric(out["fcf"], errors="coerce").astype(float),
            "gross_margin": pd.to_numeric(out["gp"], errors="coerce") / revenue,
            "operating_margin": pd.to_numeric(out["ng_op_inc"], errors="coerce") / revenue,
            "fcf_margin": pd.to_numeric(out["fcf"], errors="coerce") / revenue,
        })
        for col in ("fcf", "gross_margin", "operating_margin", "fcf_margin"):
            if not np.isfinite(margins[col].to_numpy(dtype=float)).all():
                raise ForecastContractError(f"the model produced a non-finite {col}")
        return margins
