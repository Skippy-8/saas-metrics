"""Backtesting: the frozen-model reference scorecard and the extensible point-in-time harness.

The historical names (`bridge`, `scorecard`, `actual_rows`, the damped-trend helpers and the
dynamic `GROUPS` / `GROUP_LABELS` / `EXPLAINS`) keep their module paths, so ``from saas_metrics
import backtest`` and ``from saas_metrics.backtest import explains`` behave exactly as before.

New in Phase 4:

* `BaseForecaster` / `ForecastContractError` / `LookAheadBiasError` -- the model contract and its
  two safety errors.
* `DefaultSaaSForecaster` -- the operating model's recursion behind that contract.
* `PointInTimeBacktester` -- fit any forecaster at historical origins with strict masking.
* `sec_history` / `slice_billing` -- the two source-specific point-in-time slices.
"""
from __future__ import annotations

from saas_metrics.backtest.backtester import (  # noqa: F401
    METRICS,
    BacktestResult,
    PointInTimeBacktester,
    format_result,
)
from saas_metrics.backtest.engine import (  # noqa: F401
    ARR_GROUPS,
    CASH_GROUPS,
    COST_GROUPS,
    DAMPED_GRID,
    __getattr__,  # noqa: F401
    actual_rows,
    bridge,
    damped_fit,
    damped_forecast,
    damped_path,
    explains,
    group_labels,
    groups,
    implied_drivers,
    scorecard,
    simulate,
)
from saas_metrics.backtest.forecasters import DefaultSaaSForecaster  # noqa: F401
from saas_metrics.backtest.plan import (  # noqa: F401
    derive_plan,
    normalise_history,
    plan_from_history,
    require_plan,
)
from saas_metrics.backtest.point_in_time import (  # noqa: F401
    BILLING_DATE_COLUMN,
    SEC_VINTAGE,
    billing_history_loader,
    evaluation_dates,
    last_known_quarter,
    latest_actuals,
    sec_history,
    slice_billing,
)
from saas_metrics.backtest.protocol import (  # noqa: F401
    CANONICAL_COLUMNS,
    OPTIONAL_COLUMNS,
    REQUIRED_COLUMNS,
    BaseForecaster,
    ForecastContractError,
    LookAheadBiasError,
    as_date,
    date_columns,
    guard_no_future,
    validate_forecast,
)

__all__ = [
    # frozen-model reference implementation
    "ARR_GROUPS", "CASH_GROUPS", "COST_GROUPS", "DAMPED_GRID", "actual_rows", "bridge",
    "damped_fit", "damped_forecast", "damped_path", "explains", "group_labels", "groups",
    "implied_drivers", "scorecard", "simulate",
    # protocol
    "BaseForecaster", "ForecastContractError", "LookAheadBiasError", "as_date", "date_columns",
    "guard_no_future", "validate_forecast", "REQUIRED_COLUMNS", "OPTIONAL_COLUMNS",
    "CANONICAL_COLUMNS",
    # default model
    "DefaultSaaSForecaster", "derive_plan", "plan_from_history", "require_plan", "normalise_history",
    # point-in-time slices
    "SEC_VINTAGE", "BILLING_DATE_COLUMN", "sec_history", "slice_billing", "billing_history_loader",
    "latest_actuals", "evaluation_dates", "last_known_quarter",
    # harness
    "PointInTimeBacktester", "BacktestResult", "format_result", "METRICS",
]
