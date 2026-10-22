-- SaaS efficiency metrics per quarter and data vintage.
-- Non-GAAP here = GAAP less stock-based compensation and intangible amortisation only; MongoDB's own
-- non-GAAP also removes items such as employer payroll tax on equity and restructuring, so these
-- figures are close to, not identical with, the company's reported non-GAAP numbers.
CREATE OR REPLACE VIEW unit_economics AS
WITH w AS (
    SELECT q.*, c.arr_proxy,
           q.snm - q.sbc_snm AS snm_ng,
           q.revenue_subscription - q.cost_subscription + q.sbc_cost_subscription AS sub_gp_ng,
           q.operating_income + q.sbc_cost_subscription + q.sbc_cost_services + q.sbc_rnd + q.sbc_snm
             + q.sbc_gna + q.amort_intangibles AS op_inc_ng,
           q.cfo - q.capex AS fcf
    FROM q_wide q
    JOIN cohort_calibration c USING (vintage, period_end)
    WHERE q.in_window
),
x AS (
    SELECT *,
        sub_gp_ng / revenue_subscription                                    AS sub_gross_margin_ng,
        op_inc_ng / revenue                                                 AS op_margin_ng,
        fcf / revenue                                                       AS fcf_margin,
        arr_proxy - lag(arr_proxy) OVER v                                   AS net_new_arr,
        lag(snm_ng) OVER v                                                  AS snm_ng_prev,
        revenue / lag(revenue, 4) OVER v - 1                                AS revenue_growth_yoy,
        sum(fcf) OVER (v ROWS 3 PRECEDING) / sum(revenue) OVER (v ROWS 3 PRECEDING) AS fcf_margin_ttm
    FROM w
    WINDOW v AS (PARTITION BY vintage ORDER BY period_end)
)
SELECT vintage, period_end, fiscal_label, revenue, arr_proxy, net_new_arr,
       sub_gross_margin_ng, op_inc_ng, op_margin_ng, fcf, fcf_margin, fcf_margin_ttm, revenue_growth_yoy,
       -- months of prior-quarter S&M to recover via gross-margin-adjusted net new ARR
       CASE WHEN net_new_arr > 0 THEN 12 * snm_ng_prev / (net_new_arr * sub_gross_margin_ng) END AS cac_payback_months,
       -- annualised subscription revenue added per dollar of prior-quarter S&M
       (revenue_subscription - lag(revenue_subscription) OVER v2) * 4 / snm_ng_prev AS magic_number,
       100 * (revenue_growth_yoy + fcf_margin_ttm)                          AS rule_of_40,
       -- burn multiple only means something while FCF is negative
       CASE WHEN fcf < 0 AND net_new_arr > 0 THEN -fcf / net_new_arr END AS burn_multiple
FROM x
WINDOW v2 AS (PARTITION BY vintage ORDER BY period_end)
ORDER BY vintage, period_end;
