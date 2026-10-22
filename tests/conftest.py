"""Offline fixtures: the store is rebuilt in memory from parsed public SEC tables in tests/fixtures."""
from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from saas_metrics import build

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def source_tables() -> dict:
    con = duckdb.connect()
    return {t: con.execute(f"SELECT * FROM '{FIX / (t + '.parquet')}'").df() for t in build.SOURCE_TABLES}


@pytest.fixture(scope="session")
def db(source_tables) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    build.assemble(con, source_tables)
    return con


@pytest.fixture(scope="session")
def companyfacts_path() -> Path:
    return FIX / "companyfacts_subset.json.gz"
