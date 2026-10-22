"""FX normalisation: turn a multi-currency ledger into one constant-currency ledger.

The rate convention is the one a treasury team would write on a whiteboard: `rate(currency)` is the
number of `base` units one unit of `currency` buys, so `x USD = x * rate(EUR)` and `rate(USD) = 1`.
A rate table is any mapping from an ISO-4217 code to that number — a period-average table is the
right choice for revenue recognition, a spot table for a point-in-time ARR view.

Nothing is assumed. A currency with no rate is an error by default: silently converting at 1.0 would
understate or overstate revenue by the size of the FX move, and the whole point of the adapter layer
is that a number either has a source or is flagged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Protocol, runtime_checkable

import pandas as pd

from saas_metrics.contracts import MONEY, Diagnostic


@runtime_checkable
class FXConverter(Protocol):
    """Anything that can say what one unit of a currency is worth in the base currency."""

    @property
    def base(self) -> str: ...

    def rate(self, currency: str, as_of: date | None = None) -> Decimal: ...


@dataclass
class StaticFXProvider:
    """A rates table held in memory: `rates["EUR"] = 1.08` means 1 EUR = 1.08 USD."""
    rates: dict[str, Decimal]
    base_currency: str = "USD"

    def __post_init__(self) -> None:
        self.rates = {str(k).strip().upper(): Decimal(str(v)) for k, v in self.rates.items()}
        self.base_currency = self.base_currency.strip().upper()

    @property
    def base(self) -> str:
        return self.base_currency

    def rate(self, currency: str, as_of: date | None = None) -> Decimal:
        c = currency.strip().upper()
        if c == self.base_currency:
            return Decimal(1)
        if c not in self.rates:
            raise KeyError(c)
        return self.rates[c]

    def known(self) -> set[str]:
        return set(self.rates) | {self.base_currency}


def load_rates(source: str | Path | pd.DataFrame | dict, *, currency_col: str = "currency",
               rate_col: str = "rate") -> StaticFXProvider:
    """Read a rate table from a dict, a DataFrame, or a CSV/Parquet path with currency and rate columns."""
    if isinstance(source, StaticFXProvider):
        return source
    if isinstance(source, dict):
        rates = source
    else:
        df = source if isinstance(source, pd.DataFrame) else pd.read_csv(source)
        cols = {c.strip().lower(): c for c in df.columns}
        if currency_col not in cols or rate_col not in cols:
            raise ValueError(f"rate table needs columns {currency_col!r} and {rate_col!r}; found {list(df.columns)}")
        rates = dict(zip(df[cols[currency_col]], df[cols[rate_col]], strict=True))
    return StaticFXProvider({str(k): Decimal(str(v)) for k, v in rates.items()})


@dataclass
class FXReport:
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.level == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for d in self.diagnostics:
            counts[d.code] = counts.get(d.code, 0) + 1
        return ", ".join(f"{k} x{v}" for k, v in sorted(counts.items())) or "no diagnostics"


def convert(frame: pd.DataFrame, fx: FXConverter | None, *, target: str = "USD",
            on_missing: str = "error", amount_col: str = "mrr_amount",
            total_col: str = "recognized_amount", currency_col: str = "currency") -> tuple[pd.DataFrame, FXReport]:
    """Convert `amount_col` and `total_col` to `target` currency at `fx`, keeping the originals.

    `on_missing`: "error" (default, fail closed), "keep" (leave the row in its own currency, flagged),
    or "drop" (remove the row, flagged). The original amount is always preserved in
    `<col>_local`, and `source_currency` records what it was.
    """
    report = FXReport()
    out = frame.copy()
    if currency_col not in out.columns:
        report.diagnostics.append(Diagnostic("error", "missing_currency_column", "no currency column to convert"))
        return out, report
    target = target.strip().upper()
    if fx is None:
        non_target = out[currency_col].astype("string").str.upper().ne(target)
        if non_target.any():
            report.diagnostics.append(Diagnostic(
                "error", "no_fx_provider",
                f"{int(non_target.sum())} row(s) are not in {target} and no FX provider was given; pass "
                f"fx=StaticFXProvider({{...}}) or set the currency explicitly", field=currency_col))
        out["source_currency"] = out[currency_col]
        return out, report
    if fx.base.strip().upper() != target:
        raise ValueError(f"FX provider is based in {fx.base}, not {target}")

    amounts = out[amount_col].tolist() if amount_col in out.columns else [None] * len(out)
    totals = out[total_col].tolist() if total_col in out.columns else [None] * len(out)
    keep, applied = [], []
    missing: list[str] = []
    for cur in out[currency_col].astype("string").str.strip().str.upper():
        if cur == target:
            keep.append(True)
            applied.append(Decimal(1))
            continue
        try:
            rate = fx.rate(cur)
        except KeyError:
            missing.append(cur)
            if on_missing == "keep":
                keep.append(True)
                applied.append(Decimal(1))
            elif on_missing == "error":
                keep.append(False)
                applied.append(Decimal(1))      # leave the amount untouched: the error blocks the ledger
            else:                               # "drop"
                keep.append(False)
                applied.append(None)
        else:
            keep.append(True)
            applied.append(rate)
    if missing and on_missing == "error":
        codes = sorted(set(missing))
        report.diagnostics.append(Diagnostic(
            "error", "unconverted_currency",
            f"no {target} rate for {codes} ({len(missing)} row(s)); add them to the rate table or pass "
            f"on_missing='keep' to leave those rows in their own currency", field=currency_col))
    if missing and on_missing == "keep":
        codes = sorted(set(missing))
        report.diagnostics.append(Diagnostic(
            "warning", "unconverted_currency",
            f"{len(missing)} row(s) in {codes} were left unconverted (no {target} rate); they mix currencies in one "
            "ledger", field=currency_col))

    out["fx_rate"] = [r for r in applied]
    out["source_currency"] = out[currency_col]
    out[amount_col] = [
        None if (a is None or r is None) else (Decimal(str(a)) * r).quantize(MONEY)
        for a, r in zip(amounts, applied, strict=True)]
    out[total_col] = [
        None if (t is None or r is None) else (Decimal(str(t)) * r).quantize(MONEY)
        for t, r in zip(totals, applied, strict=True)]
    out[currency_col] = target
    if any(not k for k in keep):
        dropped = sum(1 for k in keep if not k)
        if on_missing == "drop":
            report.diagnostics.append(Diagnostic(
                "warning", "dropped_unconverted_rows", f"{dropped} row(s) dropped: no {target} rate", field=currency_col))
            out = out[keep].reset_index(drop=True)
    else:
        converted = sum(1 for r in applied if r is not None and r != Decimal(1))
        if converted:
            report.diagnostics.append(Diagnostic(
                "info", "fx_converted", f"{converted} row(s) converted to {target} at the supplied rates"))
    return out, report
