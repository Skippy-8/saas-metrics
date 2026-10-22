-- Retention triangles by acquisition vintage (first positive period).
--   logo_retention(age) = customers billed at that age / customers in the vintage
--   ndr(age)            = vintage revenue at that age / vintage revenue in its first period
-- Returning customers count as active again, so logo retention can rise with age.
CREATE OR REPLACE VIEW retention AS
WITH v AS (
    SELECT first_end AS vintage, customer_id, revenue,
           pidx - min(pidx) OVER (PARTITION BY customer_id) AS age
    FROM grid
),
agg AS (
    SELECT vintage, age, count(*) FILTER (WHERE revenue > 0) AS active_logos, sum(revenue) AS revenue
    FROM v GROUP BY vintage, age
),
base AS (SELECT vintage, active_logos AS logos_start, revenue AS revenue_start FROM agg WHERE age = 0)
SELECT a.vintage, a.age, b.logos_start, b.revenue_start, a.active_logos, a.revenue,
       a.active_logos / b.logos_start AS logo_retention,
       a.revenue / b.revenue_start AS ndr
FROM agg a JOIN base b USING (vintage)
ORDER BY a.vintage, a.age;
