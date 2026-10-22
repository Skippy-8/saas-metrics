"""Revenue and cash quality from XBRL: deferred revenue, RPO, SBC, implied billings, SBC-adjusted FCF.

A subscription P&L can look healthy while the balance sheet quietly disagrees. Three disclosures
close that gap, and none of them is a reported line item — each is assembled from tags:

* **Deferred revenue** (a contract liability, an instant) is the cash already billed but not yet
  recognised. Its change plus revenue is **implied billings**: what the quarter actually sold,
  regardless of when it is recognised. A quarter where revenue beats billings is a quarter funded
  from the backlog.
* **Remaining performance obligations** (an instant) are the contracted work not yet delivered:
  the backlog behind next year's plan, and the number that moves before revenue does.
* **Stock-based compensation** (a flow, added back in operating cash flow) is real compensation
  paid in stock. **SBC-adjusted FCF** = CFO − capex − SBC: the free cash flow a company would report
  if it paid its people in cash.

Tag fallback — the reason this module exists
-------------------------------------------
The same disclosure is tagged differently across a company's own history, and reading one tag gives
a series that starts or stops without warning:

* ASC 606 (fiscal 2018-2019) replaced ``us-gaap:DeferredRevenue`` and
  ``us-gaap:DeferredRevenueCurrent`` / ``Noncurrent`` with ``us-gaap:ContractWithCustomerLiability``
  and its Current / Noncurrent children. A company spanning the change has *both*, on either side of
  the crossover, and the old tag sometimes lingers for a quarter after the new one appears.
* ``us-gaap:ShareBasedCompensation`` (the cash-flow add-back) predates
  ``us-gaap:AllocatedShareBasedCompensationExpense`` (the income-statement allocation) at some filers.
* RPO is ``us-gaap:RevenueRemainingPerformanceObligation`` at most filers but a company extension at
  some; ``benchmark.quality`` in the config overrides the family list per company.

So each metric is defined as an ordered list of **families**, a family being the concepts that must be
summed together to mean one thing (current + noncurrent, never current alone). A family counts only
when *every* concept in it is tagged for that period, so a half-tagged balance sheet is a blank and
not a partial figure. One family is chosen as the **primary** — the one covering the latest period,
then a components family over a single total, then the widest coverage — and every earlier period it
misses is spliced from the next family that covers it, with the substitution recorded per quarter. A
series never mixes a current balance from one taxonomy with a noncurrent balance from another, and a
tag switch is reported rather than silently producing a step change in the chart.

Read as: order of preference, per period, within the reported history. Nothing is ever inferred from
a neighbouring quarter, and a metric the filer does not tag is left **blank, never zero** — TTD tags
no deferred revenue at all, so its implied billings are absent by design.

Every number is traceable: each quarter carries the family it came from, and the change in deferred
revenue is reconciled against the cash-flow statement's own ``IncreaseDecreaseInContractWithCustomer-
Liability`` where the filer tags it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from saas_metrics import benchmark
from saas_metrics import company as companies
from saas_metrics.fiscal import FiscalCalendar

RAW = companies.ROOT / "data" / "raw" / "benchmark"

INSTANT, DURATION = "instant", "duration"
METRIC_KINDS = {"deferred_revenue": INSTANT, "rpo": INSTANT, "sbc": DURATION}

COLUMNS = {  # output column -> description (also the CSV/xlsx header order)
    "ticker": "Ticker", "name": "Company", "fiscal_label": "Latest fiscal quarter", "period_end": "Quarter end",
    "revenue_ttm": "Revenue, TTM",
    "deferred_revenue": "Deferred revenue (quarter end)",
    "deferred_revenue_yoy": "Deferred revenue growth (YoY)",
    "implied_billings_ttm": "Implied billings, TTM",
    "implied_billings_growth_yoy": "Implied billings growth (TTM vs prior TTM)",
    "billings_to_revenue": "Implied billings / revenue (TTM)",
    "rpo": "Remaining performance obligations",
    "rpo_growth_yoy": "RPO growth (YoY)",
    "rpo_cover_years": "RPO / trailing revenue (years, proxy)",
    "sbc_ttm": "Stock-based compensation, TTM",
    "sbc_pct_revenue": "SBC % revenue (TTM)",
    "cfo_ttm": "Operating cash flow, TTM",
    "capex_ttm": "Capex, TTM",
    "fcf_ttm": "Free cash flow, TTM",
    "fcf_margin": "FCF margin (TTM)",
    "sbc_adjusted_fcf_ttm": "SBC-adjusted FCF, TTM",
    "sbc_adjusted_fcf_margin": "SBC-adjusted FCF margin (TTM)",
}

# tolerance for "the same number, tagged twice": 0.5% covers a filer rounding a total to $0.1M
# while tagging its components to the dollar
AGREEMENT_TOLERANCE = 0.005


@dataclass(frozen=True)
class Family:
    """The concepts that together mean one number, under one accounting regime."""
    label: str
    regime: str
    concepts: tuple[str, ...]

    @property
    def components(self) -> bool:
        return len(self.concepts) > 1


# A balance-sheet series must stay on one *structure*: a current balance plus its noncurrent
# companion, before and after the accounting change, because that is the figure that reconciles
# across the crossover. A single total tag is a fallback for filers that only tag a total, and it is
# kept behind both components families rather than interleaved with them — mixing a total into a
# components series is how a $7.8M tagging difference becomes a fake billings spike.
DEFAULT_FAMILIES: dict[str, tuple[Family, ...]] = {
    "deferred_revenue": (
        Family("ContractWithCustomerLiability, current + noncurrent", "asc606",
               ("us-gaap:ContractWithCustomerLiabilityCurrent", "us-gaap:ContractWithCustomerLiabilityNoncurrent")),
        Family("DeferredRevenue, current + noncurrent", "legacy",
               ("us-gaap:DeferredRevenueCurrent", "us-gaap:DeferredRevenueNoncurrent")),
        Family("ContractWithCustomerLiability (total)", "asc606", ("us-gaap:ContractWithCustomerLiability",)),
        Family("DeferredRevenue (total)", "legacy", ("us-gaap:DeferredRevenue",)),
    ),
    "rpo": (Family("RevenueRemainingPerformanceObligation", "asc606",
                   ("us-gaap:RevenueRemainingPerformanceObligation",)),),
    "sbc": (Family("ShareBasedCompensation (cash flow)", "cashflow", ("us-gaap:ShareBasedCompensation",)),
            Family("AllocatedShareBasedCompensationExpense", "income",
                   ("us-gaap:AllocatedShareBasedCompensationExpense",))),
}

CASH_FLOW_CHANGE_TAGS = ("us-gaap:IncreaseDecreaseInContractWithCustomerLiability",
                         "us-gaap:IncreaseDecreaseInDeferredRevenue")


# ---- config -------------------------------------------------------------------------------------
def families_for(c: companies.Company) -> dict[str, tuple[Family, ...]]:
    """The family lists for `c`: the defaults, with any `benchmark.quality` section replacing one."""
    configured = (c.raw.get("benchmark") or {}).get("quality") or {}
    out = dict(DEFAULT_FAMILIES)
    for metric, entries in configured.items():
        if metric not in METRIC_KINDS:
            raise companies.ConfigError(f"{c.ticker}: benchmark.quality.{metric} is not one of "
                                        f"{sorted(METRIC_KINDS)}")
        out[metric] = parse_families(metric, entries)
    return out


def parse_families(metric: str, entries) -> tuple[Family, ...]:
    """`[[c1, c2], [c3]]`, `[c1, c2]`, or `[{concepts: [...], label: ...}]` -> ordered families."""
    if not entries:
        return ()
    fams: list[Family] = []
    for i, entry in enumerate(entries):
        if isinstance(entry, str):
            fams.append(Family(entry.split(":")[-1], "configured", (entry,)))
        elif isinstance(entry, dict):
            concepts = tuple(entry.get("concepts") or entry.get("tags") or ())
            fams.append(Family(entry.get("label") or f"configured {i + 1}", entry.get("regime", "configured"),
                               concepts))
        else:
            concepts = tuple(entry)
            fams.append(Family(" + ".join(c.split(":")[-1] for c in concepts), "configured", concepts))
        if not fams[-1].concepts:
            raise companies.ConfigError(f"benchmark.quality.{metric}: family {i + 1} lists no concepts")
    return tuple(fams)


# ---- series -------------------------------------------------------------------------------------
@dataclass
class Resolution:
    """One metric, resolved period by period, with where each value came from."""
    metric: str
    values: pd.Series
    sources: pd.Series
    primary: Family | None = None
    fallbacks: list[str] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)

    @property
    def coverage(self) -> int:
        return int(self.values.notna().sum())

    def get(self, period: str) -> float | None:
        if period not in self.values.index:
            return None
        v = self.values[period]
        return None if pd.isna(v) else float(v)


def _series(kn: pd.DataFrame, concept: str, kind: str, cal: FiscalCalendar) -> pd.Series | None:
    rows = kn[kn["concept"] == concept]
    if rows.empty:
        return None
    if kind == INSTANT:
        inst = rows[rows["start"].isna()]
        if inst.empty:
            return None
        s = inst.set_index("end")["value"].astype(float)
        return s[~s.index.duplicated(keep="last")].sort_index()
    q = benchmark.quarterly(kn, concept, cal)
    return q if len(q) else None


def _family_value(series: dict[str, pd.Series | None], family: Family, period: str) -> float | None:
    total = 0.0
    for concept in family.concepts:
        s = series.get(concept)
        if s is None or period not in s.index or pd.isna(s[period]):
            return None
        total += float(s[period])
    return total


def _resolve(metric: str, families: tuple[Family, ...], series: dict[str, pd.Series | None],
             periods: list[str]) -> Resolution:
    values = pd.Series(index=periods, dtype=float)
    sources = pd.Series(index=periods, dtype=object)
    if not families:
        return Resolution(metric, values, sources, None)

    candidates = {f.label: {p: _family_value(series, f, p) for p in periods} for f in families}
    # primary = the family that speaks for the latest period, then components (more precise) before a
    # total, then the widest coverage, then the declared order
    order = {f.label: i for i, f in enumerate(families)}
    latest = next((p for p in reversed(periods) if any(c[p] is not None for c in candidates.values())), None)
    ranked = sorted(
        (f for f in families if any(v is not None for v in candidates[f.label].values())),
        key=lambda f: (latest is not None and candidates[f.label][latest] is not None,
                       f.components,
                       sum(v is not None for v in candidates[f.label].values()),
                       -order[f.label]),
        reverse=True,
    )
    primary = ranked[0] if ranked else None
    out = Resolution(metric, values, sources, primary)

    for p in periods:
        if primary is not None and candidates[primary.label][p] is not None:
            out.values[p] = candidates[primary.label][p]
            out.sources[p] = primary.label
            continue
        for family in families:
            if candidates[family.label][p] is not None:
                out.values[p] = candidates[family.label][p]
                out.sources[p] = family.label
                if primary is not None:
                    out.fallbacks.append(p)
                break

    for p in periods:
        present = {f.label: candidates[f.label][p] for f in families if candidates[f.label][p] is not None}
        if len(present) > 1:
            top = max(present.values())
            worst = min(present.values())
            if top and (top - worst) / abs(top) > AGREEMENT_TOLERANCE:
                out.conflicts.append({"period": p, "values": present, "used": out.sources[p]})
    return out


# ---- derivation ---------------------------------------------------------------------------------
@dataclass
class QualityResult:
    row: dict
    quarterly: pd.DataFrame
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    resolutions: dict[str, Resolution] = field(default_factory=dict)
    billings_reconciliation: pd.DataFrame = field(default_factory=pd.DataFrame)


def _ttm(series: pd.Series, period: str, cal: FiscalCalendar, n: int = 4) -> float | None:
    ends = [period]
    for _ in range(n - 1):
        ends.append(cal.prev_quarter_end(ends[-1]).isoformat())
    vals = [series.get(e) for e in ends]
    if any(v is None or pd.isna(v) for v in vals):
        return None
    return float(sum(vals))


def _quarter_yoy(series: pd.Series, period: str, cal: FiscalCalendar) -> float | None:
    d = period
    for _ in range(4):
        d = cal.prev_quarter_end(d).isoformat()
    prev, cur = series.get(d), series.get(period)
    if prev is None or cur is None or pd.isna(prev) or pd.isna(cur) or not prev:
        return None
    return float(cur) / float(prev) - 1


def derive(c: companies.Company, cf: dict, as_of: str | None = None) -> QualityResult:
    """Deferred revenue, RPO, SBC, implied billings and SBC-adjusted FCF for one company."""
    cal = FiscalCalendar(c.fiscal_year_end_month)
    comp = benchmark.company_comps(c, cf, as_of)
    periods = [str(p) for p in comp.quarterly.index]
    fams = families_for(c)

    kind_of = {x: METRIC_KINDS[m] for m, fs in fams.items() for f in fs for x in f.concepts}
    concepts = set(kind_of) | set(CASH_FLOW_CHANGE_TAGS)
    kn = benchmark.known(benchmark.load_facts(cf, concepts), as_of)
    series = {x: _series(kn, x, kind, cal) for x, kind in kind_of.items()}

    # resolve over the whole tagged history (so a pre-ASC-606 spliced period is reported), report the window
    history = sorted({p for s in series.values() if s is not None for p in s.index if cal.is_quarter_end(p)})
    history = sorted(set(history) | set(periods))
    resolutions = {m: _resolve(m, fams[m], series, history) for m in METRIC_KINDS}
    dr, rpo, sbc = resolutions["deferred_revenue"], resolutions["rpo"], resolutions["sbc"]

    revenue = comp.quarterly["revenue"]
    q = pd.DataFrame(index=periods)
    q["revenue"] = [float(revenue.get(p)) if p in revenue.index else None for p in periods]
    q["deferred_revenue"] = [dr.get(p) for p in periods]
    q["deferred_revenue_source"] = [dr.sources.get(p) for p in periods]
    q["deferred_revenue_spliced"] = [p in dr.fallbacks for p in periods]
    q["delta_deferred_revenue"] = [None if (v := dr.get(p)) is None or (pv := dr.get(cal.prev_quarter_end(p).isoformat())) is None
                                   else v - pv for p in periods]
    q["implied_billings"] = [None if (r := q.at[p, "revenue"]) is None or (d := q.at[p, "delta_deferred_revenue"]) is None
                             else r + d for p in periods]
    q["rpo"] = [rpo.get(p) for p in periods]
    q["sbc"] = [sbc.get(p) for p in periods]
    q["sbc_source"] = [sbc.sources.get(p) for p in periods]
    q["cfo"] = [float(comp.quarterly["cfo"].get(p)) if p in comp.quarterly.index else None for p in periods]
    q["capex"] = [float(comp.quarterly["capex"].get(p)) if p in comp.quarterly.index else None for p in periods]
    q["fcf"] = [None if (a := q.at[p, "cfo"]) is None or (b := q.at[p, "capex"]) is None else a - b for p in periods]
    q["sbc_adjusted_fcf"] = [None if (a := q.at[p, "fcf"]) is None or (b := q.at[p, "sbc"]) is None else a - b
                             for p in periods]

    last = periods[-1]
    ib = pd.Series({p: q.at[p, "implied_billings"] for p in periods}, dtype=float)
    drs = pd.Series({p: q.at[p, "deferred_revenue"] for p in periods}, dtype=float)
    rpos = pd.Series({p: q.at[p, "rpo"] for p in periods}, dtype=float)
    sbcs = pd.Series({p: q.at[p, "sbc"] for p in periods}, dtype=float)
    revenue_ttm = _ttm(revenue.astype(float), last, cal)
    billings_ttm = _ttm(ib.dropna(), last, cal)
    prior_4 = last
    for _ in range(4):
        prior_4 = cal.prev_quarter_end(prior_4).isoformat()
    billings_prior = _ttm(ib.dropna(), prior_4, cal)
    sbc_ttm = _ttm(sbcs.dropna(), last, cal)
    cfo_ttm = _ttm(comp.quarterly["cfo"].astype(float), last, cal)
    capex_ttm = _ttm(comp.quarterly["capex"].astype(float), last, cal)
    fcf_ttm = None if cfo_ttm is None or capex_ttm is None else cfo_ttm - capex_ttm
    adj_ttm = None if fcf_ttm is None or sbc_ttm is None else fcf_ttm - sbc_ttm
    dr_end, rpo_end = dr.get(last), rpo.get(last)

    def ratio(a, b):
        return None if a is None or not b else a / b

    row = {
        "ticker": c.ticker, "name": c.name, "fiscal_label": cal.label(last), "period_end": last,
        "revenue_ttm": revenue_ttm, "deferred_revenue": dr_end,
        "deferred_revenue_yoy": _quarter_yoy(drs.dropna(), last, cal),
        "implied_billings_ttm": billings_ttm,
        "implied_billings_growth_yoy": (billings_ttm / billings_prior - 1) if billings_ttm and billings_prior else None,
        "billings_to_revenue": ratio(billings_ttm, revenue_ttm),
        "rpo": rpo_end, "rpo_growth_yoy": _quarter_yoy(rpos.dropna(), last, cal),
        "rpo_cover_years": ratio(rpo_end, revenue_ttm),
        "sbc_ttm": sbc_ttm, "sbc_pct_revenue": ratio(sbc_ttm, revenue_ttm),
        "cfo_ttm": cfo_ttm, "capex_ttm": capex_ttm, "fcf_ttm": fcf_ttm, "fcf_margin": ratio(fcf_ttm, revenue_ttm),
        "sbc_adjusted_fcf_ttm": adj_ttm, "sbc_adjusted_fcf_margin": ratio(adj_ttm, revenue_ttm),
    }
    res = QualityResult(row, q, resolutions=resolutions)
    res.billings_reconciliation = _reconcile_billings(kn, cal, dr, periods)
    res.checks = _checks(c, resolutions, series, fams, periods)
    res.notes = _notes(c, resolutions, periods, q, res.billings_reconciliation)
    return res


def _reconcile_billings(kn: pd.DataFrame, cal: FiscalCalendar, dr: Resolution,
                        periods: list[str]) -> pd.DataFrame:
    """Compare quarter-on-quarter Δ deferred revenue with the cash-flow statement's own change line."""
    frames = {}
    for tag in CASH_FLOW_CHANGE_TAGS:
        s = _series(kn, tag, DURATION, cal)
        if s is not None and s.notna().any():
            frames[tag.split(":")[-1]] = s
    if not frames:
        return pd.DataFrame()
    tag = max(frames, key=lambda k: int(frames[k].notna().sum()))    # the tag with the widest coverage
    out = pd.DataFrame({tag: frames[tag]})
    cash_flow = tag
    out["delta_deferred_revenue"] = [None if (v := dr.get(p)) is None or
                                     (pv := dr.get(cal.prev_quarter_end(p).isoformat())) is None else v - pv
                                     for p in out.index]
    out = out.loc[[p for p in out.index if p in periods]].dropna(how="all")
    out["deferred_revenue"] = [dr.get(p) for p in out.index]
    out["difference"] = out[cash_flow] - out["delta_deferred_revenue"]
    out["difference_relative"] = (out["difference"] / out["deferred_revenue"]).abs()
    if out["delta_deferred_revenue"].notna().sum() == 0:
        return pd.DataFrame()          # no deferred-revenue series to reconcile against
    return out


def _pair_agreement(families: tuple[Family, ...], series: dict[str, pd.Series | None],
                    periods: list[str]) -> list[dict]:
    """Where a components family and a total family of the same regime are both tagged, how far apart."""
    out = []
    for comp in (f for f in families if f.components):
        for tot in (f for f in families if not f.components and f.regime == comp.regime):
            pairs = [(p, _family_value(series, comp, p), _family_value(series, tot, p)) for p in periods]
            pairs = [(p, a, b) for p, a, b in pairs if a is not None and b is not None]
            if not pairs:
                continue
            gap = max(abs(a - b) for _, a, b in pairs)
            base = max(abs(b) for _, _, b in pairs) or 1.0
            worst = max(pairs, key=lambda t: abs(t[1] - t[2]))
            out.append({"components": comp, "total": tot, "quarters": len(pairs), "gap": gap,
                        "relative": gap / base, "worst": worst})
    return out


def _sbc_agreement(families: tuple[Family, ...], series: dict[str, pd.Series | None],
                   periods: list[str]) -> dict | None:
    single = [f for f in families if len(f.concepts) == 1]
    if len(single) < 2:
        return None
    pairs = []
    for p in periods:
        a, b = _family_value(series, single[0], p), _family_value(series, single[1], p)
        if a is not None and b is not None:
            pairs.append((p, a, b))
    if not pairs:
        return None
    gap = max(abs(a - b) for _, a, b in pairs)
    base = max(abs(a) for _, a, _ in pairs) or 1.0
    worst = max(pairs, key=lambda t: abs(t[1] - t[2]))
    return {"a": single[0], "b": single[1], "quarters": len(pairs), "gap": gap, "relative": gap / base,
            "worst": worst}


def _checks(c, resolutions: dict[str, Resolution], series: dict[str, pd.Series | None],
            families: dict[str, tuple[Family, ...]], periods: list[str]) -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []
    dr, rpo = resolutions["deferred_revenue"], resolutions["rpo"]
    recent = periods[-8:]                       # the two trailing TTM years the table reports
    blank = [p for p in recent if dr.get(p) is None]
    if dr.primary is None:
        out.append((f"{c.ticker}: deferred revenue is tagged", True,
                    "no deferred-revenue concept is tagged; implied billings are left blank, not zero"))
    else:
        out.append((f"{c.ticker}: deferred revenue is tagged for every quarter of the trailing two years", not blank,
                    f"{len(recent) - len(blank)}/{len(recent)} quarters from {dr.primary.label}"
                    + (f"; blank: {', '.join(blank[:4])}" if blank else "")))
    for agree in _pair_agreement(families["deferred_revenue"], series, recent):
        out.append((f"{c.ticker}: deferred revenue components sum to the total tag",
                    agree["relative"] <= AGREEMENT_TOLERANCE,
                    f"{agree['components'].label} vs {agree['total'].label}: {agree['quarters']} quarter(s), "
                    f"largest gap ${agree['gap'] / 1e6:,.2f}M ({agree['relative'] * 100:.2f}%) at {agree['worst'][0]}"))
    if rpo.primary is None:
        out.append((f"{c.ticker}: RPO is tagged", True,
                    "no RPO concept is tagged; RPO growth and cover are left blank, not zero"))
    else:
        last = periods[-1]
        val = rpo.get(last)
        out.append((f"{c.ticker}: RPO is tagged at the latest quarter", val is not None,
                    f"${val / 1e6:,.0f}M at {last}" if val is not None
                    else f"last tagged {max((p for p in rpo.values.index if rpo.get(p) is not None), default='never')}"))
    sbc = _sbc_agreement(families["sbc"], series, recent)
    if sbc:
        out.append((f"{c.ticker}: the two SBC tags agree", sbc["relative"] <= AGREEMENT_TOLERANCE,
                    f"{sbc['a'].label} vs {sbc['b'].label}: {sbc['quarters']} quarter(s), largest gap "
                    f"${sbc['gap'] / 1e6:,.2f}M at {sbc['worst'][0]}"))
    return out


def _notes(c, resolutions: dict[str, Resolution], periods: list[str],
           q: pd.DataFrame, billings: pd.DataFrame) -> list[str]:
    dr, rpo, sbc = resolutions["deferred_revenue"], resolutions["rpo"], resolutions["sbc"]
    out: list[str] = []
    if dr.primary is not None and dr.fallbacks:
        labels = sorted({str(dr.sources[p]) for p in dr.fallbacks})
        out.append(f"{c.ticker}: deferred revenue reads {dr.primary.label} for "
                   f"{int(dr.values.notna().sum()) - len(dr.fallbacks)} quarter(s); {len(dr.fallbacks)} earlier "
                   f"quarter(s) are spliced from {', '.join(labels)} because the filer changed tags "
                   "(pre-ASC 606 -> ASC 606). Ordered tag fallback, recorded per quarter")
    if dr.conflicts:
        worst = max(dr.conflicts, key=lambda d: max(d["values"].values()) - min(d["values"].values()))
        gap = max(worst["values"].values()) - min(worst["values"].values())
        out.append(f"{c.ticker}: {len(dr.conflicts)} quarter(s) tag deferred revenue under two families with "
                   f"different values (largest gap ${gap / 1e6:,.1f}M at {worst['period']}); "
                   f"{worst['used']} was used and the other is left out rather than averaged")
    if dr.primary is None:
        out.append(f"{c.ticker}: no deferred-revenue concept is tagged in companyfacts, so implied billings, "
                   "Δdeferred revenue and billings-to-revenue are blank — absent, not zero")
    blanks = [p for p in periods[-8:] if pd.isna(q.at[p, "implied_billings"])]
    if blanks and dr.primary is not None:
        out.append(f"{c.ticker}: implied billings needs two consecutive deferred-revenue balances; "
                   f"{len(blanks)} quarter(s) in the trailing two years cannot form one and are blank")
    if rpo.primary is None:
        out.append(f"{c.ticker}: no RPO concept is tagged, so RPO growth and RPO cover are blank — not zero")
    if sbc.primary is not None and sbc.fallbacks:
        out.append(f"{c.ticker}: SBC reads {sbc.primary.label} for most quarters and {len(sbc.fallbacks)} "
                   "quarter(s) from the other tag where it was absent")
    if sbc.primary is None:
        out.append(f"{c.ticker}: no SBC concept is tagged; SBC-adjusted FCF is blank, not equal to FCF")
    if not billings.empty:
        rel = billings["difference_relative"].dropna()
        cash_flow = billings.columns[0]
        if len(rel):
            within = int((rel <= 0.02).sum())
            out.append(f"{c.ticker}: Δ deferred revenue agrees with {cash_flow} within 2% of the balance in {within} "
                       f"of {len(billings)} quarter(s) (largest gap {rel.max() * 100:,.1f}% at {rel.idxmax()}); the "
                       "rest is FX translation on deferred balances and the ASC 606 transition, which the cash-flow "
                       "line carries and the balance sheet does not")
    return out


# ---- table --------------------------------------------------------------------------------------
def table(tickers: list[str], as_of: str | None = None, refresh: bool = False, raw_dir: Path = RAW,
          offline: dict[str, dict] | None = None) -> tuple[pd.DataFrame, dict[str, QualityResult]]:
    """Revenue-quality table for `tickers`. `offline` maps ticker -> companyfacts dict (tests)."""
    results = {}
    for t in tickers:
        c = companies.load(t)
        if "benchmark" not in c.raw:
            raise companies.ConfigError(f"{t}: config has no `benchmark` section")
        cf = offline[c.ticker] if offline else json.loads(benchmark.fetch(c, refresh, raw_dir).read_text())
        results[c.ticker] = derive(c, cf, as_of)
    frame = pd.DataFrame([r.row for r in results.values()])[list(COLUMNS)]
    return frame, results


def format_table(frame: pd.DataFrame) -> str:
    t = frame.copy()
    for col in ("revenue_ttm", "deferred_revenue", "implied_billings_ttm", "rpo", "sbc_ttm", "cfo_ttm",
                "capex_ttm", "fcf_ttm", "sbc_adjusted_fcf_ttm"):
        t[col] = t[col].map(lambda v: "n/a" if v is None or pd.isna(v) else f"${v / 1e6:,.0f}M")
    for col in ("deferred_revenue_yoy", "implied_billings_growth_yoy", "billings_to_revenue", "rpo_growth_yoy",
                "sbc_pct_revenue", "fcf_margin", "sbc_adjusted_fcf_margin"):
        t[col] = t[col].map(lambda v: "n/a" if v is None or pd.isna(v) else f"{v * 100:.1f}%")
    t["rpo_cover_years"] = t["rpo_cover_years"].map(lambda v: "n/a" if v is None or pd.isna(v) else f"{v:.1f}y")
    return t.drop(columns=["name"]).to_string(index=False)


def series_frame(results: dict[str, QualityResult]) -> pd.DataFrame:
    """One long frame of the quarterly series behind the table (ticker, period_end, values)."""
    parts = []
    for t, r in results.items():
        f = r.quarterly.copy()
        f.insert(0, "period_end", f.index)
        f.insert(0, "ticker", t)
        parts.append(f.reset_index(drop=True))
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


INPUT_COLS = ("revenue_ttm", "deferred_revenue", "implied_billings_ttm", "rpo", "sbc_ttm", "cfo_ttm", "capex_ttm",
              "fcf_ttm", "sbc_adjusted_fcf_ttm")
FORMULAS = {
    "deferred_revenue_yoy": None,          # an input, computed over quarters; see the Quarterly sheet
    "implied_billings_growth_yoy": None,
    "billings_to_revenue": "={implied_billings_ttm}/{revenue_ttm}",
    "rpo_growth_yoy": None,
    "rpo_cover_years": "={rpo}/{revenue_ttm}",
    "sbc_pct_revenue": "={sbc_ttm}/{revenue_ttm}",
    "fcf_margin": "={fcf_ttm}/{revenue_ttm}",
    "sbc_adjusted_fcf_margin": "={sbc_adjusted_fcf_ttm}/{revenue_ttm}",
}


def write(frame: pd.DataFrame, results: dict[str, QualityResult], out: Path) -> Path:
    if out.suffix == ".csv":
        frame.to_csv(out, index=False)
        series_frame(results).to_csv(out.with_name(out.stem + "_quarterly.csv"), index=False)
    elif out.suffix == ".parquet":
        import duckdb
        duckdb.connect().register("t", frame).execute(f"COPY t TO '{out}' (FORMAT parquet)")
        series_frame(results).to_parquet(out.with_name(out.stem + "_quarterly.parquet"), index=False)
    elif out.suffix == ".xlsx":
        _write_xlsx(frame, results, out)
    else:
        raise ValueError("--out must end in .csv, .parquet or .xlsx")
    return out


def _write_xlsx(frame: pd.DataFrame, results: dict[str, QualityResult], out: Path) -> Path:
    """Revenue_Quality sheet: inputs as values (blue), ratios as live formulas; then the quarters."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Revenue_Quality"
    ws["A1"] = ("Revenue and cash quality from SEC XBRL: deferred revenue, RPO, SBC, implied billings and "
                "SBC-adjusted FCF (free cash flow less stock-based compensation)")
    ws["A1"].font = Font(bold=True, size=13)
    head = ["ticker", "name", "fiscal_label", "period_end", *INPUT_COLS, *FORMULAS]
    col = {h: get_column_letter(i + 1) for i, h in enumerate(head)}
    for i, h in enumerate(head):
        cell = ws.cell(3, i + 1, COLUMNS.get(h, h.replace("_ttm", " (TTM, $)")))
        cell.font, cell.fill = Font(bold=True), PatternFill("solid", fgColor="DDEBF7")
    for r, res in enumerate(results.values(), start=4):
        for h in ("ticker", "name", "fiscal_label", "period_end"):
            ws[f"{col[h]}{r}"] = res.row[h]
        for c in INPUT_COLS:
            cell = ws[f"{col[c]}{r}"]
            cell.value, cell.font, cell.number_format = res.row[c], Font(color="0000FF"), '$#,##0'
        for name, tmpl in FORMULAS.items():
            cell = ws[f"{col[name]}{r}"]
            if tmpl is None:
                cell.value = res.row[name]
                cell.number_format = "0.0%"
                continue
            formula = tmpl
            for c in INPUT_COLS:
                formula = formula.replace("{" + c + "}", f"{col[c]}{r}")
            cell.value, cell.number_format = formula, "0.0%"
    r = len(results) + 5
    ws.cell(r, 1, "Notes").font = Font(bold=True)
    for t, res in results.items():
        for n in res.notes:
            r += 1
            ws.cell(r, 1, f"{t}: {n}")
    for i in range(1, len(head) + 1):
        ws.column_dimensions[get_column_letter(i)].width = 16
    ws.column_dimensions["B"].width = 24

    qs = wb.create_sheet("Quarterly")
    long = series_frame(results)
    for j, h in enumerate(long.columns, start=1):
        cell = qs.cell(1, j, h)
        cell.font, cell.fill = Font(bold=True), PatternFill("solid", fgColor="DDEBF7")
    for i, rec in enumerate(long.itertuples(index=False), start=2):
        for j, v in enumerate(rec, start=1):
            cell = qs.cell(i, j, None if isinstance(v, float) and pd.isna(v) else v)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and not pd.isna(v):
                cell.number_format = "$#,##0" if abs(v) > 1000 else "0.0000"
    qs.freeze_panes = "A2"
    wb.save(out)
    return out
