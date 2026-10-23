"""saas-metrics: SaaS revenue metrics from a raw billing export, with written, tested definitions.

    import saas_metrics as sm
    report = sm.analyze("invoices.csv", mapping={"customer_id": "Customer", "period": "Date", "revenue": "Amount"})
    report.metrics; report.waterfall; report.to_excel("metrics.xlsx")

Definitions: docs/definitions.md.

The same package also carries the SEC and point-in-time toolkit (`benchmark`, `backtest`, `model`,
`workbook`, `roi`, `cohort_engine`). Those modules pull their company-specific values from
`configs/<TICKER>.yaml`; importing them has no side effects beyond the default active company (MDB).
"""
from saas_metrics.engine import Check, Report, analyze
from saas_metrics.inputs import InputError

__version__ = "0.2.1"
__all__ = ["analyze", "Report", "Check", "InputError", "__version__"]
