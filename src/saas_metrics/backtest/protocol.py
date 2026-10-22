"""The forecaster contract and the point-in-time safety checks around it.

A forecaster is anything with two methods::

    fit(historical_df, as_of_date) -> None
    predict(horizon_quarters) -> DataFrame   # period_end, revenue, fcf_margin [, margins]

The engine never trusts either side of that boundary.  `guard_no_future` refuses history that
contains a timestamp after the evaluation date, and `validate_forecast` refuses a forecast that
does not line up with the requested horizon.  Both raise a typed error so a caller can tell a
look-ahead violation from a malformed forecast.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, Protocol, runtime_checkable

import numpy as np
import pandas as pd

REQUIRED_COLUMNS = ("period_end", "revenue", "fcf_margin")
OPTIONAL_COLUMNS = ("gross_margin", "operating_margin")
CANONICAL_COLUMNS = ("period_end", "revenue", "fcf", "gross_margin", "operating_margin", "fcf_margin")

# Columns whose names carry a date.  Matching is deliberately by name (not by trying to parse
# every object column): a revenue column must never be mistaken for a timestamp.
_DATE_HINTS = ("date", "period_end", "period_start", "filed", "as_of", "known_at", "service_start",
               "service_end")


class ForecastContractError(ValueError):
    """The forecaster returned something the engine cannot safely score."""


class LookAheadBiasError(ValueError):
    """Historical data contains information from after the evaluation date."""

    def __init__(self, as_of_date: date, offenders: Iterable[tuple[str, str, object]]):
        self.as_of_date = as_of_date
        self.offenders = list(offenders)
        shown = "; ".join(f"{col}={val} (row {row})" for col, val, row in self.offenders[:5])
        extra = len(self.offenders) - 5
        if extra > 0:
            shown += f"; +{extra} more"
        super().__init__(f"{len(self.offenders)} timestamp(s) after the evaluation date "
                         f"{as_of_date}: {shown}. Filter SEC data by filing_date and billing data "
                         f"by period_end on or before the evaluation date.")


@runtime_checkable
class BaseForecaster(Protocol):
    """Contract every forecast model implements to run in the point-in-time harness."""

    def fit(self, historical_df: pd.DataFrame, as_of_date: date) -> None:
        """Fit on history known at `as_of_date`.  Must be callable again for the next origin."""

    def predict(self, horizon_quarters: int) -> pd.DataFrame:
        """Return exactly `horizon_quarters` rows: period_end, revenue and fcf_margin required."""


def as_date(value: date | datetime | str | pd.Timestamp) -> date:
    """Normalise a date-like value, rejecting anything ambiguous."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError as exc:
            raise ForecastContractError(f"not an ISO date: {value!r}") from exc
    raise ForecastContractError(f"not a date: {value!r}")


def _parsed(series: pd.Series) -> pd.Series:
    """Best-effort datetime parsing for a date-like column; unparseable values become NaT."""
    try:
        out = pd.to_datetime(series, errors="coerce")
    except (TypeError, ValueError):
        try:
            out = pd.to_datetime(series, errors="coerce", format="mixed")
        except (TypeError, ValueError):
            return pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    if isinstance(out.dtype, pd.DatetimeTZDtype):
        out = out.dt.tz_localize(None)
    return out


def date_columns(frame: pd.DataFrame) -> list[str]:
    """Columns that look like timestamps: datetime dtype, or a name in `_DATE_HINTS`."""
    out = []
    for col in frame.columns:
        name = str(col).lower()
        if pd.api.types.is_datetime64_any_dtype(frame[col]) or any(h in name for h in _DATE_HINTS):
            out.append(str(col))
    return out


def _row_label(frame: pd.DataFrame, pos: int):
    try:
        label = frame.index[pos]
    except IndexError:  # pragma: no cover - defensive
        return pos
    return label.item() if hasattr(label, "item") else label


def guard_no_future(frame: pd.DataFrame, as_of_date: date | datetime | str) -> pd.DataFrame:
    """Raise `LookAheadBiasError` if any timestamp in `frame` is after `as_of_date`.

    Every named date column is scanned, plus a DatetimeIndex.  The frame must carry at least one
    of them: without a date there is no way to prove the history is point-in-time, so a
    `ForecastContractError` is raised rather than silently accepting it.
    """
    if not isinstance(frame, pd.DataFrame):
        raise ForecastContractError(f"historical_df must be a DataFrame, got {type(frame).__name__}")
    as_of = as_date(as_of_date)
    stamp = pd.Timestamp(as_of)
    columns = date_columns(frame)
    index_name = frame.index.name
    index_is_dates = isinstance(frame.index, pd.DatetimeIndex) or (
        index_name is not None and any(h in str(index_name).lower() for h in _DATE_HINTS)
    )
    if not columns and not index_is_dates:
        raise ForecastContractError(
            "historical_df has no date column (expected period_end or filing_date); cannot prove "
            "that it is point-in-time"
        )

    offenders: list[tuple[str, str, object]] = []
    for col in columns:
        parsed = _parsed(frame[col])
        bad = parsed.notna() & (parsed > stamp)
        for pos in np.flatnonzero(bad.to_numpy()):
            offenders.append((col, parsed.iloc[pos].date().isoformat(), _row_label(frame, pos)))
    if index_is_dates:
        parsed = _parsed(pd.Series(list(frame.index)))
        bad = parsed.notna() & (parsed > stamp)
        for pos in np.flatnonzero(bad.to_numpy()):
            offenders.append((str(index_name or "index"), parsed.iloc[pos].date().isoformat(),
                              list(frame.index)[pos]))
    if offenders:
        raise LookAheadBiasError(as_of, offenders)
    return frame


def validate_forecast(frame: pd.DataFrame, horizon_quarters: int, as_of_date: date | datetime | str,
                      expected_periods: Iterable[str | date] | None = None) -> pd.DataFrame:
    """Validate and normalise a forecaster's output.

    Required columns: `period_end`, `revenue`, `fcf_margin`.  `gross_margin` and
    `operating_margin` are optional.  `fcf` is always derived as `revenue * fcf_margin` so the
    engine and the forecaster cannot disagree about cash flow.  Periods must be unique, strictly
    after the evaluation date, and (when supplied) exactly `expected_periods`.
    """
    if not isinstance(frame, pd.DataFrame):
        raise ForecastContractError(f"predict() must return a DataFrame, got {type(frame).__name__}")
    try:
        horizon = int(horizon_quarters)
    except (TypeError, ValueError) as exc:
        raise ForecastContractError(f"horizon_quarters must be an integer, got {horizon_quarters!r}") from exc
    if horizon < 1:
        raise ForecastContractError(f"horizon_quarters must be at least 1, got {horizon}")

    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        raise ForecastContractError(
            f"forecast is missing required column(s): {', '.join(missing)}; expected "
            f"{', '.join(REQUIRED_COLUMNS)} (and optionally {', '.join(OPTIONAL_COLUMNS)})"
        )
    if len(frame) != horizon:
        raise ForecastContractError(f"forecast has {len(frame)} row(s); horizon is {horizon}")

    out = frame.copy()
    period = _parsed(out["period_end"])
    if period.isna().any():
        bad = out.loc[period.isna(), "period_end"].head(3).tolist()
        raise ForecastContractError(f"forecast has unparseable period_end value(s): {bad}")
    out["period_end"] = period.dt.strftime("%Y-%m-%d")
    if out["period_end"].duplicated().any():
        dupes = out.loc[out["period_end"].duplicated(), "period_end"].tolist()
        raise ForecastContractError(f"forecast has duplicate period_end value(s): {dupes}")
    out = out.sort_values("period_end").reset_index(drop=True)

    as_of = as_date(as_of_date)
    if expected_periods is None and (period <= pd.Timestamp(as_of)).any():
        raise ForecastContractError(f"forecast periods must be strictly after {as_of}")

    expected = None
    if expected_periods is not None:
        expected = [as_date(p).isoformat() for p in expected_periods]
        got = out["period_end"].tolist()
        if got != expected:
            raise ForecastContractError(f"forecast periods {got} do not match the requested horizon "
                                        f"{expected} after the last known quarter")

    for col in ("revenue", "fcf_margin", *[c for c in OPTIONAL_COLUMNS if c in out.columns]):
        values = pd.to_numeric(out[col], errors="coerce")
        if values.isna().any():
            raise ForecastContractError(f"forecast column {col!r} has a missing or non-numeric value")
        if not np.isfinite(values.to_numpy(dtype=float)).all():
            raise ForecastContractError(f"forecast column {col!r} has a non-finite value")
        out[col] = values.astype(float)

    out["fcf"] = out["revenue"] * out["fcf_margin"]
    ordered = [c for c in CANONICAL_COLUMNS if c in out.columns]
    return out[ordered]
