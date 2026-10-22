"""Ramp classification: separate *contracted* expansion from *organic* upsell.

A multi-year ramped contract (a 3-year deal that steps from $10k to $15k to $20k MRR on its
anniversaries) is sold at signature. Every step is contracted revenue the moment the ink is dry. An
organic upsell — a customer buying more seats or more usage mid-term — is a different event and a
different line in a bridge.

Both look the same in a billing export: a later-dated row with a bigger MRR. This module labels each
row so the two never get merged:

    new                     the first row of a contract
    renewal                 a continuation at the same (or lower) rate
    contracted_expansion    a scheduled step at or after the prior term ends, at a higher rate
    contraction             a scheduled step down
    upsell                  an intra-term increase (service starts before the prior term ends)

An explicit marker wins when the source provides one — Salesforce `Type`, a Chargebee/Stripe
`change_type`, or any column named by `kind_col`. `is_contracted_expansion` is the boolean the
waterfall and the bridge will read.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import pandas as pd


@dataclass
class RampReport:
    counts: dict[str, int] = field(default_factory=dict)

    def notes(self) -> list[str]:
        if not self.counts:
            return []
        parts = ", ".join(f"{k} {v}" for k, v in sorted(self.counts.items()))
        ramps = self.counts.get("contracted_expansion", 0)
        upsell = self.counts.get("upsell", 0)
        note = f"contract steps classified: {parts}"
        if ramps or upsell:
            note += (f"; {ramps} contracted ramp step(s) are kept out of {upsell} organic upsell step(s)")
        return [note]


def _as_date(v) -> date:
    return v if isinstance(v, date) else pd.Timestamp(v).date()


def _money(v) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal(0)


# source markers that already say what a step is, mapped onto the canonical kinds
_MARKERS = {
    "new": "new", "new business": "new", "newbusiness": "new",
    "renewal": "renewal", "renew": "renewal", "renewals": "renewal",
    "ramp": "contracted_expansion", "contracted expansion": "contracted_expansion",
    "contracted_expansion": "contracted_expansion", "step": "contracted_expansion",
    "upgrade": "upsell", "upsell": "upsell", "expansion": "upsell", "add-on": "upsell", "addon": "upsell",
    "downgrade": "contraction", "contraction": "contraction", "churn": "contraction",
}


def classify(frame: pd.DataFrame, *, contract_col: str = "contract_id", start_col: str = "service_start",
             end_col: str = "service_end", mrr_col: str = "mrr_amount", kind_col: str | None = "source_kind"
             ) -> tuple[pd.DataFrame, RampReport]:
    """Label every row with an `expansion_kind` and an `is_contracted_expansion` flag."""
    report = RampReport()
    df = frame.copy().reset_index(drop=True)
    df["_order"] = df.index
    kinds: dict[int, str] = {}

    explicit = [None] * len(df)
    if kind_col and kind_col in df.columns:
        explicit = [(_MARKERS.get(str(v).strip().lower()) if pd.notna(v) else None) for v in df[kind_col]]

    # A row with no contract id (e.g. a one-off Stripe invoice) cannot be a renewal of another
    # blank-id row — `groupby` drops NaN keys, and grouping them together would invent a contract.
    # Give each blank row its own key so it is classified as new, then drop the key.
    key_col = "_contract_key"
    df[key_col] = df[contract_col]
    blank = df[contract_col].isna() | df[contract_col].astype("string").str.strip().eq("")
    if blank.any():
        df.loc[blank, key_col] = [f"_blank_{i}" for i in df.index[blank]]

    df = df.sort_values([key_col, start_col, end_col], kind="stable")
    for _, group in df.groupby(key_col, sort=False, dropna=False):
        rows = group.index.tolist()
        prev = None
        for i in rows:
            explicit_kind = explicit[i]
            if prev is None:
                kind = explicit_kind or "new"
            else:
                prev_end = _as_date(df.at[prev, end_col])
                start = _as_date(df.at[i, start_col])
                step_up = _money(df.at[i, mrr_col]) > _money(df.at[prev, mrr_col])
                step_down = _money(df.at[i, mrr_col]) < _money(df.at[prev, mrr_col])
                if explicit_kind:
                    kind = explicit_kind
                elif start <= prev_end:
                    kind = "upsell" if step_up else ("contraction" if step_down else "renewal")
                elif step_up:
                    kind = "contracted_expansion"
                elif step_down:
                    kind = "contraction"
                else:
                    kind = "renewal"
            kinds[i] = kind
            prev = i

    df["expansion_kind"] = [kinds[i] for i in df.index]
    df["is_contracted_expansion"] = df["expansion_kind"].eq("contracted_expansion")
    df = df.sort_values("_order").drop(columns=["_order", key_col]).reset_index(drop=True)
    if kind_col and kind_col in df.columns:
        df = df.drop(columns=[kind_col])
    counts: dict[str, int] = {}
    for k in df["expansion_kind"]:
        counts[k] = counts.get(k, 0) + 1
    report.counts = counts
    return df, report
