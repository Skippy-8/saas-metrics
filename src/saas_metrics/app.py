"""Zero-install Streamlit workbench for the deterministic SaaS finance pipelines.

Run it with ``saas-metrics app`` after ``pip install "saas-metrics[web]"`` (or ``streamlit run app.py``
from a source checkout).  Everything on the page is a thin wrapper over the library API: no financial logic is
duplicated here, and every action is deterministic.

Tabs
----
1. **SaaS SEC Comps** -- deferred revenue, RPO, implied billings and SBC-adjusted FCF from XBRL.
2. **ARR Waterfall & Cohort Retention** -- a Stripe / Salesforce / Chargebee export -> canonical
   ledger -> ARR summary, cohort chart and an audited Excel model.
3. **Point-in-Time Backtest Arena** -- the walking-forward backtester with its scorecard and
   one-quarter / full-horizon variance bridges. Shown only when a company store has been built
   (``saas-metrics build``); a hosted copy without one shows the first two tabs.
"""
from __future__ import annotations

import atexit
import io
import os
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from saas_metrics import company, config, engine, quality
from saas_metrics.adapters import ADAPTERS, Ledger
from saas_metrics.backtest import BacktestResult, DefaultSaaSForecaster, PointInTimeBacktester

st.set_page_config(
    page_title="saas-metrics workbench",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

MONEY_COLUMNS = (
    "revenue_ttm",
    "deferred_revenue",
    "implied_billings_ttm",
    "rpo",
    "sbc_ttm",
    "cfo_ttm",
    "capex_ttm",
    "fcf_ttm",
    "sbc_adjusted_fcf_ttm",
)
PCT_COLUMNS = (
    "deferred_revenue_yoy",
    "implied_billings_growth_yoy",
    "billings_to_revenue",
    "rpo_growth_yoy",
    "sbc_pct_revenue",
    "fcf_margin",
    "sbc_adjusted_fcf_margin",
)
QUALITY_VIEW = (
    ("ticker", "Ticker"),
    ("fiscal_label", "Quarter"),
    ("revenue_ttm", "Revenue TTM"),
    ("deferred_revenue", "Deferred revenue"),
    ("implied_billings_ttm", "Implied billings TTM"),
    ("implied_billings_growth_yoy", "Billings growth"),
    ("billings_to_revenue", "Billings / revenue"),
    ("rpo", "RPO"),
    ("rpo_growth_yoy", "RPO growth"),
    ("sbc_pct_revenue", "SBC % revenue"),
    ("fcf_margin", "FCF margin"),
    ("sbc_adjusted_fcf_margin", "SBC-adj. FCF margin"),
)
ADAPTER_LABELS = {"Stripe": "stripe", "Salesforce": "salesforce", "Chargebee": "chargebee"}


# ---- formatting ----------------------------------------------------------------------------------
def _is_missing(value: Any) -> bool:
    try:
        return value is None or bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _money(value: Any) -> str:
    if _is_missing(value):
        return "n/a"
    return f"${float(value) / 1e6:,.0f}M"


def _pct(value: Any, digits: int = 1) -> str:
    if _is_missing(value):
        return "n/a"
    return f"{float(value) * 100:.{digits}f}%"


def _years(value: Any) -> str:
    if _is_missing(value):
        return "n/a"
    return f"{float(value):.1f}y"


def _cell(column: str, value: Any) -> str:
    if column in MONEY_COLUMNS:
        return _money(value)
    if column in PCT_COLUMNS:
        return _pct(value)
    if column == "rpo_cover_years":
        return _years(value)
    if _is_missing(value):
        return "n/a"
    return str(value)


def _quality_markdown(frame: pd.DataFrame) -> str:
    """Markdown table without relying on `tabulate`, which is not a declared dependency."""
    lines = [
        "| " + " | ".join(label for _, label in QUALITY_VIEW) + " |",
        "|" + "|".join("---" for _ in QUALITY_VIEW) + "|",
    ]
    for _, row in frame.iterrows():
        lines.append("| " + " | ".join(_cell(column, row[column]) for column, _ in QUALITY_VIEW) + " |")
    return "\n".join(lines)


# ---- tab 1: SEC peer comps -----------------------------------------------------------------------
def _render_quality() -> None:
    st.subheader("SaaS SEC Comps")
    st.caption(
        "Trailing-four-quarter revenue quality from SEC XBRL. Implied billings = revenue + the change "
        "in deferred revenue; SBC-adjusted FCF = CFO - capex - stock-based compensation. Absent tags "
        "stay blank rather than being replaced by zero."
    )
    tickers = st.multiselect(
        "Tickers",
        options=["DDOG", "SNOW", "MDB", "TTD"],
        default=["DDOG", "SNOW", "MDB"],
        help="Each ticker needs a configs/<TICKER>.yaml with a benchmark section.",
    )
    if not tickers:
        st.info("Select at least one ticker to build the comp table.")
        return

    try:
        frame, results = quality.table(tickers)
    except RuntimeError as exc:
        if "SEC_USER_AGENT" not in str(exc):
            raise
        st.warning(
            "This tab downloads public filings from SEC EDGAR, and the SEC asks every client to identify "
            "itself. Set `SEC_USER_AGENT` to a name and email (for example `Jane Doe jane@example.com`): "
            "as an environment variable or `.env` file locally, or as a secret on Streamlit Community Cloud."
        )
        return
    except Exception as exc:  # SEC cache / config problems should be shown, not crash the page
        st.error(f"Could not build the SEC comp table: {exc}")
        st.caption("The first run may need network access to data.sec.gov. Cached companyfacts are reused.")
        return

    st.markdown(_quality_markdown(frame))
    checks = [(ticker, check) for ticker, result in results.items() for check in result.checks]
    failed = [(ticker, check) for ticker, check in checks if not check[1]]
    with st.expander(f"Checks and tag notes ({len(checks) - len(failed)} passed, {len(failed)} failed)"):
        if failed:
            for ticker, (name, _, detail) in failed:
                st.write(f"**FAIL {ticker}** — {name}: {detail}")
        else:
            st.write("Every configured reconciliation passed.")
        for ticker, result in results.items():
            for note in result.notes:
                st.write(f"**{ticker}** — {note}")


# ---- tab 2: billing ledger -> ARR / cohorts / Excel ----------------------------------------------
def _ledger_errors(ledger: Ledger) -> list:
    errors = list(ledger.report.errors)
    if ledger.fx_report is not None:
        errors += list(ledger.fx_report.errors)
    return errors


def _filtered_ledger(frame: pd.DataFrame, adapter: str) -> Ledger:
    """Run the Phase 1 adapter pipeline on an uploaded frame."""
    return ADAPTERS[adapter](frame)


def _session_db_path() -> Path:
    """A DuckDB file private to this Streamlit session.

    ``mkstemp`` creates an empty placeholder, but DuckDB refuses to open an existing zero-byte file.
    Unlink it immediately and let the ingest create the database. The path stays in session state so
    the upload, metrics and Excel download all use the same database across Streamlit reruns.
    """
    existing = st.session_state.get("_billing_db_path")
    if existing is not None:
        return Path(existing)

    fd, name = tempfile.mkstemp(prefix="saas-metrics-", suffix=".duckdb")
    os.close(fd)
    Path(name).unlink(missing_ok=True)
    atexit.register(lambda path=name: Path(path).unlink(missing_ok=True))
    st.session_state["_billing_db_path"] = name
    return Path(name)


def _ingest_upload(data: bytes, adapter: str, db_path: Path) -> str:
    """Run the same ingest tool the MCP clients use, writing the canonical ledger to ``db_path``."""
    from saas_metrics.mcp_server import ingest_billing_ledger

    fd, export_path = tempfile.mkstemp(prefix="saas-metrics-", suffix=".csv")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        return ingest_billing_ledger(export_path, adapter=adapter, db_path=str(db_path))
    finally:
        Path(export_path).unlink(missing_ok=True)


def _cohort_chart(report: engine.Report) -> pd.DataFrame:
    """Ending ARR by acquisition cohort and period (columns are cohort labels)."""
    matrix = report.customers.set_index("customer_id")
    positive = matrix.where(matrix > 0)
    cohort = positive.apply(lambda row: row.first_valid_index(), axis=1)
    keep = cohort.notna()
    matrix = matrix.loc[keep]
    labels = pd.to_datetime(cohort.loc[keep]).dt.strftime("%Y-%m")
    annualize = engine.ANNUALIZE[report.grain]
    by_cohort = matrix.groupby(labels).sum(numeric_only=True).T * annualize
    by_cohort.index = pd.to_datetime(by_cohort.index)
    by_cohort.index.name = "Period end"
    by_cohort.columns.name = "Cohort"
    return by_cohort


def _excel_download(db_path: Path) -> tuple[bytes, str]:
    """Run ``generate_cohort_report`` against the session ledger and return the .xlsx bytes."""
    from saas_metrics.mcp_server import generate_cohort_report

    fd, workbook = tempfile.mkstemp(prefix="saas-metrics-", suffix=".xlsx")
    os.close(fd)
    try:
        summary = generate_cohort_report(workbook, db_path=str(db_path))
        return Path(workbook).read_bytes(), summary
    finally:
        Path(workbook).unlink(missing_ok=True)


def _render_billing() -> None:
    st.subheader("ARR Waterfall & Cohort Retention")
    st.caption(
        "Upload a billing export. The adapter maps columns, validates the rows, bridges renewal gaps, "
        "spreads invoices over their service periods, and writes the canonical ledger to DuckDB before "
        "the waterfall or the workbook is built."
    )
    left, right = st.columns([1, 2])
    adapter_label = left.selectbox("Adapter", list(ADAPTER_LABELS), index=0)
    uploaded = right.file_uploader("Billing CSV", type=["csv"], accept_multiple_files=False)
    if uploaded is None:
        st.info("Upload a CSV from Stripe, Salesforce or Chargebee to see the ARR waterfall and cohorts.")
        return

    adapter = ADAPTER_LABELS[adapter_label]
    data = uploaded.getvalue()
    try:
        frame = pd.read_csv(io.BytesIO(data), dtype=str)
    except Exception as exc:
        st.error(f"Could not read the CSV: {exc}")
        return
    if frame.empty:
        st.error("The uploaded CSV has no rows.")
        return

    with st.spinner("Running the adapter pipeline..."):
        try:
            ledger = _filtered_ledger(frame, adapter)
        except Exception as exc:
            st.error(f"The {adapter_label} adapter rejected the export: {exc}")
            return

    errors = _ledger_errors(ledger)
    if errors:
        st.error(f"The adapter found {len(errors)} blocking diagnostic(s); no metrics are shown.")
        for diagnostic in errors[:10]:
            st.write(f"- `{diagnostic.code}`: {diagnostic.message}")
        if len(errors) > 10:
            st.caption(f"... and {len(errors) - 10} more.")
        return
    if ledger.frame is None or ledger.frame.empty:
        st.error("The adapter produced an empty ledger.")
        return

    warnings = list(ledger.report.warnings)
    if ledger.fx_report is not None:
        warnings += [d for d in ledger.fx_report.diagnostics if d.level == "warning"]
    if warnings:
        with st.expander(f"Validation warnings ({len(warnings)})"):
            for diagnostic in warnings:
                st.write(f"- `{diagnostic.code}`: {diagnostic.message}")

    db_path = _session_db_path()
    with st.spinner("Writing the canonical ledger to this session's DuckDB..."):
        try:
            ingest_summary = _ingest_upload(data, adapter, db_path)
        except Exception as exc:
            st.error(f"The canonical ledger could not be written: {exc}")
            return

    with st.spinner("Running the DuckDB waterfall engine..."):
        try:
            tape, engine_notes = ledger.to_engine_input()
            report = engine.analyze(tape, allow_warnings=True)
        except Exception as exc:
            st.error(f"The ledger could not be turned into a report: {exc}")
            return

    if not report.ok:
        st.error("The waterfall engine could not build a report.")
        for check in report.checks:
            if check.level in ("error", "warning"):
                st.write(f"- **{check.level.upper()} {check.name}**: {check.detail}")
        return

    waterfall, metrics = report.waterfall, report.metrics
    annualize = engine.ANNUALIZE[report.grain]
    starting_arr = float(waterfall.iloc[-1]["beginning"]) * annualize
    ending_arr = float(metrics.iloc[-1]["arr"])
    total_mrr = pd.to_numeric(ledger.frame["mrr_amount"], errors="coerce").sum()

    cols = st.columns(4)
    cols[0].metric("Row count", f"{len(ledger.frame):,}")
    cols[1].metric("Total MRR", f"${total_mrr:,.0f}")
    cols[2].metric("Starting ARR", f"${starting_arr:,.0f}")
    cols[3].metric("Ending ARR", f"${ending_arr:,.0f}")
    st.caption(
        f"{len(waterfall)} {report.grain}(s); starting ARR is the beginning of the last period "
        f"({pd.Timestamp(waterfall.iloc[-1]['period_end']).date()}); ending ARR is its end."
    )
    with st.expander("DuckDB ingest log"):
        st.text(ingest_summary)

    st.markdown("#### Ending ARR by acquisition cohort")
    chart = _cohort_chart(report)
    if chart.empty:
        st.info("No positive customer revenue was available to assign a cohort.")
    else:
        st.bar_chart(chart)
        st.caption("Each customer is assigned to its first positive-revenue period; revenue is annualised.")

    try:
        with st.spinner("Generating the audited Excel workbook..."):
            workbook_bytes, report_summary = _excel_download(db_path)
    except Exception as exc:
        st.warning(f"The ARR summary is ready, but the Excel model could not be generated: {exc}")
        st.caption("Install the `web` extra (`pip install \"saas-metrics[web]\"`) to enable the download.")
    else:
        st.download_button(
            "Download audited Excel model",
            data=workbook_bytes,
            file_name="cohort_report.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="download_cohort_report",
            type="primary",
        )
        with st.expander("Workbook generation log"):
            st.text(report_summary)

    if engine_notes or report.notes or ledger.all_notes():
        with st.expander("Pipeline notes"):
            for note in [*engine_notes, *report.notes, *ledger.all_notes()]:
                st.write(f"- {note}")


# ---- tab 3: point-in-time backtest ---------------------------------------------------------------
def _model_tickers() -> list[str]:
    """Full-model tickers whose local DuckDB store exists, so the tab never starts on a missing file."""
    found: list[str] = []
    for ticker in company.available():
        try:
            candidate = company.load(ticker)
            if not candidate.has_model:
                continue
            with config.using(candidate):
                exists = config.DB_PATH.exists()
            if exists:
                found.append(candidate.ticker)
        except Exception:
            continue
    example = company.CONFIG_DIR / "examples" / "SYNTHCO.yaml"
    if example.exists():
        try:
            candidate = company.load(example)
            with config.using(candidate):
                exists = config.DB_PATH.exists()
            if exists:
                found.append(candidate.ticker)
        except Exception:
            pass
    return found


@st.cache_data(show_spinner="Running the point-in-time backtest...")
def _run_point_in_time(ticker: str, evaluations: int, horizon: int) -> BacktestResult:
    return PointInTimeBacktester(
        ticker,
        forecaster=DefaultSaaSForecaster(),
        max_evaluations=evaluations,
        horizon=horizon,
    ).run()


def _format_summary(result: BacktestResult) -> pd.DataFrame:
    summary = result.summary.copy()
    summary["mape"] = summary["mape"].map(_pct)
    summary["bias"] = summary["bias"].map(lambda value: "n/a" if _is_missing(value) else f"{float(value):+.2%}")
    return summary.rename(columns={"mape": "MAPE", "bias": "Bias", "direction": "Direction"})


def _format_bridges(result: BacktestResult) -> pd.DataFrame:
    bridges = result.bridges.copy()
    for column in ("forecast", "actual", "variance"):
        bridges[column] = bridges[column].map(lambda value: f"{float(value) / 1e6:,.1f}")
    bridges["variance_pct"] = bridges["variance_pct"].map(
        lambda value: "n/a" if _is_missing(value) else f"{float(value):+.1%}"
    )
    return bridges.rename(
        columns={
            "as_of": "Origin",
            "last_known": "Last known",
            "metric": "Metric",
            "window": "Window",
            "forecast": "Forecast $M",
            "actual": "Actual $M",
            "variance": "Variance $M",
            "variance_pct": "Variance %",
        }
    )


def _render_backtest() -> None:
    st.subheader("Point-in-Time Backtest Arena")
    st.caption(
        "Each origin sees only filings available on that date, fits the default SaaS operating model, "
        "and is scored against actuals that were still in the future. The variance bridge isolates the "
        "next quarter (1Q) and the full horizon."
    )
    tickers = _model_tickers()
    if not tickers:
        st.info("No company store has been built here. Run `saas-metrics fetch` and `saas-metrics build` first.")
        return
    left, middle, right = st.columns(3)
    ticker = left.selectbox("Ticker", tickers, index=0)
    evaluations = middle.number_input("Evaluation quarters", min_value=1, max_value=12, value=4, step=1)
    horizon = right.number_input("Horizon (quarters)", min_value=1, max_value=12, value=4, step=1)

    try:
        result = _run_point_in_time(ticker, int(evaluations), int(horizon))
    except Exception as exc:
        st.error(f"The point-in-time backtest could not run: {exc}")
        st.caption("Build the company store first with `saas-metrics --config configs/MDB.yaml build`.")
        return

    origins = result.evaluations["as_of"].nunique()
    st.caption(f"{result.ticker} | {result.forecaster} | {origins} origin(s) | {result.horizon}Q horizon")
    st.markdown("#### MAPE and bias scorecard")
    st.dataframe(_format_summary(result), hide_index=True, use_container_width=True)
    st.caption("Bias is mean signed error as a share of absolute actual; MAPE is mean absolute percentage error.")

    st.markdown(f"#### 1Q and {result.horizon}Q variance bridge")
    st.dataframe(_format_bridges(result), hide_index=True, use_container_width=True)
    with st.expander("Evaluation detail"):
        st.dataframe(result.evaluations, hide_index=True, use_container_width=True)


# ---- page ----------------------------------------------------------------------------------------
st.title("saas-metrics workbench")
st.caption(
    "Point-in-time FP&A backtesting, SEC XBRL peer quality and audited Excel models — "
    "for Strategic Finance, PE Operating Partners and Quant Analysts."
)
has_store = bool(_model_tickers())       # the backtest needs a locally built store (not on a fresh host)
labels = ["SaaS SEC Comps", "ARR Waterfall & Cohort Retention"] + (["Point-in-Time Backtest Arena"] if has_store else [])
tabs = st.tabs(labels)
with tabs[0]:
    _render_quality()
with tabs[1]:
    _render_billing()
if has_store:
    with tabs[2]:
        _render_backtest()
else:
    st.caption("The point-in-time backtest appears once a company store is built (`saas-metrics fetch` then "
               "`saas-metrics build`).")
