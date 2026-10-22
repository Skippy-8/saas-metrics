"""Chargebee adapter: an invoice/subscription export -> the canonical ledger.

Chargebee exports differ from Stripe in three mundane but fatal ways if you assume otherwise:

* **Money is in major units.** `amount` is a decimal currency amount, not cents. No scaling is applied.
* **Dates are ISO strings** (`date`, `start_date`, `end_date`), not epochs.
* **The contract id is the subscription**, and a plan change arrives as a new subscription row with a
  bigger amount — the ramp classifier then decides whether that step is a contracted ramp or organic
  upsell (an explicit `change_type` column, when exported, always wins).

`end_date` is often blank on the newest subscription row (the term is open-ended). When it is blank the
adapter falls back to `term_months`/`billing_period` if present, and otherwise to a single month from
`start_date`, flagging the assumption rather than guessing a multi-year term.
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
    "customer_id": ("customer_id", "customer", "Customer ID", "cust_id", "customer_email"),
    "contract_id": ("subscription_id", "subscription", "Subscription ID", "contract_id", "invoice_id",
                    "Invoice ID"),
    "invoice_date": ("date", "invoice_date", "created_at", "Date", "Invoice Date"),
    "service_start": ("start_date", "service_start", "current_term_start", "Start Date", "Service Start"),
    "service_end": ("end_date", "service_end", "current_term_end", "End Date", "Service End"),
    "currency": ("currency_code", "currency", "Currency", "Currency Code"),
    "product_tier": ("plan_id", "plan_price_id", "item_price_id", "plan", "product_tier", "Plan", "Plan Tier"),
    "recognized_amount": ("recognized_amount", "amount", "total", "sub_total", "amount_paid", "Amount"),
    "mrr_amount": ("mrr_amount", "mrr", "MRR", "recurring_amount"),
    "term_months": ("term_months", "billing_period", "billing_period_unit"),
    "source_kind": ("source_kind", "change_type", "event_type", "transaction_type"),
    "channel": ("channel", "source", "channel_id"),
    "source_row": ("source_row",),
}

_OPEN_ENDED = frozenset({"", "nan", "none", "null", "<na>"})


def from_chargebee_export(source: str | Path | pd.DataFrame, *, mapping: dict[str, str] | None = None,
                          fx: FXConverter | None = None, target_currency: str = "USD", grace_days: int = 30,
                          grace_mode: str = grace.MODE_BLANK_PERIOD, on_missing_fx: str = "error",
                          default_term_months: int = 1, today: date | None = None,
                          allow_warnings: bool = True) -> Ledger:
    """Normalise a Chargebee export into a `Ledger`. Major-unit amounts, ISO dates, subscription terms."""
    raw = read_source(source)
    renamed, notes = rename_columns(raw, ALIASES, mapping)
    frame = renamed.copy()

    for col in ("invoice_date", "service_start", "service_end"):
        if col in frame.columns:
            frame[col] = pd.to_datetime(frame[col], errors="coerce", format="mixed").dt.date
            frame[col] = frame[col].astype(object)          # a date column we may still back-fill by hand

    # open-ended terms: an absent end_date becomes one month (or the exported term), and is flagged
    if "service_end" in frame.columns and "service_start" in frame.columns:
        missing_end = frame["service_end"].isna()
        if missing_end.any():
            term = _terms(frame, default_term_months)
            frame.loc[missing_end, "service_end"] = [
                _add_months(_d(s), t) - timedelta(days=1)
                for s, t in zip(frame.loc[missing_end, "service_start"], term[missing_end], strict=True)]
            notes.append(f"{int(missing_end.sum())} open-ended subscription(s): service_end defaulted to the "
                         f"exported term ({default_term_months} month(s) where none was given)")

    if "recognized_amount" in frame.columns:
        frame["recognized_amount"] = _money(frame["recognized_amount"])
    if "mrr_amount" in frame.columns:
        frame["mrr_amount"] = _money(frame["mrr_amount"])
    _derive_mrr(frame, notes)

    ledger = build_ledger(frame, adapter="chargebee", aliases={}, mapping=None, fx=fx,
                          target_currency=target_currency, grace_days=grace_days, grace_mode=grace_mode,
                          on_missing_fx=on_missing_fx, today=today, allow_warnings=allow_warnings,
                          source=str(source) if not isinstance(source, pd.DataFrame) else None)
    ledger.notes = [*notes, *ledger.notes]
    return ledger


def _money(s: pd.Series) -> list[Decimal | None]:
    num = pd.to_numeric(s.astype("string").str.replace(r"[,$\s]", "", regex=True), errors="coerce")
    return [None if pd.isna(v) else Decimal(str(v)).quantize(Decimal("0.01")) for v in num]


def _terms(frame: pd.DataFrame, default: int) -> pd.Series:
    out: list[int] = []
    months = frame["term_months"].tolist() if "term_months" in frame.columns else [None] * len(frame)
    for v in months:
        if v is None or pd.isna(v) or str(v).strip().lower() in _OPEN_ENDED:
            out.append(default)
            continue
        text = str(v).strip().lower()
        if text.endswith("year") or text in ("annual", "yearly", "y"):
            out.append(12)
        elif text.endswith("month") or text in ("monthly", "m"):
            out.append(1)
        else:
            try:
                out.append(max(int(float(text)), 1))
            except ValueError:
                out.append(default)
    return pd.Series(out, index=frame.index, dtype="int64")


def _derive_mrr(frame: pd.DataFrame, notes: list[str]) -> None:
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
        notes.append(f"mrr_amount derived for {derived} row(s) as invoice amount / months covered")


def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    return date(d.year + m // 12, m % 12 + 1, 1)


def _d(v) -> date:
    return v if isinstance(v, date) else pd.Timestamp(v).date()
