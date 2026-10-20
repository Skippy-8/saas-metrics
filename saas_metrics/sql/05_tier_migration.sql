-- Tier migration between consecutive periods. Input table tiers(lower DECIMAL, label, ord), with
-- lower bounds in annualised revenue; `annualize` (12 monthly, 4 quarterly) is substituted in.
-- A customer at zero revenue (churned, or between periods) is in the tier "Inactive".
CREATE OR REPLACE VIEW tier_migration AS
WITH t AS (
    SELECT customer_id, period_end, prev, cur,
           (SELECT label FROM tiers WHERE prev * {ANNUALIZE} >= lower ORDER BY lower DESC LIMIT 1) AS from_tier,
           (SELECT label FROM tiers WHERE cur * {ANNUALIZE} >= lower ORDER BY lower DESC LIMIT 1) AS to_tier
    FROM movements WHERE prev > 0 OR cur > 0
)
SELECT period_end,
       CASE WHEN prev > 0 THEN from_tier ELSE 'Inactive' END AS from_tier,
       CASE WHEN cur > 0 THEN to_tier ELSE 'Inactive' END AS to_tier,
       count(*) AS customers, sum(prev) AS revenue_from, sum(cur) AS revenue_to
FROM t GROUP BY ALL ORDER BY period_end, from_tier, to_tier;
