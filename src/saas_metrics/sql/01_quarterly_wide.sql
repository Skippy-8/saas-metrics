-- One row per (vintage, quarter) with every line as a column.
-- {LINES} is filled in from saas_metrics/lines.py by views.create().
CREATE OR REPLACE VIEW q_wide AS
PIVOT (SELECT vintage, period_end, fiscal_label, in_window, line, value FROM quarterly_financials)
ON line IN ({LINES}) USING first(value)
ORDER BY vintage, period_end;
