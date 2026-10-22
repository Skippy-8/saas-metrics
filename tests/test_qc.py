"""Every pre-flight check passes on the fixture build (the same checks `saas_metrics qc` runs)."""
from __future__ import annotations

import pytest

from saas_metrics import qc


@pytest.fixture(scope="module")
def checks(db, companyfacts_path):
    return {c.name: c for c in qc.run(db, companyfacts_path)}


def test_check_count(checks):
    assert len(checks) == 24


@pytest.mark.parametrize("name", [
    "every line has 16 quarters (12 as of cutoff)",
    "derived quarters sum to the 10-K fiscal year",
    "parsed facts match SEC companyfacts",
    "in-scope facts agree across filings",
    "no post-cutoff information in the as-of-cutoff vintage",
    "cohort ARR sums to the ARR proxy",
    "cohort logos sum to reported customers",
    "no negative new-cohort ARR or new logos",
    "ledger NRR within 1.5pp of disclosed (windows without constrained quarters)",
])
def test_named_check(checks, name):
    assert checks[name].passed, checks[name].detail


def test_all_identities(checks):
    failed = [c for n, c in checks.items() if n.startswith("[") and not c.passed]
    assert not failed, [f"{c.name}: {c.detail}" for c in failed]


def test_known_values(db):
    """Spot values typed in from the Q2 FY26 10-Q (quarter ended 2025-07-31), in dollars."""
    w = db.execute("SELECT * FROM q_wide WHERE vintage='latest' AND period_end='2025-07-31'").df().iloc[0]
    assert w.revenue == 591_402_000
    assert w.revenue_atlas == 438_970_000
    assert w.revenue_other_subscription == 133_385_000
    assert w.revenue_services == 19_047_000
    assert w.operating_income == -65_294_000
    assert w.rpo == 794_200_000
