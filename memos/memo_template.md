# MongoDB: frozen-forecast QBR and GTM capital-allocation memo

*An FP&A exercise built only from public SEC filings. The operating model was frozen at {{t0}} using information filed by {{cutoff}}, then scored against the {{n_quarters}} quarters MongoDB reported afterwards ({{window}}). Figures are in US dollars; "non-GAAP" in the model excludes stock-based compensation and amortization of intangibles. Not investment advice.*

## Section 1: Executive summary and scorecard

- **Revenue beat the frozen plan by {{revenue_var_abs}} ({{revenue_var_pct}}).** The plan's total error over the year ({{tot_model}}) was smaller than either naive baseline's, and far smaller than the error in management's own guidance at the cutoff ({{tot_guidance}} over the {{n_guided}} quarters it covered). Quarter by quarter, however, the driver model (mean absolute error {{mape_model}}) did not beat a simple trend extrapolation ({{mape_trend}}).
- **Profit and cash beat by more than revenue.** Non-GAAP operating income was {{ng_op_inc_var_abs}} above plan ({{ng_op_inc_var_pct}}) and free cash flow {{fcf_var_abs}} above ({{fcf_var_pct}}). Most of the cash surprise came from a planning assumption (cash conversion), not from the business.
- **Efficiency improved through the year.** Rule of 40 rose from {{rule40_t0}} to {{rule40_latest}}, CAC payback shortened from {{cac_t0}} to {{cac_latest}} months, and the magic number rose from {{magic_t0}} to {{magic_latest}}.
- **Recommendation:** do not commit to {{roi_n_ae}} incremental Enterprise AEs on the case's assumptions. Fund a pilot of {{pilot_n}} and release the rest only if ramped reps reach {{breakeven_att}} of quota, the attainment at which the hiring case breaks even.

{{table_plan_vs_actual}}

## Section 2: Forecast backtest and variance diagnosis

**How the forecast was built.** Atlas revenue follows an ARR roll-forward: the existing book expands at the disclosed net ARR expansion rate ({{nrr_t0}} at the cutoff) and new logos add ARR at the trailing-year rate. Enterprise Advanced (EA) and services revenue grow year over year from the same quarter, which keeps their seasonality. Operating expenses are headcount times non-GAAP spend per employee by department. New-logo counts come from a calibrated synthetic cohort panel that reconciles to reported customers; MongoDB does not disclose cohorts or churn, so the churn rate behind it is an assumption. Nothing filed after the cutoff feeds the plan.

**Accuracy against simple alternatives.** The two naive baselines use only pre-cutoff revenue: the seasonal version grows each quarter of last year by trailing-year growth ({{g_ttm}}), and the trend version compounds the last year's average quarterly growth ({{g_q}}).

{{table_errors}}

The scenario band was too narrow: actual revenue fell inside the Downside-to-Upside range in only {{n_inside}} of the {{n_quarters}} quarters, above the Upside case in {{n_above}} and below the Downside case in {{n_below}}. In {{q1_label}} the plan over-forecast by {{err_q3_model}}, and even the Downside scenario was too high ({{err_q3_downside}}). The most accurate forecast per quarter was the {{best_forecast}} ({{best_mape}} mean absolute error). Management guided that quarter to {{guide_q3}} against an actual {{act_q3}}, and guided non-GAAP operating income to {{guide_ng_q3}} against {{act_ng_q3_company}} (company definition): guidance was conservative, which made it a poor point forecast.

<!-- WATERFALLS -->

**Revenue bridge.** Each driver is replaced in turn by the value that reproduces the reported result; the order is fixed (volume before rate, revenue before cost), and the attribution depends on it.

{{table_rev_bridge}}

- **EA timing explains the quarterly pattern.** The plan grew EA {{ea_yoy_plan}} year over year in every quarter; actual year-over-year growth ranged from {{ea_yoy_act_range}}. Upfront recognition of term licenses moved revenue between quarters: {{br_rev_ea_q3}} in the first test quarter, then {{br_rev_ea_q4}} in the next.
- **Expansion added {{br_revenue_expansion_abs}} to revenue** as the disclosed net ARR expansion rate rose from {{nrr_t0}} to {{nrr_latest}}. The plan held it flat.
- **New-logo ARR was lower than planned ({{br_revenue_new_logo_arr}})** while logo volume was higher ({{br_revenue_new_logo_volume}}). In the calibrated panel, more customers landed at smaller initial spend; because the panel's churn rate is assumed, treat this split as directional.

**Operating income bridge.**

{{table_ng_bridge}}

- **The people cost lines offset each other.** The plan added {{net_hires_plan}} employees a quarter from an estimated {{hc_t0_est}} at the cutoff. The {{hc_fy26_label}} 10-K later showed {{hc_fy26}} employees, only {{hires_fy26}} more than a year earlier, and implies about {{hc_t0_act}} at the cutoff. Fewer heads ({{br_ng_op_inc_headcount_pace}}) at a higher cost per head ({{br_ng_op_inc_cost_per_head}}) netted to {{br_ng_people_net}}. Headcount is disclosed only annually, so the split between pace and rate is uncertain for the latest quarters; the net is exact.
- **Cash conversion drove the free cash flow beat ({{br_fcf_fcf_conversion}}).** The plan converted non-GAAP operating income to cash at the prior year's ratios for the same quarters ({{conv_plan_q3}} and {{conv_plan_q4}}); actual conversion was {{conv_act_q3}} and {{conv_act_q4}}.

**What I would change in the next forecast.**

- Forecast EA from term-license renewals and bookings rather than a flat year-over-year rate; it is the largest source of quarterly revenue error.
- Model cash conversion from billings and collections, or at least a multi-year seasonal average, not a single prior-year quarter.
- Let the expansion rate move with the trend in disclosed net ARR expansion instead of holding it at the cutoff value.
- Keep a naive benchmark in every forecast review: this year the trend baseline was more accurate per quarter than the Base case.

## Section 3: Unit economics and GTM capital allocation

{{table_unit_econ}}

Sales efficiency improved, but MongoDB still recovered sales and marketing spend in roughly {{cac_latest}} months of gross-margin-adjusted net new ARR, and generated ${{magic_latest}} of annualized subscription revenue per dollar of prior-year sales and marketing.

**The case.** Case A hires {{roi_n_ae}} Enterprise AEs at {{roi_ae_cost}} each, with a {{roi_quota}} annual quota, {{roi_attain}} attainment and a {{roi_ramp}}-month ramp. Case B sells an add-on to the {{roi_target_customers}} customers above {{threshold_100k}} ARR at a {{roi_attach}} attach rate. Both use the disclosed expansion rate, the trailing non-GAAP subscription gross margin and a {{roi_discount}} hurdle rate over {{roi_months}} months; the remaining inputs are illustrative.

{{table_roi}}

**On its own assumptions, hiring wins:** case A pays back in {{a_payback}} with an NPV of {{a_npv}}, ahead of case B ({{b_payback}}, {{b_npv}}). **But those assumptions imply ${{a_eff}} of new ARR per dollar of AE cost, against MongoDB's magic number of {{co_magic}} at the cutoff.** At MongoDB's current efficiency, attainment would be about {{att_co}} instead of {{roi_attain}}, and case A does not pay back within {{roi_months}} months (NPV {{a_low_npv}}). The decision turns on one number: attainment. The case breaks even at {{breakeven_att}}.

**Recommendation.**

- Approve {{pilot_n}} Enterprise AEs now, about a third of the request.
- After the {{roi_ramp}}-month ramp, release the remaining hires only if ramped attainment is at or above {{breakeven_att}}; below it, redirect the budget.
- Run case B as a low-cost pricing test first: its NPV is positive but thin ({{b_npv}}), and the {{roi_attach}} attach rate is unproven.

*Methods: {{n_filings}} quarterly and annual reports (10-Q, 10-K) and {{n_releases}} earnings releases (8-K), parsed from XBRL and filing text with every value traced to its accession number; the store passes a pre-flight of tie-out checks, and every figure in this memo is generated from the model and verified by tests. The cohort panel is calibrated synthetic data, the GTM inputs marked illustrative are assumptions, and headcount between annual 10-K disclosures is interpolated or estimated.*
