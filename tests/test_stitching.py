"""Account stitching: recover a contract id Salesforce dropped, without inventing a new logo.

The three tiers are tested in the order the adapter applies them — explicit link, account-stitch
renewal, intra-term add-on — plus the two things the heuristic must refuse to do: join across
accounts, and join across a lapse longer than the grace window. All offline; no network or browser.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd

from saas_metrics.adapters import from_salesforce_opportunities
from saas_metrics.adapters.stitching import (
    SOURCE_ADD_ON,
    SOURCE_EXPLICIT,
    SOURCE_NEW,
    SOURCE_RENEWAL,
    stitch_accounts,
)


def sf(*rows: dict) -> pd.DataFrame:
    """A Salesforce opportunity export with only the columns the stitching reads."""
    return pd.DataFrame(list(rows))


def opp(account: str, contract: str | None, start: str, end: str, amount: str,
        kind: str = "New", close: str | None = None) -> dict:
    return {"AccountId": account, "ContractId": contract, "StartDate": start, "EndDate": end,
            "Amount": amount, "CloseDate": close or start, "CurrencyIsoCode": "USD",
            "Type": kind, "Product_Tier__c": "Pro"}


def ledger(*rows: dict, **kwargs):
    return from_salesforce_opportunities(sf(*rows), **kwargs)


# ---- tier 1: explicit link ------------------------------------------------------------------------
def test_explicit_contract_id_is_believed_even_when_the_dates_overlap():
    """Two agreements on one account that overlap in time are two agreements: the export said so."""
    led = ledger(opp("a1", "K1", "2024-01-01", "2024-12-31", "12000"),
                 opp("a1", "K2", "2024-06-01", "2025-05-31", "6000"))
    assert led.ok, led.summary()
    assert list(led.frame.contract_id) == ["K1", "K2"]
    assert list(led.frame.contract_source) == [SOURCE_EXPLICIT, SOURCE_EXPLICIT]
    assert list(led.frame.stitched) == [False, False]
    assert led.stitch_report.stitched == 0
    assert not any("stitched" in n for n in led.all_notes())


def test_contract_number_is_an_explicit_link_too():
    """Some exports rename ContractId to `Contract Number`; it is the same tier-one signal."""
    raw = pd.DataFrame({"AccountId": ["a1", "a1"], "Contract Number": ["K9", "K9"],
                        "StartDate": ["2024-01-01", "2025-01-01"], "EndDate": ["2024-12-31", "2025-12-31"],
                        "Amount": ["12000", "12000"], "CloseDate": ["2024-01-01", "2025-01-01"],
                        "CurrencyIsoCode": ["USD", "USD"]})
    led = from_salesforce_opportunities(raw)
    assert list(led.frame.contract_id) == ["K9", "K9"]
    assert list(led.frame.contract_source) == [SOURCE_EXPLICIT, SOURCE_EXPLICIT]


# ---- tier 2: account-stitch renewal ---------------------------------------------------------------
def test_late_signed_renewal_without_a_contract_id_continues_the_lifecycle():
    """A renewal that lost its ContractId must not read as churn followed by a new logo."""
    led = ledger(opp("a1", "K1", "2024-01-01", "2024-12-31", "12000"),
                 opp("a1", None, "2025-01-11", "2026-01-10", "12000", kind="New"))
    assert led.ok, led.summary()
    assert list(led.frame.contract_id) == ["K1", "K1"]
    assert list(led.frame.contract_source) == [SOURCE_EXPLICIT, SOURCE_RENEWAL]
    assert list(led.frame.stitched) == [False, True]
    assert led.stitch_report.renewals == 1
    assert list(led.frame.expansion_kind) == ["new", "renewal"]
    assert "new logo" in " ".join(led.all_notes())


def test_a_gap_beyond_the_grace_window_is_a_lapse_not_a_renewal():
    """Past the window a lapse is a lapse: stitching it back would hide real churn."""
    led = ledger(opp("a1", "K1", "2024-01-01", "2024-12-31", "12000"),
                 opp("a1", None, "2025-02-15", "2026-02-14", "12000"))
    assert list(led.frame.contract_id) == ["K1", "a1:2025-02-15"]
    assert list(led.frame.contract_source) == [SOURCE_EXPLICIT, SOURCE_NEW]
    assert led.stitch_report.renewals == 0


def test_the_grace_boundary_is_inclusive_at_the_last_day():
    """gap_days == grace_days stitches; one day more does not, matching the invoice bridge."""
    inside = ledger(opp("a1", "K1", "2024-01-01", "2024-12-31", "12000"),
                    opp("a1", None, "2025-01-31", "2026-01-30", "12000"))
    assert inside.stitch_report.renewals == 1                      # 30 days late
    outside = ledger(opp("a1", "K1", "2024-01-01", "2024-12-31", "12000"),
                     opp("a1", None, "2025-02-01", "2026-01-31", "12000"))
    assert outside.stitch_report.renewals == 0                     # 31 days late


# ---- tier 3: intra-term add-on --------------------------------------------------------------------
def test_mid_term_add_on_is_an_upsell_and_is_co_terminated():
    """An add-on's truncated end is aligned to the parent term, and its rate re-derived over it."""
    led = ledger(opp("a1", "K1", "2024-01-01", "2024-12-31", "12000"),
                 opp("a1", None, "2024-07-01", "2024-09-30", "3000", kind="New"))
    add_on = led.frame.iloc[1]
    assert add_on.contract_id == "K1"
    assert add_on.contract_source == SOURCE_ADD_ON
    assert add_on.expansion_kind == "upsell"                       # `Type = New` is overridden
    assert not bool(add_on.is_contracted_expansion)
    assert add_on.service_end == date(2024, 12, 31)                # co-terminated to the parent
    assert add_on.co_term_end == date(2024, 12, 31)
    assert bool(add_on.co_terminated)
    assert add_on.term_months == 6                                 # extended coverage, same $3,000
    assert add_on.mrr_amount == Decimal("500.00")                  # 3000 / 6, not 3000 / 3
    assert add_on.recognized_amount == Decimal("3000.00")          # revenue never invented
    assert any("co-terminated" in n for n in led.all_notes())


def test_co_termination_never_shortens_an_add_on_that_outlives_the_parent():
    """An add-on running past the parent term keeps its own end; the parent is never re-dated."""
    led = ledger(opp("a1", "K1", "2024-01-01", "2024-12-31", "12000"),
                 opp("a1", None, "2024-07-01", "2025-06-30", "6000"))
    parent, add_on = led.frame.iloc[0], led.frame.iloc[1]
    assert parent.service_end == date(2024, 12, 31)
    assert add_on.service_end == date(2025, 6, 30)
    assert add_on.co_term_end == date(2024, 12, 31)                # recorded, not enforced backwards
    assert not bool(add_on.co_terminated)


def test_co_termination_can_be_switched_off():
    """`co_terminate=False` keeps the dates as exported and still records the parent term end."""
    led = ledger(opp("a1", "K1", "2024-01-01", "2024-12-31", "12000"),
                 opp("a1", None, "2024-07-01", "2024-09-30", "3000"), co_terminate=False)
    add_on = led.frame.iloc[1]
    assert add_on.service_end == date(2024, 9, 30)
    assert add_on.co_term_end == date(2024, 12, 31)
    assert not bool(add_on.co_terminated)


def test_a_stale_type_marker_cannot_relabel_a_stitched_add_on():
    """Salesforce often says `Type = New` on a quote-created add-on; the stitching wins."""
    led = ledger(opp("a1", "K1", "2024-01-01", "2024-12-31", "12000", kind="New"),
                 opp("a1", None, "2024-07-01", "2024-12-31", "3600", kind="New"))
    assert list(led.frame.expansion_kind) == ["new", "upsell"]


# ---- what it refuses to do ------------------------------------------------------------------------
def test_two_accounts_are_never_stitched_together():
    """Same dates, different AccountId: two lifecycles, two ids. No name or date guessing across accounts."""
    led = ledger(opp("a1", None, "2024-01-01", "2024-12-31", "12000"),
                 opp("a2", None, "2024-01-01", "2024-12-31", "12000"))
    assert list(led.frame.contract_id) == ["a1:2024-01-01", "a2:2024-01-01"]
    assert list(led.frame.contract_source) == [SOURCE_NEW, SOURCE_NEW]
    assert led.stitch_report.stitched == 0


def test_without_an_account_column_stitching_is_skipped_loudly():
    """No AccountId means no safe inference, so the ledger says so instead of guessing."""
    frame = pd.DataFrame({"contract_id": [None], "customer_id": ["a1"],
                          "service_start": [date(2024, 1, 1)], "service_end": [date(2024, 12, 31)]})
    out, report = stitch_accounts(frame.drop(columns=["customer_id"]), account_col="customer_id")
    assert report.skipped and "skipped" in report.skipped
    assert report.notes() == [report.skipped]
    assert list(out.contract_id) == [None]


def test_undated_rows_are_left_alone_rather_than_merged():
    """A row with no start date cannot be placed on a timeline, so it keeps its own id."""
    frame = pd.DataFrame({"customer_id": ["a1", "a1"], "contract_id": [None, None],
                          "service_start": [date(2024, 1, 1), None],
                          "service_end": [date(2024, 12, 31), None]})
    out, report = stitch_accounts(frame)
    assert out.contract_id.iloc[0] == "a1:2024-01-01"
    assert out.contract_id.iloc[1] == "a1:undated"
    assert report.stitched == 0


# ---- the reason it exists: no phantom churn -----------------------------------------------
def test_a_renewal_without_a_contract_id_leaves_no_empty_month_for_the_waterfall():
    """The engine sees revenue in every calendar month of the relationship, so GRR has no churn."""
    led = ledger(opp("a1", "K1", "2024-01-01", "2024-12-31", "12000"),
                 opp("a1", None, "2025-01-11", "2026-01-10", "12000"))
    tape, _ = led.to_engine_input()
    months = sorted({pd.Timestamp(p).strftime("%Y-%m") for p in tape.period})
    expected = [f"{y}-{m:02d}" for y in (2024, 2025, 2026) for m in range(1, 13)]
    expected = [m for m in expected if "2024-01" <= m <= "2026-01"]
    assert months == expected
    per_month = pd.to_numeric(tape.groupby(
        tape.period.map(lambda p: pd.Timestamp(p).strftime("%Y-%m"))).revenue.sum())
    assert (per_month > 0).all()
