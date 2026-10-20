"""Command line.

  saas-metrics report INPUT [--customer COL --date COL --amount COL] [--service-start COL --service-end COL]
                     [--segment COL --channel COL] [--grain month|quarter] [--fy-end-month N]
                     [--tiers 0,10000,100000] [--as-of DATE] [--allow-warnings] [--out DIR] [--excel FILE]
  saas-metrics sample FILE      write a synthetic billing export to try it on

Definitions: https://github.com/Skippy-8/saas-metrics/blob/main/docs/definitions.md
"""
from __future__ import annotations

import argparse

import pandas as pd

from saas_metrics import analyze, sample
from saas_metrics.engine import DEFAULT_TIERS
from saas_metrics.inputs import InputError

FLAGS = {"customer": "customer_id", "date": "period", "amount": "revenue", "service_start": "service_start",
         "service_end": "service_end", "segment": "segment", "channel": "channel"}


def cmd_report(a) -> None:
    mapping = {target: getattr(a, flag) for flag, target in FLAGS.items() if getattr(a, flag)}
    tiers = [float(x) for x in a.tiers.split(",")] if a.tiers else DEFAULT_TIERS
    try:
        r = analyze(a.input, mapping=mapping, grain=a.grain, fy_end_month=a.fy_end_month, tiers=tiers,
                    allow_warnings=a.allow_warnings, as_of=a.as_of)
    except InputError as e:
        raise SystemExit(f"input error: {e}") from e
    print("Pre-flight checks:")
    for c in r.checks:
        print(f"  {c.level.upper():<7} {c.name}: {c.detail}")
    if not r.ok:
        raise SystemExit("stopped: fix the errors above" + ("" if any(c.level == "error" for c in r.checks)
                                                             else ", or rerun with --allow-warnings"))
    w = r.waterfall
    print(f"\nRevenue waterfall, last 6 {a.grain}s (identity holds in every period: {bool((w.check_diff == 0).all())}):")
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(w[["period_end", "beginning", "new_logo", "reactivation", "expansion", "contraction", "churn", "ending"]]
              .tail(6).to_string(index=False))
        m = r.metrics.tail(6).copy()
        for c in ("logo_churn_rate", "gross_revenue_churn_rate", "nrr_12m", "grr_12m", "logo_retention_12m"):
            m[c] = m[c].map(lambda v: "" if v != v else f"{v:.1%}")
        m["quick_ratio"] = m["quick_ratio"].map(lambda v: "" if v != v else f"{v:.2f}")
        print("\nHeadline metrics:")
        print(m[["period_end", "arr", "active_customers", "quick_ratio", "logo_churn_rate", "nrr_12m", "grr_12m"]]
              .to_string(index=False))
    for n in r.notes:
        print(f"\nNote: {n}")
    if a.out:
        for p in r.to_csv(a.out):
            print(f"wrote {p}")
    if a.excel:
        print(f"wrote {r.to_excel(a.excel)}")


def cmd_sample(a) -> None:
    p = sample.write(a.file)
    print(f"wrote {p} (synthetic). Try:\n  saas-metrics report {p} --customer \"Customer\" --date \"Invoice Date\" "
          "--amount \"Amount\" --service-start \"Service Start\" --service-end \"Service End\" --segment \"Plan Tier\" "
          "--channel \"Channel\" --excel metrics.xlsx")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="saas-metrics", description="SaaS revenue metrics from a billing export")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("report", help="waterfall, retention and headline metrics")
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
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
