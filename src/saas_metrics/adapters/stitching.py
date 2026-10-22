"""Account stitching: keep one customer lifecycle whole when the export drops the contract id.

Salesforce is a CRM, not a billing system, and its Opportunity exports drop or disconnect
`ContractId` constantly — most often on add-ons and on renewals created from a quote rather than
from the contract record. Read literally, every such row is a brand-new agreement, so a mid-term
add-on and a renewal both surface as **new logos**, and the period the old contract expires shows up
as **churn**. That inflates gross churn and breaks the cohort waterfall even though the customer
never left.

The adapter therefore resolves an opportunity to a lifecycle in three tiers, in this order:

1. **Explicit link.** `ContractId` / `Contract Number` populated -> that is the lifecycle. Nothing is
   inferred and nothing is renamed.
2. **Account-stitch renewal.** No link, but the same `AccountId` has a term whose end date is within
   the grace window of this start date -> the row continues that lifecycle (a renewal). The window is
   the same `grace_days` the invoice bridge uses, so a late-signed renewal is continuous in both
   layers.
3. **Add-on (intra-term).** No link, and this start date falls *inside* another term for the same
   account -> it is an expansion of that term, labelled `upsell`, and it is co-terminated: its end is
   aligned to the parent term's end when the export truncated it. The parent term is never re-dated,
   and revenue is never truncated — only coverage is extended.

Anything that matches none of the three is a genuinely new lifecycle and keeps its own id.

What this module does **not** do is guess across accounts: two rows join only if they share an
`AccountId`. If the account column is missing, stitching is skipped with a note rather than falling
back to name matching.

The stitched id is stable and readable — ``<account>:<first service start>`` — so it survives a
re-export and can be joined against a billing system's subscription id later.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

SOURCE_EXPLICIT = "explicit"
SOURCE_RENEWAL = "account_stitch"
SOURCE_ADD_ON = "add_on"
SOURCE_NEW = "new"


@dataclass
class StitchReport:
    explicit: int = 0
    renewals: int = 0
    add_ons: int = 0
    fresh: int = 0
    co_terminated: list[dict] = field(default_factory=list)
    skipped: str | None = None

    @property
    def stitched(self) -> int:
        return self.renewals + self.add_ons

    def notes(self) -> list[str]:
        if self.skipped:
            return [self.skipped]
        out = []
        if self.stitched:
            out.append(f"{self.stitched} opportunity(ies) without a contract id were stitched by account and "
                       f"date ({self.renewals} renewal(s), {self.add_ons} add-on(s)); without this they would "
                       "read as new logos and the term they continue would read as churn")
        if self.co_terminated:
            out.append(f"{len(self.co_terminated)} add-on(s) co-terminated to the parent term end "
                       "(coverage extended, revenue unchanged)")
        return out


@dataclass
class _Cluster:
    """One customer lifecycle: the rows that belong to it, and the terms they cover."""
    id: str
    explicit: bool
    rows: list[int] = field(default_factory=list)
    terms: list[tuple[date, date]] = field(default_factory=list)

    def attach(self, row: int, start: date | None, end: date | None) -> None:
        self.rows.append(row)
        if start is not None and end is not None:
            self.terms.append((start, end))

    @property
    def start(self) -> date | None:
        return min((s for s, _ in self.terms), default=None)

    @property
    def end(self) -> date | None:
        return max((e for _, e in self.terms), default=None)

    def containing_end(self, start: date) -> date | None:
        """The end of the term this date falls inside, if any (the parent of an add-on)."""
        inside = [e for s, e in self.terms if s <= start <= e]
        return max(inside) if inside else None


def _as_date(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, date):
        return v
    ts = pd.to_datetime(v, errors="coerce")
    return None if pd.isna(ts) else ts.date()


def _blank(v) -> bool:
    if v is None:
        return True
    try:
        if pd.isna(v):
            return True
    except (TypeError, ValueError):
        pass
    return str(v).strip().lower() in ("", "nan", "none", "<na>")


def stitch_accounts(frame: pd.DataFrame, *, account_col: str = "customer_id", contract_col: str = "contract_id",
                    start_col: str = "service_start", end_col: str = "service_end", grace_days: int = 30,
                    co_terminate: bool = True) -> tuple[pd.DataFrame, StitchReport]:
    """Resolve every row to a lifecycle id, in the three tiers above. Returns (frame, report)."""
    report = StitchReport()
    if len(frame) == 0:
        return frame, report
    if account_col not in frame.columns:
        report.skipped = (f"account stitching skipped: no {account_col!r} column, so a dropped contract id "
                          "cannot be inferred without guessing across customers")
        return frame, report

    df = frame.copy().reset_index(drop=True)
    for col in ("contract_source", "co_term_end", "co_terminated", "stitched"):
        if col not in df.columns:
            df[col] = None if col in ("contract_source", "co_term_end") else False
    if contract_col not in df.columns:
        df[contract_col] = None

    starts = {i: _as_date(df.at[i, start_col]) for i in df.index}
    ends = {i: _as_date(df.at[i, end_col]) for i in df.index}
    order = (df.assign(_s=[starts[i] for i in df.index], _e=[ends[i] for i in df.index])
               .sort_values([account_col, "_s", "_e"], kind="stable", na_position="last").index)

    clusters: dict[str, list[_Cluster]] = {}
    for i in order:
        account = str(df.at[i, account_col])
        start, end = starts[i], ends[i]
        own = df.at[i, contract_col]
        bucket = clusters.setdefault(account, [])

        # 1. explicit link: the export told us, so believe it
        if not _blank(own):
            own = str(own).strip()
            cluster = next((c for c in bucket if c.id == own), None)
            if cluster is None:
                cluster = _Cluster(id=own, explicit=True)
                bucket.append(cluster)
            cluster.attach(i, start, end)
            df.at[i, "contract_source"] = SOURCE_EXPLICIT
            report.explicit += 1
            continue

        # 2 & 3. account fallback: a term this row continues (renewal) or starts inside (add-on)
        cluster, role, co_term = _match(bucket, start, grace_days)
        if cluster is not None:
            cluster.attach(i, start, end)
            df.at[i, contract_col] = cluster.id
            df.at[i, "contract_source"] = role
            if role == SOURCE_RENEWAL:
                report.renewals += 1
            else:
                report.add_ons += 1
                df.at[i, "co_term_end"] = co_term
                if co_terminate and co_term is not None and end is not None and end < co_term:
                    df.at[i, end_col] = co_term
                    df.at[i, "co_terminated"] = True
                    report.co_terminated.append({"contract_id": cluster.id, "row": i,
                                                 "from": end, "to": co_term})
            continue

        new_id = f"{account}:{start.isoformat() if start else 'undated'}"
        cluster = _Cluster(id=new_id, explicit=False)
        bucket.append(cluster)
        cluster.attach(i, start, end)
        df.at[i, contract_col] = new_id
        df.at[i, "contract_source"] = SOURCE_NEW
        report.fresh += 1

    df["stitched"] = df["contract_source"].isin([SOURCE_RENEWAL, SOURCE_ADD_ON])
    return df, report


def _match(bucket: list[_Cluster], start: date | None,
           grace_days: int) -> tuple[_Cluster | None, str, date | None]:
    """The lifecycle this row continues, and how. Intra-term beats a renewal; the latest term wins."""
    if start is None:
        return None, SOURCE_NEW, None
    best: tuple[tuple, _Cluster, str, date | None] | None = None
    for cluster in bucket:
        parent_end = cluster.containing_end(start)
        if parent_end is not None:
            candidate = ((0, -parent_end.toordinal()), cluster, SOURCE_ADD_ON, parent_end)
        elif cluster.end is not None and 0 <= (start - cluster.end).days - 1 <= grace_days:
            candidate = ((1, -cluster.end.toordinal()), cluster, SOURCE_RENEWAL, None)
        else:
            continue
        if best is None or candidate[0] < best[0]:
            best = candidate
    if best is None:
        return None, SOURCE_NEW, None
    return best[1], best[2], best[3]


def reconcile_kinds(frame: pd.DataFrame, kind_col: str = "expansion_kind",
                    source_col: str = "contract_source") -> pd.DataFrame:
    """A stitched renewal is a renewal and a stitched add-on is an upsell, whatever the source marker said.

    Salesforce `Type` is frequently "New" on a renewal created from a quote, and that marker would
    otherwise relabel the row the stitching just rescued.
    """
    df = frame.copy()
    if kind_col not in df.columns or source_col not in df.columns:
        return df
    renewal = df[source_col].eq(SOURCE_RENEWAL) & df[kind_col].eq("new")
    add_on = df[source_col].eq(SOURCE_ADD_ON) & df[kind_col].eq("new")
    df.loc[renewal, kind_col] = "renewal"
    df.loc[add_on, kind_col] = "upsell"
    if "is_contracted_expansion" in df.columns:
        df["is_contracted_expansion"] = df[kind_col].eq("contracted_expansion")
    return df
