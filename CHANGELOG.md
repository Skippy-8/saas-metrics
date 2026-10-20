# Changelog

## 0.1.0 (2026-10-20)

First release.

- **Inputs:** read CSV, CSV.GZ, Parquet, XLSX or a DataFrame, with column mapping.
- **Spreading:** service periods are spread by day, exact to the cent; anything after the as-of date is deferred.
- **Pre-flight checks.**
- **Engine:** a DuckDB SQL waterfall, identity exact to the cent, plus segment and channel waterfalls, a retention
  triangle, concentration and tier migration.
- **Headline metrics:** ARR, ARPA, quick ratio, logo and gross revenue churn, and NRR, GRR and logo retention
  over 12 months.
- **An auditable Excel workbook:** live formulas and a Checks tab.
- **A command line** and a synthetic sample export.
