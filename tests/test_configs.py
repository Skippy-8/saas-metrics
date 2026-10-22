"""Configs load and validate, and the model reads its company-specific values from them."""
from __future__ import annotations

import re
from datetime import date

import pytest
import yaml

from saas_metrics import (
    backtest,
    build,
    cohort,
    company,
    config,
    drivers,
    kpis,
    lines,
    periods,
    roi,
    segments,
)
from saas_metrics.fiscal import FiscalCalendar

MDB = company.load("MDB")


def test_all_configs_load():
    assert company.available() == ["DDOG", "MDB", "SNOW", "TTD"]
    for t in company.available():
        c = company.load(t)
        assert c.ticker == t and "benchmark" in c.raw
    assert MDB.has_model and not company.load("DDOG").has_model


def test_config_module_reads_mdb_yaml():
    w, b, s = MDB.raw["windows"], MDB.raw["backtest"], MDB.raw["sec"]
    assert (config.TICKER, config.CIK) == ("MDB", 1441816)
    assert (config.FIRST_QUARTER_END, config.LAST_QUARTER_END, config.PREWINDOW_FROM) == (
        w["first_quarter_end"], w["last_quarter_end"], w["prewindow_from"])
    assert (config.T0_QUARTER_END, config.CUTOFF_DATE) == (b["t0_quarter_end"], b["cutoff_date"])
    assert (config.FILINGS_FROM, config.EIGHT_K_FROM, config.EIGHT_K_TO) == (
        s["filings_from"], s["eight_k_from"], s["eight_k_to"])
    assert s["scope_months"] == build.scope_months()


def test_lines_and_kpis_come_from_the_config():
    assert [ln.name for ln in lines.LINES] == list(MDB.raw["lines"])
    rpo = next(ln for ln in lines.LINES if ln.name == "rpo")
    assert rpo.kind == "instant" and rpo.alts[0][1] == "*"
    sbc = next(ln for ln in lines.LINES if ln.name == "sbc_cost_subscription")
    # two-axis alternate: axes sorted and joined, members in the same order
    assert sbc.alts[0] == ("srt:ProductOrServiceAxis|us-gaap:StatementOfIncomeLocationBalanceAxis",
                           "us-gaap:LicenseMember|us-gaap:CostOfGoodsAndServicesSold")
    assert [n for n, _, _ in kpis.PATTERNS] == [k["name"] for k in MDB.raw["kpis"]]


def test_mdb_model_sections_drive_the_code():
    assert MDB.raw["bridge"]["order"] == list(backtest.GROUPS)
    assert MDB.raw["departments"] == list(drivers.DEPTS)
    assert MDB.raw["cohort_ledger"]["churn_q"] == cohort.assumptions().churn_q
    assert [s.key for s in segments.active()] == [s["key"] for s in MDB.raw["segments"]]
    assert roi.MONTHS == MDB.raw["unit_econ"]["horizon_months"]


def _unit_econ_values(raw: dict) -> dict:
    ue = raw["unit_econ"]
    a, b = ue["case_a"], ue["case_b"]
    return {"roi_n_ae": a["n_ae"], "roi_ae_cost": a["ae_cost"], "roi_support_pct": a["support_pct"],
            "roi_quota": a["quota"], "roi_attain": a["attainment"], "roi_ramp": a["ramp_months"],
            "roi_commission": a["commission"], "roi_attach": b["attach_rate"], "roi_attach_arr": b["arr_per_attach"],
            "roi_adopt": b["adoption_months"], "roi_upfront": b["upfront_cost"], "roi_run_cost": b["run_cost_monthly"],
            "roi_discount": ue["discount_rate"]}


def test_gtm_inputs_follow_the_config(db, tmp_path):
    got = {x.key: x.value for x in roi.roi_inputs(db)}
    assert {k: got[k] for k in _unit_econ_values(MDB.raw)} == _unit_econ_values(MDB.raw)
    raw = yaml.safe_load(MDB.path.read_text())
    raw["unit_econ"]["case_a"].update(n_ae=12, quota=800_000)
    raw["unit_econ"]["discount_rate"] = 0.12
    p = tmp_path / "MDB.yaml"
    p.write_text(yaml.safe_dump(raw))
    with config.using(p):
        got = {x.key: x.value for x in roi.roi_inputs(db)}
    assert (got["roi_n_ae"], got["roi_quota"], got["roi_discount"]) == (12, 800_000, 0.12)


def test_fiscal_calendar_matches_periods_for_january_year_end():
    cal = FiscalCalendar(1)
    for e in periods.quarter_ends("2019-04-30", "2028-01-31"):
        assert cal.label(e) == periods.label(e)
        assert cal.fiscal(e) == periods.fiscal(e)
        assert cal.fy_start(e) == periods.fy_start(e)
        assert cal.quarter_start(e) == periods.quarter_start(e)


def test_fiscal_calendar_december_year_end():
    cal = FiscalCalendar(12)
    assert cal.label("2026-06-30") == "Q2 FY26" and cal.label("2025-12-31") == "Q4 FY25"
    assert cal.fy_start("2026-06-30") == date(2026, 1, 1)
    assert cal.is_quarter_end("2026-03-31") and not cal.is_quarter_end("2026-04-30")
    with pytest.raises(ValueError):
        cal.fiscal("2026-04-30")


@pytest.mark.parametrize("mutate, message", [
    (lambda r: r["company"].pop("cik"), "company.cik"),
    (lambda r: r["company"].update(fiscal_year_end_month=13), "1-12"),
    (lambda r: r["benchmark"]["lines"].pop("revenue"), "missing ['revenue']"),
    (lambda r: r["benchmark"]["lines"].update(revenue="Revenue"), "prefixed XBRL concept"),
    (lambda r: r["benchmark"]["lines"].update(ebitda="us-gaap:X"), "unknown ['ebitda']"),
    (lambda r: r["backtest"].update(cutoff_date="2025-07-01"), "cutoff_date"),
    (lambda r: r["backtest"].update(t0_quarter_end="2025-04-30", cutoff_date="2025-06-01"), "fiscal Q2"),
    (lambda r: r.pop("segments"), "needs all of"),
    (lambda r: r.pop("cohort_ledger"), "`cohort_ledger`"),
    (lambda r: r["segments"][1].update(method="arr_residual"), "exactly one recurring segment"),
    (lambda r: r["segments"][1].pop("arr_basis"), "needs arr_basis"),
    (lambda r: r["segments"][2].update(recurring=True, arr_basis="trailing_12m"), "exactly one non-recurring"),
    (lambda r: r["segments"][0].update(key="arr"), "must be lowercase"),
    (lambda r: r["segments"][2].update(line="revenue_svc"), "lines not defined"),
    (lambda r: r["arr_proxy"]["trailing_12m"].clear(), "arr_proxy.trailing_12m"),
    (lambda r: r["bridge"].update(order=list(reversed(r["bridge"]["order"]))), "bridge.order must be"),
])
def test_invalid_configs_are_rejected(tmp_path, mutate, message):
    raw = yaml.safe_load(MDB.path.read_text())
    mutate(raw)
    p = tmp_path / "BAD.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(company.ConfigError, match=re.escape(message)):
        company.load(p)


def test_mdb_keeps_its_original_paths():
    assert config.RAW == config.ROOT / "data" / "raw"
    assert config.DB_PATH == config.ROOT / "data" / "saasfpa.duckdb"
    assert config.WORKBOOK == config.ROOT / "models" / "saas_operating_model.xlsx"
    assert config.SOURCES == config.ROOT / "docs" / "sources.md"
