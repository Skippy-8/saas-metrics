"""Revenue segments of the active company (configs/<TICKER>.yaml `segments:`).

Supported shapes (validated here, so a config that does not fit fails clearly):
  - exactly one recurring segment with method `arr_residual`: its revenue is what the ARR roll-forward
    leaves after the other recurring revenue, annualised by days in the quarter (MongoDB: Atlas);
  - any number of recurring `yoy_same_quarter` segments; those with `arr_basis: trailing_12m` enter
    the ARR proxy at their trailing twelve months (MongoDB: EA);
  - exactly one non-recurring `yoy_same_quarter` segment (MongoDB: services).
Each yoy segment has a forecast driver `<key>_yoy` and its own bridge group (`bridge_group`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

METHODS = ("arr_residual", "yoy_same_quarter")
# Workbook and model row keys a segment key must not shadow (segment keys become row keys).
RESERVED = {"sub", "revenue", "cost", "gp", "gm", "opex", "amort", "cfo", "capex", "fcf", "customers", "arr", "nrr",
            "headcount", "g", "nl", "arpl", "hc", "ng", "conv", "rnd", "snm", "gna"}
# Budget-vs-actual bridge: fixed groups before and after one group per yoy segment (saas_metrics.backtest).
BRIDGE_BEFORE = ("expansion", "new_logo_volume", "new_logo_arr")
BRIDGE_AFTER = ("gross_margin", "headcount_pace", "cost_per_head", "amortization", "fcf_conversion")


@dataclass(frozen=True)
class Segment:
    key: str
    line: str
    label: str                  # Operating_Model row label
    short: str                  # short name used inside other labels ("Atlas", "EA", "Services")
    method: str
    recurring: bool = True
    arr_basis: str | None = None
    bridge_group: str | None = None
    bridge_label: str | None = None
    ca_label: str | None = None    # Cohort_ARR_Build row label (ttm segments)
    act_label: str | None = None   # BvA actuals row label

    @property
    def is_yoy(self) -> bool:
        return self.method == "yoy_same_quarter"

    @property
    def is_ttm(self) -> bool:
        return self.arr_basis == "trailing_12m"


def from_config(raw: dict) -> list[Segment]:
    segs = []
    for e in raw["segments"]:
        if e.get("method") not in METHODS:
            raise ValueError(f"segment {e.get('key')}: method must be one of {METHODS}")
        segs.append(Segment(
            key=e["key"], line=e["line"], label=e["label"], short=e.get("short", e["key"]), method=e["method"],
            recurring=e.get("recurring", True), arr_basis=e.get("arr_basis"),
            bridge_group=e.get("bridge_group", f"{e['key']}_mix_timing" if e["method"] == "yoy_same_quarter" else None),
            bridge_label=e.get("bridge_label"), ca_label=e.get("ca_label"), act_label=e.get("act_label")))
    validate(segs)
    if "arr_proxy" in raw:
        check_arr_proxy(raw, segs)
    order = raw.get("bridge", {}).get("order")
    if order is not None and list(order) != bridge_order(segs):
        raise ValueError(f"bridge.order must be {bridge_order(segs)} (it follows the segments)")
    return segs


def validate(segs: list[Segment]) -> None:
    res = [s for s in segs if s.method == "arr_residual"]
    if len(res) != 1 or not res[0].recurring:
        raise ValueError("exactly one recurring segment must use method arr_residual")
    nonrec = [s for s in segs if not s.recurring]
    if len(nonrec) != 1 or not nonrec[0].is_yoy:
        raise ValueError("exactly one non-recurring segment (method yoy_same_quarter) is supported")
    if any(s.is_ttm and (not s.recurring or not s.is_yoy) for s in segs):
        raise ValueError("arr_basis trailing_12m is only for recurring yoy segments")
    if any(s.recurring and s.is_yoy and not s.is_ttm for s in segs):
        raise ValueError("a recurring yoy segment needs arr_basis: trailing_12m (it must enter the ARR proxy)")
    keys = [s.key for s in segs]
    if len(set(keys)) != len(keys):
        raise ValueError("segment keys must be unique")
    bad = [k for k in keys if not re.fullmatch(r"[a-z][a-z0-9]*", k) or k in RESERVED]
    if bad:
        raise ValueError(f"segment keys {bad} must be lowercase letters/digits and not one of {sorted(RESERVED)}")
    groups = [s.bridge_group for s in segs if s.is_yoy]
    if len(set(groups)) != len(groups) or set(groups) & set(BRIDGE_BEFORE + BRIDGE_AFTER):
        raise ValueError(f"segment bridge groups {groups} must be unique and new")


def bridge_order(segs: list[Segment]) -> list[str]:
    return [*BRIDGE_BEFORE, *(s.bridge_group for s in segs if s.is_yoy), *BRIDGE_AFTER]


def check_arr_proxy(raw: dict, segs: list[Segment]) -> None:
    """The arr_proxy section must name the same lines as the segments."""
    ap = raw["arr_proxy"]
    res = next(s for s in segs if s.method == "arr_residual")
    if ap["run_rate"]["line"] != res.line:
        raise ValueError(f"arr_proxy.run_rate.line must be the arr_residual segment's line {res.line}")
    if [t["line"] for t in ap.get("trailing_12m", [])] != [s.line for s in segs if s.is_ttm]:
        raise ValueError("arr_proxy.trailing_12m must list the trailing_12m segments' lines, in order")


def active() -> list[Segment]:
    from saas_metrics import config
    return from_config(config.ACTIVE.raw)


def residual() -> Segment:
    return next(s for s in active() if s.method == "arr_residual")


def recurring() -> list[Segment]:
    return [s for s in active() if s.recurring]


def nonrecurring() -> Segment:
    return next(s for s in active() if not s.recurring)


def yoy() -> list[Segment]:
    return [s for s in active() if s.is_yoy]


def ttm() -> list[Segment]:
    return [s for s in active() if s.is_ttm]
