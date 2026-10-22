-- Customer concentration per period (billed customers only).
CREATE OR REPLACE VIEW concentration AS
WITH r AS (
    SELECT period_end, customer_id, revenue,
           row_number() OVER (PARTITION BY period_end ORDER BY revenue DESC, customer_id) AS rk,
           count(*) OVER (PARTITION BY period_end) AS n,
           sum(revenue) OVER (PARTITION BY period_end) AS total
    FROM grid WHERE revenue > 0
)
SELECT period_end, any_value(n) AS customers, any_value(total) AS revenue,
       sum(revenue) FILTER (WHERE rk <= ceil(n / 10)) / any_value(total) AS top_decile_share,
       sum(revenue) FILTER (WHERE rk <= 10) / any_value(total) AS top10_share,
       sum((revenue / total) * (revenue / total)) AS hhi
FROM r GROUP BY period_end ORDER BY period_end;
