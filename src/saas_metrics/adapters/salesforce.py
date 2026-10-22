"""Salesforce adapter: Opportunity / OpportunityLineItem records -> the canonical ledger.

Salesforce is not a billing system, which is exactly why it is the hardest adapter and the most useful
one: it is where **contracted** revenue lives, including ramped multi-year deals that a billing export
has not yet invoiced.

* **`Amount` is the total contract value (TCV)**, not a monthly figure. The recurring amount is
  `Amount / term_months`, where the term comes from `StartDate`..`EndDate` (or an explicit
  `Contract_Term__c` / `term_months` when the end date is blank).
* **A ramped deal is several opportunities on one contract.** Three opportunities against the same
  `ContractId`, starting on successive anniversaries with $10k, $15k, $20k, are one contract with two
  scheduled steps. Grouped by `ContractId` and sorted by start date, the later rows become
  `contracted_expansion` and are flagged `is_contracted_expansion` — the pipeline keeps them out of
  organic upsell. An explicit `Type` / `RecordType.Name` of "Ramp" or "Renewal" wins over inference.
* **An intra-term increase is organic.** An opportunity whose service starts *before* the prior term
  ends is an upsell the customer bought mid-cycle, and is labelled `upsell`, not a ramp step.

Renewal opportunities (`Type = "Renewal"`) are labelled `renewal`, so the grace-period bridge can keep
a late-signed renewal from reading as churn followed by a new logo.
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
    "customer_id": ("AccountId", "Account.Id", "customer_id", "account_id", "Account Name", "Account.Name"),
    "contract_id": ("ContractId", "Contract.Id", "contract_id", "Contract Number", "ContractNumber",
                    "subscription_id"),
    "invoice_date": ("CloseDate", "CreatedDate", "invoice_date", "Opportunity Close Date", "Close Date"),
    "service_start": ("StartDate", "ServiceDate", "Service_Start__c", "Contract_Start_Date__c", "service_start",
                      "Subscription Start Date"),
    "service_end": ("EndDate", "Service_End__c", "Contract_Term_End__c", "Contract_End_Date__c", "service_end",
                    "Subscription End Date"),
    "currency": ("CurrencyIsoCode", "Currency", "currency", "Currency ISO Code"),
    "product_tier": ("Product_Tier__c", "Tier__c", "Plan__c", "product_tier", "Product Family", "Product2.Family",
                     "PricebookEntry.Product2.Family"),
    "recognized_amount": ("Amount", "Total_Contract_Value__c", "TCV__c", "recognized_amount", "Amount (TCV)"),
    "mrr_amount": ("mrr_amount", "MRR__c", "Monthly_Recurring__c", "Recurring_Amount__c", "MRR"),
    "term_months": ("term_months", "Contract_Term__c", "Term_Months__c", "Subscription_Term__c"),
    "source_kind": ("source_kind", "Type", "RecordType.Name", "RecordType", "Opportunity Type", "StageName"),
    "channel": ("channel", "LeadSource", "Channel__c", "Sales_Channel__c"),
    "source_row": ("source_row",),
}

_DEFAULT_TERM_MONTHS = 12


def from_salesforce_opportunities(source: str | Path | pd.DataFrame, *, mapping: dict[str, str] | None = None,
                                  fx: FXConverter | None = None, target_currency: str = "USD",
                                  grace_days: int = 30, grace_mode: str = grace.MODE_BLANK_PERIOD,
                                  on_missing_fx: str = "error", default_term_months: int = _DEFAULT_TERM_MONTHS,
                                  today: date | None = None, allow_warnings: bool = True,
                                  stitch_accounts: bool = True, co_terminate: bool = True) -> Ledger:
    """Normalise a Salesforce opportunity export into a `Ledger`. TCV -> MRR, ramps isolated from upsell.

    `stitch_accounts` recovers a dropped `ContractId` from the account id and the dates (see
    `adapters/stitching.py`); `co_terminate` aligns an intra-term add-on's end to the parent term.
    """
    raw = read_source(source)
    renamed, notes = rename_columns(raw, ALIASES, mapping)
    frame = renamed.copy()

    for col in ("invoice_date", "service_start", "service_end"):
        if col in frame.columns:
            frame[col] = pd.to_datetime(frame[col], errors="coerce", format="mixed").dt.date
            frame[col] = frame[col].astype(object)          # a date column we may still back-fill by hand

    if "service_start" not in frame.columns or frame["service_start"].isna().any():
        if "invoice_date" in frame.columns:
            frame["service_start"] = frame["service_start"] if "service_start" in frame.columns else frame["invoice_date"]
            frame["service_start"] = frame["service_start"].fillna(frame["invoice_date"])
            notes.append("service_start defaulted to the close date where the opportunity carried none")

    term = _terms_months(frame, default_term_months)
    if "service_end" not in frame.columns:
        frame["service_end"] = None
    missing_end = frame["service_end"].isna()
    if missing_end.any():
        # an unparseable start date is left blank here and flagged by validation rather than guessed
        derived = [None if s is None or pd.isna(s) else _add_months(_d(s), t) - timedelta(days=1)
                   for s, t in zip(frame.loc[missing_end, "service_start"], term[missing_end], strict=True)]
        frame.loc[missing_end, "service_end"] = derived
        blank = int(sum(d is None for d in derived))
        notes.append(f"{int(missing_end.sum()) - blank} opportunity(ies) had no end date: the term defaulted to "
                     f"Contract_Term__c or {default_term_months} months")
        if blank:
            notes.append(f"{blank} opportunity(ies) had neither an end date nor a usable start date and are left "
                         "blank for validation to flag")

    if "recognized_amount" in frame.columns:
        frame["recognized_amount"] = _money(frame["recognized_amount"])
    if "mrr_amount" in frame.columns:
        frame["mrr_amount"] = _money(frame["mrr_amount"])
    _derive_mrr(frame, term, notes)

    ledger = build_ledger(frame, adapter="salesforce", aliases={}, mapping=None, fx=fx,
                          target_currency=target_currency, grace_days=grace_days, grace_mode=grace_mode,
                          on_missing_fx=on_missing_fx, today=today, allow_warnings=allow_warnings,
                          stitch_accounts=stitch_accounts, co_terminate=co_terminate,
                          source=str(source) if not isinstance(source, pd.DataFrame) else None)
    ledger.notes = [*notes, *ledger.notes]
    return ledger


def _terms_months(frame: pd.DataFrame, default: int) -> pd.Series:
    out: list[int] = []
    months = frame["term_months"].tolist() if "term_months" in frame.columns else [None] * len(frame)
    start = frame["service_start"].tolist() if "service_start" in frame.columns else [None] * len(frame)
    end = frame["service_end"].tolist() if "service_end" in frame.columns else [None] * len(frame)
    for v, s, e in zip(months, start, end, strict=True):
        if v is not None and pd.notna(v) and str(v).strip().lower() not in ("", "nan", "none", "<na>"):
            try:
                out.append(max(int(round(float(str(v).strip()))), 1))
                continue
            except ValueError:
                pass
        if s is not None and pd.notna(s) and e is not None and pd.notna(e):
            out.append(proration.months_covered(_d(s), _d(e)))
        else:
            out.append(default)
    return pd.Series(out, index=frame.index, dtype="int64")


def _derive_mrr(frame: pd.DataFrame, term: pd.Series, notes: list[str]) -> None:
    derived = 0
    mrr = frame["mrr_amount"].tolist() if "mrr_amount" in frame.columns else [None] * len(frame)
    total = frame["recognized_amount"].tolist() if "recognized_amount" in frame.columns else [None] * len(frame)
    out: list[Decimal | None] = []
    for i, (m, t) in enumerate(zip(mrr, total, strict=True)):
        if m is not None and not pd.isna(m):
            out.append(Decimal(str(m)))
            continue
        if t is not None and pd.notna(t):
            months = int(term.iloc[i]) if i < len(term) else _DEFAULT_TERM_MONTHS
            out.append((Decimal(str(t)) / Decimal(max(months, 1))).quantize(Decimal("0.01")))
            derived += 1
            continue
        out.append(None)
    frame["mrr_amount"] = out
    if derived:
        notes.append(f"mrr_amount derived for {derived} row(s) as total contract value / term in months")


def _money(s: pd.Series) -> list[Decimal | None]:
    num = pd.to_numeric(s.astype("string").str.replace(r"[,$\s]", "", regex=True), errors="coerce")
    return [None if pd.isna(v) else Decimal(str(v)).quantize(Decimal("0.01")) for v in num]


def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    return date(d.year + m // 12, m % 12 + 1, 1)


def _d(v) -> date:
    return v if isinstance(v, date) else pd.Timestamp(v).date()
