"""The canonical ledger contract: what is accepted, what is flagged, and what is refused.

Every check here is a rule an adapter relies on, exercised on a tiny hand-built frame so the expected
diagnostic code is obvious. Nothing touches the network or a browser.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from saas_metrics.contracts import (
    BillingLedgerRow,
    ContractError,
    parse_row,
    validate_ledger,
)

COLUMNS = ["customer_id", "contract_id", "invoice_date", "service_start", "service_end", "mrr_amount", "currency"]


def frame(*rows: list, columns: list[str] | None = None) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=columns or COLUMNS)


CLEAN = frame(["c1", "k1", "2026-01-05", "2026-01-01", "2026-12-31", "100", "usd"])


# ---- accepted, and normalised on the way through -------------------------------------------------
def test_clean_row_normalises_dates_money_and_currency():
    rep = validate_ledger(CLEAN)
    assert rep.errors == []
    assert rep.ok                                    # nothing blocks: no errors, no warnings
    row = rep.frame.iloc[0]
    assert row.customer_id == "c1"
    assert row.invoice_date == date(2026, 1, 5)
    assert row.service_start == date(2026, 1, 1)
    assert row.service_end == date(2026, 12, 31)
    assert row.mrr_amount == Decimal("100.00")
    assert isinstance(row.mrr_amount, Decimal)
    assert row.currency == "USD"                     # lower case normalised up
    assert row.term_months == 12
    # the invoice total was not in the export: it is derived from the recurring amount and the term
    assert row.recognized_amount == Decimal("1200.00")
    assert "currency_case" in rep.codes()
    assert "derived_recognized_amount" in rep.codes()


def test_recognized_amount_is_kept_when_the_export_carries_one():
    df = frame(["c1", "k1", "2026-01-05", "2026-01-01", "2026-12-31", "100", "USD", "1188.00"],
               columns=[*COLUMNS, "recognized_amount"])
    rep = validate_ledger(df)
    assert rep.frame.iloc[0].recognized_amount == Decimal("1188.00")


def test_explicit_recognized_amount_survives_a_partial_year():
    """A 12-month term billed 1188 is 99/month; the export's own total is never overwritten."""
    df = frame(["c1", "k1", "2026-01-05", "2026-01-01", "2026-12-31", "99.00", "USD", "1188.00"],
               columns=[*COLUMNS, "recognized_amount"])
    rep = validate_ledger(df)
    assert rep.frame.iloc[0].mrr_amount == Decimal("99.00")
    assert rep.frame.iloc[0].recognized_amount == Decimal("1188.00")


# ---- the three the brief calls out: negative duration, missing customer, unnormalised currency ----
def test_negative_contract_duration_blocks():
    bad = frame(["c1", "k1", "2026-01-05", "2026-03-01", "2026-02-01", "100", "USD"])
    rep = validate_ledger(bad)
    assert not rep.ok
    assert [d.code for d in rep.errors] == ["negative_duration"]
    assert rep.errors[0].field == "service_end"
    with pytest.raises(ContractError, match="negative_duration"):
        rep.raise_if_blocking()


def test_missing_customer_id_blocks():
    bad = frame(["", "k1", "2026-01-05", "2026-01-01", "2026-01-31", "100", "USD"])
    rep = validate_ledger(bad)
    assert [d.code for d in rep.errors] == ["missing_customer_id"]
    assert rep.errors[0].row == 0
    assert rep.errors[0].field == "customer_id"


def test_unnormalized_currency_blocks_and_case_is_only_noted():
    junk = frame(["c1", "k1", "2026-01-05", "2026-01-01", "2026-01-31", "100", "us dollars"])
    rep = validate_ledger(junk)
    assert [d.code for d in rep.errors] == ["unnormalized_currency"]
    assert rep.errors[0].value == "us dollars"

    case = validate_ledger(frame(["c1", "k1", "2026-01-05", "2026-01-01", "2026-01-31", "100", "eur"]))
    assert case.errors == []
    assert case.frame.iloc[0].currency == "EUR"
    assert "currency_case" in case.codes()


def test_unknown_three_letter_code_warns_rather_than_blocks():
    rep = validate_ledger(frame(["c1", "k1", "2026-01-05", "2026-01-01", "2026-01-31", "100", "ZZZ"]))
    assert [d.code for d in rep.warnings] == ["unknown_currency_code"]
    assert rep.errors == []
    assert not rep.ok                     # a warning still blocks unless the caller opts in
    rep.raise_if_blocking(allow_warnings=True)


# ---- the rest of the contract --------------------------------------------------------------------
def test_missing_columns_reports_what_it_found_and_stops():
    rep = validate_ledger(pd.DataFrame({"customer": ["c1"], "amount": ["100"]}))
    assert [d.code for d in rep.errors] == ["missing_columns"]
    assert "customer" in rep.errors[0].message
    assert "missing_columns" in rep.summary()


def test_implausible_duration_warns():
    rep = validate_ledger(frame(["c1", "k1", "2026-01-05", "2000-01-01", "2099-01-01", "100", "USD"]))
    assert [d.code for d in rep.warnings] == ["implausible_duration"]


def test_negative_mrr_is_a_credit_and_only_warns():
    rep = validate_ledger(frame(["c1", "k1", "2026-01-05", "2026-01-01", "2026-01-31", "-50", "USD"]))
    assert [d.code for d in rep.warnings] == ["negative_mrr_amount"]
    assert rep.frame.iloc[0].mrr_amount == Decimal("-50.00")


def test_unparseable_and_missing_dates_block():
    rep = validate_ledger(frame(["c1", "k1", "2026-01-05", "not-a-date", None, "100", "USD"]))
    codes = sorted(d.code for d in rep.errors)
    assert codes == ["missing_date", "unparseable_date"]


def test_non_numeric_mrr_blocks():
    rep = validate_ledger(frame(["c1", "k1", "2026-01-05", "2026-01-01", "2026-01-31", "one hundred", "USD"]))
    assert [d.code for d in rep.errors] == ["invalid_mrr_amount"]


def test_contract_id_is_derived_when_the_export_has_none():
    df = pd.DataFrame({"customer_id": ["c1"], "invoice_date": ["2026-01-05"], "service_start": ["2026-01-01"],
                       "service_end": ["2026-01-31"], "mrr_amount": ["100"], "currency": ["USD"]})
    rep = validate_ledger(df)
    assert rep.ok
    assert "derived_contract_id" in rep.codes()
    assert rep.frame.iloc[0].contract_id == "c1:2026-01-01"


def test_blank_contract_id_warns_but_keeps_the_rows():
    rep = validate_ledger(frame(["c1", "", "2026-01-05", "2026-01-01", "2026-01-31", "100", "USD"]))
    assert [d.code for d in rep.warnings] == ["missing_contract_id"]


def test_duplicate_contract_periods_are_kept_and_noted():
    dup = frame(["c1", "k1", "2026-01-05", "2026-01-01", "2026-01-31", "100", "USD"],
                ["c1", "k1", "2026-01-20", "2026-01-01", "2026-01-31", "40", "USD"])
    rep = validate_ledger(dup)
    assert "duplicate_contract_period" in rep.codes()
    assert len(rep.frame) == 2                     # split invoices sum in the engine, they are not folded here


def test_future_service_warns_against_a_today():
    rep = validate_ledger(frame(["c1", "k1", "2026-01-05", "2026-06-01", "2026-06-30", "100", "USD"]),
                          today=date(2026, 1, 31))
    assert [d.code for d in rep.warnings] == ["future_service"]


def test_empty_input_is_reported_not_crashed():
    rep = validate_ledger(pd.DataFrame())
    assert [d.code for d in rep.errors] == ["empty_input"]


def test_report_frame_and_summary_are_inspectable():
    rep = validate_ledger(frame(["", "k1", "2026-01-05", "2026-03-01", "2026-02-01", "100", "USD"]))
    assert set(rep.to_frame().columns) == {"level", "code", "row", "field", "value", "message"}
    assert len(rep.to_frame()) == len(rep.diagnostics) == 2
    assert rep.summary() == "missing_customer_id x1, negative_duration x1"
    assert all(str(d).startswith(("ERROR", "WARNING", "INFO")) for d in rep.diagnostics)


# ---- the Pydantic row model ----------------------------------------------------------------------
def test_pydantic_row_accepts_a_canonical_dict():
    row = parse_row({"customer_id": " c1 ", "contract_id": "k1", "invoice_date": "2026-01-05",
                     "service_start": "2026-01-01", "service_end": "2026-12-31", "mrr_amount": "100.5",
                     "currency": "usd"})
    assert row.customer_id == "c1"                 # whitespace stripped
    assert row.mrr_amount == Decimal("100.50")     # quantised to the cent
    assert row.currency == "USD"
    assert row.term_days == 365


def test_pydantic_row_rejects_each_broken_field():
    base = {"customer_id": "c1", "contract_id": "k1", "invoice_date": "2026-01-05",
            "service_start": "2026-01-01", "service_end": "2026-01-31", "mrr_amount": "100", "currency": "USD"}
    with pytest.raises(ContractError, match="currency"):
        parse_row({**base, "currency": "usdollars"})
    with pytest.raises(ContractError, match="service_end"):
        parse_row({**base, "service_end": "2025-12-01"})
    with pytest.raises(ContractError, match="customer_id"):
        parse_row({**base, "customer_id": "   "})
    with pytest.raises(ContractError, match="implausible"):
        parse_row({**base, "service_end": "2099-01-01"})


def test_pydantic_model_allows_adapter_extras():
    """Adapters carry provenance through the same model rather than inventing a parallel one."""
    row = BillingLedgerRow(customer_id="c1", contract_id="k1", invoice_date=date(2026, 1, 5),
                           service_start=date(2026, 1, 1), service_end=date(2026, 1, 31),
                           mrr_amount=Decimal("100"), currency="USD", source_adapter="stripe", channel="direct")
    assert row.source_adapter == "stripe"
    assert row.channel == "direct"          # extra fields are kept, not dropped
