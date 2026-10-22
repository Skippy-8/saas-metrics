-- Customer x period grid. Input tables (registered by saas_metrics.engine):
--   billing_norm(customer_id, period_end, revenue DECIMAL(18,2), segment, channel)  one row per customer-period
--   calendar(period_end, pidx)                                                        every period, gaps included
-- Each customer appears from its first period with positive revenue to the last calendar period;
-- periods without billing count as 0, so churn and reactivation are visible.
CREATE OR REPLACE TABLE grid AS
WITH firsts AS (
    SELECT customer_id, min(period_end) AS first_end
    FROM billing_norm WHERE revenue > 0 GROUP BY customer_id
),
attrs AS (  -- a customer's group = its segment / channel in its last billed period (stable assignment)
    SELECT customer_id, arg_max(segment, period_end) AS segment, arg_max(channel, period_end) AS channel
    FROM billing_norm WHERE revenue > 0 GROUP BY customer_id
)
SELECT f.customer_id, c.period_end, c.pidx, f.first_end,
       coalesce(b.revenue, 0)::DECIMAL(18, 2) AS revenue, a.segment, a.channel
FROM firsts f
JOIN calendar c ON c.period_end >= f.first_end
JOIN attrs a USING (customer_id)
LEFT JOIN billing_norm b ON b.customer_id = f.customer_id AND b.period_end = c.period_end;
