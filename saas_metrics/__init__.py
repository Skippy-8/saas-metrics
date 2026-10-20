"""saas-metrics: SaaS revenue metrics from a raw billing export, with written, tested definitions.

    import saas_metrics as sm
    report = sm.analyze("invoices.csv", mapping={"customer_id": "Customer", "period": "Date", "revenue": "Amount"})
    report.metrics; report.waterfall; report.to_excel("metrics.xlsx")

Definitions: docs/definitions.md.
"""
from saas_metrics.engine import Check, Report, analyze
from saas_metrics.inputs import InputError

__version__ = "0.1.0"
__all__ = ["analyze", "Report", "Check", "InputError", "__version__"]
