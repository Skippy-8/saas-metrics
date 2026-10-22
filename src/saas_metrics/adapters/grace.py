"""Grace periods: stop a late renewal reading as churn followed by a new-logo reactivation.

Invoices do not tile the calendar. A monthly subscription's January invoice may end on the 30th while
February's starts on the 1st, and an annual renewal may be billed days after the prior term ends
because of a billing-run cut-off or a signature date.

At monthly grain a few unpaid days cost nothing: as long as the customer has revenue in January *and*
in February, the waterfall sees one continuous customer. What does cost something is a gap that blanks
out a whole period — service ending 31 January and the renewal not starting until 1 March. February
then has no revenue, and the waterfall books the return in March as **reactivation**, inflating both
churn and new logos for what is one continuous customer.

So the default mode bridges only when a whole calendar month would otherwise be empty:

    service_end = next.service_start - 1 day     when 0 < gap <= grace_days
                                                 and the month after service_end is fully uncovered

The billed amount does not change; only the number of days it is spread over. The row is marked
`grace_bridged` with the `gap_days` it covered, and everything is reported. Pass `mode="always"` to
close every gap inside the window (a strict contiguous-coverage normalisation), and remember the
trade-off: extending the earlier term spreads the *same* billed amount over more days, so the bridged
month carries the prior rate rather than a newly priced one. Nothing is invented; a gap longer than
`grace_days` is left alone, because past the window a lapse is a lapse.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from saas_metrics.adapters.proration import month_end, next_month_start

MODE_BLANK_PERIOD = "blank_period"
MODE_ALWAYS = "always"
MODES = (MODE_BLANK_PERIOD, MODE_ALWAYS)


@dataclass
class GraceReport:
    bridged: list[dict] = field(default_factory=list)
    short: list[dict] = field(default_factory=list)     # inside the window, but no period went blank
    long: list[dict] = field(default_factory=list)      # beyond the window

    @property
    def n_bridged(self) -> int:
        return len(self.bridged)

    def notes(self) -> list[str]:
        out = []
        if self.bridged:
            days = sorted({b["gap_days"] for b in self.bridged})
            out.append(f"{len(self.bridged)} late renewal(s) bridged within the grace period "
                       f"(gaps of {days[0]}-{days[-1]} day(s) closed so they are not read as churn + reactivation)")
        if self.short:
            out.append(f"{len(self.short)} gap(s) inside the grace window did not blank a period and were left "
                       "as billed (each month still carries revenue)")
        if self.long:
            worst = max(g["gap_days"] for g in self.long)
            out.append(f"{len(self.long)} gap(s) left open (longest {worst} days), beyond the grace period: "
                       "these are treated as a lapse")
        return out


def _as_date(v) -> date:
    return v if isinstance(v, date) else pd.Timestamp(v).date()


def blanks_a_period(prev_end: date, next_start: date) -> bool:
    """True when the month after `prev_end` is entirely uncovered by either term."""
    following = next_month_start(prev_end)
    return next_start > month_end(following)


def bridge_gaps(frame: pd.DataFrame, *, grace_days: int = 30, mode: str = MODE_BLANK_PERIOD,
                contract_col: str = "contract_id", start_col: str = "service_start",
                end_col: str = "service_end") -> tuple[pd.DataFrame, GraceReport]:
    """Close the gaps of at most `grace_days` between consecutive rows of the same contract."""
    report = GraceReport()
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    if len(frame) == 0 or grace_days <= 0 or contract_col not in frame.columns:
        return frame, report
    df = frame.copy().reset_index(drop=True)
    df["_order"] = df.index
    df = df.sort_values([contract_col, start_col, end_col], kind="stable")
    for c in ("grace_bridged", "gap_days"):
        if c not in df.columns:
            df[c] = False if c == "grace_bridged" else 0

    for _, group in df.groupby(contract_col, sort=False):
        rows = group.index.tolist()
        for prev_i, cur_i in zip(rows, rows[1:], strict=False):
            prev_end = _as_date(df.at[prev_i, end_col])
            cur_start = _as_date(df.at[cur_i, start_col])
            gap = (cur_start - prev_end).days - 1
            if gap <= 0:
                continue
            record = {"contract_id": df.at[cur_i, contract_col], "gap_days": gap,
                      "previous_end": prev_end, "next_start": cur_start}
            if gap > grace_days:
                report.long.append(record)
                continue
            if mode == MODE_ALWAYS or blanks_a_period(prev_end, cur_start):
                df.at[prev_i, end_col] = cur_start - timedelta(days=1)
                df.at[prev_i, "grace_bridged"] = True
                df.at[cur_i, "grace_bridged"] = True
                df.at[cur_i, "gap_days"] = gap
                report.bridged.append(record)
            else:
                report.short.append(record)
    df = df.sort_values("_order").drop(columns="_order").reset_index(drop=True)
    return df, report
