"""Billing adapters: raw exports from Stripe, Chargebee and Salesforce -> the canonical ledger.

    from saas_metrics.adapters import from_stripe_export
    ledger = from_stripe_export("stripe_invoices.csv")
    ledger.ok, ledger.summary()
    tape, notes = ledger.to_engine_input()          # customer_id / period / revenue
    import saas_metrics as sm
    report = sm.analyze(tape, allow_warnings=True)  # the waterfall, retention and headline metrics
    ledger.to_duckdb("billing.duckdb")              # money as DECIMAL(18,2), read back to verify

The three pieces an adapter composes, each usable on its own:

    proration   monthly-bucket standardisation: the recurring rate, the partial first bucket, the flag
    grace       close a late-renewal gap so it is not read as churn + new-logo reactivation
    ramps       separate contracted ramp steps from organic upsell
    stitching   recover a dropped contract id from the account id and the dates (Salesforce)

`ADAPTERS` maps a CLI/MCP name to the factory, so `--adapter stripe` works without importing anything
by name.
"""
from __future__ import annotations

from saas_metrics.adapters import grace, proration, ramps, stitching
from saas_metrics.adapters.base import (
    LEDGER_COLUMNS,
    Ledger,
    build_ledger,
    ledger_from_frame,
    read_source,
    rename_columns,
)
from saas_metrics.adapters.chargebee import from_chargebee_export
from saas_metrics.adapters.salesforce import from_salesforce_opportunities
from saas_metrics.adapters.stripe import from_stripe_export

ADAPTERS = {
    "stripe": from_stripe_export,
    "chargebee": from_chargebee_export,
    "salesforce": from_salesforce_opportunities,
    "canonical": ledger_from_frame,
}

__all__ = [
    "ADAPTERS",
    "LEDGER_COLUMNS",
    "Ledger",
    "build_ledger",
    "from_chargebee_export",
    "from_salesforce_opportunities",
    "from_stripe_export",
    "grace",
    "ledger_from_frame",
    "proration",
    "ramps",
    "read_source",
    "rename_columns",
    "stitching",
]
