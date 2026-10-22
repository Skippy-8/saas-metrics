# Revenue and cash quality from SEC XBRL

`saas-metrics quality` reads four disclosures a subscription P&L does not show, for the tickers in
`configs/` that carry a `benchmark` section (default: MDB, DDOG, SNOW, TTD):

| metric | tag family (in the order tried) | kind |
|---|---|---|
| deferred revenue | `ContractWithCustomerLiability` current + noncurrent, then the legacy `DeferredRevenue` current + noncurrent, then either total | instant |
| remaining performance obligations (RPO) | `RevenueRemainingPerformanceObligation` | instant |
| stock-based compensation | `ShareBasedCompensation` (cash-flow add-back), then `AllocatedShareBasedCompensationExpense` | flow |

A **family** is the set of concepts that together mean one number. It counts only when every concept
in it is tagged for that period: a balance sheet tagged current but not noncurrent is a blank, not a
partial balance. Families are tried per period, so a company that changed tags mid-history gets one
continuous series with the substitution recorded on every spliced quarter (`deferred_revenue_source`,
`deferred_revenue_spliced`). `benchmark.quality` in a config replaces a metric's family list — that is
how a company-extension RPO tag is read.

## Formulas

For fiscal quarter *q* and trailing four quarters *TTM*:

- **Δ deferred revenue** = deferred revenue(*q*) − deferred revenue(*q*−1), both quarter-end balances.
- **Implied billings** = revenue(*q*) + Δ deferred revenue(*q*). Cash billed in the quarter,
  regardless of when it is recognised. TTM billings is the sum of the four quarters, and
  **billings / revenue** = TTM implied billings ÷ TTM revenue.
- **RPO cover** = RPO ÷ TTM revenue (years). A proxy: the strict version divides by *next*-year
  revenue, which is a forecast.
- **Free cash flow** = operating cash flow − capex, TTM.
- **SBC-adjusted FCF** = FCF − stock-based compensation, TTM. Operating cash flow adds SBC back as a
  non-cash charge; subtracting it is what free cash flow would be if employees were paid in cash.
  The margin divides by TTM revenue.

## What is absent stays absent

- A metric the filer does not tag is **blank, never zero**. TTD tags no deferred revenue and no RPO
  in companyfacts, so its implied billings, billings-to-revenue and RPO metrics are `n/a`; its
  SBC-adjusted FCF is still computed.
- Implied billings needs two consecutive balances. A quarter with no prior balance is blank rather
  than compared with a guessed opening balance.
- When two families tag the same period with different values, one is used (the primary) and the gap
  is reported. They are never averaged. Example: DDOG's 2018-12-31 `ContractWithCustomerLiability`
  (62.9M) disagrees with its deferred-revenue components (70.7M) by 7.8M.
- Every run cross-checks Δ deferred revenue against the cash-flow statement's own
  `IncreaseDecreaseInContractWithCustomerLiability`. Gaps above 2% of the balance are reported; they
  are FX translation on deferred balances and the ASC 606 transition, which the cash-flow line
  carries and the balance sheet does not.

## Checks (a failing check exits 1)

1. Deferred revenue is tagged for every quarter of the trailing two years (skipped as a pass, with a
   note, when the filer tags no deferred revenue at all).
2. Deferred revenue components sum to the total tag, where the same filer tags both in one period,
   within 0.5%.
3. RPO is tagged at the latest quarter (or a note says it is not tagged).
4. The two SBC tags agree within 0.5% where both are tagged.

## Outputs

`--out table.csv|.parquet` writes the summary and a `<name>_quarterly` file with the per-quarter
series. `.xlsx` writes `Revenue_Quality` (inputs as blue values, ratios as live formulas, notes
underneath) and `Quarterly`.
