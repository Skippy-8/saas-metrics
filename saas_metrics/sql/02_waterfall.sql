-- Revenue waterfall, per customer and period, then rolled up.
--   Beginning + New + Reactivation + Expansion - Contraction - Churn = Ending   (exact, in cents)
-- New: first ever positive period. Reactivation: positive again after a period at zero.
CREATE OR REPLACE TABLE movements AS
WITH m AS (
    SELECT customer_id, period_end, pidx, segment, channel, revenue AS cur,
           lag(revenue, 1, 0) OVER w AS prev,
           coalesce(max(CASE WHEN revenue > 0 THEN 1 ELSE 0 END) OVER (
               PARTITION BY customer_id ORDER BY pidx ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS seen_before
    FROM grid
    WINDOW w AS (PARTITION BY customer_id ORDER BY pidx)
)
SELECT *,
       CASE WHEN prev = 0 AND cur > 0 AND seen_before = 0 THEN cur ELSE 0 END AS new_logo,
       CASE WHEN prev = 0 AND cur > 0 AND seen_before = 1 THEN cur ELSE 0 END AS reactivation,
       CASE WHEN prev > 0 AND cur > prev THEN cur - prev ELSE 0 END AS expansion,
       CASE WHEN prev > 0 AND cur > 0 AND cur < prev THEN prev - cur ELSE 0 END AS contraction,
       CASE WHEN prev > 0 AND cur = 0 THEN prev ELSE 0 END AS churn
FROM m;

CREATE OR REPLACE VIEW waterfall AS
SELECT period_end, any_value(pidx) AS pidx,
       sum(prev) AS beginning, sum(new_logo) AS new_logo, sum(reactivation) AS reactivation,
       sum(expansion) AS expansion, sum(contraction) AS contraction, sum(churn) AS churn, sum(cur) AS ending,
       count(*) FILTER (WHERE new_logo > 0) AS new_logos, count(*) FILTER (WHERE reactivation > 0) AS reactivated_logos,
       count(*) FILTER (WHERE churn > 0) AS churned_logos, count(*) FILTER (WHERE cur > 0) AS active_logos,
       sum(prev) + sum(new_logo) + sum(reactivation) + sum(expansion) - sum(contraction) - sum(churn) - sum(cur) AS check_diff
FROM movements GROUP BY period_end ORDER BY period_end;

CREATE OR REPLACE VIEW waterfall_by_segment AS
SELECT segment, period_end,
       sum(prev) AS beginning, sum(new_logo) AS new_logo, sum(reactivation) AS reactivation,
       sum(expansion) AS expansion, sum(contraction) AS contraction, sum(churn) AS churn, sum(cur) AS ending,
       sum(prev) + sum(new_logo) + sum(reactivation) + sum(expansion) - sum(contraction) - sum(churn) - sum(cur) AS check_diff
FROM movements GROUP BY ALL ORDER BY segment, period_end;

CREATE OR REPLACE VIEW waterfall_by_channel AS
SELECT channel, period_end,
       sum(prev) AS beginning, sum(new_logo) AS new_logo, sum(reactivation) AS reactivation,
       sum(expansion) AS expansion, sum(contraction) AS contraction, sum(churn) AS churn, sum(cur) AS ending,
       sum(prev) + sum(new_logo) + sum(reactivation) + sum(expansion) - sum(contraction) - sum(churn) - sum(cur) AS check_diff
FROM movements GROUP BY ALL ORDER BY channel, period_end;
