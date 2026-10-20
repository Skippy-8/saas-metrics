"""Auditable Excel workbook: every metric is a live formula over the data, and a Checks tab proves the
workbook reproduces the Python engine.

Sheets
  About      grain, annualisation factor (a named input cell), definitions summary
  Waterfall  Beginning links to the prior Ending; Ending = B + New + Reactivation + Expansion - Contraction
             - Churn, checked against the data
  Customers  customers x periods revenue (the basis of the retention formulas)
  Metrics    ARR, ARPA, quick ratio, churn rates, and NRR / GRR / logo retention as SUMPRODUCT formulas
  Engine     the Python engine's metric values (reference only)
  Checks     per-period differences between Metrics formulas and Engine values; PASS within tolerance
Other tables (retention, concentration, tier migration, segment and channel waterfalls) follow as values.
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.workbook.defined_name import DefinedName

from saas_metrics.calendar import ANNUALIZE

BLUE, BOLD, GREY = Font(color="0000FF"), Font(bold=True), Font(color="808080", italic=True)
HDR = PatternFill("solid", fgColor="DDEBF7")
MONEY, PCT, RATIO = "#,##0.00", "0.0%", "0.00"
TOL = 1e-6


def _header(ws, cols):
    for i, h in enumerate(cols, start=1):
        c = ws.cell(1, i, h)
        c.font, c.fill = BOLD, HDR


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    return None if f != f else f


def write(report, path: Path, max_customers: int = 50_000) -> Path:
    if not report.ok:
        raise ValueError("report has no tables (a pre-flight check blocked it)")
    k = ANNUALIZE[report.grain]
    w, m = report.tables["waterfall"], report.tables["metrics"]
    cust = report.tables["customers"]
    n = len(w)
    wb = Workbook()

    about = wb.active
    about.title = "About"
    about["A1"], about["A1"].font = "saas-metrics workbook", Font(bold=True, size=13)
    about["A3"], about["B3"] = "Grain", report.grain
    about["A4"], about["B4"] = "Periods per year (annualisation and the 12-month look-back)", k
    about["B4"].font = BLUE
    wb.defined_names["PeriodsPerYear"] = DefinedName("PeriodsPerYear", attr_text="'About'!$B$4")
    about["A6"] = "Definitions: https://github.com/Skippy-8/saas-metrics/blob/main/docs/definitions.md"
    about["A7"] = "Blue cells are data; black cells are formulas. The Checks tab must read PASS."
    about["A6"].font = about["A7"].font = GREY
    about.column_dimensions["A"].width = 62

    ws = wb.create_sheet("Waterfall")
    _header(ws, ["Period end", "Beginning", "New", "Reactivation", "Expansion", "Contraction", "Churn", "Ending",
                 "Ending (data)", "Difference", "New customers", "Reactivated", "Churned customers", "Active customers"])
    for r, row in enumerate(w.itertuples(), start=2):
        ws.cell(r, 1, row.period_end)
        ws.cell(r, 2, float(row.beginning) if r == 2 else f"=H{r - 1}")
        for col, val in zip("CDEFG", (row.new_logo, row.reactivation, row.expansion, row.contraction, row.churn), strict=True):
            c = ws[f"{col}{r}"]
            c.value, c.font = float(val), BLUE
        ws[f"H{r}"] = f"=B{r}+C{r}+D{r}+E{r}-F{r}-G{r}"
        ws[f"I{r}"], ws[f"I{r}"].font = float(row.ending), BLUE
        ws[f"J{r}"] = f"=H{r}-I{r}"
        for col, val in zip("KLMN", (row.new_logos, row.reactivated_logos, row.churned_logos, row.active_logos), strict=True):
            c = ws[f"{col}{r}"]
            c.value, c.font = int(val), BLUE
        for col in "BCDEFGHIJ":
            ws[f"{col}{r}"].number_format = MONEY
    ws.column_dimensions["A"].width = 12

    with_matrix = len(cust) <= max_customers
    if with_matrix:
        cs = wb.create_sheet("Customers")
        periods = [c for c in cust.columns if c != "customer_id"]
        _header(cs, ["Customer", *[str(p) for p in periods]])
        for r, row in enumerate(cust.itertuples(index=False), start=2):
            cs.cell(r, 1, row[0])
            for j, v in enumerate(row[1:], start=2):
                c = cs.cell(r, j, float(v))
                c.font, c.number_format = BLUE, MONEY
        last = len(cust) + 1

        def ccol(i: int) -> str:
            col = get_column_letter(2 + i)
            return f"Customers!${col}$2:${col}${last}"

    ms = wb.create_sheet("Metrics")
    names = ["Period end", "Revenue", "ARR", "Active customers", "ARPA", "Quick ratio", "Logo churn rate",
             "Gross revenue churn rate", "NRR (12m)", "GRR (12m)", "Logo retention (12m)"]
    _header(ms, names)
    for i in range(n):
        r = i + 2
        ms[f"A{r}"] = f"=Waterfall!A{r}"
        ms[f"B{r}"] = f"=Waterfall!H{r}"
        ms[f"C{r}"] = f"=B{r}*PeriodsPerYear"
        ms[f"D{r}"] = f"=Waterfall!N{r}"
        ms[f"E{r}"] = f'=IF(D{r}>0,B{r}/D{r},"")'
        ms[f"F{r}"] = f'=IF(Waterfall!F{r}+Waterfall!G{r}>0,(Waterfall!C{r}+Waterfall!D{r}+Waterfall!E{r})/(Waterfall!F{r}+Waterfall!G{r}),"")'
        ms[f"G{r}"] = f'=IF(Waterfall!N{r - 1}>0,Waterfall!M{r}/Waterfall!N{r - 1},"")' if i else ""
        ms[f"H{r}"] = f'=IF(Waterfall!B{r}>0,(Waterfall!F{r}+Waterfall!G{r})/Waterfall!B{r},"")'
        if i >= k and with_matrix:
            base, now = ccol(i - k), ccol(i)
            ok = f'SUMIF({base},">0")>0'
            ms[f"I{r}"] = f'=IF({ok},SUMPRODUCT(({base}>0)*{now})/SUMIF({base},">0"),"")'
            ms[f"J{r}"] = (f'=IF({ok},SUMPRODUCT(({base}>0)*(({now}<{base})*{now}+({now}>={base})*{base}))'
                           f'/SUMIF({base},">0"),"")')
            ms[f"K{r}"] = f'=IF(COUNTIF({base},">0")>0,COUNTIFS({base},">0",{now},">0")/COUNTIF({base},">0"),"")'
        elif i >= k:   # very large tapes: retention as values (see max_customers)
            for col, key in zip("IJK", ("nrr_12m", "grr_12m", "logo_retention_12m"), strict=True):
                ms[f"{col}{r}"], ms[f"{col}{r}"].font = _num(m.at[i, key]), BLUE
        for col, fmt in zip("BCDEFGHIJK", (MONEY, MONEY, "0", MONEY, RATIO, PCT, PCT, PCT, PCT, PCT), strict=True):
            ms[f"{col}{r}"].number_format = fmt
    ms.column_dimensions["A"].width = 12

    eng = wb.create_sheet("Engine")
    keys = ["revenue", "arr", "active_customers", "arpa", "quick_ratio", "logo_churn_rate",
            "gross_revenue_churn_rate", "nrr_12m", "grr_12m", "logo_retention_12m"]
    _header(eng, ["Period end", *keys])
    for i in range(n):
        eng.cell(i + 2, 1, m.at[i, "period_end"])
        for j, key in enumerate(keys, start=2):
            eng.cell(i + 2, j, _num(m.at[i, key])).font = BLUE

    ck = wb.create_sheet("Checks")
    _header(ck, ["Period end", "Waterfall: formula - data", *[f"{h}: formula - engine" for h in names[1:]]])
    for i in range(n):
        r = i + 2
        ck[f"A{r}"] = f"=Waterfall!A{r}"
        ck[f"B{r}"] = f"=ABS(Waterfall!J{r})"
        for j in range(len(keys)):
            mc, ec = get_column_letter(2 + j), get_column_letter(2 + j)
            a, b = f"Metrics!{mc}{r}", f"Engine!{ec}{r}"
            ck.cell(r, 3 + j, f'=IF(AND(ISNUMBER({a}),ISNUMBER({b})),ABS({a}-{b})/MAX(1,ABS({b})),'
                              f'IF(AND(NOT(ISNUMBER({a})),NOT(ISNUMBER({b}))),0,1))')
    lastcol = get_column_letter(2 + len(keys))
    tr = n + 3
    ck[f"A{tr}"], ck[f"A{tr}"].font = "All checks", BOLD
    ck[f"B{tr}"] = f'=IF(MAX(B2:{lastcol}{n + 1})<=Tolerance,"PASS","FAIL")'
    ck[f"B{tr}"].font = BOLD
    ck[f"A{tr + 1}"], ck[f"B{tr + 1}"] = "Tolerance (relative; money in currency units)", TOL
    ck[f"B{tr + 1}"].font = BLUE
    wb.defined_names["Tolerance"] = DefinedName("Tolerance", attr_text=f"'Checks'!$B${tr + 1}")
    wb.defined_names["AllChecks"] = DefinedName("AllChecks", attr_text=f"'Checks'!$B${tr}")

    for name in ("retention", "concentration", "tier_migration", "waterfall_by_segment", "waterfall_by_channel"):
        t = report.tables.get(name)
        if t is None:
            continue
        s = wb.create_sheet(name[:31])
        _header(s, list(t.columns))
        for r, row in enumerate(t.itertuples(index=False), start=2):
            for j, v in enumerate(row, start=1):
                s.cell(r, j, _num(v) if hasattr(v, "as_tuple") else v)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path
