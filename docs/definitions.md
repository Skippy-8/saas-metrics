# Definitions

There is no single standard for SaaS metrics. Two companies can each report "NRR" and mean different things.
This page states exactly what saas-metrics computes. Each definition is enforced by a test: the worked
example at the end is `tests/test_definitions.py`.

Notation: `rev(c, t)` is customer `c`'s revenue in period `t` (a month or a fiscal quarter). `k` is the
number of periods in a year: 12 at monthly grain, 4 at quarterly.

## 1. From billing lines to period revenue

1. **Mapping.** Columns are mapped to `customer_id`, `period` (a date inside the period), `revenue`, and
   optionally `service_start`, `service_end`, `segment` and `channel`.
2. **Spreading (optional, recommended for annual or multi-month plans).** A line with service dates is
   recognised evenly by day across its service period.
   - Each month gets amount × (days of service in that month) / (total service days).
   - Amounts are rounded to whole cents, and the leftover cents go to the months with the largest
     remainders, so the pieces sum exactly to the billed amount.
   - Service after the as-of date is deferred revenue, not yet earned, and is excluded and reported. The
     as-of date defaults to the end of the month of the export's latest line.
   - Without spreading, an annual invoice looks like a customer who arrives and then churns the next
     month. In the bundled sample, invoice-month accounting shows about twice the true churn events.
3. **Netting.** All lines for a customer in a period are summed, so split invoices and credit notes net
   out.
4. **Negative periods.** A customer-period that is net negative is flagged as a warning (it blocks unless
   allowed) and counted as 0 if allowed.
5. **Money** is held as `DECIMAL(18,2)`. The waterfall identity is checked to the cent.

## 2. Revenue waterfall (per period)

Each customer's movement from `rev(c, t-1)` (previous) to `rev(c, t)` (current):

| Component | Rule |
|---|---|
| New | previous = 0, current > 0, and the customer has never had revenue before |
| Reactivation | previous = 0, current > 0, and the customer had revenue in some earlier period |
| Expansion | previous > 0 and current > previous: current − previous |
| Contraction | previous > 0 and 0 < current < previous: previous − current |
| Churn | previous > 0 and current = 0: previous |

**Beginning + New + Reactivation + Expansion − Contraction − Churn = Ending**, exactly, every period.
Beginning is the previous period's Ending. Counts: new, reactivated and churned customers; active customers
are those with current > 0.

A customer enters the grid in its first period with revenue and stays in it afterwards. Periods without
billing count as 0, which is how churn and reactivation become visible. That is also why a period with
no billing at all is a blocking warning: it would read as everyone churning.

## 3. Headline metrics (per period t)

| Metric | Definition | Notes |
|---|---|---|
| Revenue | Ending of period t | MRR at monthly grain |
| ARR | Revenue × k | Run-rate: MRR × 12, or quarterly revenue × 4 |
| ARPA | Revenue / active customers | |
| Quick ratio | (New + Reactivation + Expansion) / (Contraction + Churn) | Blank when nothing was lost |
| Logo churn rate | Churned customers in t / active customers in t−1 | |
| Gross revenue churn rate | (Contraction + Churn) / Beginning | |
| NRR (12m) | Σ rev(c, t) / Σ rev(c, t−k), over customers with rev(c, t−k) > 0 | Point to point, one year apart. New customers since t−k are excluded. A customer who left and came back counts at its current revenue. |
| GRR (12m) | Σ min(rev(c, t), rev(c, t−k)) / Σ rev(c, t−k), same customers | Expansion is capped at the year-ago revenue, so GRR ≤ 1 and GRR ≤ NRR |
| Logo retention (12m) | Customers with rev(c, t−k) > 0 and rev(c, t) > 0 / customers with rev(c, t−k) > 0 | |

NRR, GRR and logo retention are blank for the first k periods, before there is a year-ago base.

**Choices other tools make differently**, so compare like with like:
- Some report NRR as a trailing-12-month average of monthly rates, or on ARR snapshots rather than period
  revenue. Here it is point to point, on recognised period revenue.
- Some treat a customer who returns as "new". Here it is reactivation, and it is still in the year-ago base.
- **Usage-based revenue** moves month to month, so monthly NRR is noisy. Quarterly grain (`--grain quarter`)
  is often the better view.

## 4. Other tables

- **Retention triangle.** For each acquisition vintage (first period with revenue) and age:
  - active customers ÷ vintage size (logo retention);
  - vintage revenue ÷ its first-period revenue (NDR by cohort).
- **Concentration.** Per period: the top-10 share of revenue, the top-decile share, and the
  Herfindahl–Hirschman index (sum of squared revenue shares).
- **Tier migration.** Customers and revenue moving between tiers of annualised revenue (defaults: 0, 10k,
  100k, 1M) from one period to the next. "Inactive" means no revenue.
- **Waterfalls by segment and by channel.** The same identity, within each group. A customer's group is
  its segment or channel in its last billed period.

## 5. Worked example (tests/test_definitions.py)

Monthly, January 2025 to February 2026 (14 months):

| Customer | Months 1–5 | Months 6–12 | Months 13–14 |
|---|---|---|---|
| A | 100 | 100 | 100 |
| B | 100 | 100 | 150 (expansion) |
| C | 100 | 0 (churned in month 6) | 80 (reactivation) |
| D | – | – | 50 (new) |
| E | 200 | 200 | 120 (contraction) |

**Month 6 (June 2025).**
- C churns: Beginning 500, Churn 100.
- Logo churn rate = 1 / 4 = 25%.
- Gross revenue churn rate = 100 / 500 = 20%.

**Month 13 (January 2026).**
- **Waterfall:** Beginning 400 + New 50 (D) + Reactivation 80 (C) + Expansion 50 (B) − Contraction 80 (E)
  − Churn 0 = Ending 500.
- **Quick ratio** = (50 + 80 + 50) / 80 = 2.25.
- **Year-ago base** (January 2025): A, B, C and E, totalling 500. D is new, so it is excluded.
- **NRR** = (100 + 150 + 80 + 120) / 500 = **90%**.
- **GRR** = (100 + min(150, 100) + 80 + 120) / 500 = **80%**.
- **Logo retention** = 4 / 4 = 100% (C is back).

**Month 14:** ARR = 500 × 12 = 6,000; ARPA = 500 / 5 = 100.
