"""The MongoDB build reproduces the golden snapshot taken before the toolkit refactor.

The other tests compare the workbook with the Python model; a refactor that changed both the same
way would pass them. This test pins the outputs themselves: every workbook cell (formulas exactly,
numbers to 1e-9 relative), the memo facts, the bridge, the scorecard and the core tables.

Additions since the snapshot are allowed only where listed in ADDED: new cells below the snapshot's
last row on the sheets named there (appended sections never move an existing cell) and the named new
scorecard keys. Anything changed or missing still fails.
"""
from __future__ import annotations

import gzip
import json
import math
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import make_golden  # noqa: E402

GOLDEN = Path(__file__).parent / "golden" / "mdb.json.gz"
ADDED = {  # toolkit Phase 4: the damped-trend baseline (BvA section E, its fitted inputs, scorecard column)
    "append_below_last_row": ("BvA_Variance_Backtest", "Inputs_Scenarios"),
    "scorecard_keys": ("damped_trend",),
}


def _diff(a, b, path="", out=None, rel=1e-9):
    out = [] if out is None else out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append(f"{path}/{k}: {'missing now' if k not in b else 'new'}")
            else:
                _diff(a[k], b[k], f"{path}/{k}", out, rel)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: length {len(a)} -> {len(b)}")
        for i, (x, y) in enumerate(zip(a, b, strict=False)):
            _diff(x, y, f"{path}[{i}]", out, rel)
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        if not (math.isclose(a, b, rel_tol=rel, abs_tol=1e-6) or (math.isnan(a) and math.isnan(b))):
            out.append(f"{path}: {a!r} -> {b!r}")
    elif a != b:
        out.append(f"{path}: {a!r} -> {b!r}")
    return out


@pytest.fixture(scope="module")
def golden():
    with gzip.open(GOLDEN, "rt") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def current(db):
    return json.loads(json.dumps(make_golden.snapshot(db)))   # same JSON normalisation as the file


def _allowed(diff: str, golden: dict) -> bool:
    if not diff.endswith(": new"):
        return False
    m = re.fullmatch(r"workbook/([^/]+)/([A-Z]+)(\d+): new", diff)
    if m and m.group(1) in ADDED["append_below_last_row"]:
        last = max(int(re.sub(r"[A-Z]+", "", c)) for c in golden["workbook"][m.group(1)])
        return int(m.group(3)) > last
    m = re.fullmatch(r"scorecard\[\d+\]/(\w+): new", diff)
    return bool(m) and m.group(1) in ADDED["scorecard_keys"]


@pytest.mark.parametrize("part", ["workbook", "memo_facts", "bridge", "scorecard", "tables"])
def test_matches_golden(golden, current, part):
    d = [x for x in _diff(golden[part], current[part], part) if not _allowed(x, golden)]
    assert not d, f"{len(d)} differences, first: {d[:5]}"


def test_additions_are_only_the_listed_ones(golden, current):
    added = [x for part in ("workbook", "scorecard") for x in _diff(golden[part], current[part], part)]
    assert added and all(_allowed(x, golden) for x in added)
    assert any("Inputs_Scenarios" in x for x in added) and any("damped_trend" in x for x in added)


def test_allowlist_rejects_changes_and_insertions(golden):
    assert not _allowed("workbook/BvA_Variance_Backtest/D12: 1.0 -> 2.0", golden)     # a changed value
    assert not _allowed("workbook/BvA_Variance_Backtest/D12: new", golden)            # inside the old block
    assert not _allowed("workbook/Operating_Model/D99: new", golden)                  # sheet not listed
    assert not _allowed("scorecard[0]/model_base: new", golden)
