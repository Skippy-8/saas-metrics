# Changelog

## 0.2.1 (2026-10-22)

- **Workbench fix:** the billing tab's "Total MRR" tile summed MRR over every row of the export's history (total
  billings). It now shows **current MRR** (ending ARR / 12), from the new `Report.headline()`.

## 0.2.0 (2026-10-22)

The billing library becomes a toolkit. Everything in 0.1.0 works as before (`analyze`, `report`, `sample`).

- **SEC XBRL peer benchmark and revenue quality** (`benchmark`, `quality`): deferred revenue, RPO, implied
  billings, SBC and SBC-adjusted FCF from companyfacts, reconciled to each filer's 10-K.
- **Point-in-time FP&A backtest** (`fetch`, `build`, `backtest --pit`): the forecast sees only filings available at
  each origin; MAPE / bias scorecard and variance bridges. The full operating model, memo and sources page come
  with it (formerly the saas-fpa-backtest project; `import saasfpa` still works).
- **Billing adapters** for Stripe, Chargebee and Salesforce exports (`ledger`), and a cohort engine.
- **Streamlit workbench** (`pip install "saas-metrics[web]"`, then `saas-metrics app`) and an **MCP server**
  (`saas-metrics[mcp]`, `saas-metrics mcp`).
- **Installs cleanly:** company configs and the memo template ship in the wheel; downloads and generated files go to
  the user data folder or `$SAAS_METRICS_HOME`, never into site-packages.
- **Requires Python 3.12+** (0.1.0 supported 3.11).

## 0.1.0 (2026-10-20)

First release.

- **Inputs:** read CSV, CSV.GZ, Parquet, XLSX or a DataFrame, with column mapping.
- **Spreading:** service periods are spread by day, exact to the cent; anything after the as-of date is deferred.
- **Pre-flight checks.**
- **Engine:** a DuckDB SQL waterfall, identity exact to the cent, plus segment and channel waterfalls, a retention
  triangle, concentration and tier migration.
- **Headline metrics:** ARR, ARPA, quick ratio, logo and gross revenue churn, and NRR, GRR and logo retention
  over 12 months.
- **An auditable Excel workbook:** live formulas and a Checks tab.
- **A command line** and a synthetic sample export.
