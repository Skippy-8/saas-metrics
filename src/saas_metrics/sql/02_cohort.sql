-- [Calibrated Synthetic Cohort Panel] views. Cohorts are acquisition quarters; 'opening' is the
-- book at the first window quarter. Only NET expansion is identified: gross churn and contraction
-- are not disclosed, so the waterfall does not pretend to split them.

CREATE OR REPLACE VIEW cohort_ledger AS
SELECT a.vintage, a.cohort, a.period_end, a.arr, l.customers, a.label
FROM cohort_arr a JOIN cohort_logos l USING (vintage, cohort, period_end);

-- Trailing-4Q net ARR waterfall: start (cohorts alive 4 quarters ago) -> net expansion -> new cohorts -> end
CREATE OR REPLACE VIEW ndr_waterfall AS
WITH q AS (
    SELECT vintage, period_end,
           row_number() OVER (PARTITION BY vintage ORDER BY period_end) AS qn
    FROM (SELECT DISTINCT vintage, period_end FROM cohort_arr)
),
base AS (  -- for each quarter t, the quarter t-4
    SELECT t.vintage, t.period_end, b.period_end AS base_end
    FROM q t JOIN q b ON b.vintage = t.vintage AND b.qn = t.qn - 4
)
SELECT b.vintage, b.period_end, b.base_end,
       sum(s.arr) AS starting_arr,
       sum(e.arr) - sum(s.arr) AS net_expansion_existing,
       (SELECT sum(arr) FROM cohort_arr n
         WHERE n.vintage = b.vintage AND n.period_end = b.period_end
           AND n.cohort NOT IN (SELECT cohort FROM cohort_arr x WHERE x.vintage = b.vintage AND x.period_end = b.base_end)
       ) AS new_cohort_arr_t4q,
       (SELECT sum(arr) FROM cohort_arr z WHERE z.vintage = b.vintage AND z.period_end = b.period_end) AS ending_arr,
       sum(e.arr) / sum(s.arr) AS ndr_t4q
FROM base b
JOIN cohort_arr s ON s.vintage = b.vintage AND s.period_end = b.base_end
JOIN cohort_arr e ON e.vintage = b.vintage AND e.period_end = b.period_end AND e.cohort = s.cohort
GROUP BY ALL
ORDER BY b.vintage, b.period_end;

-- Logo retention by cohort: surviving share of each cohort's starting logos, by quarters since acquisition
CREATE OR REPLACE VIEW logo_retention AS
WITH start AS (
    SELECT l.vintage, l.cohort, l.period_end AS start_end, l.customers AS start_customers
    FROM cohort_logos l
    QUALIFY row_number() OVER (PARTITION BY l.vintage, l.cohort ORDER BY l.period_end) = 1
)
SELECT l.vintage, l.cohort, l.period_end,
       datediff('month', CAST(s.start_end AS DATE), CAST(l.period_end AS DATE)) // 3 AS quarters_since_start,
       l.customers / s.start_customers AS logo_retention
FROM cohort_logos l
JOIN start s USING (vintage, cohort)
ORDER BY l.vintage, l.cohort, l.period_end;
