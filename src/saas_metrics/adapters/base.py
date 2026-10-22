"""The adapter pipeline: raw export -> canonical ledger -> engine input or DuckDB.

Every adapter (`from_stripe_export`, `from_chargebee_export`, `from_salesforce_opportunities`) runs
the same six steps, in this order, so the differences between a Stripe CSV and a Salesforce report
are confined to the column aliases and the money/date conventions:

    read -> map columns -> validate (contracts.py) -> FX to one currency -> bridge grace gaps
         -> classify contract steps -> flag proration

What comes out is a `Ledger`: the canonical frame plus every diagnostic raised on the way. Nothing is
dropped silently and no amount is invented. `Ledger.to_engine_input()` turns it into the
`customer_id / period / revenue` tape the waterfall engine already consumes, and
`Ledger.to_duckdb()` writes it with the same `DECIMAL(18,2)` money the engine checks to the cent.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from saas_metrics.adapters import grace as grace_mod
from saas_metrics.adapters import proration as proration_mod
from saas_metrics.adapters import ramps as ramps_mod
from saas_metrics.adapters import stitching as stitching_mod
from saas_metrics.contracts import (
    ContractError,
    Diagnostic,
    ValidationReport,
    validate_ledger,
)
from saas_metrics.fx import FXConverter
from saas_metrics.fx import convert as fx_convert

LEDGER_COLUMNS = ("customer_id", "contract_id", "invoice_date", "service_start", "service_end", "mrr_amount",
                  "currency", "product_tier", "recognized_amount", "term_months", "expansion_kind",
                  "is_contracted_expansion", "contract_source", "stitched", "co_term_end", "co_terminated",
                  "source_currency", "fx_rate", "grace_bridged", "gap_days", "prorated", "channel",
                  "source_adapter", "source_row")

_MONEY_COLUMNS = ("mrr_amount", "recognized_amount")
_CASTS = {
    "invoice_date": "DATE", "service_start": "DATE", "service_end": "DATE", "co_term_end": "DATE",
    "mrr_amount": "DECIMAL(18, 2)", "recognized_amount": "DECIMAL(18, 2)",
    "term_months": "INTEGER", "gap_days": "INTEGER", "source_row": "INTEGER",
    "fx_rate": "DECIMAL(18, 6)", "grace_bridged": "BOOLEAN", "prorated": "BOOLEAN",
    "is_contracted_expansion": "BOOLEAN", "stitched": "BOOLEAN", "co_terminated": "BOOLEAN",
}


def read_source(source: str | Path | pd.DataFrame) -> pd.DataFrame:
    """A DataFrame, or a .csv / .csv.gz / .parquet / .xlsx path, read with every value as text."""
    from saas_metrics.inputs import read

    return read(source)


def _lower(df: pd.DataFrame) -> dict[str, str]:
    return {str(c).strip().lower(): c for c in df.columns}


def pick(df: pd.DataFrame, names: tuple[str, ...]) -> str | None:
    """The first of `names` present in `df`, case-insensitively (also matching snake_case variants)."""
    lower = _lower(df)
    for n in names:
        if n.strip().lower() in lower:
            return lower[n.strip().lower()]
        snake = re.sub(r"[^a-z0-9]+", "_", n.strip().lower()).strip("_")
        if snake in lower:
            return lower[snake]
    return None


def rename_columns(df: pd.DataFrame, aliases: dict[str, tuple[str, ...]],
                   mapping: dict[str, str] | None = None) -> tuple[pd.DataFrame, list[str]]:
    """Rename source columns onto canonical names. Returns (frame, notes) with a note per column used.

    `aliases` is canonical -> candidate source names. `mapping` (canonical -> source column) wins over
    the aliases, which is how a caller overrides a guess for an unusual export.
    """
    out = df.copy()
    lower = _lower(out)
    notes: list[str] = []
    explicit = {k: v for k, v in (mapping or {}).items() if v}
    unknown = sorted(set(explicit) - set(LEDGER_COLUMNS) - {"source_kind"})
    if unknown:
        raise ContractError(f"unknown target field(s) {unknown}; use {list(LEDGER_COLUMNS)}")
    for canonical in (*LEDGER_COLUMNS, "source_kind"):
        if canonical in out.columns and canonical not in explicit:
            continue
        src = explicit.get(canonical)
        if src is not None:
            if src.strip().lower() not in lower:
                raise ContractError(f"column {src!r} (for {canonical}) not found; columns are {list(df.columns)}")
            out = out.rename(columns={lower[src.strip().lower()]: canonical})
            notes.append(f"{canonical} <- {lower[src.strip().lower()]!r} (explicit)")
            continue
        found = pick(out, aliases.get(canonical, ()))
        if found is not None and found != canonical:
            out = out.rename(columns={found: canonical})
            notes.append(f"{canonical} <- {found!r}")
    return out, notes


@dataclass
class Ledger:
    """A canonical billing ledger with everything learned while building it."""
    frame: pd.DataFrame
    report: ValidationReport = field(default_factory=ValidationReport)
    adapter: str = "canonical"
    source: str | None = None
    notes: list[str] = field(default_factory=list)
    fx_report: Any | None = None
    grace_report: Any | None = None
    ramp_report: Any | None = None
    stitch_report: Any | None = None

    # ---- status ---------------------------------------------------------------------------------
    @property
    def ok(self) -> bool:
        return not self.blocking

    @property
    def blocking(self) -> list[Diagnostic]:
        bad = list(self.report.blocking)
        if self.fx_report is not None:
            bad += list(self.fx_report.errors)
        return bad

    @property
    def diagnostics(self) -> list[Diagnostic]:
        out = list(self.report.diagnostics)
        if self.fx_report is not None:
            out += list(self.fx_report.diagnostics)
        return out

    def all_notes(self) -> list[str]:
        out = [*self.notes, *self.report.notes]
        if self.grace_report is not None:
            out += self.grace_report.notes()
        if self.ramp_report is not None:
            out += self.ramp_report.notes()
        return out

    def summary(self) -> str:
        n = 0 if self.frame is None else len(self.frame)
        codes = self.report.codes()
        if self.fx_report is not None:
            for d in self.fx_report.diagnostics:
                codes[d.code] = codes.get(d.code, 0) + 1
        tail = ", ".join(f"{k} x{v}" for k, v in sorted(codes.items())) or "clean"
        return f"{self.adapter}: {n} row(s) | {tail}"

    def raise_if_blocking(self, allow_warnings: bool = False) -> Ledger:
        bad = self.fx_report.errors if (allow_warnings and self.fx_report) else self.blocking
        if bad:
            first = "; ".join(str(d) for d in bad[:5])
            more = f" (+{len(bad) - 5} more)" if len(bad) > 5 else ""
            raise ContractError(f"{len(bad)} blocking diagnostic(s): {first}{more}")
        return self

    # ---- outputs --------------------------------------------------------------------------------
    def to_frame(self, columns: tuple[str, ...] = LEDGER_COLUMNS) -> pd.DataFrame:
        df = self.frame.copy()
        for c in columns:
            if c not in df.columns:
                df[c] = None
        return df[list(columns)]

    def to_engine_input(self, *, grain: str = "month", as_of: date | str | None = None,
                        prorate: bool = True) -> tuple[pd.DataFrame, list[str]]:
        """The `customer_id / period / revenue` tape `saas_metrics.analyze` consumes.

        With `prorate=True` (default) each contract is recognised evenly by day across its service
        period, in whole cents. Without it, the whole billed amount lands in the invoice month. The
        default `as_of` recognises all covered service; pass an explicit `as_of` to defer revenue
        earned after that date.
        """
        from saas_metrics import inputs

        if self.frame is None or len(self.frame) == 0:
            return pd.DataFrame(), ["empty ledger"]
        df = self.frame
        out = pd.DataFrame({
            "customer_id": df["customer_id"].astype("string"),
            "period": df["service_start"],
            "revenue": [None if v is None else str(v) for v in df["recognized_amount"]],
            "segment": df["product_tier"].astype("string").fillna("(none)"),
            "channel": (df["channel"].astype("string").fillna("(none)") if "channel" in df.columns
                        else "(none)"),
        })
        if prorate:
            out["service_start"] = df["service_start"]
            out["service_end"] = df["service_end"]
        else:
            out["period"] = df["invoice_date"]
        if as_of is None:
            last = df["service_end"].max() if prorate else df["invoice_date"].max()
            as_of = proration_mod.month_end(pd.Timestamp(last).date())
        lines, notes = inputs.prepare(out, as_of=as_of)
        return lines, [f"recognised from the {self.adapter} ledger", *notes]

    def to_duckdb(self, target: Any, table: str = "billing_ledger", verify: bool = True) -> dict:
        """Write the ledger to DuckDB, money as `DECIMAL(18,2)`, and read it back to prove the write."""
        import duckdb

        con = target if isinstance(target, duckdb.DuckDBPyConnection) else duckdb.connect(str(target))
        df = self.to_frame()
        for c in _MONEY_COLUMNS:
            df[c] = [None if v is None else str(v) for v in df[c]]
        df["fx_rate"] = [None if v is None else str(v) for v in df["fx_rate"]]
        con.register("_ledger_frame", df)
        cols = ", ".join(f'CAST("{c}" AS {_CASTS.get(c, "VARCHAR")}) AS "{c}"' for c in df.columns)
        con.execute(f'CREATE OR REPLACE TABLE "{table}" AS SELECT {cols} FROM _ledger_frame')
        con.unregister("_ledger_frame")
        written = con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        total = con.execute(f'SELECT sum(mrr_amount) FROM "{table}"').fetchone()[0]
        out = {"table": table, "rows": int(written), "mrr_total": Decimal(str(total or 0)).quantize(Decimal("0.01"))}
        if verify:
            expected = sum((Decimal(str(v)) for v in self.frame["mrr_amount"] if v is not None), Decimal(0))
            out["verified"] = (written == len(self.frame)) and (out["mrr_total"] == expected.quantize(Decimal("0.01")))
            if not out["verified"]:
                raise ContractError(f"write-back check failed: wrote {written} rows / {out['mrr_total']} "
                                    f"against {len(self.frame)} rows / {expected}")
        if not isinstance(target, duckdb.DuckDBPyConnection):
            con.close()
        return out

    def to_csv(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.to_frame().to_csv(p, index=False, quoting=csv.QUOTE_MINIMAL)
        return p

    def to_parquet(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.to_frame().to_parquet(p, index=False)
        return p


def build_ledger(raw: pd.DataFrame, *, adapter: str, aliases: dict[str, tuple[str, ...]],
                 mapping: dict[str, str] | None = None, fx: FXConverter | None = None,
                 target_currency: str = "USD", grace_days: int = 30, grace_mode: str = grace_mod.MODE_BLANK_PERIOD,
                 on_missing_fx: str = "error", today: date | None = None, allow_warnings: bool = True,
                 source: str | None = None, stitch_accounts: bool = False,
                 co_terminate: bool = True) -> Ledger:
    """Run the shared pipeline. Adapters call this with their own `aliases` and money conventions.

    `stitch_accounts=True` resolves a dropped contract id by account and date before anything else
    (see `adapters/stitching.py`); it is on for Salesforce and off for billing exports, where the
    subscription id is authoritative and two overlapping subscriptions per customer are legitimate.
    """
    renamed, notes = rename_columns(raw, aliases, mapping)
    renamed["source_adapter"] = adapter
    if "source_row" not in renamed.columns:
        renamed["source_row"] = range(len(renamed))
    stitch_report = None
    if stitch_accounts:
        renamed, stitch_report = stitching_mod.stitch_accounts(renamed, grace_days=grace_days,
                                                               co_terminate=co_terminate)
        notes = [*notes, *stitch_report.notes()]
        if stitch_report.co_terminated and "recognized_amount" in renamed.columns:
            # co-terminating extends coverage, so the recurring rate is the same money over the longer term
            for entry in stitch_report.co_terminated:
                i = entry["row"]
                start = pd.Timestamp(renamed.at[i, "service_start"]).date()
                renamed.at[i, "mrr_amount"] = proration_mod.monthly_mrr(
                    renamed.at[i, "recognized_amount"], start, entry["to"])
    report = validate_ledger(renamed, today=today)
    ledger = Ledger(frame=report.frame, report=report, adapter=adapter, source=source, notes=notes,
                    stitch_report=stitch_report)
    if report.errors:
        return ledger

    frame, fx_report = fx_convert(report.frame, fx, target=target_currency, on_missing=on_missing_fx,
                                  amount_col="mrr_amount", total_col="recognized_amount")
    ledger.fx_report = fx_report
    ledger.frame = frame
    if not fx_report.ok:
        return ledger

    frame, grace_report = grace_mod.bridge_gaps(frame, grace_days=grace_days, mode=grace_mode)
    ledger.grace_report = grace_report
    if grace_report.n_bridged:
        term = (pd.to_datetime(frame["service_end"]) - pd.to_datetime(frame["service_start"])).dt.days + 1
        frame["term_months"] = (term / 30.44).round().clip(lower=1).astype("int64")
    frame, ramp_report = ramps_mod.classify(frame)
    ledger.ramp_report = ramp_report
    if stitch_report is not None:
        frame = stitching_mod.reconcile_kinds(frame)
    frame = proration_mod.flag_proration(frame)
    ledger.frame = frame.reset_index(drop=True)
    return ledger


def ledger_from_frame(frame: str | Path | pd.DataFrame, *, adapter: str = "canonical", today: date | None = None,
                      fx: FXConverter | None = None, target_currency: str = "USD",
                      grace_days: int = 30, grace_mode: str = grace_mod.MODE_BLANK_PERIOD,
                      on_missing_fx: str = "error", mapping: dict[str, str] | None = None) -> Ledger:
    """Canonicalise a frame (or a CSV/Parquet path) that is *already* in canonical shape."""
    empty: dict[str, tuple[str, ...]] = {}
    raw = frame if isinstance(frame, pd.DataFrame) else read_source(frame)
    return build_ledger(raw, adapter=adapter, aliases=empty, mapping=mapping, fx=fx,
                        target_currency=target_currency, grace_days=grace_days, grace_mode=grace_mode,
                        on_missing_fx=on_missing_fx, today=today,
                        source=str(frame) if not isinstance(frame, pd.DataFrame) else None)
