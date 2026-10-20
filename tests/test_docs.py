"""Figures quoted in the README and definitions page come from the code."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import saas_metrics as sm
from saas_metrics import sample

ROOT = Path(__file__).resolve().parents[1]


def test_readme_spreading_figures():
    df = sample.generate()
    spread = sm.analyze(df, mapping=sample.MAPPING, today=date(2026, 10, 20))
    plain = sm.analyze(df, mapping={k: v for k, v in sample.MAPPING.items() if k not in ("service_start", "service_end")},
                       today=date(2026, 10, 20))
    readme = (ROOT / "README.md").read_text()
    assert f"**{int(plain.waterfall.churned_logos.sum())} churn events**" in readme
    assert f"gives **{int(spread.waterfall.churned_logos.sum())}**" in readme


def test_definitions_page_links_its_test():
    assert "tests/test_definitions.py" in (ROOT / "docs" / "definitions.md").read_text()
