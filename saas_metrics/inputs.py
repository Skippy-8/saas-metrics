"""Read a billing export, map its columns, and (optionally) spread invoices over their service periods.

Canonical line columns:
    customer_id    required
    period         a date inside the period the line belongs to (required unless service dates are given)
    revenue        amount, in currency units; credits are negative
    service_start  optional: first day of the service the line pays for
    service_end    optional: last day of that service (inclusive)
    segment, channel  optional grouping attributes

Why spreading matters: an annual plan invoiced once looks like a customer who arrives in January and
churns in February. With service dates, each line is recognised evenly by day across its service period
(ratable), in whole cents, so the lines still sum exactly to what was billed.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import duckdb
import pandas as pd

from saas_metrics.calendar import month_end

CANONICAL = ("customer_id", "period", "revenue", "service_start", "service_end", "segment", "channel")


class InputError(ValueError):
    pass


def read(source: str | Path | pd.DataFrame) -> pd.DataFrame:
    """A DataFrame, or a .csv / .csv.gz / .parquet / .xlsx path, read with every value as text."""
    if isinstance(source, pd.DataFrame):
        return source.astype(object).where(source.notna(), None).astype("string")
    p = Path(source)
    if not p.exists():
        raise InputError(f"{p} does not exist")
    if p.suffix == ".parquet":
        df = duckdb.connect().execute("SELECT * FROM read_parquet(?)", [str(p)]).df()
    elif p.name.endswith((".csv", ".csv.gz")):
        df = duckdb.connect().execute("SELECT * FROM read_csv(?, all_varchar = true, header = true)", [str(p)]).df()
    elif p.suffix == ".xlsx":
        df = pd.read_excel(p, dtype=str)
    else:
        raise InputError("input must be .csv, .csv.gz, .parquet, .xlsx or a DataFrame")
    return df.astype("string")


def apply_mapping(df: pd.DataFrame, mapping: dict[str, str] | None = None) -> pd.DataFrame:
    """Rename source columns to canonical names. `mapping` is canonical -> source column (case-insensitive).
    Columns already named canonically are kept as they are."""
    lower = {c.strip().lower(): c for c in df.columns}
    out = pd.DataFrame(index=df.index)
    mapping = {k: v for k, v in (mapping or {}).items() if v}
    unknown = sorted(set(mapping) - set(CANONICAL))
    if unknown:
        raise InputError(f"unknown target field(s) {unknown}; use {list(CANONICAL)}")
    for name in CANONICAL:
        src = mapping.get(name, name)
        col = lower.get(src.strip().lower())
        if col is None:
            if name in mapping:
                raise InputError(f"column {src!r} (for {name}) not found; columns are {list(df.columns)}")
            continue
        out[name] = df[col]
    return out


def _cents(v) -> int:
    return int(round(float(v) * 100))


def spread(lines: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Spread lines that have service_start and service_end evenly by day across the months they cover.
    Returns (lines with a 'period' per piece, number of lines spread). Other lines are unchanged."""
    if "service_start" not in lines or "service_end" not in lines:
        return lines, 0
    s = pd.to_datetime(lines["service_start"], errors="coerce")
    e = pd.to_datetime(lines["service_end"], errors="coerce")
    has = s.notna() & e.notna()
    if not has.any():
        return lines, 0
    if (e[has] < s[has]).any():
        bad = lines[has & (e < s)].iloc[0]
        raise InputError(f"service_end before service_start for customer {bad['customer_id']}")
    rest = lines[~has]
    pieces = []
    for (_, row), start, end in zip(lines[has].iterrows(), s[has].dt.date, e[has].dt.date, strict=True):
        total = _cents(row["revenue"])
        days = (end - start).days + 1
        months, d = [], start
        while d <= end:
            me = month_end(d.year, d.month)
            seg_end = min(me, end)
            months.append((me, (seg_end - d).days + 1))
            d = seg_end + timedelta(days=1)
        sign, amount = (1 if total >= 0 else -1), abs(total)
        base = [amount * n // days for _, n in months]
        frac = sorted(range(len(months)), key=lambda i: (-(amount * months[i][1] % days), i))
        for i in frac[: amount - sum(base)]:          # largest remainders get the leftover cents
            base[i] += 1
        for (me, _), c in zip(months, base, strict=True):
            pieces.append({**row.to_dict(), "period": me.isoformat(), "revenue": f"{sign * c / 100:.2f}"})
    out = pd.concat([rest, pd.DataFrame(pieces, columns=lines.columns)], ignore_index=True)
    return out, int(has.sum())


def prepare(source, mapping: dict[str, str] | None = None, as_of=None) -> tuple[pd.DataFrame, list[str]]:
    """read -> map -> spread. Returns (canonical lines, notes).

    Spread revenue is recognised only through `as_of` (default: the end of the month of the latest line in
    the export). Service after that date is deferred revenue, not yet earned, and is left out and reported.
    """
    lines = apply_mapping(read(source), mapping)
    notes = []
    if "period" not in lines and not {"service_start", "service_end"} <= set(lines.columns):
        raise InputError("need a period column, or service_start and service_end")
    if "period" not in lines:
        lines["period"] = lines["service_start"]
    dates = pd.to_datetime(lines["period"], errors="coerce")
    if as_of is None and dates.notna().any():
        last = dates.max().date()
        as_of = month_end(last.year, last.month)
    lines, n = spread(lines)
    if n:
        notes.append(f"{n} line(s) spread evenly by day over their service periods (ratable)")
    if as_of is not None:
        as_of = pd.Timestamp(as_of).date()
        when = pd.to_datetime(lines["period"], errors="coerce").dt.date
        later = when > as_of
        if later.any():
            deferred = pd.to_numeric(lines.loc[later, "revenue"], errors="coerce").sum()
            notes.append(f"{deferred:,.2f} of spread revenue falls after {as_of} (deferred, not yet earned) "
                         "and is excluded")
            lines = lines[~later]
    return lines.drop(columns=[c for c in ("service_start", "service_end") if c in lines]), notes

