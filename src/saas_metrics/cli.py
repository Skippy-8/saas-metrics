"""Command line for saas-metrics: the billing report, the SEC toolkit and the point-in-time model.

Billing export (the packaged library):
  saas-metrics report INPUT [--customer COL --date COL --amount COL] [--service-start COL --service-end COL]
                     [--segment COL --channel COL] [--grain month|quarter] [--fy-end-month N]
                     [--tiers 0,10000,100000] [--as-of DATE] [--allow-warnings] [--out DIR] [--excel FILE]
  saas-metrics sample FILE      write a synthetic billing export to try it on

Full operating model (the active company config; MongoDB, configs/MDB.yaml):
  saas-metrics fetch | build | qc | model (alias: workbook) | backtest | memo | sources
  saas-metrics backtest --pit [--horizon 4] [--evaluations 4]   # any BaseForecaster, strict masking
Peer benchmarking (any config with a `benchmark` section):
  saas-metrics benchmark --tickers MDB DDOG SNOW TTD [--as-of DATE] [--out comps.csv|.parquet|.xlsx] [--refresh]
Revenue and cash quality from XBRL (deferred revenue, RPO, SBC, implied billings, SBC-adjusted FCF):
  saas-metrics quality [--tickers MDB DDOG SNOW TTD] [--as-of DATE] [--out quality.xlsx] [--refresh]
GTM unit economics (defaults from the config's unit_econ section; flags override):
  saas-metrics unit-econ [--n-ae 20] [--attainment 0.8] [--rep-attrition 0.25] [--gross-margin 0.75 | --cogs-pct 0.25]
                    [--target-payback 24] [--out flows.csv]
Your own billing data (or the MongoDB cohort ledger):
  saas-metrics cohorts --input billing.csv [--grain month|quarter] [--tiers 0,10000,100000] [--out DIR]
  saas-metrics cohorts --ledger MDB

A CRM or billing-system export (normalise, then report in one step):
  saas-metrics ledger stripe_invoices.csv --adapter stripe --analyze
  saas-metrics ledger chargebee.csv --adapter chargebee --fx-file rates.csv --duckdb billing.duckdb
  saas-metrics ledger opportunities.csv --adapter salesforce --out ledger.parquet --engine tape.parquet

Model Context Protocol (any MCP client: Claude Desktop, an agent host, or `fastmcp` itself):
  saas-metrics mcp              expose ledger, report, benchmark and quality as MCP tools over stdio

The same command line is installed as `saasfpa` for backwards compatibility.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _db(read_only: bool = True):
    import duckdb

    from saas_metrics import config
    if read_only and not config.DB_PATH.exists():
        raise SystemExit(f"no {config.TICKER} store at {config.DB_PATH}: run `saas-metrics fetch` and "
                         "`saas-metrics build` first (set SAAS_METRICS_HOME to keep data elsewhere)")
    return duckdb.connect(str(config.DB_PATH), read_only=read_only)


def cmd_fetch(a) -> None:
    from saas_metrics import fetch
    print(json.dumps(fetch.run()))


def cmd_build(a) -> None:
    from saas_metrics import build
    print(json.dumps(build.run(), indent=1, default=str))


def cmd_qc(a) -> None:
    from saas_metrics import qc
    checks = qc.run(_db())
    for c in checks:
        print(f"{'PASS' if c.passed else 'FAIL'}  {c.name}: {c.detail}")
    raise SystemExit(0 if all(c.passed for c in checks) else 1)


def cmd_model(a) -> None:
    from saas_metrics import workbook
    print(workbook.build(_db()))


def cmd_backtest(a) -> None:
    from saas_metrics import backtest
    con = _db()
    if getattr(a, "pit", False):
        result = backtest.PointInTimeBacktester(con=con, horizon=a.horizon,
                                                max_evaluations=a.evaluations).run()
        print(backtest.format_result(result))
        return
    long, _ = backtest.bridge(con)
    four_q = long.groupby(["metric", "component"], sort=False)["value"].sum() / 1e6
    print("Budget-vs-actual bridge, 4 quarters, $M (sequential substitution in the order listed):")
    print(four_q.round(1).to_string())
    sc = backtest.scorecard(con)
    err = ["model_base", "model_upside", "model_downside", "seasonal_naive", "trend_naive", "damped_trend", "guidance"]
    print("\nRevenue forecast error vs actual:")
    print(sc[err].div(sc.actual_revenue, axis=0).sub(1).round(4).set_index(sc.fiscal_label).to_string())
    p = sc.attrs["damped"]
    print(f"\nDamped trend fitted to T0: alpha {p['alpha']:.2f}, beta {p['beta']:.2f}, phi {p['phi']:.2f}")


def cmd_memo(a) -> None:
    from saas_metrics import config, memo
    if config.TICKER != "MDB":
        raise SystemExit("the memo is the MongoDB write-up; for other companies use `workbook` and `backtest`")
    print(*memo.write(_db()), sep="\n")


def cmd_sources(a) -> None:
    from saas_metrics import sources
    print(sources.write(_db()))


def cmd_mcp(a) -> None:
    try:
        from saas_metrics.mcp_server import main
    except ModuleNotFoundError as e:
        if (e.name or "").split(".")[0] == "fastmcp":
            raise SystemExit('the MCP server needs the optional extra: pip install "saas-metrics[mcp]"') from e
        raise
    main()


def cmd_app(a) -> None:
    """Launch the Streamlit workbench that ships in the package."""
    import importlib.util
    import subprocess
    from pathlib import Path

    if importlib.util.find_spec("streamlit") is None:
        raise SystemExit('the workbench needs the optional extra: pip install "saas-metrics[web]"')
    app = Path(__file__).resolve().parent / "app.py"
    raise SystemExit(subprocess.call([sys.executable, "-m", "streamlit", "run", str(app), *a.streamlit_args]))


def cmd_benchmark(a) -> None:
    from saas_metrics import benchmark
    table, results = benchmark.comps(a.tickers, as_of=a.as_of, refresh=a.refresh)
    print(f"Peer comps: trailing four fiscal quarters from SEC XBRL{' as known on ' + a.as_of if a.as_of else ''}")
    print(benchmark.format_table(table))
    failed = False
    print("\nChecks:")
    for res in results.values():
        for name, ok, detail in res.checks:
            failed |= not ok
            print(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}")
    notes = [(t, n) for t, r in results.items() for n in r.notes]
    if notes:
        print("\nNotes:")
        for t, n in notes:
            print(f"  {t}: {n}")
    print("\nNon-GAAP operating margin here is an approximation (GAAP + SBC + amortization); "
          "company definitions exclude more.")
    if a.out:
        print(f"\nwrote {benchmark.write(table, results, Path(a.out))}")
    raise SystemExit(1 if failed else 0)


def cmd_quality(a) -> None:
    from saas_metrics import company, quality
    tickers = a.tickers or [t for t in company.available() if "benchmark" in company.load(t).raw]
    frame, results = quality.table(tickers, as_of=a.as_of, refresh=a.refresh)
    print(f"Revenue and cash quality: trailing four fiscal quarters from SEC XBRL"
          f"{' as known on ' + a.as_of if a.as_of else ''}")
    print(quality.format_table(frame))
    failed = False
    print("\nChecks:")
    for res in results.values():
        for name, ok, detail in res.checks:
            failed |= not ok
            print(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}")
    notes = [n for r in results.values() for n in r.notes]
    if notes:
        print("\nNotes:")
        for n in notes:
            print(f"  {n}")
    print("\nSBC-adjusted FCF = operating cash flow - capex - stock-based compensation: free cash flow if "
          "employees were paid in cash. Implied billings = revenue + the change in deferred revenue.")
    if a.out:
        print(f"\nwrote {quality.write(frame, results, Path(a.out))}")
    raise SystemExit(1 if failed else 0)


UE_FLAGS = {  # flag dest -> GTM input key
    "n_ae": "roi_n_ae", "ae_cost": "roi_ae_cost", "support_pct": "roi_support_pct", "quota": "roi_quota",
    "attainment": "roi_attain", "ramp_months": "roi_ramp", "commission": "roi_commission",
    "rep_attrition": "roi_rep_attrition", "attach_rate": "roi_attach", "arr_per_attach": "roi_attach_arr",
    "adoption_months": "roi_adopt", "upfront_cost": "roi_upfront", "run_cost_monthly": "roi_run_cost",
    "nrr": "roi_nrr", "gross_margin": "roi_gm", "target_customers": "roi_target_customers",
    "discount_rate": "roi_discount", "months": "roi_months",
}


def cmd_unit_econ(a) -> None:
    import pandas as pd

    from saas_metrics import config, roi
    over = {key: getattr(a, dest) for dest, key in UE_FLAGS.items() if getattr(a, dest) is not None}
    if a.cogs_pct is not None:
        if a.gross_margin is not None:
            raise SystemExit("give --gross-margin or --cogs-pct, not both")
        over["roi_gm"] = 1 - a.cogs_pct
    con = _db() if config.DB_PATH.exists() else None
    try:
        inp, labels, src = roi.resolve_inputs(con, over)
    except ValueError as e:
        raise SystemExit(str(e)) from e
    print(f"GTM capital allocation, {config.ACTIVE.short_name}: case A hires {inp['roi_n_ae']:g} Enterprise AEs, "
          f"case B is a product-attach initiative; {int(inp['roi_months'])} months, monthly cash flows.")
    print("\nInputs:")
    for k, v in inp.items():
        print(f"  {labels[k]:<62} {f'{v:,.0f}' if abs(v) >= 1000 else f'{v:.4g}':>14}   [{src[k]}]")
    cases = roi.gtm_cases(inp)
    summ = roi.summary(cases, inp)
    money = lambda v: f"{'-' if v < 0 else ''}${abs(v) / 1e6:,.1f}M"  # noqa: E731
    out = pd.DataFrame({
        "cumulative net cash": summ.cumulative_36m.map(money), "trough": summ.trough.map(money),
        "payback month": summ.payback_month.map(lambda v: "beyond horizon" if pd.isna(v) else f"{v:.0f}"),
        "NPV": summ.npv.map(money), "IRR (annual)": summ.irr_annual.map(lambda v: "n/a" if pd.isna(v) else f"{v:.0%}"),
        "ARR at horizon": summ.arr_month_36.map(money)})
    print("\nSummary (payback month = 1 + the months with cumulative net cash below zero):")
    print(out.to_string())
    print(f"\nCase A ramped sales efficiency (new ARR per $ of AE cost): {roi.ae_efficiency(inp):.2f}")
    if con is not None:
        from saas_metrics import drivers, model
        hist = drivers.history(con)
        cal = con.execute("SELECT * FROM cohort_calibration WHERE vintage='as_of_cutoff'").df().set_index("period_end")
        hc = drivers.headcount_history(con, hist).set_index("period_end")["headcount"]
        drv = drivers.derive(con)
        rows = model.run(hist, cal, hc, drv, "Base")
        plan = {d.key: d.base for d in drv}
        magic = roi.scorecard(rows[rows.kind == "Actual"], cal.new_logos, plan["churn_q"]).at[config.T0_QUARTER_END,
                                                                                              "magic_number"]
        print(f"{config.ACTIVE.short_name} magic number at T0 (trailing 4Q): {magic:.2f}; case A matches it at "
              f"{roi.attainment_at_efficiency(inp, magic):.0%} quota attainment")
    try:
        print(f"Quota attainment for zero NPV over the horizon: {roi.breakeven_attainment(inp):.0%}")
    except ValueError:
        print("Quota attainment for zero NPV: outside 0-200%")
    if a.target_payback:
        need = roi.attainment_for_payback(inp, a.target_payback)
        print(f"Quota attainment to pay back by month {a.target_payback}: "
              + (f"{need:.0%}" if need is not None else "not reachable below 300%"))
    if a.out:
        flows = pd.concat({k: v for k, v in cases.items()}, names=["case"]).reset_index()
        flows.to_csv(a.out, index=False)
        print(f"\nwrote {a.out}")


def cmd_cohorts(a) -> None:
    import pandas as pd

    from saas_metrics import cohort_engine as ce
    if bool(a.input) == bool(a.ledger):
        raise SystemExit("give exactly one of --input PATH or --ledger TICKER")
    if a.ledger:
        res = ce.ledger(a.ledger.upper())
    else:
        tiers = [float(x) for x in a.tiers.split(",")] if a.tiers else ce.DEFAULT_TIERS
        res = ce.analyze(a.input, a.grain, a.fy_end_month, tiers, a.allow_warnings)
    print("Pre-flight checks:")
    for c in res.checks:
        print(f"  {c.level.upper():<7} {c.name}: {c.detail}")
    if not res.tables:
        raise SystemExit("stopped: fix the errors above" + ("" if any(c.level == "error" for c in res.checks)
                                                             else ", or rerun with --allow-warnings"))
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        if "waterfall" in res.tables:
            w = res.tables["waterfall"]
            print("\nRevenue waterfall (Beginning + New + Reactivation + Expansion - Contraction - Churn = Ending):")
            print(w.drop(columns=["pidx"]).tail(6).to_string(index=False))
            print(f"  identity holds in every period: {bool((w.check_diff == 0).all())}")
        r = res.tables["retention"]
        print("\nRetention at age 4 and 8 (by vintage):")
        print(r[r.age.isin([4, 8])].pivot(index="vintage", columns="age", values=["logo_retention", "ndr"])
              .round(3).head(8).to_string())
    for n in res.notes:
        print(f"\nNote: {n}")
    if a.out:
        for p in ce.write(res, Path(a.out)):
            print(f"wrote {p}")


# ---- billing report (the standalone library's original command line) ------------------------------
FLAGS = {"customer": "customer_id", "date": "period", "amount": "revenue", "service_start": "service_start",
         "service_end": "service_end", "segment": "segment", "channel": "channel"}


def cmd_report(a) -> None:

    from saas_metrics import analyze
    from saas_metrics.engine import DEFAULT_TIERS
    from saas_metrics.inputs import InputError

    mapping = {target: getattr(a, flag) for flag, target in FLAGS.items() if getattr(a, flag)}
    tiers = [float(x) for x in a.tiers.split(",")] if a.tiers else DEFAULT_TIERS
    try:
        r = analyze(a.input, mapping=mapping, grain=a.grain, fy_end_month=a.fy_end_month, tiers=tiers,
                    allow_warnings=a.allow_warnings, as_of=a.as_of)
    except InputError as e:
        raise SystemExit(f"input error: {e}") from e
    _emit_report(r, a.grain)
    if a.out:
        for p in r.to_csv(a.out):
            print(f"wrote {p}")
    if a.excel:
        print(f"wrote {r.to_excel(a.excel)}")


def _emit_report(r, grain: str) -> None:
    """The shared report body: pre-flight checks, the waterfall, then the headline metrics."""
    import pandas as pd

    print("Pre-flight checks:")
    for c in r.checks:
        print(f"  {c.level.upper():<7} {c.name}: {c.detail}")
    if not r.ok:
        raise SystemExit("stopped: fix the errors above" + ("" if any(c.level == "error" for c in r.checks)
                                                            else ", or rerun with --allow-warnings"))
    w = r.waterfall
    print(f"\nRevenue waterfall, last 6 {grain}s (identity holds in every period: {bool((w.check_diff == 0).all())}):")
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(w[["period_end", "beginning", "new_logo", "reactivation", "expansion", "contraction", "churn",
                 "ending"]].tail(6).to_string(index=False))
        m = r.metrics.tail(6).copy()
        for c in ("logo_churn_rate", "gross_revenue_churn_rate", "nrr_12m", "grr_12m", "logo_retention_12m"):
            m[c] = m[c].map(lambda v: "" if v != v else f"{v:.1%}")
        m["quick_ratio"] = m["quick_ratio"].map(lambda v: "" if v != v else f"{v:.2f}")
        print("\nHeadline metrics:")
        print(m[["period_end", "arr", "active_customers", "quick_ratio", "logo_churn_rate", "nrr_12m",
                 "grr_12m"]].to_string(index=False))
    for n in r.notes:
        print(f"\nNote: {n}")


def _tri(value: str) -> bool | None:
    """`auto` -> None (detect), `on` -> True, `off` -> False."""
    return {"auto": None, "on": True, "off": False}[value]


def cmd_ledger(a) -> None:
    """Normalise a Stripe / Chargebee / Salesforce export into the canonical ledger, then use it."""
    from saas_metrics import analyze
    from saas_metrics.adapters import ADAPTERS
    from saas_metrics.contracts import ContractError
    from saas_metrics.fx import load_rates

    mapping: dict[str, str] = {}
    for item in a.mapping or []:
        if "=" not in item:
            raise SystemExit(f"--map expects CANONICAL=SOURCE_COLUMN, got {item!r}")
        key, value = item.split("=", 1)
        mapping[key.strip()] = value.strip()
    kwargs: dict = {}
    if a.adapter == "stripe":
        kwargs |= {"minor_units": _tri(a.minor_units), "half_open_end": _tri(a.half_open_end)}
    elif a.adapter in ("chargebee", "salesforce"):
        kwargs["default_term_months"] = a.default_term_months
    fx = load_rates(a.fx_file) if a.fx_file else None
    ledger = ADAPTERS[a.adapter](a.input, mapping=mapping or None, fx=fx, target_currency=a.currency,
                                 grace_days=a.grace_days, grace_mode=a.grace_mode,
                                 on_missing_fx=a.on_missing_fx, **kwargs)
    print(f"{ledger.adapter} export -> canonical ledger")
    for n in ledger.notes:
        print(f"  column {n}")
    try:
        ledger.raise_if_blocking(allow_warnings=a.allow_warnings)
    except ContractError as e:
        for d in ledger.blocking:
            print(f"  {d}")
        raise SystemExit(f"ledger rejected: {e}") from e
    for d in ledger.diagnostics:
        print(f"  {d}")
    for n in ledger.all_notes():
        print(f"  note: {n}")
    print(f"  {ledger.summary()}")
    if a.out:
        print(f"wrote {ledger.to_parquet(a.out)}")
    if a.duckdb:
        info = ledger.to_duckdb(a.duckdb)
        print(f"wrote {info['table']} ({info['rows']} rows, MRR {info['mrr_total']}) to {a.duckdb}")
    if a.engine:
        tape, _ = ledger.to_engine_input(grain=a.grain, as_of=a.as_of)
        tape.to_parquet(a.engine, index=False)
        print(f"wrote {len(tape)} engine line(s) to {a.engine}")
    if a.analyze:
        tape, notes = ledger.to_engine_input(grain=a.grain, as_of=a.as_of)
        for n in notes:
            print(f"  note: {n}")
        r = analyze(tape, grain=a.grain, fy_end_month=a.fy_end_month, allow_warnings=True)
        _emit_report(r, a.grain)


def cmd_sample(a) -> None:
    from saas_metrics import sample
    p = sample.write(a.file)
    print(f"wrote {p} (synthetic). Try:\n  saas-metrics report {p} --customer \"Customer\" --date \"Invoice Date\" "
          "--amount \"Amount\" --service-start \"Service Start\" --service-end \"Service End\" --segment \"Plan Tier\" "
          "--channel \"Channel\" --excel metrics.xlsx")


def parser(prog: str = "saas-metrics") -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog, description="Corporate finance, FP&A and revenue BI toolkit")
    p.add_argument("--config", default=os.environ.get("SAAS_METRICS_CONFIG") or os.environ.get("SAASFPA_CONFIG"),
                   help="full-model company: a ticker in configs/ or a path to a YAML (default MDB; env SAAS_METRICS_CONFIG)")
    sub = p.add_subparsers(dest="command", required=True)
    for name, fn, help_ in (
        ("fetch", cmd_fetch, "download the company's filings from SEC (MongoDB: into data/raw)"),
        ("build", cmd_build, "build the DuckDB store from the downloaded filings"),
        ("qc", cmd_qc, "pre-flight checks on the built store"),
        ("model", cmd_model, "write the Excel operating model"),
        ("workbook", cmd_model, "alias of model"),
        ("backtest", cmd_backtest, "print the budget-vs-actual bridge and the forecast scorecard"),
        ("memo", cmd_memo, "render the MongoDB QBR memo from the model"),
        ("sources", cmd_sources, "write the sources page (MongoDB: docs/sources.md)"),
        ("mcp", cmd_mcp, "run the Model Context Protocol server over stdio (needs the mcp extra)"),
    ):
        sub.add_parser(name, help=help_).set_defaults(fn=fn)
    ap = sub.add_parser("app", help='open the Streamlit workbench (needs the web extra: pip install "saas-metrics[web]")')
    ap.set_defaults(fn=cmd_app, streamlit_args=[])     # any further options go to `streamlit run` (e.g. --server.port 8502)
    bt = sub.choices["backtest"]
    bt.add_argument("--pit", action="store_true",
                    help="run the extensible point-in-time forecaster backtest (default: the frozen bridge/scorecard)")
    bt.add_argument("--horizon", type=int, default=4, help="forecast horizon in quarters with --pit (default 4)")
    bt.add_argument("--evaluations", type=int, default=4,
                    help="number of historical origins with --pit (default 4)")
    b = sub.add_parser("benchmark", help="public peer comp table from SEC XBRL")
    b.add_argument("--tickers", nargs="+", required=True, help="tickers with a configs/<TICKER>.yaml")
    b.add_argument("--as-of", help="use only filings on or before this date (YYYY-MM-DD)")
    b.add_argument("--out", help="write the table to .csv, .parquet or .xlsx")
    b.add_argument("--refresh", action="store_true", help="re-download companyfacts")
    b.set_defaults(fn=cmd_benchmark)
    ql = sub.add_parser("quality", help="deferred revenue, RPO, SBC, implied billings and SBC-adjusted FCF")
    ql.add_argument("--tickers", nargs="+",
                    help="tickers with a configs/<TICKER>.yaml (default: every config with a benchmark section)")
    ql.add_argument("--as-of", help="use only filings on or before this date (YYYY-MM-DD)")
    ql.add_argument("--out", help="write the table to .csv, .parquet or .xlsx (quarterly series alongside)")
    ql.add_argument("--refresh", action="store_true", help="re-download companyfacts")
    ql.set_defaults(fn=cmd_quality)
    u = sub.add_parser("unit-econ", help="GTM capital-allocation case: AE hiring vs product attach")
    for flag, dest, kind, help_ in (
        ("--n-ae", "n_ae", float, "AEs hired (case A)"), ("--ae-cost", "ae_cost", float, "fully loaded cost per AE, annual"),
        ("--support-pct", "support_pct", float, "support overhead, share of AE cost"),
        ("--quota", "quota", float, "annual new-ARR quota per ramped AE"), ("--attainment", "attainment", float, "quota attainment"),
        ("--ramp-months", "ramp_months", float, "months to full productivity"),
        ("--commission", "commission", float, "commission, share of booked ARR"),
        ("--rep-attrition", "rep_attrition", float, "annual AE attrition; leavers are backfilled and ramp again"),
        ("--attach-rate", "attach_rate", float, "case B attach rate at maturity"),
        ("--arr-per-attach", "arr_per_attach", float, "case B ARR per attaching customer"),
        ("--adoption-months", "adoption_months", float, "case B months to full adoption"),
        ("--upfront-cost", "upfront_cost", float, "case B build cost, month 1"),
        ("--run-cost-monthly", "run_cost_monthly", float, "case B cost per month"),
        ("--nrr", "nrr", float, "net expansion of the new ARR, annual (1.20 = 120%%)"),
        ("--gross-margin", "gross_margin", float, "gross margin on the new ARR"),
        ("--cogs-pct", "cogs_pct", float, "cost of revenue as a share of revenue (sets gross margin = 1 - this)"),
        ("--target-customers", "target_customers", float, "case B target customer count"),
        ("--discount-rate", "discount_rate", float, "annual discount rate for NPV"),
        ("--months", "months", int, "horizon in months"),
        ("--target-payback", "target_payback", int, "report the attainment needed to pay back by this month"),
    ):
        u.add_argument(flag, dest=dest, type=kind, help=help_)
    u.add_argument("--out", help="write the monthly cash flows of both cases to CSV")
    u.set_defaults(fn=cmd_unit_econ)
    c = sub.add_parser("cohorts", help="waterfall, retention, concentration and tier migration")
    c.add_argument("--input", help="CSV or Parquet billing tape (customer_id, period, revenue[, segment, channel])")
    c.add_argument("--ledger", help="use a full-model company's calibrated cohort ledger, e.g. MDB")
    c.add_argument("--grain", choices=["month", "quarter"], default="month")
    c.add_argument("--fy-end-month", type=int, default=12, help="fiscal year end month, for quarterly grain")
    c.add_argument("--tiers", help="tier lower bounds in annualised revenue, e.g. 0,10000,100000,1000000")
    c.add_argument("--allow-warnings", action="store_true", help="run despite warnings (e.g. missing periods)")
    c.add_argument("--out", help="directory for CSVs and cohorts.xlsx")
    c.set_defaults(fn=cmd_cohorts)
    # billing exports from a CRM/billing system: normalise first, then report
    g = sub.add_parser("ledger", help="normalise a Stripe / Chargebee / Salesforce export into the canonical ledger")
    g.add_argument("input", help="the export (.csv, .csv.gz, .parquet or .xlsx)")
    g.add_argument("--adapter", choices=["stripe", "chargebee", "salesforce", "canonical"], default="stripe")
    g.add_argument("--map", dest="mapping", action="append", metavar="CANONICAL=SOURCE",
                   help="override a column (repeatable), e.g. --map customer_id='Account Name'")
    g.add_argument("--currency", default="USD", help="the single currency to normalise to (default USD)")
    g.add_argument("--fx-file", help="rate table with `currency,rate` columns (1 unit of currency in --currency)")
    g.add_argument("--on-missing-fx", choices=["error", "keep", "drop"], default="error",
                   help="what to do with a currency that has no rate (default: error)")
    g.add_argument("--grace-days", type=int, default=30, help="close a renewal gap this long (0 disables it)")
    g.add_argument("--grace-mode", choices=["blank_period", "always"], default="blank_period",
                   help="bridge only when a whole period would be empty (default), or every gap in the window")
    g.add_argument("--minor-units", choices=["auto", "on", "off"], default="auto",
                   help="stripe only: whether amounts are in cents (default: detect from the column name)")
    g.add_argument("--half-open-end", choices=["auto", "on", "off"], default="auto",
                   help="stripe only: whether period_end is exclusive (default: yes for period_end)")
    g.add_argument("--default-term-months", type=int, default=12,
                   help="chargebee / salesforce: term to assume when the export gives no end date")
    g.add_argument("--grain", choices=["month", "quarter"], default="month")
    g.add_argument("--fy-end-month", type=int, default=12)
    g.add_argument("--as-of", help="recognise revenue earned on or before this date (default: all of it)")
    g.add_argument("--allow-warnings", action="store_true", help="keep rows that raised a warning")
    g.add_argument("--out", help="write the canonical ledger to .parquet or .csv")
    g.add_argument("--duckdb", help="write the ledger to this DuckDB file (money as DECIMAL(18,2), read back)")
    g.add_argument("--engine", help="write the recognised customer_id/period/revenue tape to .parquet")
    g.add_argument("--analyze", action="store_true", help="run the report on the recognised tape")
    g.set_defaults(fn=cmd_ledger)
    # billing report: the packaged library's own command line
    r = sub.add_parser("report", help="waterfall, retention and headline metrics from a billing export")
    r.add_argument("input", help="CSV, CSV.GZ, Parquet or XLSX billing export")
    r.add_argument("--customer", help="column holding the customer id (default customer_id)")
    r.add_argument("--date", help="column holding the invoice or period date (default period)")
    r.add_argument("--amount", help="column holding the amount (default revenue)")
    r.add_argument("--service-start", dest="service_start", help="column: first day of service (enables spreading)")
    r.add_argument("--service-end", dest="service_end", help="column: last day of service")
    r.add_argument("--segment", help="column for segment / plan")
    r.add_argument("--channel", help="column for sales channel")
    r.add_argument("--grain", choices=["month", "quarter"], default="month")
    r.add_argument("--fy-end-month", type=int, default=12, help="fiscal year-end month, for quarterly grain")
    r.add_argument("--tiers", help="tier lower bounds in annualised revenue, e.g. 0,10000,100000,1000000")
    r.add_argument("--as-of", dest="as_of", help="recognise spread revenue only through this date (YYYY-MM-DD)")
    r.add_argument("--allow-warnings", action="store_true", help="run despite warnings (e.g. a missing period)")
    r.add_argument("--out", help="folder for CSV tables")
    r.add_argument("--excel", help="write an auditable workbook (live formulas, Checks tab)")
    r.set_defaults(fn=cmd_report)
    s = sub.add_parser("sample", help="write a synthetic billing export")
    s.add_argument("file")
    s.set_defaults(fn=cmd_sample)
    return p


def main(argv: list[str] | None = None) -> None:
    prog = Path(sys.argv[0]).name or "saas-metrics"
    a, extra = parser(prog).parse_known_args(argv)
    if extra and getattr(a, "fn", None) is not cmd_app:      # only `app` passes options through to streamlit
        parser(prog).parse_args(argv)
    if extra:
        a.streamlit_args = extra
    if a.config and getattr(a, "command", None) not in ("report", "sample"):
        from saas_metrics import company, config
        try:
            config.activate(a.config)
        except company.ConfigError as e:
            raise SystemExit(str(e)) from e
    a.fn(a)


if __name__ == "__main__":
    main()
