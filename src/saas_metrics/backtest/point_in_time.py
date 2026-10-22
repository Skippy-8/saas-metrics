"""Point-in-time data slices.

Two rules, one per source:

* **SEC XBRL** facts are known when they are *filed*.  A slice at T derives quarterly values from
  facts with ``filed <= T`` only; a quarter whose numbers were first reported after T simply does
  not exist in that slice.
* **Billing / ledger CSVs** are known when the service period has ended.  Rows are filtered with
  ``period_end <= T``.

The engine also calls `guard_no_future` on whatever a loader returns, so a faulty loader cannot
turn into a silent look-ahead.
"""
from __future__ import annotations

from datetime import date

import duckdb
import pandas as pd

from saas_metrics import build, cohort, config, drivers, model, periods, views
from saas_metrics.backtest.plan import normalise_history
from saas_metrics.backtest.protocol import ForecastContractError, as_date, guard_no_future

SEC_VINTAGE = "point_in_time"
BILLING_DATE_COLUMN = "period_end"


def slice_billing(frame: pd.DataFrame, as_of_date: date | str,
                  date_column: str = BILLING_DATE_COLUMN) -> pd.DataFrame:
    """Rows of a billing CSV known at `as_of_date`: ``period_end <= as_of``.

    Unparseable dates are a contract error, not something to drop silently: an undateable row
    cannot be proven safe.
    """
    if date_column not in frame.columns:
        raise ForecastContractError(f"billing frame has no {date_column!r} column")
    as_of = as_date(as_of_date)
    parsed = pd.to_datetime(frame[date_column], errors="coerce", format="mixed")
    if parsed.isna().any():
        raise ForecastContractError(f"billing frame has unparseable {date_column!r} value(s)")
    return frame.loc[parsed <= pd.Timestamp(as_of)].copy()


def billing_history_loader(frame: pd.DataFrame, date_column: str = BILLING_DATE_COLUMN):
    """A `history_loader` for the backtester backed by a billing/ledger frame.

        harness = PointInTimeBacktester(con=..., forecaster=..., history_loader=billing_history_loader(ledger))
    """
    def load(as_of_date: date | str) -> pd.DataFrame:
        return slice_billing(frame, as_of_date, date_column)
    return load


def last_known_quarter(frame: pd.DataFrame, as_of_date: date | str) -> str:
    """The latest period_end in the slice on or before `as_of` (ISO string)."""
    as_of = as_date(as_of_date)
    if "period_end" not in frame.columns:
        raise ForecastContractError("history frame has no period_end column")
    parsed = pd.to_datetime(frame["period_end"], errors="coerce")
    if parsed.isna().any():
        raise ForecastContractError("history frame has unparseable period_end value(s)")
    known = parsed[parsed <= pd.Timestamp(as_of)]
    if known.empty:
        raise ForecastContractError(f"history has no quarter ending on or before {as_of}")
    return known.max().date().isoformat()


def sec_history(con: duckdb.DuckDBPyConnection, as_of_date: date | str) -> pd.DataFrame:
    """Model-style quarterly history as known on `as_of_date`, from SEC filings only.

    Rebuilds quarterly values from the stored `facts` table with ``filed <= T``, recreates the
    analytical view in a private in-memory database, calibrates the cohort ledger from KPI
    disclosures filed by T, and attaches the disclosed NRR, new-logo counts and the filing date
    of each quarter so the look-ahead guard can verify the slice.
    """
    as_of = as_date(as_of_date).isoformat()
    facts = con.execute("SELECT * FROM facts").df()
    quarterly = build.derive_quarterly(facts, as_of, SEC_VINTAGE)
    kpi = con.execute("SELECT * FROM kpi_disclosures").df()
    if "filed" in kpi.columns:
        filed = pd.to_datetime(kpi["filed"], errors="coerce")
        kpi = kpi.loc[filed <= pd.Timestamp(as_of)].copy()

    pit = duckdb.connect()
    try:
        pit.register("_quarterly", quarterly)
        pit.register("_kpi", kpi)
        pit.execute("CREATE TABLE quarterly_financials AS SELECT * FROM _quarterly")
        pit.execute("CREATE TABLE kpi_disclosures AS SELECT * FROM _kpi")
        views.create(pit, upto="01")
        qw = pit.execute("SELECT * FROM q_wide WHERE vintage = ?", [SEC_VINTAGE]).df()
        if qw.empty:
            raise ForecastContractError(f"no SEC quarters known at {as_of}")
        cal = cohort.build(cohort.inputs(qw, kpi))[2]
        hc = drivers.headcount_history(pit, qw, as_of=as_of)
        rows = model._history_rows(qw, cal.set_index("period_end"),
                                   hc.set_index("period_end")["headcount"])
        for i, row in enumerate(rows):
            model._derived(row, rows[max(0, i - 3):i] if i >= 3 else [])
            row["fcf"] = row["cfo"] - row["capex"]
    finally:
        pit.close()

    frame = pd.DataFrame(rows)
    frame["in_window"] = frame["period_end"].map(qw.set_index("period_end")["in_window"])
    for col in ("new_logos", "new_cohort_arr", "churn_q_assumption"):
        frame[col] = frame["period_end"].map(cal.set_index("period_end")[col])
    nrr_kpi = config.ACTIVE.raw["cohort_ledger"]["nrr_kpi"]
    nrr = kpi.loc[kpi["kpi"] == nrr_kpi].set_index("period_end")["value"] / 100
    frame["nrr"] = frame["period_end"].map(nrr)
    filed_by_quarter = quarterly.groupby("period_end")["filed"].max()
    frame["filed"] = frame["period_end"].map(filed_by_quarter)

    frame = normalise_history(frame)
    guard_no_future(frame, as_of)
    return frame


def latest_actuals(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Every quarter as finally reported, with the model's FCF definition (CFO - capex)."""
    qw = con.execute("SELECT * FROM q_wide WHERE vintage = 'latest' ORDER BY period_end").df()
    if qw.empty:
        raise ForecastContractError("the store has no 'latest' vintage to score against")
    for col in ("period_end", "revenue", "cfo", "capex"):
        if col not in qw.columns:
            raise ForecastContractError(f"latest q_wide has no {col!r} column")
    out = qw[["period_end", "revenue", "cfo", "capex"]].copy()
    out["fcf"] = out["cfo"] - out["capex"]
    return out.set_index("period_end").sort_index()


def evaluation_dates(con: duckdb.DuckDBPyConnection, horizon: int = 4,
                     max_evaluations: int = 4) -> list[date]:
    """Quarter ends on which a forecast could have been made, most recent last.

    A date qualifies when (a) the store has actuals for the full horizon after it and (b) the
    history back to `prewindow_from` holds enough quarters to derive every driver.  The history
    itself is still filtered by filing date inside `sec_history`; this is only a calendar filter.
    """
    if horizon < 1:
        raise ForecastContractError("horizon must be at least 1")
    actuals = latest_actuals(con)
    last_actual = actuals.index.max()
    all_quarters = periods.quarter_ends(config.PREWINDOW_FROM, config.LAST_QUARTER_END)
    first_window = date.fromisoformat(config.FIRST_QUARTER_END)
    out: list[date] = []
    for q in all_quarters:
        if q < first_window:
            continue
        if model.quarters_after(q.isoformat(), horizon)[-1] > last_actual:
            continue
        history_through_q = [x for x in all_quarters if x <= q]
        window_through_q = [x for x in history_through_q if x >= first_window]
        if len(history_through_q) < 8 or len(window_through_q) < 5:
            continue
        out.append(q)
    return out[-max_evaluations:] if max_evaluations > 0 else out
