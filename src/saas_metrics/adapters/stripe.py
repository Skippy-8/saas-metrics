"""Stripe adapter: a Stripe invoice export -> the canonical ledger.

Stripe's invoices CSV is the most convenient starting point most SaaS companies have, and it is also
the one with the most traps:

* **Money is in minor units.** `subtotal`, `total`, `amount_paid` and `amount_due` are integer cents
  (or the currency's smallest unit). A $49 plan reads `4900`. The adapter scales by 100 unless the
  export you are reading already holds major units — pass `minor_units=False` for a pre-scaled export.
* **Dates are Unix seconds.** `created`, `period_start` and `period_end` are epoch integers. They are
  converted to real dates here, before any validation, so a term cannot be misread as ~1970.
* **One invoice covers a term.** An annual pre-pay has `period_start`/`period_end` twelve months
  apart. The invoice total is the recognised amount; the recurring amount is total / months, and the
  term is what makes a mid-cycle first month prorate correctly downstream.

The invoice's `subscription` (or its own `id`) becomes the `contract_id`, so renewal detection and
the grace-period bridge have something stable to group on.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd

from saas_metrics.adapters import grace, proration
from saas_metrics.adapters.base import Ledger, build_ledger, read_source, rename_columns
from saas_metrics.fx import FXConverter

ALIASES: dict[str, tuple[str, ...]] = {
    "customer_id": ("customer_id", "customer", "Customer ID", "customer id", "Customer", "cust_id", "email"),
    "contract_id": ("subscription_id", "subscription", "Subscription ID", "contract_id", "id", "Invoice ID",
                    "invoice_id"),
    "invoice_date": ("invoice_date", "created", "date", "Date", "created_at", "finalized_at", "Invoice Date"),
    "service_start": ("service_start", "period_start", "current_period_start", "Period Start", "Service Start"),
    "service_end": ("service_end", "period_end", "current_period_end", "Period End", "Service End"),
    "currency": ("currency", "Currency", "currency_code"),
    "product_tier": ("product_tier", "plan", "Plan", "price_nickname", "product", "description", "Plan Tier",
                     "nickname", "price_id"),
    "recognized_amount": ("recognized_amount", "amount_paid", "subtotal", "total", "amount_due", "amount",
                          "Amount", "Total"),
    "mrr_amount": ("mrr_amount", "mrr", "MRR", "recurring_amount", "recurring"),
    "source_kind": ("source_kind", "change_type", "billing_reason", "type"),
    "channel": ("channel", "source", "collection_method"),
    "source_row": ("source_row",),
}

# column names that Stripe reports in minor units (integer cents)
_MINOR_UNIT_COLUMNS = frozenset({"amount_paid", "subtotal", "total", "amount_due", "amount", "total_amount"})
# period boundaries Stripe reports half-open ([start, end)): the last covered day is end - 1
_HALF_OPEN_END_COLUMNS = frozenset({"period_end", "current_period_end"})


def _coerce_dates(s: pd.Series) -> pd.Series:
    """Epoch seconds or ISO text -> `datetime.date` (None when unparseable)."""
    num = pd.to_numeric(s, errors="coerce")
    if num.notna().any() and num.notna().sum() == s.notna().sum() and num.abs().max() >= 1e8:
        return pd.to_datetime(num, unit="s", errors="coerce").dt.date
    return pd.to_datetime(s, errors="coerce", format="mixed").dt.date


def _scaled(s: pd.Series, factor: int) -> list[Decimal | None]:
    num = pd.to_numeric(s.astype("string").str.replace(r"[,$\s]", "", regex=True), errors="coerce")
    return [None if pd.isna(v) else (Decimal(str(v)) / Decimal(factor)).quantize(Decimal("0.01")) for v in num]


def from_stripe_export(source: str | Path | pd.DataFrame, *, mapping: dict[str, str] | None = None,
                       minor_units: bool | None = None, half_open_end: bool | None = None,
                       fx: FXConverter | None = None, target_currency: str = "USD", grace_days: int = 30,
                       grace_mode: str = grace.MODE_BLANK_PERIOD, on_missing_fx: str = "error",
                       today: date | None = None, allow_warnings: bool = True) -> Ledger:
    """Normalise a Stripe invoice export into a `Ledger` (see the module docstring for the conventions).

    `mapping` overrides a guessed column; `minor_units` forces the cents scaling; `half_open_end`
    forces the `period_end` convention; `fx` converts to one currency before anything is compared.
    Diagnostics are on the returned ledger: nothing is dropped and no amount is invented.
    """
    raw = read_source(source)
    renamed, notes = rename_columns(raw, ALIASES, mapping)
    frame = renamed.copy()

    for col in ("invoice_date", "service_start", "service_end"):
        if col in frame.columns:
            frame[col] = _coerce_dates(frame[col])

    # Stripe's period_end is exclusive: the last day of service is end - 1 day. Leaving it inclusive
    # would stretch every monthly invoice to 31 days and every annual term to 366.
    source_end = _source_name(raw, mapping, ALIASES, "service_end")
    half_open = half_open_end if half_open_end is not None else (source_end in _HALF_OPEN_END_COLUMNS)
    if half_open and "service_end" in frame.columns:
        frame["service_end"] = [None if pd.isna(v) else v - timedelta(days=1) for v in frame["service_end"]]
        notes.append("service_end treated as exclusive (Stripe period_end): the last covered day is end - 1")

    # minor units: auto-detect from the source column, or obey the caller
    if "recognized_amount" in frame.columns:
        source_total = _source_name(raw, mapping, ALIASES, "recognized_amount")
        cents = minor_units if minor_units is not None else (source_total in _MINOR_UNIT_COLUMNS)
        frame["recognized_amount"] = _scaled(frame["recognized_amount"], 100 if cents else 1)
        if cents:
            notes.append("amounts scaled from minor units (cents) to major units /100")
    if "mrr_amount" in frame.columns:
        frame["mrr_amount"] = _scaled(frame["mrr_amount"], 1)

    _derive_mrr(frame, notes)

    ledger = build_ledger(frame, adapter="stripe", aliases={}, mapping=None, fx=fx,
                          target_currency=target_currency, grace_days=grace_days, grace_mode=grace_mode,
                          on_missing_fx=on_missing_fx, today=today, allow_warnings=allow_warnings,
                          source=str(source) if not isinstance(source, pd.DataFrame) else None)
    ledger.notes = [*notes, *ledger.notes]
    return ledger


def _derive_mrr(frame: pd.DataFrame, notes: list[str]) -> None:
    """Fill `mrr_amount` from the invoice total and the term wherever the export has no MRR column."""
    derived = 0
    mrr = frame["mrr_amount"].tolist() if "mrr_amount" in frame.columns else [None] * len(frame)
    total = frame["recognized_amount"].tolist() if "recognized_amount" in frame.columns else [None] * len(frame)
    start = frame["service_start"].tolist() if "service_start" in frame.columns else [None] * len(frame)
    end = frame["service_end"].tolist() if "service_end" in frame.columns else [None] * len(frame)
    out: list[Decimal | None] = []
    for m, t, s, e in zip(mrr, total, start, end, strict=True):
        if m is not None and not pd.isna(m):
            out.append(Decimal(str(m)))
            continue
        if t is not None and pd.notna(t) and s is not None and pd.notna(s) and e is not None and pd.notna(e):
            out.append(proration.monthly_mrr(t, _d(s), _d(e)))
            derived += 1
            continue
        out.append(None)
    frame["mrr_amount"] = out
    if derived:
        notes.append(f"mrr_amount derived for {derived} row(s) as invoice total / months covered")


def _d(v) -> date:
    return v if isinstance(v, date) else pd.Timestamp(v).date()


def _source_name(raw: pd.DataFrame, mapping: dict[str, str] | None, aliases: dict[str, tuple[str, ...]],
                 canonical: str) -> str:
    """Which source column fed `canonical`: the explicit mapping, else the alias the exporter used."""
    from saas_metrics.adapters.base import pick

    if mapping and mapping.get(canonical):
        return str(mapping[canonical]).strip().lower()
    found = pick(raw, aliases.get(canonical, ()))
    return str(found).strip().lower() if found is not None else ""
