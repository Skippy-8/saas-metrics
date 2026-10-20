# saas-metrics

SaaS revenue metrics from a raw billing export, with **written, tested definitions**:
- a revenue waterfall, exact to the cent;
- net and gross revenue retention, logo churn, quick ratio and ARR;
- cohort triangles, concentration and tier migration;
- an Excel workbook in which every metric is a live formula you can audit.

Every finance team computes these metrics, and few define them the same way. saas-metrics is a small,
open reference implementation:
- it reads the export you already have (Stripe, Chargebee, an ERP, a spreadsheet) and maps its columns;
- it spreads annual and multi-month invoices over their service periods;
- it shows its work: pre-flight checks before any number, [definitions](docs/definitions.md) a test
  enforces, and a workbook whose Checks tab proves it matches the engine.

```bash
pip install "saas-metrics @ git+https://github.com/Skippy-8/saas-metrics"   # PyPI release: pip install saas-metrics

saas-metrics sample billing.csv          # a synthetic export to try it on
saas-metrics report billing.csv --customer "Customer" --date "Invoice Date" --amount "Amount" \
    --service-start "Service Start" --service-end "Service End" --segment "Plan Tier" --channel "Channel" \
    --excel metrics.xlsx --out tables/
```

```python
import saas_metrics as sm

report = sm.analyze("invoices.csv", mapping={"customer_id": "Customer", "period": "Date", "revenue": "Amount"})
report.checks        # pre-flight checks (errors and warnings block; info explains what was done)
report.metrics       # per period: revenue, ARR, ARPA, quick ratio, churn rates, NRR / GRR / logo retention (12m)
report.waterfall     # New, Reactivation, Expansion, Contraction, Churn; identity exact to the cent
report.to_excel("metrics.xlsx")
```

## Why spreading matters

An annual plan invoiced in January looks like a customer who arrives in January and churns in February.
On the bundled sample export, counting invoices in the month billed produces **214 churn events**.
Spreading each invoice by day over its service period gives **106**.

Pass `--service-start` and `--service-end`, and each line is recognised evenly by day, in whole cents, so
it still sums to what was billed. Service after the export's last month is deferred and reported, not
counted.

## What it checks before computing anything

- **Errors** (always block): missing required columns, empty values, unparseable dates or amounts.
- **Warnings** (block unless `--allow-warnings`):
  - a period with no billing at all, which would read as everyone churning;
  - a customer whose net revenue for a period is negative;
  - dates in the future.
- **Info:** split invoices and credits that were netted, how many lines were spread, and coverage.

## Definitions

[docs/definitions.md](docs/definitions.md) has every rule. In short:
- **Waterfall:** New is first-ever revenue. Reactivation is revenue after a gap. Expansion, Contraction
  and Churn are measured against the previous period. The identity holds exactly.
- **NRR (12m):** revenue now ÷ revenue a year ago, over customers who had revenue a year ago.
- **GRR (12m):** the same, with each customer's revenue capped at its year-ago level.
- **Logo retention (12m):** the share of those customers still billing.
- **Quick ratio:** (New + Reactivation + Expansion) ÷ (Contraction + Churn).
- **ARR:** revenue × 12 at monthly grain, × 4 at quarterly.

The page ends with a worked example small enough to check by hand. It is also a test.

## The workbook

| Sheet | What it holds |
|---|---|
| Waterfall | Beginning links to the prior Ending; Ending is a formula, compared with the data |
| Customers | Revenue by customer and period |
| Metrics | Every metric as a formula. NRR and GRR use SUMPRODUCT over Customers. |
| Engine | The Python results |
| Checks | Reads PASS when every formula matches the engine |

Blue cells are data; black cells are formulas. No number is typed into a formula except 0 and 1.

## How it is built

```
saas_metrics/inputs.py    read (CSV, CSV.GZ, Parquet, XLSX, DataFrame), map columns, spread service periods
saas_metrics/engine.py    pre-flight checks; DuckDB SQL engine (sql/*.sql); headline metrics; Report
saas_metrics/excel.py     the auditable workbook
saas_metrics/sample.py    a seeded synthetic billing export
tests/                    an independent plain-Python waterfall, the hand-computed definitions, spreading,
                          QC, the workbook recalculated with `formulas`
```

The engine began as the billing-data module of
[saas-fpa-backtest](https://github.com/Skippy-8/saas-fpa-backtest), a point-in-time FP&A model of
MongoDB. Its waterfall matches the one there to the cent on the same input.

## Licence

MIT. The sample data is synthetic.
