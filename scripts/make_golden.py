"""Capture golden snapshots of the MongoDB build from the offline fixtures.

Run once, before a refactor, from a known-good commit. tests/test_golden_mdb.py then requires
every later build to reproduce these values (formulas exactly, numbers to 1e-9 relative).
Never regenerate this file to make a failing golden test pass: find the drift instead.

    .venv/bin/python scripts/make_golden.py
"""
from __future__ import annotations

import gzip
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import duckdb  # noqa: E402
import openpyxl  # noqa: E402

from saas_metrics import backtest, build, memo, workbook  # noqa: E402

OUT = ROOT / "tests" / "golden" / "mdb.json.gz"
TABLES = {
    "quarterly_financials": "vintage, period_end, line",
    "kpi_disclosures": "accn, kpi",
    "guidance": "accn, period, metric",
    "reported_non_gaap": "accn, metric",
    "cohort_calibration": "vintage, period_end",
    "cohort_arr": "vintage, cohort, period_end",
    "cohort_logos": "vintage, cohort, period_end",
}


def fixture_db() -> duckdb.DuckDBPyConnection:
    fix = ROOT / "tests" / "fixtures"
    raw = duckdb.connect()
    src = {t: raw.execute(f"SELECT * FROM '{fix / (t + '.parquet')}'").df() for t in build.SOURCE_TABLES}
    con = duckdb.connect()
    build.assemble(con, src)
    return con


def cell_value(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def snapshot(con: duckdb.DuckDBPyConnection) -> dict:
    out: dict = {"commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                                          text=True).stdout.strip()}
    with tempfile.TemporaryDirectory() as d:
        path = workbook.build(con, Path(d) / "saas_operating_model.xlsx")
        wb = openpyxl.load_workbook(path)
        out["workbook"] = {ws.title: {c.coordinate: cell_value(c.value) for row in ws.iter_rows() for c in row
                                      if c.value is not None} for ws in wb.worksheets}
    out["memo_facts"] = memo.facts(con)
    long, _ = backtest.bridge(con)
    out["bridge"] = long.to_dict("records")
    sc = backtest.scorecard(con)
    out["scorecard"] = json.loads(sc.reset_index().to_json(orient="records", double_precision=15))
    out["tables"] = {t: json.loads(con.execute(f"SELECT * FROM {t} ORDER BY {k}").df()
                                   .to_json(orient="records", double_precision=15, date_format="iso"))
                     for t, k in TABLES.items()}
    return out


if __name__ == "__main__":
    snap = snapshot(fixture_db())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wt") as fh:
        json.dump(snap, fh, sort_keys=True)
    n_cells = sum(len(v) for v in snap["workbook"].values())
    print(f"{OUT.relative_to(ROOT)}: {n_cells} workbook cells, {len(snap['memo_facts'])} memo facts, "
          f"{len(snap['bridge'])} bridge rows, " + ", ".join(f"{t} {len(v)}" for t, v in snap["tables"].items()))
