"""Model Context Protocol server: the four deterministic pipelines as agent-callable tools.

Every tool is a thin wrapper over an existing top-level API — `adapters.ADAPTERS` for ingest,
`engine.analyze` for the cohort report, `benchmark.comps` for peer comps, `quality.table` for the
revenue-quality scorecard. No financial logic is duplicated here, and the server never calls an LLM.

Install the extra and run it over stdio:

    pip install "saas-metrics[mcp]"
    saas-metrics mcp

or point an MCP client at `saas_metrics/mcp_server.py:mcp`.

The ledger database is `data/billing.duckdb` under the work root (see company.work_root) unless `SAAS_METRICS_LEDGER_DB`
(or the tool's `db_path`) says otherwise. `ingest_billing_ledger` writes the canonical ledger there;
`generate_cohort_report` reads it back, so the two tools compose: ingest once, report as often as you
like, with no re-parsing of the source export.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import pandas as pd
from fastmcp import FastMCP

from saas_metrics import benchmark, config, engine, quality
from saas_metrics.adapters import ADAPTERS, Ledger
from saas_metrics.contracts import ContractError

LEDGER_TABLE = "billing_ledger"
DB_ENV = "SAAS_METRICS_LEDGER_DB"
DEFAULT_DB = config.ROOT / "data" / "billing.duckdb"

mcp = FastMCP(
    "saas-metrics",
    instructions=(
        "Deterministic SaaS finance tools. Typical order: ingest_billing_ledger -> generate_cohort_report. "
        "run_sec_peer_benchmark and analyze_revenue_quality read SEC XBRL company facts and need no local "
        "billing data. Every number is computed in Python; nothing is estimated by a model."
    ),
)


# ---- shared helpers ------------------------------------------------------------------------------
def ledger_db_path(db_path: str | os.PathLike | None = None) -> Path:
    """The DuckDB file the two billing tools share: explicit argument, env var, then the project default."""
    if db_path:
        p = Path(db_path).expanduser()
    elif os.environ.get(DB_ENV):
        p = Path(os.environ[DB_ENV]).expanduser()
    else:
        p = DEFAULT_DB
    return p.resolve()


def _load_ledger(db_path: str | None = None) -> Ledger:
    """Read the canonical ledger an earlier ingest wrote, as a `Ledger` the engine can consume."""
    import duckdb

    p = ledger_db_path(db_path)
    if not p.exists():
        raise ValueError(f"no ledger database at {p}; call ingest_billing_ledger first or pass db_path")
    con = duckdb.connect(str(p), read_only=True)
    try:
        found = con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'main' AND table_name = ?",
            [LEDGER_TABLE],
        ).fetchone()[0]
        if not found:
            raise ValueError(f"{p} has no {LEDGER_TABLE!r} table; call ingest_billing_ledger first")
        frame = con.execute(f'SELECT * FROM "{LEDGER_TABLE}"').df()
    finally:
        con.close()
    if frame.empty:
        raise ValueError(f"{p}: {LEDGER_TABLE!r} is empty; call ingest_billing_ledger first")
    return Ledger(frame=frame, adapter="duckdb", source=str(p))


def _errors(ledger: Ledger) -> list:
    out = list(ledger.report.errors)
    if ledger.fx_report is not None:
        out += list(ledger.fx_report.errors)
    return out


def _num(v) -> float | None:
    return None if v is None or pd.isna(v) else float(v)


def _money(v, digits: int = 0) -> str:
    v = _num(v)
    return "n/a" if v is None else f"${v:,.{digits}f}"


def _millions(v) -> str:
    v = _num(v)
    return "n/a" if v is None else f"${v / 1e6:,.0f}M"


def _compact(v) -> str:
    """$1.2M / $48K / $950 — never hide a real number behind a rounded zero."""
    v = _num(v)
    if v is None:
        return "n/a"
    if abs(v) >= 1_000_000:
        return f"${v / 1e6:,.1f}M"
    if abs(v) >= 1_000:
        return f"${v / 1e3:,.0f}K"
    return f"${v:,.0f}"


def _day(v) -> str:
    return "" if v is None or pd.isna(v) else str(pd.Timestamp(v).date())


def _pct(v, digits: int = 1) -> str:
    v = _num(v)
    return "n/a" if v is None else f"{v * 100:.{digits}f}%"


def _md_table(headers: list[tuple[str, str]], rows: list[list[str]]) -> str:
    """Markdown table from (column, label) pairs and pre-formatted cells."""
    lines = ["| " + " | ".join(label for _, label in headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)



# ---- tools ---------------------------------------------------------------------------------------
@mcp.tool
def ingest_billing_ledger(export_path: str, adapter: Literal["stripe", "chargebee", "salesforce"] = "stripe",
                          db_path: str | None = None) -> str:
    """Normalise a Stripe, Chargebee or Salesforce export and write the canonical ledger to DuckDB.

    Runs the full Phase 1 pipeline: column mapping, Pydantic contracts, FX conversion, grace bridging,
    ramp classification, proration, and (Salesforce) account stitching. The write is read back and
    checked before this returns. Validation errors block the write; warnings are returned for review.

    Args:
        export_path: path to the billing export (.csv, .csv.gz, .parquet or .xlsx).
        adapter: which system produced it.
        db_path: DuckDB file to write (default: data/billing.duckdb, or SAAS_METRICS_LEDGER_DB).
    """
    source = Path(export_path).expanduser()
    if not source.exists():
        raise ValueError(f"export not found: {source}")
    ledger = ADAPTERS[adapter](source)
    bad = _errors(ledger)
    if bad:
        raise ContractError(f"{len(bad)} blocking diagnostic(s): " + "; ".join(str(d) for d in bad[:5]))
    target = ledger_db_path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    written = ledger.to_duckdb(target, table=LEDGER_TABLE)
    warnings = list(ledger.report.warnings)
    if ledger.fx_report is not None:
        warnings += [d for d in ledger.fx_report.diagnostics if d.level == "warning"]
    lines = [
        f"Ingested {written['rows']} row(s) from the {adapter} export into {target} "
        f"(table {LEDGER_TABLE}); total MRR {_money(written['mrr_total'], 2)}."
    ]
    lines.append("Write-back verified against the in-memory ledger." if written.get("verified")
                 else "WARNING: write-back count did not match the in-memory ledger.")
    if warnings:
        lines.append(f"\nValidation warnings ({len(warnings)}):")
        lines += [f"- {d}" for d in warnings]
    else:
        lines.append("\nValidation warnings: none.")
    notes = ledger.all_notes()
    if notes:
        lines.append("\nNotes:")
        lines += [f"- {n}" for n in notes]
    return "\n".join(lines)


@mcp.tool
def generate_cohort_report(output_filename: str | None = None, grain: Literal["month", "quarter"] = "month",
                           db_path: str | None = None) -> str:
    """Build the ARR waterfall and cohort retention model as an audited Excel workbook.

    Reads the canonical ledger written by `ingest_billing_ledger`, spreads it into a
    customer-period tape, runs the DuckDB engine, and writes the workbook. The workbook's Checks
    sheet recomputes every metric from live formulas and compares it with the Python engine.

    Args:
        output_filename: where to write the .xlsx (default: cohort_report.xlsx beside the ledger).
        grain: month (default) or quarter.
        db_path: DuckDB file to read (default: data/billing.duckdb, or SAAS_METRICS_LEDGER_DB).
    """
    ledger = _load_ledger(db_path)
    tape, notes = ledger.to_engine_input(grain=grain)
    report = engine.analyze(tape, grain=grain, allow_warnings=True)
    if not report.ok:
        detail = "; ".join(f"{c.name}: {c.detail}" for c in report.checks if c.level in ("error", "warning"))
        raise ValueError(f"the ledger cannot produce a report: {detail or 'no tables were built'}")
    out = Path(output_filename).expanduser() if output_filename else ledger_db_path(db_path).parent / \
        "cohort_report.xlsx"
    out = out if out.is_absolute() else Path.cwd() / out
    out.parent.mkdir(parents=True, exist_ok=True)
    path = report.to_excel(out)

    w, m = report.waterfall, report.metrics
    k = engine.ANNUALIZE[grain]
    last = m.iloc[-1]
    starting = float(w.iloc[-1]["beginning"]) * k
    ending = float(last["arr"])
    flagged = [c for c in report.checks if c.level in ("error", "warning")]
    lines = [
        f"Wrote {path.resolve()}",
        "",
        f"- Starting ARR: {_compact(starting)} (beginning of {_day(last['period_end'])})",
        f"- Ending ARR: {_compact(ending)} (end of {_day(last['period_end'])})",
        f"- ARR change: {_compact(ending - starting)}",
        f"- GRR (12m): {_pct(last['grr_12m'])}",
        f"- NRR (12m): {_pct(last['nrr_12m'])}",
        "",
        f"{len(w)} {grain}(s); {len(report.checks)} pre-flight check(s), {len(flagged)} flagged.",
    ]
    lines += [f"- {c.level.upper()} {c.name}: {c.detail}" for c in flagged]
    if notes or report.notes:
        lines.append("\nNotes:")
        lines += [f"- {n}" for n in [*notes, *report.notes]]
    return "\n".join(lines)


@mcp.tool
def run_sec_peer_benchmark(tickers: list[str], quarters_back: int = 4, as_of: str | None = None) -> str:
    """Peer comp table from SEC XBRL: TTM revenue, margins, growth and Rule of 40.

    The summary row is the trailing four fiscal quarters (Rule of 40 is defined on TTM). `quarters_back`
    adds a trend of that many trailing quarters per ticker. `as_of` restricts to filings on or before
    that date, which is how a point-in-time view is reproduced.

    Args:
        tickers: tickers with a configs/<TICKER>.yaml benchmark section, e.g. ["DDOG", "SNOW"].
        quarters_back: trailing quarters of trend to show (default 4, minimum 1).
        as_of: use only filings on or before this date (YYYY-MM-DD).
    """
    if not tickers:
        raise ValueError("tickers must not be empty")
    if quarters_back < 1:
        raise ValueError("quarters_back must be at least 1")
    table, results = benchmark.comps([t.upper() for t in tickers], as_of=as_of)

    rows = [[r["ticker"], r["fiscal_label"], _millions(r["revenue_ttm"]), _pct(r["revenue_growth_yoy"]),
             _pct(r["gross_margin"]), _pct(r["non_gaap_op_margin_approx"]), _pct(r["fcf_margin"]),
             _pct(r["rule_of_40"])] for _, r in table.iterrows()]
    out = [
        "## SEC XBRL peer comps — trailing four fiscal quarters"
        + (f" as known on {as_of}" if as_of else ""),
        "",
        _md_table([("ticker", "Ticker"), ("label", "Quarter"), ("revenue", "Revenue, TTM"),
                   ("growth", "Growth (TTM YoY)"), ("gm", "Gross margin"), ("op", "Non-GAAP op. margin"),
                   ("fcf", "FCF margin"), ("r40", "Rule of 40")], rows),
        "",
        "Rule of 40 = revenue growth (TTM vs prior TTM) + FCF margin. Non-GAAP operating margin is an "
        "approximation (GAAP + SBC + amortization); company definitions exclude more.",
    ]

    trend_rows = []
    for t, res in results.items():
        q = res.quarterly
        start = max(0, len(q) - quarters_back)
        for i in range(start, len(q)):
            rev = _num(q["revenue"].iloc[i])
            prior = _num(q["revenue"].iloc[i - 4]) if i >= 4 else None
            growth = rev / prior - 1 if rev and prior else None
            cfo = _num(q["cfo"].iloc[i]) if "cfo" in q else None
            capex = _num(q["capex"].iloc[i]) if "capex" in q else 0.0
            fcf = cfo - (capex or 0.0) if cfo is not None else None
            margin = fcf / rev if fcf is not None and rev else None
            trend_rows.append([t, str(q.index[i]), _millions(rev), _pct(growth), _pct(margin),
                               _pct(growth + margin if growth is not None and margin is not None else None)])
    if trend_rows:
        out += ["", f"## {quarters_back}-quarter trend",
                _md_table([("t", "Ticker"), ("p", "Quarter end"), ("r", "Revenue"), ("g", "Growth (YoY)"),
                           ("f", "FCF margin"), ("r40", "Rule of 40")], trend_rows)]

    checks = [(t, c) for t, res in results.items() for c in res.checks]
    failed = [(t, c) for t, c in checks if not c[1]]
    out += ["", f"## Checks\n{len(checks) - len(failed)} passed, {len(failed)} failed."]
    out += [f"- FAIL {t}: {c[0]} — {c[2]}" for t, c in failed]
    notes = [n for res in results.values() for n in res.notes]
    if notes:
        out += ["", "## Notes"] + [f"- {n}" for n in notes]
    return "\n".join(out)


@mcp.tool
def analyze_revenue_quality(tickers: list[str], as_of: str | None = None) -> str:
    """Revenue and cash quality scorecard from SEC XBRL: deferred revenue, RPO, SBC, implied billings.

    Implied billings = revenue + the change in deferred revenue; SBC-adjusted FCF = CFO − capex − SBC.
    Tag families are resolved per quarter with ASC 606 splicing, and absent tags stay blank ("n/a") —
    never zero. `as_of` restricts to filings on or before that date.

    Args:
        tickers: tickers with a configs/<TICKER>.yaml benchmark section, e.g. ["DDOG", "SNOW"].
        as_of: use only filings on or before this date (YYYY-MM-DD).
    """
    if not tickers:
        raise ValueError("tickers must not be empty")
    frame, results = quality.table([t.upper() for t in tickers], as_of=as_of)

    rows = [[r["ticker"], r["fiscal_label"], _millions(r["revenue_ttm"]), _millions(r["deferred_revenue"]),
             _millions(r["implied_billings_ttm"]), _pct(r["billings_to_revenue"]), _millions(r["rpo"]),
             _pct(r["sbc_pct_revenue"]), _pct(r["sbc_adjusted_fcf_margin"])] for _, r in frame.iterrows()]
    out = [
        "## Revenue-quality scorecard — trailing four fiscal quarters"
        + (f" as known on {as_of}" if as_of else ""),
        "",
        _md_table([("t", "Ticker"), ("q", "Quarter"), ("rev", "Revenue, TTM"),
                   ("dr", "Deferred revenue"), ("ib", "Implied billings, TTM"), ("b2r", "Billings / revenue"),
                   ("rpo", "RPO"), ("sbc", "SBC % revenue"), ("fcf", "SBC-adj. FCF margin")], rows),
        "",
        "Implied billings = revenue + the change in deferred revenue (TTM, so the change is over four "
        "quarters). SBC-adjusted FCF = operating cash flow − capex − stock-based compensation. "
        "RPO is reported as a proxy for backlog, not a defined revenue metric.",
    ]

    notes = [n for res in results.values() for n in res.notes]
    if notes:
        out += ["", "## Missing tags and tag notes"] + [f"- {n}" for n in notes]
    checks = [(t, c) for t, res in results.items() for c in res.checks]
    failed = [(t, c) for t, c in checks if not c[1]]
    out += ["", f"## Checks\n{len(checks) - len(failed)} passed, {len(failed)} failed."]
    out += [f"- FAIL {t}: {c[0]} — {c[2]}" for t, c in failed]
    return "\n".join(out)


def main() -> None:
    """Run the server over stdio (what `saas-metrics mcp` calls)."""
    mcp.run()


if __name__ == "__main__":
    main()
