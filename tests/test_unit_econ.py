"""`saas_metrics unit-econ`: the GTM case with rep attrition, margin overrides and a payback target."""
from __future__ import annotations

import pandas as pd
import pytest

from saas_metrics import cli, config, roi


@pytest.fixture(scope="module")
def inp(db):
    return {x.key: x.value for x in roi.roi_inputs(db)}


def test_zero_attrition_is_the_workbook_case(inp):
    base = roi.gtm_cases(inp)
    same = roi.gtm_cases({**inp, "roi_rep_attrition": 0.0})
    for k in ("A", "B"):
        pd.testing.assert_frame_equal(base[k], same[k])
    assert roi.summary(base, inp).loc["A", "payback_month"] == 25        # the memo's figure


def test_attrition_backfills_restart_the_ramp(inp):
    lost = roi.gtm_cases({**inp, "roi_rep_attrition": 0.30})["A"]
    full = roi.gtm_cases(inp)["A"]
    payroll = lambda df: df.cost - inp["roi_commission"] * df.booked_arr  # noqa: E731
    assert payroll(lost).values == pytest.approx(payroll(full).values)  # backfilled: same team, same payroll
    assert (lost.booked_arr <= full.booked_arr + 1e-6).all() and lost.booked_arr.iloc[-1] < full.booked_arr.iloc[-1]
    # long run: productivity settles at the tenure mix, sum over t of min(1, t/R) m (1-m)^(t-1)
    m = 1 - 0.7 ** (1 / 12)
    r = inp["roi_ramp"]
    steady = sum(min(1.0, t / r) * m * (1 - m) ** (t - 1) for t in range(1, 2000))
    long = roi.gtm_cases({**inp, "roi_rep_attrition": 0.30, "roi_months": 400})["A"]
    assert long.ramp.iloc[-1] == pytest.approx(steady, rel=1e-6)


def test_payback_target_attainment(inp):
    need = roi.attainment_for_payback(inp, 24)
    at = roi.summary(roi.gtm_cases({**inp, "roi_attain": need}), inp).loc["A", "payback_month"]
    below = roi.summary(roi.gtm_cases({**inp, "roi_attain": need - 0.005}), inp).loc["A", "payback_month"]
    assert at <= 24 and (pd.isna(below) or below > 24)
    assert roi.attainment_for_payback({**inp, "roi_ae_cost": 5e7}, 12) is None


def test_inputs_without_a_store_need_the_sourced_values():
    with pytest.raises(ValueError, match="--target-customers --nrr --gross-margin"):
        roi.resolve_inputs(None, {})
    vals, _, src = roi.resolve_inputs(None, {"roi_target_customers": 500, "roi_nrr": 1.1, "roi_gm": 0.7})
    ue = config.ACTIVE.raw["unit_econ"]
    assert vals["roi_n_ae"] == ue["case_a"]["n_ae"] and src["roi_n_ae"] == "config"
    assert vals["roi_gm"] == 0.7 and src["roi_gm"] == "command line"


def test_cli_runs_offline_with_overrides(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "none.duckdb")
    out = tmp_path / "flows.csv"
    cli.main(["unit-econ", "--target-customers", "800", "--nrr", "1.15", "--cogs-pct", "0.25", "--n-ae", "12",
              "--rep-attrition", "0.2", "--target-payback", "30", "--out", str(out)])
    text = capsys.readouterr().out
    assert "case A hires 12 Enterprise AEs" in text and "Quota attainment to pay back by month 30" in text
    assert "0.75   [command line]" in text                               # gross margin = 1 - COGS
    flows = pd.read_csv(out)
    assert set(flows["case"]) == {"A", "B"} and len(flows) == 2 * config.ACTIVE.raw["unit_econ"]["horizon_months"]
    with pytest.raises(SystemExit, match="not both"):
        cli.main(["unit-econ", "--gross-margin", "0.7", "--cogs-pct", "0.3"])
