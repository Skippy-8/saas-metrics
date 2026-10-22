"""Forecast drivers, derived only from information known at the cutoff date.

Every driver has Base / Upside / Downside values, a tag saying where it comes from, and a note
explaining the derivation. Nothing here reads the 'latest' vintage: the forecast must not see
anything filed after config.CUTOFF_DATE.

Tags
  [Sourced - SEC Filing]   a disclosed number used as-is
  [Historical Derived]     computed from disclosed history (e.g. a trailing average)
  [Forecast Assumption]    a judgement, usually the scenario band around a derived base
  [Illustrative]           not disclosed and not derivable; a stated placeholder
  [Estimated]              a disclosed series extended past its last disclosure
"""
from __future__ import annotations

from dataclasses import dataclass

import duckdb
import pandas as pd

from saas_metrics import config, periods

SCENARIOS = ("Base", "Upside", "Downside")
DEPTS = ("rnd", "snm", "gna")


@dataclass
class Driver:
    key: str
    label: str
    base: float
    upside: float
    downside: float
    tag: str
    note: str
    fmt: str = "0.0%"  # Excel number format

    def value(self, scenario: str) -> float:
        return {"Base": self.base, "Upside": self.upside, "Downside": self.downside}[scenario]


def _flat(key, label, v, tag, note, fmt="0.0%") -> Driver:
    return Driver(key, label, v, v, v, tag, note, fmt)


def history(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """As-of-cutoff quarterly history (in-window and pre-window), one row per quarter."""
    return con.execute("SELECT * FROM q_wide WHERE vintage = 'as_of_cutoff' ORDER BY period_end").df()


def headcount_history(con: duckdb.DuckDBPyConnection, hist: pd.DataFrame, as_of: str | None = config.CUTOFF_DATE
                      ) -> pd.DataFrame:
    """Quarterly headcount: annual 10-K points, linearly interpolated, extended past the last
    disclosure at the prior year's hiring pace. Returns columns headcount, hc_tag.
    `as_of=None` uses every 10-K filed (for scoring the forecast)."""
    pts = con.execute("""SELECT period_end, value FROM kpi_disclosures
                         WHERE kpi = 'employees' AND filed <= ? ORDER BY period_end""",
                      [as_of or "9999-12-31"]).df().set_index("period_end")["value"]
    ends = list(hist["period_end"])
    out, tags = [], []
    known = list(pts.index)
    for e in ends:
        if e in pts.index:
            out.append(pts[e])
            tags.append("[Sourced - SEC Filing]")
            continue
        before = [k for k in known if k < e]
        after = [k for k in known if k > e]
        if before and after:
            a, b = before[-1], after[0]
            n_q = 4  # annual points are four quarters apart
            k = sum(1 for x in ends if a < x <= e)
            out.append(pts[a] + (pts[b] - pts[a]) * k / n_q)
            tags.append("[Interpolated]")
        elif before and len(before) >= 2:
            a, p = before[-1], before[-2]
            k = sum(1 for x in ends if a < x <= e)
            out.append(pts[a] + (pts[a] - pts[p]) * k / 4)
            tags.append("[Estimated]")
        else:
            out.append(float("nan"))
            tags.append("")
    return pd.DataFrame({"period_end": ends, "headcount": out, "hc_tag": tags})


def _spec(key: str) -> dict:
    """Band and wording overrides for one driver (drivers.<key> in the config)."""
    return dict(config.ACTIVE.raw.get("drivers", {}).get(key) or {})


def _ttm_growth(h: pd.DataFrame, line: str) -> float:
    ttm = h[line].rolling(4).sum()
    return ttm.loc[config.T0_QUARTER_END] / ttm.shift(4).loc[config.T0_QUARTER_END] - 1


def _yoy_driver(h: pd.DataFrame, seg) -> Driver:
    sp = _spec(f"{seg.key}_yoy")
    v, pp = _ttm_growth(h, seg.line), sp.get("band_pp", 5) / 100
    note = sp.get("note") or (
        f"Trailing-12-month {seg.short} revenue growth at T0; applied to the same quarter a year earlier, which "
        f"keeps the seasonality. Upside/Downside +/-{pp * 100:g}pp [Forecast Assumption].")
    return Driver(f"{seg.key}_yoy", sp.get("label") or f"{seg.label} growth, YoY", v, v + pp, v - pp,
                  "[Historical Derived]", note)


def derive(con: duckdb.DuckDBPyConnection) -> list[Driver]:
    from saas_metrics import segments
    h = history(con).set_index("period_end")
    win = h[h["in_window"]]
    t4 = win.tail(4)                 # the four quarters up to and including T0
    t0 = win.iloc[-1]
    assert t0.name == config.T0_QUARTER_END, "history must end at T0"
    led = config.ACTIVE.raw["cohort_ledger"]
    svc = segments.nonrecurring()

    cal = con.execute("""SELECT * FROM cohort_calibration WHERE vintage = 'as_of_cutoff'
                         ORDER BY period_end""").df().set_index("period_end")
    kpi = con.execute("""SELECT period_end, kpi, value FROM kpi_disclosures WHERE filed <= ?""",
                      [config.CUTOFF_DATE]).df()
    nrr_t0 = kpi[(kpi.kpi == led["nrr_kpi"]) & (kpi.period_end == config.T0_QUARTER_END)].value.item() / 100

    c4 = cal.tail(4)
    logos = c4["new_logos"]
    arr_per_logo = c4["new_cohort_arr"].sum() / c4["new_logos"].sum()

    sub_gm = 1 - t4["cost_subscription"].sum() / t4["revenue_subscription"].sum()
    svc_gm = 1 - t4["cost_services"].sum() / t4[svc.line].sum()

    hc = headcount_history(con, h.reset_index()).set_index("period_end")["headcount"]
    hc_hist = hc.loc[win.index]
    net_hires = (hc_hist.iloc[-1] - hc_hist.iloc[-5]) / 4

    t0_label = periods.label(config.T0_QUARTER_END)
    t0_form = "10-K" if periods.fiscal(config.T0_QUARTER_END)[1] == 4 else "10-Q"
    sp_nrr, sp_apl, sp_gm = _spec("nrr"), _spec("arr_per_new_logo"), _spec("sub_gm")
    nrr_pp, apl_pct, gm_pp = sp_nrr.get("band_pp", 2) / 100, sp_apl.get("band_pct", 25) / 100, sp_gm.get("band_pp", 1) / 100
    nrr_note = f"Disclosed {nrr_t0:.0%} at T0 ({t0_label} {t0_form}). Upside/Downside +/-{nrr_pp * 100:g}pp [Forecast Assumption]"
    nrr_note += f": {sp_nrr['note_suffix']}" if sp_nrr.get("note_suffix") else "."
    d = [
        Driver("nrr", "Net ARR expansion rate (annual)", nrr_t0, nrr_t0 + nrr_pp, nrr_t0 - nrr_pp,
               "[Sourced - SEC Filing]", nrr_note),
        Driver("new_logos", "Gross new logos per quarter", logos.mean(), logos.max(), logos.min(),
               "[Historical Derived]",
               "Calibrated cohort ledger, trailing 4 quarters to T0: mean (Base), max (Upside), min (Downside). "
               "Depends on the [Illustrative] churn rate below.", "#,##0"),
        Driver("arr_per_new_logo", "New-cohort ARR per new logo ($)", arr_per_logo, arr_per_logo * (1 + apl_pct),
               arr_per_logo * (1 - apl_pct), "[Historical Derived]",
               sp_apl.get("note") or "Ledger new-cohort ARR / new logos, trailing 4 quarters to T0. "
               f"Upside/Downside +/-{apl_pct * 100:g}% [Forecast Assumption].", "$#,##0"),
        _flat("churn_q", "Gross logo churn per quarter", cal["churn_q_assumption"].iloc[-1], "[Illustrative]",
              "Not disclosed. Same rate as the calibrated cohort ledger; affects logos, not ARR."),
        *(_yoy_driver(h, seg) for seg in segments.yoy()),
        Driver("sub_gm", "Subscription gross margin (GAAP)", sub_gm, sub_gm + gm_pp, sub_gm - gm_pp,
               "[Historical Derived]", f"Trailing 4 quarters to T0. +/-{gm_pp * 100:g}pp band."),
        _flat("svc_gm", f"{svc.short} gross margin (GAAP)", svc_gm, "[Historical Derived]",
              _spec("svc_gm").get("note") or "Trailing 4 quarters to T0."),
        _flat("net_hires_q", "Net hires per quarter", net_hires, "[Historical Derived]",
              _spec("net_hires_q").get("note") or "Quarterly pace over the 4 quarters to T0, from headcount "
              "interpolated between 10-K year-end counts and extended past the last one at the prior year's "
              "pace [Estimated].", "#,##0"),
        _flat("amort_q", "Amortization of intangibles per quarter ($)", t0["amort_intangibles"],
              "[Historical Derived]", "Held at the T0 quarter's level.", "$#,##0"),
    ]
    for dept, name in zip(DEPTS, ("R&D", "Sales and marketing", "General and administrative"), strict=True):
        ng = (win[dept] - win[f"sbc_{dept}"]) / hc_hist
        g = (ng.iloc[-1] / ng.iloc[-5]) ** 0.25 - 1
        sbc = t4[f"sbc_{dept}"].sum() / t4["revenue"].sum()
        d.append(_flat(f"cph_growth_{dept}", f"{name}: non-GAAP spend per employee, growth per quarter", g,
                       "[Historical Derived]", "Compound quarterly growth over the 4 quarters to T0."))
        d.append(_flat(f"sbc_pct_{dept}", f"{name}: stock-based compensation, % of revenue", sbc,
                       "[Historical Derived]", "Trailing 4 quarters to T0."))
    for line in ("cost_subscription", "cost_services"):
        sbc = t4[f"sbc_{line}"].sum() / t4["revenue"].sum()
        d.append(_flat(f"sbc_pct_{line}", f"SBC in {line.replace('_', ' ')}, % of revenue", sbc,
                       "[Historical Derived]", "Trailing 4 quarters to T0."))
    # FCF conversion by fiscal quarter: same quarter a year earlier (working capital is seasonal)
    ng_op = win["operating_income"] + win[[c for c in win.columns if c.startswith("sbc_") and c != "sbc_total"]].sum(axis=1) \
        + win["amort_intangibles"]
    conv = (win["cfo"] - win["capex"]) / ng_op
    for fq in (1, 2, 3, 4):
        last = conv[win["fiscal_label"].str.startswith(f"Q{fq} ")].iloc[-1]
        lab = win.loc[conv[win["fiscal_label"].str.startswith(f"Q{fq} ")].index[-1], "fiscal_label"]
        d.append(_flat(f"fcf_conv_q{fq}", f"FCF / non-GAAP operating income, fiscal Q{fq}", last,
                       "[Historical Derived]", f"Ratio in {lab}, the latest Q{fq} known at the cutoff.", "0.00x"))
    return d
