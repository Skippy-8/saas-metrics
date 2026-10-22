"""Parsers for KPI text, guidance tables and XBRL instances, on text shaped like the real filings."""
from __future__ import annotations

from saas_metrics import guidance, kpis, periods, xbrl


def test_fiscal_calendar():
    assert periods.label("2025-07-31") == "Q2 FY26"
    assert periods.label("2026-01-31") == "Q4 FY26"
    assert periods.fy_start("2026-01-31").isoformat() == "2025-02-01"
    assert periods.quarter_start("2024-04-30").isoformat() == "2024-02-01"
    assert len(periods.quarter_ends("2022-10-31", "2026-07-31")) == 16


def test_kpis_from_10q_sentences():
    text = ("As of July 31, 2025, we had over 59,900 customers spanning a wide range of industries. "
            "We had over 58,300 Mo ngoDB Atlas customers as of July 31, 2025 compared to over 49,200 as of July 31, 2024. "
            "The number of customers with $100,000 or greater in ARR and annualized MRR was 2,564 and 2,189 as of July 31, 2025 and 2024 , respectively. "
            "As of July 31, 2025, our net ARR expansion rate was approximately 119%. "
            "During the three months ended April 30, 2023, MongoDB Atlas revenue represented 65% as compared to 60% of our total revenue.")
    got = {k.kpi: k for k in kpis.parse(text)}
    assert got["total_customers"].value == 59_900 and got["total_customers"].qualifier == "over"
    assert got["atlas_customers"].value == 58_300          # despite the HTML split "Mo ngoDB"
    assert got["customers_100k_arr"].value == 2_564        # current period, not the comparative
    assert got["net_arr_expansion_rate"].value == 119
    assert got["atlas_pct_revenue_3m"].value == 65
    assert "2,564" in got["customers_100k_arr"].snippet


def test_nrr_lower_bound_wording():
    got = kpis.parse("Our net ARR expansion rate has consistently been over 120% demonstrating our ability.")
    assert got[0].value == 120 and got[0].qualifier == "over"


def test_guidance_layout_a_with_losses():
    text = ("Based on information available to management as of today, August 31, 2022, MongoDB is issuing the following "
            "financial guidance for the third quarter and full year fiscal 2023. Third Quarter Fiscal 2023 Full Year Fiscal 2023 "
            "Revenue $300.0 million to $303.0 million $1,196.0 million to $1,206.0 million "
            "Non-GAAP Loss from Operations ($10.0) million to ($8.0) million ($13.0) million to ($8.0) million "
            "Non-GAAP Net Loss per Share ($0.19) to ($0.16) ($0.35) to ($0.28) The guidance provided above")
    g = {(x.period, x.metric): x for x in guidance.parse(text)}
    assert g[("Q3 FY23", "revenue")].low == 300e6 and g[("FY23", "revenue")].high == 1_206e6
    assert g[("Q3 FY23", "non_gaap_operating_income")].low == -10e6
    assert g[("FY23", "non_gaap_eps")].high == -0.28
    assert g[("Q3 FY23", "revenue")].issued == "August 31, 2022"


def test_guidance_layout_b_takes_non_gaap_column():
    text = ("Second Quarter Fiscal 2027 Guidance Based on information available to management as of today, May 28, 2026, "
            "MongoDB is issuing the following financial guidance for the second quarter fiscal 2027. "
            "Revenues are expected to be in the range of: $729 million to $734 million GAAP Non-GAAP "
            "Income (Loss) from Operations are expected to be in the range of: $(10.0) million to $(6.0) million "
            "$152.0 million to $156.0 million Net Income (Loss) per Share is expected to be in the range of: "
            "$0.08 to $0.12 $1.58 to $1.61 Full Year Fiscal 2027 Guidance Based on information available to management "
            "as of today, May 28, 2026. Revenues are expected to be in the range of: $2.92 billion to $2.96 billion GAAP "
            "Conference Call")
    g = {(x.period, x.metric): x for x in guidance.parse(text)}
    assert (g[("Q2 FY27", "revenue")].low, g[("Q2 FY27", "revenue")].high) == (729e6, 734e6)
    assert g[("Q2 FY27", "non_gaap_operating_income")].low == 152e6   # non-GAAP, not the GAAP loss
    assert g[("Q2 FY27", "non_gaap_eps")].high == 1.61
    assert g[("FY27", "revenue")].high == 2.96e9


def test_reported_non_gaap_actuals():
    text = ("MongoDB Announces Second Quarter Fiscal 2026 Financial Results ... for the second quarter of fiscal 2026. "
            "Non-GAAP income from operations was $86.8 million, compared to $52.5 million in the year-ago period. "
            "Non-GAAP net income was $87.2 million, or $1.00 per share, based on 87.1 million shares.")
    a = {x.metric: x for x in guidance.parse_actuals(text)}
    assert a["non_gaap_operating_income"].value == 86.8e6 and a["non_gaap_operating_income"].period == "Q2 FY26"
    assert a["non_gaap_eps"].value == 1.00


INSTANCE = """<?xml version="1.0"?>
<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
      xmlns:us-gaap="http://fasb.org/us-gaap/2025" xmlns:srt="http://fasb.org/srt/2025">
  <context id="c1"><entity><identifier scheme="x">1</identifier></entity>
    <period><startDate>2026-05-01</startDate><endDate>2026-07-31</endDate></period></context>
  <context id="c2"><entity><identifier scheme="x">1</identifier><segment>
    <xbrldi:typedMember dimension="us-gaap:StatementOfIncomeLocationBalanceAxis">
      <us-gaap:QName.domain>us-gaap:ResearchAndDevelopmentExpense</us-gaap:QName.domain></xbrldi:typedMember>
    </segment></entity><period><startDate>2026-05-01</startDate><endDate>2026-07-31</endDate></period></context>
  <context id="c3"><entity><identifier scheme="x">1</identifier><segment>
    <xbrldi:explicitMember dimension="srt:ProductOrServiceAxis">us-gaap:LicenseMember</xbrldi:explicitMember>
    <xbrldi:typedMember dimension="us-gaap:StatementOfIncomeLocationBalanceAxis">
      <us-gaap:QName.domain>us-gaap:CostOfGoodsAndServicesSold</us-gaap:QName.domain></xbrldi:typedMember>
    </segment></entity><period><startDate>2026-05-01</startDate><endDate>2026-07-31</endDate></period></context>
  <us-gaap:AllocatedShareBasedCompensationExpense contextRef="c1" unitRef="usd" decimals="-5">148939000</us-gaap:AllocatedShareBasedCompensationExpense>
  <us-gaap:AllocatedShareBasedCompensationExpense contextRef="c2" unitRef="usd" decimals="-5">74506000</us-gaap:AllocatedShareBasedCompensationExpense>
  <us-gaap:AllocatedShareBasedCompensationExpense contextRef="c3" unitRef="usd" decimals="-5">9205000</us-gaap:AllocatedShareBasedCompensationExpense>
</xbrl>"""


def test_typed_members_are_dimensions_not_totals(tmp_path):
    p = tmp_path / "instance.xml"
    p.write_text(INSTANCE)
    by_value = {f.value: f for f in xbrl.parse(str(p))}
    assert by_value[148_939_000].dims == ()                                      # the only total
    assert by_value[74_506_000].dims == (("us-gaap:StatementOfIncomeLocationBalanceAxis",
                                          "us-gaap:ResearchAndDevelopmentExpense"),)
    assert len(by_value[9_205_000].dims) == 2                                    # explicit + typed
    assert by_value[148_939_000].concept == "us-gaap:AllocatedShareBasedCompensationExpense"
