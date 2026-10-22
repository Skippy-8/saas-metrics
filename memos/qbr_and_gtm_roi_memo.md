# MongoDB: frozen-forecast QBR and GTM capital-allocation memo

*An FP&A exercise built only from public SEC filings. The operating model was frozen at Q2 FY26 using information filed by 2025-08-27, then scored against the 4 quarters MongoDB reported afterwards (Q3 FY26 to Q2 FY27). Figures are in US dollars; "non-GAAP" in the model excludes stock-based compensation and amortization of intangibles. Not investment advice.*

## Section 1: Executive summary and scorecard

- **Revenue beat the frozen plan by $46.8M (+1.7%).** The plan's total error over the year (-1.7%) was smaller than either naive baseline's, and far smaller than the error in management's own guidance at the cutoff (-8.6% over the 2 quarters it covered). Quarter by quarter, however, the driver model (mean absolute error 3.4%) did not beat a simple trend extrapolation (3.0%).
- **Profit and cash beat by more than revenue.** Non-GAAP operating income was $77.4M above plan (+15.9%) and free cash flow $316.4M above (+92.3%). Most of the cash surprise came from a planning assumption (cash conversion), not from the business.
- **Efficiency improved through the year.** Rule of 40 rose from 34.5% to 54.2%, CAC payback shortened from 24.9 to 21.8 months, and the magic number rose from 0.54 to 0.66.
- **Recommendation:** do not commit to 30 incremental Enterprise AEs on the case's assumptions. Fund a pilot of 10 and release the rest only if ramped reps reach 42% of quota, the attainment at which the hiring case breaks even.

| 4 quarters, Q3 FY26 to Q2 FY27 | Frozen plan | Actual | Variance | % |
|---|---:|---:|---:|---:|
| Revenue | $2,736.0M | $2,782.8M | +$46.8M | +1.7% |
| Non-GAAP operating income | $487.7M | $565.1M | +$77.4M | +15.9% |
| Free cash flow | $342.6M | $659.0M | +$316.4M | +92.3% |

## Section 2: Forecast backtest and variance diagnosis

**How the forecast was built.** Atlas revenue follows an ARR roll-forward: the existing book expands at the disclosed net ARR expansion rate (119% at the cutoff) and new logos add ARR at the trailing-year rate. Enterprise Advanced (EA) and services revenue grow year over year from the same quarter, which keeps their seasonality. Operating expenses are headcount times non-GAAP spend per employee by department. New-logo counts come from a calibrated synthetic cohort panel that reconciles to reported customers; MongoDB does not disclose cohorts or churn, so the churn rate behind it is an assumption. Nothing filed after the cutoff feeds the plan.

**Accuracy against simple alternatives.** The two naive baselines use only pre-cutoff revenue: the seasonal version grows each quarter of last year by trailing-year growth (21.9%), and the trend version compounds the last year's average quarterly growth (5.5%).

| Revenue forecast error | Q3 FY26 | Q4 FY26 | Q1 FY27 | Q2 FY27 | Mean abs. error | Total error |
|---|---:|---:|---:|---:|---:|---:|
| Driver model (Base) | +3.9% | -3.1% | -1.1% | -5.5% | 3.4% | -1.7% |
| Driver model (Upside) | +5.7% | -1.0% | +1.5% | -2.6% | 2.7% | +0.7% |
| Trend naive | -0.7% | -5.4% | +0.9% | -5.2% | 3.0% | -2.7% |
| Seasonal naive | +2.7% | -3.8% | -2.7% | -6.6% | 3.9% | -2.8% |
| Management guidance | -6.2% | -10.8% | n/a | n/a | 8.5% | -8.6% |

The scenario band was too narrow: actual revenue fell inside the Downside-to-Upside range in only 1 of the 4 quarters, above the Upside case in 2 and below the Downside case in 1. In Q3 FY26 the plan over-forecast by +3.9%, and even the Downside scenario was too high (+2.2%). The most accurate forecast per quarter was the driver model's Upside case (2.7% mean absolute error). Management guided that quarter to $589.5M against an actual $628.3M, and guided non-GAAP operating income to $68.0M against $123.1M (company definition): guidance was conservative, which made it a poor point forecast.

<!-- WATERFALLS -->

**Revenue bridge.** Each driver is replaced in turn by the value that reproduces the reported result; the order is fixed (volume before rate, revenue before cost), and the attribution depends on it.

|  | Q3 FY26 | Q4 FY26 | Q1 FY27 | Q2 FY27 | 4Q |
|---|---:|---:|---:|---:|---:|
| Frozen plan | $652.8M | $673.7M | $680.3M | $729.2M | $2,736.0M |
| Expansion of existing customers (NRR / usage) | +$1.3M | +$4.0M | +$6.8M | +$11.8M | +$23.8M |
| New-logo volume | +$0.7M | +$1.6M | +$2.4M | +$4.0M | +$8.6M |
| ARR per new logo | -$4.4M | -$1.2M | -$7.2M | -$1.0M | -$13.8M |
| Enterprise Advanced mix / timing | -$19.2M | +$17.4M | +$6.3M | +$27.6M | +$32.0M |
| Services | -$2.7M | -$0.4M | -$1.0M | +$0.2M | -$3.8M |
| **Actual** | **$628.3M** | **$695.1M** | **$687.6M** | **$771.8M** | **$2,782.8M** |

- **EA timing explains the quarterly pattern.** The plan grew EA 9.9% year over year in every quarter; actual year-over-year growth ranged from -7.3% to +35.9%. Upfront recognition of term licenses moved revenue between quarters: -$19.2M in the first test quarter, then +$17.4M in the next.
- **Expansion added $23.8M to revenue** as the disclosed net ARR expansion rate rose from 119% to 122%. The plan held it flat.
- **New-logo ARR was lower than planned (-$13.8M)** while logo volume was higher (+$8.6M). In the calibrated panel, more customers landed at smaller initial spend; because the panel's churn rate is assumed, treat this split as directional.

**Operating income bridge.**

|  | Q3 FY26 | Q4 FY26 | Q1 FY27 | Q2 FY27 | 4Q |
|---|---:|---:|---:|---:|---:|
| Frozen plan | $118.2M | $121.2M | $113.0M | $135.3M | $487.7M |
| Expansion of existing customers (NRR / usage) | +$1.0M | +$3.1M | +$5.3M | +$9.3M | +$18.8M |
| New-logo volume | +$0.5M | +$1.3M | +$1.9M | +$3.1M | +$6.8M |
| ARR per new logo | -$3.5M | -$1.0M | -$5.6M | -$0.8M | -$10.9M |
| Enterprise Advanced mix / timing | -$15.1M | +$13.7M | +$5.0M | +$21.7M | +$25.2M |
| Services | +$1.4M | +$0.2M | +$0.5M | -$0.1M | +$2.0M |
| Gross margin rate | -$6.7M | +$2.2M | -$5.3M | +$7.1M | -$2.8M |
| Headcount pace | +$20.7M | +$28.0M | +$35.4M | +$43.0M | +$127.1M |
| Cost per head | +$2.5M | -$20.5M | -$33.3M | -$37.5M | -$88.8M |
| Amortization | +$0.0M | +$0.1M | -$0.1M | +$0.0M | +$0.0M |
| **Actual** | **$119.0M** | **$148.2M** | **$116.7M** | **$181.1M** | **$565.1M** |

- **The people cost lines offset each other.** The plan added 130 employees a quarter from an estimated 5,818 at the cutoff. The Q4 FY26 10-K later showed 5,636 employees, only 78 more than a year earlier, and implies about 5,597 at the cutoff. Fewer heads (+$127.1M) at a higher cost per head (-$88.8M) netted to +$38.2M. Headcount is disclosed only annually, so the split between pace and rate is uncertain for the latest quarters; the net is exact.
- **Cash conversion drove the free cash flow beat (+$263.4M).** The plan converted non-GAAP operating income to cash at the prior year's ratios for the same quarters (0.36x and 0.23x); actual conversion was 1.19x and 1.20x.

**What I would change in the next forecast.**

- Forecast EA from term-license renewals and bookings rather than a flat year-over-year rate; it is the largest source of quarterly revenue error.
- Model cash conversion from billings and collections, or at least a multi-year seasonal average, not a single prior-year quarter.
- Let the expansion rate move with the trend in disclosed net ARR expansion instead of holding it at the cutoff value.
- Keep a naive benchmark in every forecast review: this year the trend baseline was more accurate per quarter than the Base case.

## Section 3: Unit economics and GTM capital allocation

|  | Q2 FY26 (cutoff) | Q2 FY27 (latest) |
|---|---:|---:|
| Revenue growth, YoY | 23.7% | 30.5% |
| FCF margin, trailing 4Q | 10.8% | 23.7% |
| Rule of 40 | 34.5% | 54.2% |
| CAC payback (months) | 24.9 | 21.8 |
| Magic number | 0.54 | 0.66 |
| Net ARR expansion rate | 119% | 122% |
| LTV : CAC [Illustrative: assumed churn] | 5.2x | 5.7x |

Sales efficiency improved, but MongoDB still recovered sales and marketing spend in roughly 21.8 months of gross-margin-adjusted net new ARR, and generated $0.66 of annualized subscription revenue per dollar of prior-year sales and marketing.

**The case.** Case A hires 30 Enterprise AEs at $350k each, with a $1.0M annual quota, 70% attainment and a 6-month ramp. Case B sells an add-on to the 2,564 customers above $100k ARR at a 15% attach rate. Both use the disclosed expansion rate, the trailing non-GAAP subscription gross margin and a 10% hurdle rate over 36 months; the remaining inputs are illustrative.

| Case (36 months) | Payback | NPV | IRR | Peak investment | ARR at month 36 |
|---|---:|---:|---:|---:|---:|
| A. Hire Enterprise AEs, plan assumptions | month 25 | $23.1M | 114.2% | -$9.2M | $75.0M |
| A. Same, at MongoDB's current efficiency | not within 36 months | -$13.8M | -60.7% | -$17.5M | $27.7M |
| B. Product attach to >$100k customers | month 31 | $1.7M | 16.0% | -$16.2M | $23.6M |

**On its own assumptions, hiring wins:** case A pays back in month 25 with an NPV of $23.1M, ahead of case B (month 31, $1.7M). **But those assumptions imply $1.33 of new ARR per dollar of AE cost, against MongoDB's magic number of 0.54 at the cutoff.** At MongoDB's current efficiency, attainment would be about 26% instead of 70%, and case A does not pay back within 36 months (NPV -$13.8M). The decision turns on one number: attainment. The case breaks even at 42%.

**Recommendation.**

- Approve 10 Enterprise AEs now, about a third of the request.
- After the 6-month ramp, release the remaining hires only if ramped attainment is at or above 42%; below it, redirect the budget.
- Run case B as a low-cost pricing test first: its NPV is positive but thin ($1.7M), and the 15% attach rate is unproven.

*Methods: 17 quarterly and annual reports (10-Q, 10-K) and 17 earnings releases (8-K), parsed from XBRL and filing text with every value traced to its accession number; the store passes a pre-flight of tie-out checks, and every figure in this memo is generated from the model and verified by tests. The cohort panel is calibrated synthetic data, the GTM inputs marked illustrative are assumptions, and headcount between annual 10-K disclosures is interpolated or estimated.*
