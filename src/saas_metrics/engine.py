"""Pre-flight checks, the SQL engine (DuckDB, money as DECIMAL(18,2)), and the headline metrics.

Definitions are written out in docs/definitions.md; the tests hold the code to them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from saas_metrics.calendar import ANNUALIZE, period_end, periods_between
from saas_metrics.inputs import prepare

SQL = Path(__file__).parent / "sql" / "billing"
REQUIRED = ("customer_id", "period", "revenue")
OPTIONAL = ("segment", "channel")
DEFAULT_TIERS = (0, 10_000, 100_000, 1_000_000)   # lower bounds, annualised revenue


@dataclass
class Check:
    level: str        # "error" always blocks; "warning" blocks unless allowed; "info" never blocks
    name: str
    detail: str


@dataclass
class Report:
    checks: list[Check]
    grain: str = "month"
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.tables)

    @property
    def blocking(self) -> list[Check]:
        return [c for c in self.checks if c.level in ("error", "warning")]

    def __getattr__(self, name: str) -> pd.DataFrame:      # report.waterfall, report.metrics, ...
        tables = self.__dict__.get("tables", {})
        if name in tables:
            return tables[name]
        raise AttributeError(name)

    def headline(self) -> dict[str, float]:
        """ARR at the start and end of the last period, and current MRR (ending ARR / 12, whatever the grain)."""
        k = ANNUALIZE[self.grain]
        ending_arr = float(self.metrics.iloc[-1]["arr"])
        return {"starting_arr": float(self.waterfall.iloc[-1]["beginning"]) * k, "ending_arr": ending_arr,
                "current_mrr": ending_arr / 12}

    def to_csv(self, folder: str | Path) -> list[Path]:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        out = []
        for name, t in self.tables.items():
            t.to_csv(folder / f"{name}.csv", index=False)
            out.append(folder / f"{name}.csv")
        return out

    def to_excel(self, path: str | Path, **kw) -> Path:
        from saas_metrics.excel import write
        return write(self, Path(path), **kw)


# ---- pre-flight checks ---------------------------------------------------------------------------
def qc(lines: pd.DataFrame, grain: str = "month", fy_end_month: int = 12, today: date | None = None
       ) -> tuple[list[Check], pd.DataFrame | None]:
    """Returns (checks, one row per customer-period) or (checks, None) if a table cannot be built."""
    checks: list[Check] = []
    missing = [c for c in REQUIRED if c not in lines.columns]
    if missing:
        return [Check("error", "schema", f"missing required column(s) {missing}; found {list(lines.columns)}. "
                                         "Map your columns with --customer/--date/--amount (or mapping=...)")], None
    df = lines.copy()
    for c in OPTIONAL:
        if c not in df.columns:
            df[c] = "(none)"
    df["segment"], df["channel"] = df["segment"].fillna("(none)"), df["channel"].fillna("(none)")
    df["customer_id"] = df["customer_id"].astype("string").str.strip()
    nulls = {c: int(df[c].isna().sum() + (df[c].astype("string").str.strip() == "").sum()) for c in REQUIRED}
    if any(nulls.values()):
        checks.append(Check("error", "nulls", f"empty required values: {nulls}"))
    dates = pd.to_datetime(df["period"], errors="coerce")
    revenue = pd.to_numeric(df["revenue"].astype("string").str.replace(r"[,$]", "", regex=True), errors="coerce")
    bad_d = int(dates.isna().sum() - df["period"].isna().sum())
    bad_r = int(revenue.isna().sum() - df["revenue"].isna().sum())
    if bad_d or bad_r:
        checks.append(Check("error", "types", f"{bad_d} unparseable period(s), {bad_r} non-numeric revenue value(s)"))
    if any(c.level == "error" for c in checks):
        return checks, None

    ends = {d: period_end(d, grain, fy_end_month) for d in set(dates.dt.date)}
    df["period_end"] = dates.dt.date.map(ends)
    df["revenue"] = revenue.round(2)
    dup = int(df.assign(_d=dates.dt.date).duplicated(["customer_id", "_d"]).sum())
    if dup:
        checks.append(Check("info", "duplicate keys", f"{dup} line(s) repeat a customer and date (split invoices, "
                                                    "credits); summed"))
    norm = (df.groupby(["customer_id", "period_end"], as_index=False)
              .agg(revenue=("revenue", "sum"), segment=("segment", "last"), channel=("channel", "last")))
    norm["revenue"] = norm["revenue"].round(2)
    if len(ends) > len(set(ends.values())):
        checks.append(Check("info", "aggregation", f"{len(set(dates.dt.date))} distinct dates rolled up to "
                                                  f"{len(set(ends.values()))} {grain}-ends ({len(df)} lines -> "
                                                  f"{len(norm)} customer-periods)"))
    neg_lines = int((df["revenue"] < 0).sum())
    if neg_lines:
        checks.append(Check("info", "negative lines", f"{neg_lines} negative line(s) (credits) netted within their "
                                                      "customer-period"))
    net_neg = norm[norm["revenue"] < 0]
    if len(net_neg):
        checks.append(Check("warning", "negative revenue", f"{len(net_neg)} customer-period(s) net negative, e.g. "
                                                         f"{net_neg.iloc[0].customer_id} {net_neg.iloc[0].period_end}"))
    all_ends = sorted(norm["period_end"].unique())
    cal = periods_between(all_ends[0], all_ends[-1], grain)
    gaps = [d for d in cal if d not in set(all_ends)]
    if gaps:
        checks.append(Check("warning", "missing periods", f"{len(gaps)} period(s) with no billing at all, e.g. "
                                                        f"{gaps[0]} (would read as everyone churning)"))
    today = today or date.today()
    future = [d for d in all_ends if d > period_end(today, grain, fy_end_month)]
    if future:
        checks.append(Check("warning", "future periods", f"{len(future)} period(s) after today, e.g. {future[0]}"))
    never = norm.groupby("customer_id")["revenue"].max()
    never = never[never <= 0]
    if len(never):
        checks.append(Check("info", "never billed", f"{len(never)} customer(s) with no positive period are excluded"))
    checks.append(Check("info", "coverage", f"{norm.customer_id.nunique()} customers, {len(cal)} {grain}s "
                                            f"({cal[0]} to {cal[-1]})"))
    norm.attrs["calendar"] = cal
    return checks, norm


# ---- engine ----------------------------------------------------------------------------------------
def _tiers(bounds) -> pd.DataFrame:
    b = sorted(set(float(x) for x in bounds))
    if b[0] != 0:
        b = [0.0, *b]

    def fmt(v: float) -> str:
        return f"${v / 1e6:g}M" if v >= 1e6 else f"${v / 1e3:g}k" if v >= 1e3 else f"${v:g}"
    labels = [f"<{fmt(b[1])}" if len(b) > 1 else "all"]
    labels += [f"{fmt(b[i])}-{fmt(b[i + 1])}" for i in range(1, len(b) - 1)]
    if len(b) > 1:
        labels.append(f">={fmt(b[-1])}")
    return pd.DataFrame({"lower": b, "label": labels, "ord": range(len(b))})


def _sql_tables(norm: pd.DataFrame, grain: str, tiers) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    con = duckdb.connect()
    n = norm.copy()
    n["revenue"] = n["revenue"].clip(lower=0)      # net-negative customer-periods only reach here if allowed
    cal = pd.DataFrame({"period_end": norm.attrs["calendar"]})
    cal["pidx"] = range(1, len(cal) + 1)
    con.register("_n", n)
    con.execute("CREATE TABLE billing_norm AS SELECT customer_id, period_end::DATE AS period_end, "
                "revenue::DECIMAL(18, 2) AS revenue, segment, channel FROM _n")
    con.register("_c", cal)
    con.execute("CREATE TABLE calendar AS SELECT period_end::DATE AS period_end, pidx FROM _c")
    con.register("_t", _tiers(tiers))
    con.execute("CREATE TABLE tiers AS SELECT lower::DECIMAL(18, 2) AS lower, label, ord FROM _t")
    for f in sorted(SQL.glob("*.sql")):
        con.execute(f.read_text().replace("{ANNUALIZE}", str(ANNUALIZE[grain])))
    tables = {name: con.execute(f"SELECT * FROM {name}").df()
              for name in ("waterfall", "waterfall_by_segment", "waterfall_by_channel", "retention",
                           "concentration", "tier_migration")}
    grid = con.execute("SELECT customer_id, period_end, revenue FROM grid").df()
    return tables, grid


def customer_matrix(grid: pd.DataFrame, calendar: list) -> pd.DataFrame:
    """Customers x periods, revenue in each (0 where none), in calendar order."""
    m = grid.pivot_table(index="customer_id", columns="period_end", values="revenue", aggfunc="sum", fill_value=0)
    m = m.reindex(columns=pd.to_datetime(pd.Series(calendar)).dt.date, fill_value=0)
    m.columns = [pd.Timestamp(c).date() for c in m.columns]
    return m.astype(float)


def metrics(waterfall: pd.DataFrame, matrix: pd.DataFrame, grain: str) -> pd.DataFrame:
    """Headline metrics per period (definitions in docs/definitions.md)."""
    k = ANNUALIZE[grain]                       # 12 months or 4 quarters = one year back
    w = waterfall.reset_index(drop=True)
    out = pd.DataFrame({"period_end": w["period_end"]})
    f = lambda c: w[c].astype(float)          # noqa: E731
    out["revenue"] = f("ending")
    out["arr"] = f("ending") * k
    out["active_customers"] = w["active_logos"]
    out["arpa"] = np.where(w["active_logos"] > 0, f("ending") / w["active_logos"].replace(0, np.nan), np.nan)
    gained = f("new_logo") + f("reactivation") + f("expansion")
    lost = f("contraction") + f("churn")
    out["quick_ratio"] = np.where(lost > 0, gained / lost.replace(0, np.nan), np.nan)
    prev_active = w["active_logos"].shift(1)
    out["logo_churn_rate"] = np.where(prev_active > 0, w["churned_logos"] / prev_active, np.nan)
    beg = f("beginning")
    out["gross_revenue_churn_rate"] = np.where(beg > 0, lost / beg.replace(0, np.nan), np.nan)
    cols = list(matrix.columns)
    nrr, grr, logo = [], [], []
    for i in range(len(cols)):
        if i < k:
            values = (np.nan, np.nan, np.nan)
        else:
            base, now = matrix[cols[i - k]], matrix[cols[i]]
            in_base = base > 0
            b = base[in_base].sum()
            values = (now[in_base].sum() / b if b else np.nan,
                      np.minimum(now[in_base], base[in_base]).sum() / b if b else np.nan,
                      (now[in_base] > 0).sum() / in_base.sum() if in_base.sum() else np.nan)
        for lst, v in zip((nrr, grr, logo), values, strict=True):
            lst.append(v)
    out["nrr_12m"], out["grr_12m"], out["logo_retention_12m"] = nrr, grr, logo
    return out


def analyze(source, mapping: dict[str, str] | None = None, grain: str = "month", fy_end_month: int = 12,
            tiers=DEFAULT_TIERS, allow_warnings: bool = False, today: date | None = None, as_of=None) -> Report:
    """Billing export (path or DataFrame) -> Report. Nothing is computed if a check blocks.

    as_of: recognise spread (service-period) revenue only through this date; default the end of the month of
    the export's latest line."""
    if grain not in ANNUALIZE:
        raise ValueError(f"grain must be one of {list(ANNUALIZE)}")
    lines, notes = prepare(source, mapping, as_of)
    checks, norm = qc(lines, grain, fy_end_month, today)
    checks = [Check("info", "spreading", n) for n in notes] + checks
    stop = [c for c in checks if c.level == "error" or (c.level == "warning" and not allow_warnings)]
    if norm is None or stop:
        return Report(checks, grain)
    tables, grid = _sql_tables(norm, grain, tiers)
    matrix = customer_matrix(grid, norm.attrs["calendar"])
    tables["metrics"] = metrics(tables["waterfall"], matrix, grain)
    rep = Report(checks, grain, tables)
    rep.tables["customers"] = matrix.reset_index()
    clipped = norm[norm["revenue"] < 0]
    if len(clipped):
        rep.notes.append(f"{len(clipped)} net-negative customer-period(s) counted as 0 "
                         f"(${-clipped.revenue.sum():,.2f} of credits)")
    return rep
