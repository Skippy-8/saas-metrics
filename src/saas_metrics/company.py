"""Company configurations: configs/<TICKER>.yaml, loaded and validated.

Two kinds of config:
  benchmark-only  `company` + `benchmark` sections: enough for `saas_metrics benchmark` (XBRL comps)
  full model      also `sec`, `windows`, `backtest`, `lines`, `segments`, ... (MDB.yaml)
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

PACKAGE_DIR = Path(__file__).resolve().parent
SOURCE_ROOT = PACKAGE_DIR.parents[1]          # the repository root, when running from a source checkout


def _is_checkout() -> bool:
    return (SOURCE_ROOT / "pyproject.toml").is_file() and (SOURCE_ROOT / "src" / "saas_metrics").is_dir()


def _user_data_dir() -> Path:
    """The platform's per-user data folder for saas-metrics."""
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "saas-metrics"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")) / "saas-metrics"
    return Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share")) / "saas-metrics"


def work_root() -> Path:
    """Where downloads, the DuckDB store and generated files go: $SAAS_METRICS_HOME if set, the repository
    when running from a source checkout, otherwise the user data folder (never inside site-packages)."""
    if os.environ.get("SAAS_METRICS_HOME"):
        return Path(os.environ["SAAS_METRICS_HOME"]).expanduser().resolve()
    return SOURCE_ROOT if _is_checkout() else _user_data_dir()


ROOT = work_root()
# The bundled configs: inside the wheel at saas_metrics/configs, in a checkout at the repository's configs/.
CONFIG_DIR = PACKAGE_DIR / "configs" if (PACKAGE_DIR / "configs").is_dir() else SOURCE_ROOT / "configs"

BENCHMARK_REQUIRED = ("revenue", "cost_of_revenue", "rnd", "snm", "gna", "operating_income", "sbc", "cfo", "capex")
BENCHMARK_OPTIONAL = ("amortization",)
MODEL_SECTIONS = ("sec", "windows", "backtest", "lines", "segments")
MODEL_EXTRA = ("arr_proxy", "cohort_ledger", "unit_econ")   # required once a config is a model config
QUALITY_METRICS = ("deferred_revenue", "rpo", "sbc")
# The Income Statement and FCF lines every full model reads; revenue segments come on top (`segments`).
MODEL_LINES = ("revenue", "revenue_subscription", "cost_of_revenue", "cost_subscription", "cost_services",
               "gross_profit", "rnd", "snm", "gna", "opex_total", "operating_income", "sbc_total",
               "sbc_cost_subscription", "sbc_cost_services", "sbc_rnd", "sbc_snm", "sbc_gna",
               "amort_intangibles", "cfo", "capex")
_CONCEPT = re.compile(r"^[A-Za-z][\w-]*:[A-Za-z][\w]*$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Company:
    ticker: str
    name: str
    cik: int
    fiscal_year_end_month: int
    raw: dict = field(repr=False)
    path: Path | None = None

    @property
    def cik10(self) -> str:
        return f"{self.cik:010d}"

    @property
    def short_name(self) -> str:
        """Name used in workbook labels ("MongoDB")."""
        return self.raw["company"].get("short_name", self.name)

    @property
    def has_model(self) -> bool:
        return all(s in self.raw for s in MODEL_SECTIONS)

    def benchmark_lines(self) -> dict[str, list[str]]:
        """line -> list of concepts (summed); every line is a list, even a single concept."""
        lines = self.raw["benchmark"]["lines"]
        return {k: ([v] if isinstance(v, str) else list(v)) for k, v in lines.items()}

    def check_tags(self) -> dict[str, str]:
        return dict(self.raw.get("benchmark", {}).get("check_tags", {}))

    def notes(self) -> list[str]:
        return list(self.raw.get("benchmark", {}).get("notes", []))


def _date(s: str, where: str) -> date:
    try:
        return date.fromisoformat(str(s))
    except ValueError as e:
        raise ConfigError(f"{where}: {s!r} is not an ISO date") from e


def validate(raw: dict, where: str) -> None:
    c = raw.get("company")
    if not isinstance(c, dict):
        raise ConfigError(f"{where}: missing `company` section")
    for k, t in (("ticker", str), ("name", str), ("cik", int), ("fiscal_year_end_month", int)):
        if not isinstance(c.get(k), t):
            raise ConfigError(f"{where}: company.{k} must be {t.__name__}")
    if not 1 <= c["fiscal_year_end_month"] <= 12:
        raise ConfigError(f"{where}: company.fiscal_year_end_month must be 1-12")

    if "benchmark" in raw:
        lines = raw["benchmark"].get("lines", {})
        missing = [k for k in BENCHMARK_REQUIRED if k not in lines]
        unknown = [k for k in lines if k not in BENCHMARK_REQUIRED + BENCHMARK_OPTIONAL]
        if missing or unknown:
            raise ConfigError(f"{where}: benchmark.lines missing {missing}, unknown {unknown}")
        for k, v in lines.items():
            for concept in [v] if isinstance(v, str) else v:
                if not _CONCEPT.match(str(concept)):
                    raise ConfigError(f"{where}: benchmark.lines.{k}: {concept!r} is not a prefixed XBRL concept")
        for metric, entries in (raw["benchmark"].get("quality") or {}).items():
            if metric not in QUALITY_METRICS:
                raise ConfigError(f"{where}: benchmark.quality.{metric} must be one of {list(QUALITY_METRICS)}")
            for i, entry in enumerate(entries or []):
                if isinstance(entry, str):
                    concepts = [entry]
                elif isinstance(entry, dict):
                    concepts = entry.get("concepts") or entry.get("tags") or []
                else:
                    concepts = entry
                if not concepts:
                    raise ConfigError(f"{where}: benchmark.quality.{metric}[{i}] lists no concepts")
                for concept in concepts:
                    if not _CONCEPT.match(str(concept)):
                        raise ConfigError(f"{where}: benchmark.quality.{metric}[{i}]: {concept!r} is not a "
                                          "prefixed XBRL concept")

    present = [s for s in MODEL_SECTIONS if s in raw]
    if present and len(present) != len(MODEL_SECTIONS):
        raise ConfigError(f"{where}: a model config needs all of {MODEL_SECTIONS}; has {present}")
    if present:
        from saas_metrics import segments
        try:
            segs = segments.from_config(raw)
        except (KeyError, ValueError) as e:
            raise ConfigError(f"{where}: segments: {e}") from e
        missing = sorted({s.line for s in segs} - set(raw["lines"]))
        if missing:
            raise ConfigError(f"{where}: segments use lines not defined under `lines`: {missing}")
        missing = [ln for ln in MODEL_LINES if ln not in raw["lines"]]
        if missing:
            raise ConfigError(f"{where}: a model config must define the standard lines {missing}")
        for sec in MODEL_EXTRA:
            if sec not in raw:
                raise ConfigError(f"{where}: a model config needs a `{sec}` section")
    if present:
        w, b = raw["windows"], raw["backtest"]
        pre, first = _date(w["prewindow_from"], where), _date(w["first_quarter_end"], where)
        t0, last = _date(b["t0_quarter_end"], where), _date(w["last_quarter_end"], where)
        cutoff = _date(b["cutoff_date"], where)
        if not pre < first < t0 < last:
            raise ConfigError(f"{where}: windows must satisfy prewindow < first < t0 < last")
        if not t0 < cutoff < last:
            raise ConfigError(f"{where}: backtest.cutoff_date must fall after t0 and before the last quarter")
        from saas_metrics.fiscal import FiscalCalendar
        cal = FiscalCalendar(c["fiscal_year_end_month"])
        if not cal.is_quarter_end(t0) or cal.fiscal(t0)[1] != 2:
            # the guidance scorecard reads next-quarter guidance in Q3 and implies Q4 from full-year guidance
            raise ConfigError(f"{where}: backtest.t0_quarter_end must be a fiscal Q2 quarter end")


def load(ticker_or_path: str | Path) -> Company:
    p = Path(ticker_or_path)
    path = p if p.suffix in (".yaml", ".yml") else CONFIG_DIR / f"{str(ticker_or_path).upper()}.yaml"
    if not path.exists():
        raise ConfigError(f"no config at {path}; available: {', '.join(available())}")
    raw = yaml.safe_load(path.read_text())
    validate(raw, str(path.name))
    c = raw["company"]
    return Company(c["ticker"], c["name"], c["cik"], c["fiscal_year_end_month"], raw, path)


def available() -> list[str]:
    return sorted(p.stem for p in CONFIG_DIR.glob("*.yaml"))
