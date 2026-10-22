"""Bring-your-own billing data: QC, revenue waterfall, retention triangles, concentration, tier migration.

Input: any CSV or Parquet billing tape with the columns

    customer_id  text     required
    period       date     required  any date inside the period; normalised to the period end
    revenue      decimal  required  revenue for the period; several lines per customer-period are summed
    segment      text     optional
    channel      text     optional

Money is held as DECIMAL(18,2), so the waterfall identity is checked to the cent:

    Beginning + New + Reactivation + Expansion - Contraction - Churn = Ending

`saas_metrics cohorts --ledger MDB` runs the retention views on MongoDB's calibrated cohort ledger instead.
That ledger is cohort-level and identifies only net expansion, so it has no customer waterfall.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

from saas_metrics.fiscal import FiscalCalendar, month_end

SQL = Path(__file__).resolve().parent / "sql" / "cohort_engine"
REQUIRED = ("customer_id", "period", "revenue")
OPTIONAL = ("segment", "channel")
ANNUALIZE = {"month": 12, "quarter": 4}
DEFAULT_TIERS = (0, 10_000, 100_000, 1_000_000)   # lower bounds, annualised revenue


@dataclass
class Check:
    level: str        # "error" blocks the run (unless allowed); "warning" blocks unless allowed; "info" never
    name: str
    detail: str


@dataclass
class Result:
    checks: list[Check]
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def blocking(self) -> list[Check]:
        return [c for c in self.checks if c.level in ("error", "warning")]


class InputError(ValueError):
    pass


# ---- load and QC ---------------------------------------------------------------------------------
def load(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise InputError(f"{p} does not exist")
    con = duckdb.connect()
    if p.suffix == ".parquet":
        df = con.execute("SELECT * FROM read_parquet(?)", [str(p)]).df()
    elif p.name.endswith((".csv", ".csv.gz")):
        df = con.execute("SELECT * FROM read_csv(?, all_varchar = true, header = true)", [str(p)]).df()
    else:
        raise InputError("--input must be .csv, .csv.gz or .parquet")
    df.columns = [c.strip().lower() for c in df.columns]
    return df


def period_end(d: date, grain: str, cal: FiscalCalendar) -> date:
    me = month_end(d.year, d.month)
    if grain == "month":
        return me
    while not cal.is_quarter_end(me):
        me = month_end(me.year + (me.month == 12), me.month % 12 + 1)
    return me


def calendar_between(first: date, last: date, grain: str) -> list[date]:
    out, d = [], first
    step = 1 if grain == "month" else 3
    while d <= last:
        out.append(d)
        m = d.month + step
        d = month_end(d.year + (m - 1) // 12, (m - 1) % 12 + 1)
    return out


def qc(raw: pd.DataFrame, grain: str = "month", fy_end_month: int = 12, today: date | None = None
       ) -> tuple[list[Check], pd.DataFrame | None]:
    """Pre-flight checks. Returns (checks, normalised table or None if it cannot be built)."""
    checks: list[Check] = []
    missing = [c for c in REQUIRED if c not in raw.columns]
    if missing:
        return [Check("error", "schema", f"missing required column(s) {missing}; found {list(raw.columns)}")], None
    extra = [c for c in raw.columns if c not in REQUIRED + OPTIONAL]
    if extra:
        checks.append(Check("info", "schema", f"ignored column(s) {extra}"))
    df = raw.copy()
    for c in OPTIONAL:
        if c not in df.columns:
            df[c] = "(none)"
    df["customer_id"] = df["customer_id"].astype("string").str.strip()
    nulls = {c: int(df[c].isna().sum() + (df[c].astype("string").str.strip() == "").sum()) for c in REQUIRED}
    if any(nulls.values()):
        checks.append(Check("error", "nulls", f"empty required values: {nulls}"))
    dates = pd.to_datetime(df["period"], errors="coerce")
    revenue = pd.to_numeric(df["revenue"], errors="coerce")
    bad_d, bad_r = int(dates.isna().sum() - df["period"].isna().sum()), int(revenue.isna().sum() - df["revenue"].isna().sum())
    if bad_d or bad_r:
        checks.append(Check("error", "types", f"{bad_d} unparseable period(s), {bad_r} non-numeric revenue value(s)"))
    if any(c.level == "error" for c in checks):
        return checks, None

    cal = FiscalCalendar(fy_end_month)
    ends = {d: period_end(d, grain, cal) for d in set(dates.dt.date)}
    df["period_end"] = dates.dt.date.map(ends)
    df["revenue"] = revenue.round(2)
    lines = len(df)
    dup = int(df.assign(_d=dates.dt.date).duplicated(["customer_id", "_d"]).sum())
    if dup:
        checks.append(Check("info", "duplicate keys", f"{dup} line(s) repeat a customer and date (split invoices, "
                                                    "credits); summed"))
    norm = (df.groupby(["customer_id", "period_end"], as_index=False)
              .agg(revenue=("revenue", "sum"), segment=("segment", "last"), channel=("channel", "last")))
    if len(ends) > len(set(ends.values())):  # several input dates map to one period end
        checks.append(Check("info", "aggregation", f"{len(set(dates.dt.date))} distinct dates rolled up to "
                                                  f"{len(set(ends.values()))} {grain}-ends ({lines} lines -> "
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
    cal_ends = calendar_between(all_ends[0], all_ends[-1], grain)
    gaps = [d for d in cal_ends if d not in set(all_ends)]
    if gaps:
        checks.append(Check("warning", "missing periods", f"{len(gaps)} period(s) with no billing at all, e.g. {gaps[0]}"
                                                        " (would read as everyone churning)"))
    today = today or date.today()
    future = [d for d in all_ends if d > period_end(today, grain, cal)]
    if future:
        checks.append(Check("warning", "future periods", f"{len(future)} period(s) after today, e.g. {future[0]}"))
    never = norm.groupby("customer_id")["revenue"].max()
    never = never[never <= 0]
    if len(never):
        checks.append(Check("info", "never billed", f"{len(never)} customer(s) with no positive period are excluded"))
    checks.append(Check("info", "coverage", f"{norm.customer_id.nunique()} customers, {len(cal_ends)} {grain}s "
                                            f"({cal_ends[0]} to {cal_ends[-1]})"))
    norm.attrs["calendar"] = cal_ends
    return checks, norm


# ---- engine ---------------------------------------------------------------------------------------
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


def run(norm: pd.DataFrame, grain: str = "month", tiers=DEFAULT_TIERS, checks: list[Check] | None = None) -> Result:
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
    res = Result(checks or [])
    for name in ("waterfall", "waterfall_by_segment", "retention", "concentration", "tier_migration"):
        res.tables[name] = con.execute(f"SELECT * FROM {name}").df()
    clipped = norm[norm["revenue"] < 0]
    if len(clipped):
        res.notes.append(f"{len(clipped)} net-negative customer-period(s) counted as 0 "
                         f"(${-clipped.revenue.sum():,.2f} of credits)")
    return res


def analyze(path: str | Path, grain: str = "month", fy_end_month: int = 12, tiers=DEFAULT_TIERS,
            allow_warnings: bool = False, today: date | None = None) -> Result:
    checks, norm = qc(load(path), grain, fy_end_month, today)
    stop = [c for c in checks if c.level == "error" or (c.level == "warning" and not allow_warnings)]
    if norm is None or stop:
        return Result(checks)
    return run(norm, grain, tiers, checks)


def ledger(ticker: str = "MDB", db_path: Path | None = None) -> Result:
    """Retention views on the calibrated cohort ledger of a full-model company (cohort-level only)."""
    from saas_metrics import config

    with config.using(ticker) as co:
        db_path = db_path or config.DB_PATH
        cfg = co.path.relative_to(config.ROOT) if co.path.is_relative_to(config.ROOT) else co.path.name
    con = duckdb.connect(str(db_path), read_only=True)
    arr = con.execute("""SELECT cohort AS vintage, period_end, arr FROM cohort_arr WHERE vintage = 'latest'""").df()
    logos = con.execute("""SELECT cohort AS vintage, period_end, customers FROM cohort_logos WHERE vintage = 'latest'""").df()
    df = arr.merge(logos, on=["vintage", "period_end"])
    df["age"] = df.groupby("vintage")["period_end"].rank(method="first").astype(int) - 1
    start = df[df.age == 0].set_index("vintage")
    base = df["vintage"].map(start["arr"])
    df["ndr"] = (df["arr"] / base).where(base > 0)    # undefined for cohorts that start with $0 ARR
    df["logo_retention"] = df["customers"] / df["vintage"].map(start["customers"])
    res = Result([Check("info", "source", f"{ticker} calibrated synthetic cohort ledger (latest vintage)")])
    res.tables["retention"] = df.sort_values(["vintage", "age"]).reset_index(drop=True)
    res.notes.append("The ledger is cohort-level and identifies only net expansion: there is no customer-level "
                     "waterfall (new / expansion / contraction / churn) for it. Its cohorts share one expansion "
                     f"rate and one [Illustrative] churn rate ({cfg}) by construction, so their curves "
                     "are nearly identical: that is the calibration, not a finding.")
    zero = sorted(start.index[start["arr"] <= 0])
    if zero:
        res.notes.append(f"{len(zero)} cohort(s) start with $0 ARR (quarters where the calibration floors new-cohort "
                         f"ARR at zero: {', '.join(zero)}); their NDR is left blank.")
    return res


# ---- output ---------------------------------------------------------------------------------------
def write(res: Result, out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name, t in res.tables.items():
        p = out / f"{name}.csv"
        t.to_csv(p, index=False)
        written.append(p)
    if "waterfall" in res.tables:
        written.append(write_xlsx(res, out / "cohorts.xlsx"))
    return written


def write_xlsx(res: Result, path: Path) -> Path:
    """Waterfall with live formulas (Beginning links to the prior Ending; Ending is computed and checked
    against the data); the other views as value tables."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Waterfall"
    hdr = ["Period end", "Beginning", "New", "Reactivation", "Expansion", "Contraction", "Churn", "Ending",
           "Ending (data)", "Check"]
    for i, h in enumerate(hdr, start=1):
        c = ws.cell(1, i, h)
        c.font, c.fill = Font(bold=True), PatternFill("solid", fgColor="DDEBF7")
    w = res.tables["waterfall"]
    for r, row in enumerate(w.itertuples(), start=2):
        ws.cell(r, 1, row.period_end)
        ws.cell(r, 2, float(row.beginning) if r == 2 else f"=H{r - 1}")
        for col, val in zip("CDEFG", (row.new_logo, row.reactivation, row.expansion, row.contraction, row.churn),
                            strict=True):
            c = ws[f"{col}{r}"]
            c.value, c.font = float(val), Font(color="0000FF")
        ws[f"H{r}"] = f"=B{r}+C{r}+D{r}+E{r}-F{r}-G{r}"
        ws[f"I{r}"] = float(row.ending)
        ws[f"I{r}"].font = Font(color="0000FF")
        ws[f"J{r}"] = f"=H{r}-I{r}"
        for col in "BCDEFGHIJ":
            ws[f"{col}{r}"].number_format = '#,##0.00'
    for name, t in res.tables.items():
        if name == "waterfall":
            continue
        s = wb.create_sheet(name[:31])
        for i, h in enumerate(t.columns, start=1):
            s.cell(1, i, h).font = Font(bold=True)
        for r, row in enumerate(t.itertuples(index=False), start=2):
            for i, v in enumerate(row, start=1):
                s.cell(r, i, float(v) if hasattr(v, "as_tuple") else v)
    wb.save(path)
    return path
