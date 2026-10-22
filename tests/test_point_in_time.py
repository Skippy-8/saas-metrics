"""The as-of-cutoff vintage only ever sees what was filed by the cutoff."""
from __future__ import annotations

import pandas as pd

from saas_metrics import build, config


def _fact(accn, filed, form, end, value, start=None, concept="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"):
    return {"accn": accn, "filed": filed, "form": form, "concept": concept, "axis": "", "member": "",
            "start": start, "end": end, "value": value, "unit": "USD", "decimals": "-3", "in_scope": True}


def test_restatement_after_cutoff_is_invisible_before_it():
    q_start = "2025-05-01"
    facts = pd.DataFrame([
        _fact("A", "2025-08-27", "10-Q", "2025-07-31", 100.0, q_start),   # original
        _fact("B", "2026-09-01", "10-Q", "2025-07-31", 110.0, q_start),   # restated comparative, a year later
    ])
    cut = build.derive_quarterly(facts, config.CUTOFF_DATE, "as_of_cutoff")
    late = build.derive_quarterly(facts, None, "latest")
    v = lambda df: df[(df.line == "revenue") & (df.period_end == "2025-07-31")].value.item()  # noqa: E731
    assert v(cut) == 100.0
    assert v(late) == 110.0


def test_quarter_filed_after_cutoff_is_absent():
    facts = pd.DataFrame([_fact("C", "2025-12-02", "10-Q", "2025-10-31", 5.0, "2025-08-01")])
    cut = build.derive_quarterly(facts, config.CUTOFF_DATE, "as_of_cutoff")
    assert cut.empty


def test_q4_is_full_year_minus_nine_months():
    facts = pd.DataFrame([
        _fact("K", "2026-03-11", "10-K", "2026-01-31", 400.0, "2025-02-01"),
        _fact("Q3", "2025-12-02", "10-Q", "2025-10-31", 290.0, "2025-02-01"),
    ])
    q = build.derive_quarterly(facts, None, "latest")
    row = q[(q.line == "revenue") & (q.period_end == "2026-01-31")].iloc[0]
    assert row.value == 110.0 and row.derivation == "fy_minus_9m"


def test_out_of_scope_fact_is_ignored():
    """A 10-Q fact dated two years before its own period is a tagging error, not a restatement."""
    filings = pd.DataFrame([{"accn": "OLD", "period_end": "2023-07-31"}, {"accn": "NEW", "period_end": "2025-07-31"}])
    facts = pd.DataFrame([
        _fact("OLD", "2023-08-31", "10-Q", "2023-07-31", 2_300_000.0, "2023-05-01"),
        _fact("NEW", "2025-08-27", "10-Q", "2023-07-31", 100_000.0, "2023-05-01"),
    ])
    facts["in_scope"] = build._in_scope(facts, filings)
    assert facts.in_scope.tolist() == [True, False]
    q = build.derive_quarterly(facts, None, "latest")
    assert q[(q.line == "revenue") & (q.period_end == "2023-07-31")].value.item() == 2_300_000.0
