"""The active company, paths, windows and dates. Everything period-related is decided here, once.

The full model and backtest run for one active company at a time. Its values live in
configs/<TICKER>.yaml; `activate()` loads them into the module-level names below (kept so code
reads `config.CUTOFF_DATE` etc.) and refreshes the derived definitions in `lines` and `kpis`.
`using()` switches company temporarily (tests). The CLI picks the company with `--config`.

Each company has its own data and outputs. Defaults: data/<TICKER>/raw, data/<TICKER>/saas_metrics.duckdb,
models/<ticker>_operating_model.xlsx, docs/sources_<ticker>.md; a config's `paths:` section overrides
them (MDB.yaml keeps the original locations).
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from saas_metrics import company as _company

ROOT = _company.ROOT                 # the work root: see company.work_root()
PACKAGE_DIR = Path(__file__).resolve().parent
SQL_DIR = PACKAGE_DIR / "sql"
RAW: Path                  # cached SEC files
DB_PATH: Path              # the DuckDB store
WORKBOOK: Path             # the Excel model
SOURCES: Path              # the generated sources page

ACTIVE: _company.Company
TICKER: str
CIK: int
CIK10: str
FIRST_QUARTER_END: str     # first of the model's quarters (MDB: Q3 FY23)
PREWINDOW_FROM: str        # earlier quarters used only for trailing-12-month sums
LAST_QUARTER_END: str      # last test quarter (MDB: Q2 FY27)
T0_QUARTER_END: str        # the forecast is frozen here (MDB: Q2 FY26)
CUTOFF_DATE: str           # nothing filed later may feed the forecast
FILINGS_FROM: str
EIGHT_K_FROM: str
EIGHT_K_TO: str


def activate(c: _company.Company | str | Path) -> _company.Company:
    """Make `c` the active full-model company (a Company, a ticker, or a path to a YAML)."""
    global ACTIVE, TICKER, CIK, CIK10, FIRST_QUARTER_END, PREWINDOW_FROM, LAST_QUARTER_END
    global T0_QUARTER_END, CUTOFF_DATE, FILINGS_FROM, EIGHT_K_FROM, EIGHT_K_TO, RAW, DB_PATH, WORKBOOK, SOURCES
    c = c if isinstance(c, _company.Company) else _company.load(c)
    if not c.has_model:
        raise _company.ConfigError(f"{c.ticker}: not a full-model config (benchmark only)")
    raw = c.raw
    ACTIVE, TICKER, CIK, CIK10 = c, c.ticker, c.cik, c.cik10
    FIRST_QUARTER_END = str(raw["windows"]["first_quarter_end"])
    PREWINDOW_FROM = str(raw["windows"]["prewindow_from"])
    LAST_QUARTER_END = str(raw["windows"]["last_quarter_end"])
    T0_QUARTER_END = str(raw["backtest"]["t0_quarter_end"])
    CUTOFF_DATE = str(raw["backtest"]["cutoff_date"])
    FILINGS_FROM = str(raw["sec"]["filings_from"])
    EIGHT_K_FROM = str(raw["sec"]["eight_k_from"])
    EIGHT_K_TO = str(raw["sec"]["eight_k_to"])
    paths = raw.get("paths", {})
    t = c.ticker
    RAW = ROOT / paths.get("raw", f"data/{t}/raw")
    DB_PATH = ROOT / paths.get("store", f"data/{t}/saasfpa.duckdb")
    WORKBOOK = ROOT / paths.get("workbook", f"models/{t.lower()}_operating_model.xlsx")
    SOURCES = ROOT / paths.get("sources", f"docs/sources_{t.lower()}.md")
    from saas_metrics import kpis, lines  # derived definitions follow the active company
    lines.refresh()
    kpis.refresh()
    return c


@contextmanager
def using(c: _company.Company | str | Path):
    prev = ACTIVE
    try:
        yield activate(c)
    finally:
        activate(prev)


activate("MDB")
