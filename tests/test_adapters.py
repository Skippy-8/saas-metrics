"""The three billing adapters, the grace-period bridge, FX and the DuckDB write — all offline.

Each test builds the smallest export that exhibits the quirk being handled, so a regression names the
trap rather than a wall of numbers. No network, no browser, no subprocess.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

import saas_metrics as sm
from saas_metrics.adapters import (
    from_chargebee_export,
    from_salesforce_opportunities,
    from_stripe_export,
    ledger_from_frame,
)
from saas_metrics.contracts import ContractError
from saas_metrics.fx import StaticFXProvider, load_rates

CANONICAL_COLUMNS = ("customer_id", "contract_id", "invoice_date", "service_start", "service_end",
                     "mrr_amount", "currency")


def canonical(*rows: list) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=list(CANONICAL_COLUMNS))


def stripe_frame(*rows: dict) -> pd.DataFrame:
    return pd.DataFrame(list(rows))


def epoch(y: int, m: int, d: int = 1) -> int:
    return int(pd.Timestamp(date(y, m, d)).timestamp())


def total_money(series) -> Decimal:
    return sum((Decimal(str(v)) for v in series if v is not None), Decimal(0))


# ---- Stripe --------------------------------------------------------------------------------------
def test_stripe_money_in_cents_and_half_open_period_end():
    """$49.00 reads 4900 in the CSV, and period_end is exclusive: the term is one April, not 31 days."""
    raw = stripe_frame(
        {"id": "in_1", "customer": "cus_A", "created": epoch(2025, 4, 1), "period_start": epoch(2025, 4, 1),
         "period_end": epoch(2025, 5, 1), "subtotal": 4900, "currency": "usd", "plan": "Pro",
         "subscription": "sub_A"})
    led = from_stripe_export(raw)
    assert led.ok, led.summary()
    row = led.frame.iloc[0]
    assert row.mrr_amount == Decimal("49.00")
    assert row.recognized_amount == Decimal("49.00")
    assert row.term_months == 1
    assert row.service_start == date(2025, 4, 1)
    assert row.service_end == date(2025, 4, 30)        # exclusive end pulled back one day
    assert row.currency == "USD"
    assert not row.prorated
    assert any("minor units" in n for n in led.all_notes())
    assert any("exclusive" in n for n in led.all_notes())


def test_stripe_annual_prepay_becomes_a_monthly_recurring_amount():
    raw = stripe_frame(
        {"id": "in_2", "customer": "cus_B", "created": epoch(2025, 3, 1), "period_start": epoch(2025, 3, 1),
         "period_end": epoch(2026, 3, 1), "subtotal": 588000, "currency": "USD",
         "plan": "Enterprise", "subscription": "sub_B"})
    led = from_stripe_export(raw)
    row = led.frame.iloc[0]
    assert row.recognized_amount == Decimal("5880.00")     # the invoice total
    assert row.mrr_amount == Decimal("490.00")             # the recurring amount
    assert row.term_months == 12
    assert row.service_end == date(2026, 2, 28)


def test_stripe_mid_month_start_prorates_in_whole_cents():
    raw = stripe_frame(
        {"id": "in_3", "customer": "cus_C", "created": epoch(2025, 3, 31), "period_start": epoch(2025, 3, 31),
         "period_end": epoch(2025, 5, 1), "subtotal": 2450, "currency": "USD", "plan": "Pro",
         "subscription": "sub_C"})
    led = from_stripe_export(raw)
    assert bool(led.frame.iloc[0].prorated) is True          # two calendar months touched
    tape, _ = led.to_engine_input()
    pieces = tape[tape.customer_id == "cus_C"]
    assert len(pieces) == 2
    assert float(pieces.revenue.astype(float).sum()) == pytest.approx(24.50, abs=1e-9)
    # largest-remainder: the cents add up to exactly what was billed, no rounding drift
    assert total_money(led.frame["recognized_amount"]) == Decimal("24.50")


def test_stripe_minor_units_can_be_forced_off():
    raw = stripe_frame(
        {"id": "in_4", "customer": "cus_D", "created": epoch(2025, 1, 1), "period_start": epoch(2025, 1, 1),
         "period_end": epoch(2025, 2, 1), "subtotal": 49, "currency": "USD", "subscription": "sub_D"})
    led = from_stripe_export(raw, minor_units=False)
    assert led.frame.iloc[0].mrr_amount == Decimal("49.00")


def test_stripe_explicit_mapping_wins_over_the_alias_guess():
    raw = stripe_frame(
        {"customer": "cus_E", "created": epoch(2025, 1, 1), "period_start": epoch(2025, 1, 1),
         "period_end": epoch(2025, 2, 1), "subtotal": 1000, "currency": "USD", "subscription": "sub_E",
         "nickname": "Solo"})
    led = from_stripe_export(raw, mapping={"product_tier": "nickname"})
    assert led.frame.iloc[0].product_tier == "Solo"


def test_stripe_invoice_without_a_subscription_is_kept_and_flagged():
    """A one-off invoice has no contract id. A blank (NULL) id must not crash the ramp classifier,
    and two blank ids must not be grouped together — that would invent a renewal between unrelated
    customers."""
    raw = stripe_frame(
        {"id": "in_5", "customer": "cus_F", "created": epoch(2025, 1, 1), "period_start": epoch(2025, 1, 1),
         "period_end": epoch(2025, 2, 1), "subtotal": 4900, "currency": "USD", "plan": "Pro",
         "subscription": None},
        {"id": "in_6", "customer": "cus_G", "created": epoch(2025, 1, 1), "period_start": epoch(2025, 1, 1),
         "period_end": epoch(2025, 2, 1), "subtotal": 1000, "currency": "USD", "plan": "Pro",
         "subscription": None})
    led = from_stripe_export(raw)
    assert not led.report.errors
    assert len(led.frame) == 2
    assert list(led.frame.expansion_kind) == ["new", "new"]
    assert any(d.code == "missing_contract_id" for d in led.report.warnings)


def test_unknown_mapping_target_raises_before_anything_is_parsed():
    raw = stripe_frame({"customer": "cus_E", "subtotal": 100})
    with pytest.raises(ContractError, match="unknown target field"):
        from_stripe_export(raw, mapping={"custmer_id": "customer"})


def test_mapping_a_column_that_does_not_exist_names_the_columns_present():
    raw = stripe_frame({"customer": "cus_E", "subtotal": 100})
    with pytest.raises(ContractError, match="not found"):
        from_stripe_export(raw, mapping={"customer_id": "Account Name"})


# ---- Chargebee -----------------------------------------------------------------------------------
def test_chargebee_amounts_are_major_units_and_open_terms_are_flagged():
    raw = pd.DataFrame({
        "customer_id": ["c1", "c2"], "subscription_id": ["s1", "s2"], "date": ["2026-01-05", "2026-02-05"],
        "start_date": ["2026-01-01", "2026-02-01"], "end_date": ["2026-01-31", None],
        "amount": ["99.00", "1200.00"], "currency_code": ["EUR", "EUR"],
        "plan_id": ["starter", "growth"], "change_type": ["subscription_creation", "plan_change"]})
    led = from_chargebee_export(raw)
    assert led.frame.iloc[0].mrr_amount == Decimal("99.00")          # not scaled: Chargebee is major units
    assert led.frame.iloc[1].service_end == date(2026, 2, 28)        # open-ended -> one month, flagged
    assert any("open-ended" in n for n in led.all_notes())
    assert not led.ok                                                # two EUR rows and no FX provider
    assert any(d.code == "no_fx_provider" for d in led.blocking)


def test_chargebee_billing_period_is_honoured_when_present():
    raw = pd.DataFrame({
        "customer_id": ["c1"], "subscription_id": ["s1"], "date": ["2026-01-05"], "start_date": ["2026-01-01"],
        "end_date": [None], "amount": ["1200.00"], "currency_code": ["USD"], "plan_id": ["growth"],
        "billing_period": ["year"]})
    led = from_chargebee_export(raw)
    assert led.frame.iloc[0].term_months == 12
    assert led.frame.iloc[0].mrr_amount == Decimal("100.00")


# ---- Salesforce ----------------------------------------------------------------------------------
def salesforce_frame(**over: list) -> pd.DataFrame:
    base = {
        "AccountId": ["a1", "a1", "a1", "a2"],
        "ContractId": ["ct1", "ct1", "ct1", "ct2"],
        "CloseDate": ["2024-01-10", "2024-12-20", "2025-12-15", "2025-06-01"],
        "StartDate": ["2024-01-01", "2025-01-01", "2026-01-01", "2025-06-01"],
        "EndDate": ["2024-12-31", "2025-12-31", "2026-12-31", "2026-05-31"],
        "Amount": ["12000", "18000", "24000", "6000"],
        "CurrencyIsoCode": ["USD", "USD", "USD", "USD"],
        "Product_Tier__c": ["Pro", "Pro", "Pro", "Pro"],
        "Type": ["New", "Ramp", "Ramp", "New"],
    }
    base.update(over)
    return pd.DataFrame(base)


def test_salesforce_total_contract_value_becomes_mrr():
    led = from_salesforce_opportunities(salesforce_frame())
    assert led.ok, led.summary()
    first = led.frame.iloc[0]
    assert first.recognized_amount == Decimal("12000.00")     # TCV kept
    assert first.mrr_amount == Decimal("1000.00")             # 12000 / 12
    assert first.term_months == 12
    assert first.expansion_kind == "new"


def test_salesforce_ramp_steps_are_isolated_from_organic_upsell():
    led = from_salesforce_opportunities(salesforce_frame())
    kinds = list(led.frame[led.frame.contract_id == "ct1"].expansion_kind)
    assert kinds == ["new", "contracted_expansion", "contracted_expansion"]
    assert list(led.frame[led.frame.contract_id == "ct1"].is_contracted_expansion) == [False, True, True]
    assert "contracted ramp step" in " ".join(led.all_notes())


def test_salesforce_intra_term_increase_is_upsell_not_a_ramp():
    """A step that starts before the prior term ends was bought mid-cycle: organic, not contracted."""
    df = salesforce_frame()
    df.loc[1, "StartDate"] = "2024-07-01"                      # starts inside the 2024 term
    df.loc[1, "Type"] = "Upsell"
    led = from_salesforce_opportunities(df)
    kinds = list(led.frame[led.frame.contract_id == "ct1"].expansion_kind)
    assert kinds == ["new", "upsell", "contracted_expansion"]
    assert not bool(led.frame[led.frame.contract_id == "ct1"].is_contracted_expansion.iloc[1])


def test_salesforce_missing_end_date_uses_the_term_column():
    df = salesforce_frame()
    df.loc[3, "EndDate"] = None
    df["Contract_Term__c"] = [12, 12, 12, 24]
    led = from_salesforce_opportunities(df)
    last = led.frame[led.frame.contract_id == "ct2"].iloc[0]
    assert last.term_months == 24
    assert last.service_end == date(2027, 5, 31)
    assert last.mrr_amount == Decimal("250.00")                # 6000 / 24


def test_salesforce_without_any_end_date_defaults_and_says_so():
    df = salesforce_frame()
    df["EndDate"] = None
    df = df.drop(columns=["Type"])
    led = from_salesforce_opportunities(df)
    assert led.frame.iloc[0].term_months == 12
    assert any("no end date" in n for n in led.all_notes())


# ---- grace periods -------------------------------------------------------------------------------
def test_short_gap_inside_a_month_is_left_as_billed():
    """A 3-day gap does not blank a month: both months still carry revenue, so nothing is bridged."""
    df = canonical(["c1", "k1", "2026-01-01", "2026-01-01", "2026-01-30", "100", "USD"],
                   ["c1", "k1", "2026-02-03", "2026-02-03", "2026-02-28", "100", "USD"])
    led = ledger_from_frame(df, grace_days=30)
    assert led.grace_report.n_bridged == 0
    assert [g["gap_days"] for g in led.grace_report.short] == [3]
    assert led.frame.iloc[0].service_end == date(2026, 1, 30)          # untouched
    assert list(led.frame.grace_bridged) == [False, False]


def test_gap_that_would_blank_a_whole_month_is_bridged():
    """January ends 31 Jan and the renewal starts 1 March: February would read as churn. Bridge it."""
    df = canonical(["c1", "k1", "2026-01-01", "2026-01-01", "2026-01-31", "100", "USD"],
                   ["c1", "k1", "2026-03-01", "2026-03-01", "2026-03-31", "100", "USD"])
    led = ledger_from_frame(df, grace_days=30)
    assert led.grace_report.n_bridged == 1
    assert [b["gap_days"] for b in led.grace_report.bridged] == [28]
    assert led.frame.iloc[0].service_end == date(2026, 2, 28)          # coverage made contiguous
    assert list(led.frame.grace_bridged) == [True, True]
    expected = Decimal("200.00")                                       # two invoices of 100
    assert total_money(led.frame["recognized_amount"]) == expected     # the billed amount is unchanged

    tape = led.to_engine_input()[0].assign(revenue=lambda d: d.revenue.astype(float))
    months = tape.groupby("period").revenue.sum()
    assert len(months) == 3                                            # Jan, Feb and Mar all carry revenue
    assert all(v > 0 for v in months)
    assert "late renewal" in " ".join(led.all_notes())


def test_gap_beyond_the_window_is_a_lapse():
    df = canonical(["c1", "k1", "2026-01-01", "2026-01-01", "2026-01-31", "100", "USD"],
                   ["c1", "k1", "2026-05-01", "2026-05-01", "2026-05-31", "100", "USD"])
    led = ledger_from_frame(df, grace_days=30)
    assert led.grace_report.n_bridged == 0
    assert [g["gap_days"] for g in led.grace_report.long] == [89]
    assert led.frame.iloc[0].service_end == date(2026, 1, 31)
    assert "beyond the grace period" in " ".join(led.all_notes())


def test_grace_always_mode_closes_every_gap_in_the_window():
    df = canonical(["c1", "k1", "2026-01-01", "2026-01-01", "2026-01-30", "100", "USD"],
                   ["c1", "k1", "2026-02-03", "2026-02-03", "2026-02-28", "100", "USD"])
    led = ledger_from_frame(df, grace_days=30, grace_mode="always")
    assert led.grace_report.n_bridged == 1
    assert led.frame.iloc[0].service_end == date(2026, 2, 2)
    assert led.frame.iloc[0].term_months == 1          # 33 days is one month, not two
    assert bool(led.frame.iloc[0].prorated) is True


def test_grace_zero_switches_bridging_off():
    df = canonical(["c1", "k1", "2026-01-01", "2026-01-01", "2026-01-31", "100", "USD"],
                   ["c1", "k1", "2026-03-01", "2026-03-01", "2026-03-31", "100", "USD"])
    led = ledger_from_frame(df, grace_days=0)
    assert led.grace_report.n_bridged == 0
    assert led.frame.iloc[0].service_end == date(2026, 1, 31)


def test_unknown_grace_mode_is_refused():
    with pytest.raises(ValueError, match="mode"):
        ledger_from_frame(canonical(["c1", "k1", "2026-01-01", "2026-01-01", "2026-01-31", "1", "USD"]),
                          grace_mode="sometimes")


# ---- FX ------------------------------------------------------------------------------------------
def test_multi_currency_without_a_rate_table_fails_closed():
    df = canonical(["c1", "k1", "2026-01-01", "2026-01-01", "2026-01-31", "100", "EUR"])
    led = ledger_from_frame(df)
    assert not led.ok
    assert [d.code for d in led.blocking] == ["no_fx_provider"]
    with pytest.raises(ContractError, match="no_fx_provider"):
        led.raise_if_blocking()


def test_fx_provider_converts_and_keeps_the_original():
    df = canonical(["c1", "k1", "2026-01-01", "2026-01-01", "2026-01-31", "100", "EUR"],
                   ["c2", "k2", "2026-01-01", "2026-01-01", "2026-01-31", "50", "USD"])
    led = ledger_from_frame(df, fx=StaticFXProvider({"EUR": "1.08"}))
    assert led.ok
    eur = led.frame.iloc[0]
    assert eur.mrr_amount == Decimal("108.00")
    assert eur.source_currency == "EUR"
    assert eur.currency == "USD"
    assert eur.fx_rate == Decimal("1.08")
    assert led.frame.iloc[1].mrr_amount == Decimal("50.00")       # USD is left alone
    assert any(d.code == "fx_converted" for d in led.diagnostics)


def test_fx_on_missing_keep_leaves_the_row_and_warns():
    df = canonical(["c1", "k1", "2026-01-01", "2026-01-01", "2026-01-31", "100", "EUR"])
    led = ledger_from_frame(df, fx=StaticFXProvider({"GBP": "1.27"}), on_missing_fx="keep")
    assert led.ok
    assert led.frame.iloc[0].mrr_amount == Decimal("100.00")
    assert [d.code for d in led.diagnostics if d.level == "warning"] == ["unconverted_currency"]


def test_fx_on_missing_drop_removes_the_row():
    df = canonical(["c1", "k1", "2026-01-01", "2026-01-01", "2026-01-31", "100", "EUR"],
                   ["c2", "k2", "2026-01-01", "2026-01-01", "2026-01-31", "50", "USD"])
    led = ledger_from_frame(df, fx=StaticFXProvider({"GBP": "1.27"}), on_missing_fx="drop")
    assert len(led.frame) == 1
    assert led.frame.iloc[0].customer_id == "c2"
    assert any(d.code == "dropped_unconverted_rows" for d in led.diagnostics)


def test_rate_table_loads_from_a_csv(tmp_path):
    path = tmp_path / "rates.csv"
    path.write_text("currency,rate\nEUR,1.09\nGBP,1.27\n")
    fx = load_rates(path)
    assert fx.rate("EUR") == Decimal("1.09")
    assert fx.base == "USD"
    with pytest.raises(KeyError):
        fx.rate("JPY")


def test_rate_table_with_the_wrong_columns_says_which_it_found(tmp_path):
    path = tmp_path / "rates.csv"
    path.write_text("ccy,value\nEUR,1.09\n")
    with pytest.raises(ValueError, match="ccy"):
        load_rates(path)


# ---- DuckDB --------------------------------------------------------------------------------------
def test_duckdb_write_is_cents_exact_and_read_back(tmp_path):
    df = canonical(["c1", "k1", "2026-01-01", "2026-01-01", "2026-01-31", "100.05", "USD"],
                   ["c2", "k2", "2026-01-01", "2026-01-01", "2026-12-31", "1234.56", "USD"])
    led = ledger_from_frame(df)
    db = tmp_path / "billing.duckdb"
    info = led.to_duckdb(db)
    assert info["verified"] is True
    assert info["rows"] == 2
    assert info["mrr_total"] == Decimal("1334.61")

    import duckdb
    con = duckdb.connect(str(db), read_only=True)
    kinds = dict(con.execute("SELECT column_name, data_type FROM information_schema.columns "
                             "WHERE table_name = 'billing_ledger'").fetchall())
    assert kinds["mrr_amount"] == "DECIMAL(18,2)"
    assert kinds["service_start"] == "DATE"
    assert con.execute("SELECT sum(mrr_amount) FROM billing_ledger").fetchone()[0] == Decimal("1334.61")
    assert con.execute("SELECT customer_id FROM billing_ledger ORDER BY customer_id").fetchall() == [("c1",), ("c2",)]
    con.close()


def test_ledger_writes_parquet_and_csv(tmp_path):
    df = canonical(["c1", "k1", "2026-01-01", "2026-01-01", "2026-01-31", "100", "USD"])
    led = ledger_from_frame(df)
    parquet, csv = led.to_parquet(tmp_path / "l.parquet"), led.to_csv(tmp_path / "l.csv")
    assert pd.read_parquet(parquet).shape[0] == 1
    assert pd.read_csv(csv).shape[0] == 1


def test_canonical_adapter_requires_the_canonical_columns():
    led = ledger_from_frame(pd.DataFrame({"customer": ["c1"], "amount": ["100"]}))
    assert not led.ok
    assert [d.code for d in led.blocking] == ["missing_columns"]


# ---- end to end ----------------------------------------------------------------------------------
def monthly_stripe(months: int = 18) -> pd.DataFrame:
    """Two customers billed monthly: one steady, one that doubles halfway (organic expansion)."""
    rows = []
    for customer, sub, tier, base in (("cus_A", "sub_A", "Pro", 4900), ("cus_B", "sub_B", "Growth", 19900)):
        for i in range(months):
            start = pd.Timestamp(date(2025, 1, 1)) + pd.DateOffset(months=i)
            end = start + pd.DateOffset(months=1)
            amount = base * 2 if (customer == "cus_B" and i >= 9) else base
            rows.append({"id": f"in_{customer}_{i}", "customer": customer, "created": int(start.timestamp()),
                         "period_start": int(start.timestamp()), "period_end": int(end.timestamp()),
                         "subtotal": amount, "currency": "USD", "plan": tier, "subscription": sub})
    return pd.DataFrame(rows)


def test_stripe_export_runs_the_waterfall_end_to_end():
    led = from_stripe_export(monthly_stripe())
    assert led.ok, led.summary()
    tape, notes = led.to_engine_input()
    assert notes[0].startswith("recognised from the stripe ledger")

    report = sm.analyze(tape, allow_warnings=True)
    assert report.ok
    waterfall = report.waterfall
    assert bool((waterfall.check_diff == 0).all())               # the identity holds in every period
    assert (waterfall.ending > 0).all()
    metrics = report.metrics
    assert metrics.active_customers.iloc[-1] == 2                # nobody churned
    assert int(waterfall.new_logo.sum()) >= 2                    # both customers acquired
    assert float(metrics.nrr_12m.iloc[-1]) > 1.0                  # the doubling shows up as expansion

    recognised = total_money(led.frame["recognized_amount"])     # the billed total is fully recognised
    assert float(tape.revenue.astype(float).sum()) == pytest.approx(float(recognised), abs=0.01)


def test_recognised_tape_round_trips_through_the_engine_to_excel(tmp_path):
    led = from_stripe_export(monthly_stripe(months=14))
    tape, _ = led.to_engine_input()
    report = sm.analyze(tape, allow_warnings=True)
    path = report.to_excel(tmp_path / "m.xlsx")
    assert path.exists()


def test_non_prorated_mode_puts_the_whole_invoice_in_the_invoice_month():
    led = from_stripe_export(monthly_stripe(months=3))
    prorated, _ = led.to_engine_input()
    up_front, _ = led.to_engine_input(prorate=False)
    assert len(up_front) == len(led.frame)                       # one line per invoice, no spreading
    assert len(prorated) >= len(up_front)
    assert float(up_front.revenue.astype(float).sum()) == float(prorated.revenue.astype(float).sum())


def test_as_of_defers_revenue_that_is_not_yet_earned():
    raw = stripe_frame(
        {"id": "in_annual", "customer": "cus_Z", "created": epoch(2026, 1, 1), "period_start": epoch(2026, 1, 1),
         "period_end": epoch(2027, 1, 1), "subtotal": 120000, "currency": "USD", "subscription": "sub_Z"})
    led = from_stripe_export(raw)
    full, _ = led.to_engine_input()
    partial, _ = led.to_engine_input(as_of="2026-03-31")
    assert len(partial) == 3                                     # Jan, Feb, Mar only
    # the engine recognises by day: 90 of the 365 covered days
    assert float(partial.revenue.astype(float).sum()) == pytest.approx(1200 * 90 / 365, abs=0.02)
    assert len(full) == 12


def test_adapter_registry_dispatches_by_name():
    from saas_metrics.adapters import ADAPTERS
    assert set(ADAPTERS) == {"stripe", "chargebee", "salesforce", "canonical"}
    led = ADAPTERS["stripe"](monthly_stripe(months=1))
    assert led.adapter == "stripe"


# ---- the CLI entry point -------------------------------------------------------------------------
def test_ledger_cli_writes_parquet_and_duckdb(tmp_path):
    from saas_metrics import cli

    export = tmp_path / "stripe.csv"
    monthly_stripe(months=6).to_csv(export, index=False)
    parquet, db, tape = tmp_path / "ledger.parquet", tmp_path / "billing.duckdb", tmp_path / "tape.parquet"
    cli.main(["ledger", str(export), "--adapter", "stripe", "--out", str(parquet), "--duckdb", str(db),
              "--engine", str(tape)])
    assert parquet.exists() and db.exists() and tape.exists()
    assert len(pd.read_parquet(tape)) >= 12


def test_ledger_cli_refuses_a_broken_export(tmp_path):
    from saas_metrics import cli

    bad = tmp_path / "bad.csv"
    canonical(["c1", "k1", "2026-01-01", "2026-03-01", "2026-02-01", "100", "USD"]).to_csv(bad, index=False)
    with pytest.raises(SystemExit, match="ledger rejected"):
        cli.main(["ledger", str(bad), "--adapter", "canonical", "--analyze"])
