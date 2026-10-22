"""Write models/saas_operating_model.xlsx: a formula-driven operating model.

Every forecast cell, and every derived historical cell, is a live Excel formula. Historical
inputs (blue) come from SEC filings as known at the cutoff; drivers live on Inputs_Scenarios
and are picked by the scenario selector. No numeric constant appears inside a formula except
0 and 1 (e.g. MATCH(..., 0), 1 + growth); 365 and 4 are named input cells.

Formulas are written from templates. In a template, for the current column:
  {key}             this sheet's row `key`
  {key@-n}          the same row n columns to the left
  {Sheet:key}       another sheet's row `key`
  {Sheet:key@a..b}  a range on another sheet, from column offset a to b
  {$driver}         the selected value of a driver on Inputs_Scenarios
  {#const}          a named constant on Inputs_Scenarios
  {FCFCONV}         the four fiscal-quarter FCF conversion drivers (for INDEX)
saas_metrics.model is the reference implementation the tests hold these formulas to.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from saas_metrics import config, drivers, model, segments
from saas_metrics.drivers import DEPTS

BLUE = Font(color="0000FF")
BOLD = Font(bold=True)
TITLE = Font(bold=True, size=13)
GREY = Font(color="808080", italic=True)
FC_FILL = PatternFill("solid", fgColor="F2F2F2")
HDR_FILL = PatternFill("solid", fgColor="DDEBF7")
MONEY = '$#,##0.0,,"M";[Red]-$#,##0.0,,"M"'
PCT = "0.0%"
NUM = "#,##0"

FIRST_COL = 4          # column D is the first quarter
HDR = {"fiscal_label": 2, "period_end": 3, "kind": 4, "days": 5, "fiscal_q": 6}
FIRST_ROW = 8

CONSTANTS = {"days_year": (model.DAYS_PER_YEAR, "Days per year (annualises a quarter)"),
             "q_per_year": (model.QUARTERS_PER_YEAR, "Quarters per year"),
             "months_year": (12, "Months per year"),
             "tolerance": (0.01, "Check tolerance, dollars")}


@dataclass
class Row:
    key: str
    label: str
    hist: str | None = None   # "input:<column>" or a formula template, or None for blank
    fc: str | list | None = None  # forecast template, or one template per forecast column, or None
    fmt: str = MONEY
    tag: str = ""
    bold: bool = False
    min_col: int = 0          # first history column index where the history formula is valid
    total: str = ""           # "sum" or "mape": add a 4Q column after the last forecast quarter
    start: str | None = None  # template for the first history column, when it differs (a recursion's seed)


@dataclass
class Sheet:
    name: str
    title: str
    rows: list[Row] = field(default_factory=list)


def model_sheets() -> list[Sheet]:
    res, ttm, svc, rec = segments.residual(), segments.ttm(), segments.nonrecurring(), segments.recurring()
    ttm_short = " + ".join(t.short for t in ttm)
    ttm_plus = "".join(f"+{{{t.key}_ttm}}" for t in ttm)
    ttm_less = "".join(f"-{{{t.key}_ttm}}" for t in ttm)
    ttm_rows = []
    for t in ttm:
        ttm_rows += [
            Row(t.key, t.ca_label or t.label, f"={{Operating_Model:{t.key}}}", f"={{Operating_Model:{t.key}}}"),
            Row(f"{t.key}_ttm", f"{t.short} revenue, trailing 4 quarters", f"=SUM({{Operating_Model:{t.key}@-3..0}})",
                f"=SUM({{Operating_Model:{t.key}@-3..0}})", min_col=3),
        ]
    ca = Sheet("Cohort_ARR_Build", "Cohort and ARR build [Calibrated Synthetic Cohort Panel for history]", [
        Row("customers", "Total customers (period end)", "input:customers", "={customers@-1}*(1-{$churn_q})+{$new_logos}",
            NUM, "[Sourced - SEC Filing] history", min_col=3),
        Row("churned_logos", "Churned logos", None, "={customers@-1}*{$churn_q}", NUM, "[Illustrative] rate"),
        Row("new_logos", "Gross new logos", "input:new_logos_ledger", "={$new_logos}", NUM,
            "[Calibrated Synthetic] history; [Forecast Assumption]"),
        *ttm_rows,
        Row("arr_existing", "ARR from existing customers", None, "={arr@-1}*{$nrr}^(1/{#q_per_year})"),
        Row("arr_new", "ARR from new logos", None, "={$new_logos}*{$arr_per_new_logo}"),
        Row("arr", f"ARR proxy ({res.short} annualised" + (f" + {ttm_short} trailing 4Q)" if ttm else ")"),
            f"={{Operating_Model:{res.key}}}*{{#days_year}}/{{days}}{ttm_plus}", "={arr_existing}+{arr_new}",
            tag="[Historical Derived] proxy", bold=True, min_col=3),
        Row(f"{res.key}_run_rate", f"{res.short} annual run-rate" + (f" (ARR - {ttm_short} trailing 4Q)" if ttm else ""),
            f"={{arr}}{ttm_less}", f"={{arr}}{ttm_less}", min_col=3),
        Row(f"{res.key}_rev", f"{res.short} revenue (run-rate x days / 365)", f"={{Operating_Model:{res.key}}}",
            f"={{{res.key}_run_rate}}*{{days}}/{{#days_year}}", bold=True),
        Row("nrr", "Net ARR expansion rate (disclosed / driver)", "input:nrr", "={$nrr}", PCT,
            "[Sourced - SEC Filing] history", min_col=3),
        Row("arr_ledger", "ARR proxy per cohort ledger (check)", "input:arr_ledger", None, MONEY,
            "[Calibrated Synthetic Cohort Panel]", min_col=3),
    ])
    hx = [Row("headcount", "Employees (period end)", "input:headcount", "={headcount@-1}+{$net_hires_q}", NUM,
              "see basis row", bold=True, min_col=3),
          Row("hc_basis", "Headcount basis", "input:hc_basis", None, "@", "")]
    for d, name in zip(DEPTS, ("R&D", "Sales and marketing", "General and administrative"), strict=True):
        hx += [
            Row(f"ng_{d}", f"{name}: non-GAAP spend", f"={{Operating_Model:{d}}}-{{Operating_Model:sbc_{d}}}",
                f"={{headcount}}*{{cph_{d}}}"),
            Row(f"cph_{d}", f"{name}: non-GAAP spend per employee (quarter)",
                f'=IF({{headcount}}="","",{{ng_{d}}}/{{headcount}})',
                f"={{cph_{d}@-1}}*(1+{{$cph_growth_{d}}})", '$#,##0', min_col=3),
        ]
    hx.append(Row("ng_total", "Non-GAAP operating expenses", "={ng_rnd}+{ng_snm}+{ng_gna}",
                  "={ng_rnd}+{ng_snm}+{ng_gna}", bold=True))
    ho = Sheet("Headcount_Opex_Build", "Headcount and operating expense build", hx)

    def seg_row(sg) -> Row:
        fc = (f"={{Cohort_ARR_Build:{sg.key}_rev}}" if sg.key == res.key else f"={{{sg.key}@-4}}*(1+{{${sg.key}_yoy}})")
        return Row(sg.key, sg.label, f"input:{sg.key}", fc, tag="[Sourced - SEC Filing] history")
    sub = "=" + "+".join(f"{{{sg.key}}}" for sg in rec)
    om_rows = [
        *(seg_row(sg) for sg in rec),
        Row("sub", "Subscription revenue", sub, sub),
        seg_row(svc),
        Row("revenue", "Total revenue", f"={{sub}}+{{{svc.key}}}", f"={{sub}}+{{{svc.key}}}", bold=True),
        Row("rev_yoy", "Revenue growth, YoY", "={revenue}/{revenue@-4}-1", "={revenue}/{revenue@-4}-1", PCT,
            min_col=4),
        Row("cost_sub", "Cost of subscription", "input:cost_sub", "={sub}*(1-{$sub_gm})"),
        Row("cost_svc", f"Cost of {svc.short.lower()}", "input:cost_svc", f"={{{svc.key}}}*(1-{{$svc_gm}})"),
        Row("cost", "Cost of revenue", "={cost_sub}+{cost_svc}", "={cost_sub}+{cost_svc}"),
        Row("gp", "Gross profit", "={revenue}-{cost}", "={revenue}-{cost}", bold=True),
        Row("gm", "Gross margin", "={gp}/{revenue}", "={gp}/{revenue}", PCT),
    ]
    for s in model.SBC_LINES:
        om_rows.append(Row(f"sbc_{s}", f"Stock-based compensation in {s.replace('_', ' ')}", f"input:sbc_{s}",
                           f"={{revenue}}*{{$sbc_pct_{s}}}"))
    for d, name in zip(DEPTS, ("Research and development", "Sales and marketing", "General and administrative"),
                       strict=True):
        om_rows.append(Row(d, name, f"input:{d}", f"={{Headcount_Opex_Build:ng_{d}}}+{{sbc_{d}}}"))
    om_rows += [
        Row("opex", "Total operating expenses", "={rnd}+{snm}+{gna}", "={rnd}+{snm}+{gna}", bold=True),
        Row("op_inc", "GAAP operating income (loss)", "={gp}-{opex}", "={gp}-{opex}", bold=True),
        Row("op_inc_reported", "GAAP operating income, as reported (check)", "input:op_inc_reported", None),
        Row("op_margin", "GAAP operating margin", "={op_inc}/{revenue}", "={op_inc}/{revenue}", PCT),
        Row("sbc_total", "Stock-based compensation, total",
            "=" + "+".join(f"{{sbc_{s}}}" for s in model.SBC_LINES),
            "=" + "+".join(f"{{sbc_{s}}}" for s in model.SBC_LINES)),
        Row("amort", "Amortization of intangibles", "input:amort", "={$amort_q}"),
        Row("ng_op_inc", "Non-GAAP operating income (excl. SBC and amortization)", "={op_inc}+{sbc_total}+{amort}",
            "={op_inc}+{sbc_total}+{amort}", bold=True),
        Row("ng_op_margin", "Non-GAAP operating margin", "={ng_op_inc}/{revenue}", "={ng_op_inc}/{revenue}", PCT),
        Row("cfo", "Operating cash flow", "input:cfo", None),
        Row("capex", "Capital expenditures", "input:capex", None),
        Row("fcf", "Free cash flow", "={cfo}-{capex}", "={ng_op_inc}*INDEX({FCFCONV},{fiscal_q})", bold=True),
        Row("fcf_margin", "FCF margin", "={fcf}/{revenue}", "={fcf}/{revenue}", PCT),
    ]
    om = Sheet("Operating_Model", "Operating model: income statement and free cash flow (USD)", om_rows)
    return [ca, ho, om]


class Builder:
    def __init__(self, hist: pd.DataFrame, n_fc: int):
        self.hist = hist
        self.n_hist = len(hist)
        self.n_fc = n_fc
        self.wb = Workbook()
        self.rows: dict[tuple[str, str], int] = {}
        self.driver_rows: dict[str, int] = {}
        self.const_cells: dict[str, str] = {}
        self.fcf_range = ""

    # ---- cell addressing -------------------------------------------------------------------
    def col(self, i: int) -> str:
        return get_column_letter(FIRST_COL + i)

    def render(self, template: str, sheet: str, i: int) -> str:
        def sub(m: re.Match) -> str:
            tok = m.group(1)
            if tok == "FCFCONV":
                return self.fcf_range
            if tok.startswith("$"):
                return f"Inputs_Scenarios!$F${self.driver_rows[tok[1:]]}"
            if tok.startswith("#"):
                return self.const_cells[tok[1:]]
            other, _, key = tok.rpartition(":")
            key, _, off = key.partition("@")
            target = other or sheet
            if key in HDR:
                return f"{self.col(i)}{HDR[key]}"
            row = self.rows[(target, key)]
            if off == "T0":  # absolute: the last historical column
                ref = f"${self.col(self.n_hist - 1)}${row}"
                return f"'{target}'!{ref}" if other else ref
            if ".." in off:
                a, b = (int(x) for x in off.split(".."))
                rng = f"{self.col(i + a)}{row}:{self.col(i + b)}{row}"
                return f"'{target}'!{rng}" if other else rng
            ref = f"{self.col(i + int(off or 0))}{row}"
            return f"'{target}'!{ref}" if other else ref
        return re.sub(r"\{([^{}]+)\}", sub, template)

    # ---- sheets ------------------------------------------------------------------------------
    def inputs(self, drv: list[drivers.Driver], roi_in: list | None = None, extra: list | None = None) -> None:
        ws = self.wb.active
        ws.title = "Inputs_Scenarios"
        ws["A1"] = "Inputs and scenarios"
        ws["A1"].font = TITLE
        ws["A3"], ws["C3"] = "Scenario", "Base"
        ws["A3"].font, ws["C3"].font = BOLD, Font(bold=True, color="0000FF")
        ws["C3"].fill = PatternFill("solid", fgColor="FFF2CC")
        dv = DataValidation(type="list", formula1='"Base,Upside,Downside"', allow_blank=False)
        ws.add_data_validation(dv)
        dv.add("C3")
        ws["E3"] = f"Point-in-time cutoff: {config.CUTOFF_DATE}. Every driver uses only information filed by then."
        ws["E3"].font = GREY
        r = 5
        for key, (val, label) in CONSTANTS.items():
            ws.cell(r, 1, key)
            ws.cell(r, 2, label)
            c = ws.cell(r, 3, val)
            c.font = BLUE
            self.const_cells[key] = f"Inputs_Scenarios!$C${r}"
            r += 1
        r += 1
        hdr = r
        for j, h in enumerate(["Driver", "Description", *drivers.SCENARIOS, "Selected", "Tag", "Derivation"]):
            c = ws.cell(hdr, 1 + j, h)
            c.font, c.fill = BOLD, HDR_FILL
        for d in drv:
            r += 1
            ws.cell(r, 1, d.key)
            ws.cell(r, 2, d.label)
            for j, sc in enumerate(drivers.SCENARIOS):
                c = ws.cell(r, 3 + j, d.value(sc))
                c.font, c.number_format = BLUE, d.fmt
            sel = ws.cell(r, 6, f"=INDEX($C{r}:$E{r},MATCH($C$3,$C${hdr}:$E${hdr},0))")
            sel.number_format, sel.font = d.fmt, BOLD
            ws.cell(r, 7, d.tag)
            ws.cell(r, 8, d.note).alignment = Alignment(wrap_text=False)
            self.driver_rows[d.key] = r
        if roi_in:
            r += 2
            c = ws.cell(r, 1, "GTM capital-allocation case (Unit_Economics_ROI); flat across scenarios")
            c.font = BOLD
            for x in roi_in:
                r += 1
                ws.cell(r, 1, x.key)
                ws.cell(r, 2, x.label)
                for j in range(3):
                    cell = ws.cell(r, 3 + j, x.value)
                    cell.font, cell.number_format = BLUE, x.fmt
                sel = ws.cell(r, 6, f"=INDEX($C{r}:$E{r},MATCH($C$3,$C${hdr}:$E${hdr},0))")
                sel.number_format, sel.font = x.fmt, BOLD
                ws.cell(r, 7, x.tag)
                ws.cell(r, 8, x.note)
                self.driver_rows[x.key] = r
        for title, items in extra or []:   # appended blocks, flat across scenarios
            r += 2
            ws.cell(r, 1, title).font = BOLD
            for x in items:
                r += 1
                ws.cell(r, 1, x.key)
                ws.cell(r, 2, x.label)
                for j in range(3):
                    cell = ws.cell(r, 3 + j, x.value)
                    cell.font, cell.number_format = BLUE, x.fmt
                sel = ws.cell(r, 6, f"=INDEX($C{r}:$E{r},MATCH($C$3,$C${hdr}:$E${hdr},0))")
                sel.number_format, sel.font = x.fmt, BOLD
                ws.cell(r, 7, x.tag)
                ws.cell(r, 8, x.note)
                self.driver_rows[x.key] = r
        q = [self.driver_rows[f"fcf_conv_q{k}"] for k in (1, 2, 3, 4)]
        assert q == list(range(q[0], q[0] + 4)), "FCF conversion drivers must be contiguous"
        self.fcf_range = f"Inputs_Scenarios!$F${q[0]}:$F${q[-1]}"
        for col, w in zip("ABCDEFGH", (24, 52, 13, 13, 13, 13, 24, 110), strict=True):
            ws.column_dimensions[col].width = w
        ws.freeze_panes = f"C{hdr + 1}"

    def model_sheet(self, sh: Sheet, values: pd.DataFrame, labels: list[str], ends: list[str], kinds: list[str],
                    days: list[int], fq: list[int]) -> None:
        ws = self.wb.create_sheet(sh.name)
        ws["A1"] = sh.title
        ws["A1"].font = TITLE
        for key, r in HDR.items():
            ws.cell(r, 2, key.replace("_", " ").capitalize()).font = GREY
        for i in range(self.n_hist + self.n_fc):
            c = self.col(i)
            for key, vals in (("fiscal_label", labels), ("period_end", ends), ("kind", kinds), ("days", days),
                              ("fiscal_q", fq)):
                cell = ws[f"{c}{HDR[key]}"]
                cell.value = vals[i]
                cell.font = BLUE if key in ("days", "fiscal_q") else BOLD
                if i >= self.n_hist:
                    cell.fill = FC_FILL
        for k, row in enumerate(sh.rows):
            self.rows[(sh.name, row.key)] = FIRST_ROW + k

        for row in sh.rows:
            r = self.rows[(sh.name, row.key)]
            ws.cell(r, 1, row.key).font = GREY
            ws.cell(r, 2, row.label).font = BOLD if row.bold else Font()
            ws.cell(r, 3, row.tag).font = GREY
            for i in range(self.n_hist + self.n_fc):
                cell = ws[f"{self.col(i)}{r}"]
                spec = row.hist if i < self.n_hist else row.fc
                if i == 0 and row.start is not None:
                    spec = row.start
                if isinstance(spec, list):
                    spec = spec[i - self.n_hist]
                if spec is None or (i < self.n_hist and i < row.min_col and not spec.startswith("input:")):
                    continue
                if spec.startswith("input:"):
                    v = values.iloc[i].get(spec[6:])
                    if v is None or (isinstance(v, float) and v != v) or v == "":
                        continue
                    cell.value = v
                    cell.font = BLUE
                else:
                    cell.value = spec  # rendered later, once every sheet's rows are known
                    cell.font = Font(bold=row.bold)
                cell.number_format = row.fmt
                if i >= self.n_hist:
                    cell.fill = FC_FILL
        ws.column_dimensions["A"].width = 18
        ws.column_dimensions["B"].width = 52
        ws.column_dimensions["C"].width = 30
        for i in range(self.n_hist + self.n_fc):
            ws.column_dimensions[self.col(i)].width = 12
        ws.freeze_panes = ws[f"{self.col(0)}{FIRST_ROW}"]
        # optional 4Q column after the last forecast quarter
        tc = self.n_hist + self.n_fc
        if any(row.total for row in sh.rows):
            h = ws[f"{self.col(tc)}{HDR['fiscal_label']}"]
            h.value, h.font, h.fill = "4Q", BOLD, FC_FILL
            first, last = self.col(self.n_hist), self.col(tc - 1)
            for row in sh.rows:
                if not row.total:
                    continue
                r = self.rows[(sh.name, row.key)]
                rng = f"{first}{r}:{last}{r}"
                c = ws[f"{self.col(tc)}{r}"]
                c.value = f"=SUM({rng})" if row.total == "sum" else f"=SUMPRODUCT(ABS({rng}))/COUNT({rng})"
                c.number_format = row.fmt if row.total == "sum" else PCT
                c.font, c.fill = BOLD, FC_FILL

    def render_all(self, sheets: list[Sheet]) -> None:
        for sh in sheets:
            ws = self.wb[sh.name]
            for row in sh.rows:
                r = self.rows[(sh.name, row.key)]
                for i in range(self.n_hist + self.n_fc):
                    cell = ws[f"{self.col(i)}{r}"]
                    if isinstance(cell.value, str) and cell.value.startswith("=") and "{" in cell.value:
                        cell.value = self.render(cell.value, sh.name, i)

    def checks(self) -> None:
        ws = self.wb.create_sheet("Checks")
        ws["A1"] = "Checks (each row: largest absolute difference across its quarters; PASS within tolerance)"
        ws["A1"].font = TITLE
        n_all = self.n_hist + self.n_fc
        segs = segments.active()
        defs = [
            ("GAAP operating income = as reported (history)", "Operating_Model", "={op_inc}-{op_inc_reported}", 0, self.n_hist),
            ("ARR proxy = cohort ledger ARR (history)", "Cohort_ARR_Build", "={arr}-{arr_ledger}", 3, self.n_hist),
            ("Revenue = " + " + ".join(sg.short for sg in segs), "Operating_Model",
             "={revenue}" + "".join(f"-{{{sg.key}}}" for sg in segs), 0, n_all),
            ("Non-GAAP op. income = GAAP + SBC + amortization", "Operating_Model",
             "={ng_op_inc}-{op_inc}-{sbc_total}-{amort}", 0, n_all),
            ("Opex = non-GAAP spend + SBC", "Operating_Model",
             "={opex}-{Headcount_Opex_Build:ng_total}-{sbc_rnd}-{sbc_snm}-{sbc_gna}", 3, n_all),
            ("Forecast ARR = existing + new", "Cohort_ARR_Build", "={arr}-{arr_existing}-{arr_new}",
             self.n_hist, n_all),
        ]
        r = 3
        for j, h in enumerate(["Check", "Max |diff|", "Result"]):
            c = ws.cell(r, 1 + j, h)
            c.font, c.fill = BOLD, HDR_FILL
        for name, sheet, tmpl, a, b in defs:
            r += 1
            ws.cell(r, 1, name)
            # per-quarter differences live in columns E.. on this row
            for i in range(a, b):
                # render() returns refs local to `sheet`; qualify them for the Checks sheet
                c = ws.cell(r, 5 + i, _qualify(self.render(tmpl, sheet, i), sheet))
                c.number_format = MONEY
            first, last = get_column_letter(5 + a), get_column_letter(5 + b - 1)
            ws.cell(r, 2, f"=SUMPRODUCT(ABS({first}{r}:{last}{r}))").number_format = '#,##0.00'
            ws.cell(r, 3, f'=IF(B{r}<={self.const_cells["tolerance"]},"PASS","FAIL")').font = BOLD
        ws.column_dimensions["A"].width = 50

    def roi_block(self, sheet: str, months: int) -> dict[str, str]:
        """Monthly cash flows for case A (AE hiring) and B (product attach); returns summary cell addresses."""
        ws = self.wb[sheet]
        start = max(r for (s_, _), r in self.rows.items() if s_ == sheet) + 3
        n_ae = config.ACTIVE.raw["unit_econ"]["case_a"]["n_ae"]
        co_name = config.ACTIVE.short_name
        ws.cell(start - 1, 1, f"B. GTM capital allocation: {n_ae:g} Enterprise AEs (A) vs product attach (B), monthly, "
                              "inputs on Inputs_Scenarios [mostly Illustrative]").font = BOLD
        first_only = {  # key -> (label, first-month template, later-month template, format)
            "roi_month": ("Month", None, None, "0"),
            "roi_g_m": ("Monthly net expansion of new ARR", "={$roi_nrr}^(1/{#months_year})-1",
                        "={$roi_nrr}^(1/{#months_year})-1", "0.00%"),
            "a_ramp": ("A: ramp", "=MIN(1,{roi_month}/{$roi_ramp})", "=MIN(1,{roi_month}/{$roi_ramp})", "0%"),
            "a_booked": ("A: new ARR booked", "={$roi_n_ae}*{$roi_quota}/{#months_year}*{$roi_attain}*{a_ramp}",
                         "={$roi_n_ae}*{$roi_quota}/{#months_year}*{$roi_attain}*{a_ramp}", MONEY),
            "a_arr": ("A: ARR", "={a_booked}", "={a_arr@-1}*(1+{roi_g_m})+{a_booked}", MONEY),
            "a_gp": ("A: gross profit", "={a_arr}/{#months_year}*{$roi_gm}", "={a_arr}/{#months_year}*{$roi_gm}", MONEY),
            "a_cost": ("A: AE cost + overhead + commission",
                       "={$roi_n_ae}*{$roi_ae_cost}/{#months_year}*(1+{$roi_support_pct})+{$roi_commission}*{a_booked}",
                       "={$roi_n_ae}*{$roi_ae_cost}/{#months_year}*(1+{$roi_support_pct})+{$roi_commission}*{a_booked}",
                       MONEY),
            "a_net": ("A: net cash flow", "={a_gp}-{a_cost}", "={a_gp}-{a_cost}", MONEY),
            "a_cum": ("A: cumulative", "={a_net}", "={a_cum@-1}+{a_net}", MONEY),
            "b_adopt": ("B: adoption", "=MIN(1,{roi_month}/{$roi_adopt})", "=MIN(1,{roi_month}/{$roi_adopt})", "0%"),
            "b_target": ("B: attach ARR target", "={$roi_target_customers}*{$roi_attach}*{$roi_attach_arr}*{b_adopt}",
                         "={$roi_target_customers}*{$roi_attach}*{$roi_attach_arr}*{b_adopt}", MONEY),
            "b_added": ("B: new ARR added", "=MAX(0,{b_target})", "=MAX(0,{b_target}-{b_target@-1})", MONEY),
            "b_arr": ("B: ARR", "={b_added}", "={b_arr@-1}*(1+{roi_g_m})+{b_added}", MONEY),
            "b_gp": ("B: gross profit", "={b_arr}/{#months_year}*{$roi_gm}", "={b_arr}/{#months_year}*{$roi_gm}", MONEY),
            "b_cost": ("B: build + run cost", "={$roi_run_cost}+{$roi_upfront}", "={$roi_run_cost}", MONEY),
            "b_net": ("B: net cash flow", "={b_gp}-{b_cost}", "={b_gp}-{b_cost}", MONEY),
            "b_cum": ("B: cumulative", "={b_net}", "={b_cum@-1}+{b_net}", MONEY),
        }
        for k, key in enumerate(first_only):
            self.rows[(sheet, key)] = start + k
        for key, (label, t1, tn, fmt) in first_only.items():
            r = self.rows[(sheet, key)]
            ws.cell(r, 1, key).font = GREY
            ws.cell(r, 2, label)
            for i in range(months):
                c = ws[f"{self.col(i)}{r}"]
                if key == "roi_month":
                    c.value, c.font = i + 1, BLUE
                else:
                    c.value = self.render(t1 if i == 0 else tn, sheet, i)
                c.number_format = fmt
        # summary
        r = start + len(first_only) + 1
        cells = {}
        last = self.col(months - 1)
        ws.cell(r, 2, "Summary").font = BOLD
        for case in ("a", "b"):
            net = f"{self.col(0)}{self.rows[(sheet, f'{case}_net')]}:{last}{self.rows[(sheet, f'{case}_net')]}"
            cum_r = self.rows[(sheet, f"{case}_cum")]
            cum = f"{self.col(0)}{cum_r}:{last}{cum_r}"
            arr_r = self.rows[(sheet, f"{case}_arr")]
            items = [
                ("cumulative_36m", f"Cumulative net cash, month {months}", f"={last}{cum_r}", MONEY),
                ("trough", "Peak cumulative investment (trough)", f"=MIN({cum})", MONEY),
                ("payback_month", "Payback month",
                 f'=IF(COUNTIF({cum},"<0")<COLUMNS({cum}),COUNTIF({cum},"<0")+1,"beyond horizon")', "0"),
                ("npv", "NPV at the discount rate",
                 f"=NPV((1+{self.render('{$roi_discount}', sheet, 0)})^(1/{self.const_cells['months_year']})-1,{net})",
                 MONEY),
                ("irr_annual", "IRR (annualised)", f"=(1+IRR({net}))^{self.const_cells['months_year']}-1", PCT),
                ("arr_month_36", f"ARR at month {months}", f"={last}{arr_r}", MONEY),
            ]
            for name, label, f, fmt in items:
                r += 1
                ws.cell(r, 1, f"{case}_{name}").font = GREY
                ws.cell(r, 2, f"{case.upper()}: {label}")
                c = ws[f"{self.col(0)}{r}"]
                c.value, c.number_format, c.font = f, fmt, BOLD
                cells[f"{case.upper()}.{name}"] = f"{self.col(0)}{r}"
        r += 2
        ws.cell(r, 2, f"Benchmark: case A's ramped sales efficiency vs {co_name}'s own").font = BOLD
        r += 1
        ws.cell(r, 1, "a_magic").font = GREY
        ws.cell(r, 2, "A: new ARR per $ of fully loaded AE cost, ramped (a magic-number analogue)")
        ae = ("{$roi_quota}*{$roi_attain}/({$roi_ae_cost}*(1+{$roi_support_pct})"
              "+{$roi_commission}*{$roi_quota}*{$roi_attain})")
        c = ws[f"{self.col(0)}{r}"]
        c.value, c.number_format = "=" + self.render(ae, sheet, 0), "0.00"
        cells["A.magic"] = f"{self.col(0)}{r}"
        r += 1
        ws.cell(r, 1, "co_magic").font = GREY
        ws.cell(r, 2, f"{co_name} magic number at T0 (trailing 4Q, from the scorecard above)")
        c = ws[f"{self.col(0)}{r}"]
        c.value, c.number_format = f"={self.render('{ue_magic@T0}', sheet, 0)}", "0.00"
        cells["company.magic"] = f"{self.col(0)}{r}"
        r += 1
        ws.cell(r, 1, "a_attain_at_co_magic").font = GREY
        ws.cell(r, 2, f"A: quota attainment at which case A is only as efficient as {co_name} today")
        c = ws[f"{self.col(0)}{r}"]
        co = cells["company.magic"]
        c.value = "=" + self.render(
            f"{co}*{{$roi_ae_cost}}*(1+{{$roi_support_pct}})/({{$roi_quota}}*(1-{co}*{{$roi_commission}}))", sheet, 0)
        c.number_format = PCT
        cells["A.attain_at_company_magic"] = f"{self.col(0)}{r}"
        return cells

    def sources(self, src: pd.DataFrame) -> None:
        ws = self.wb.create_sheet("Sources")
        ws["A1"] = "Sources: every historical input, with the SEC filing it came from (as known at the cutoff)"
        ws["A1"].font = TITLE
        cols = list(src.columns)
        for j, h in enumerate(cols):
            c = ws.cell(3, 1 + j, h)
            c.font, c.fill = BOLD, HDR_FILL
        for i, rec in enumerate(src.itertuples(index=False), start=4):
            for j, v in enumerate(rec):
                ws.cell(i, 1 + j, v)
        ws.freeze_panes = "A4"


_REF = re.compile(r"(?<![!A-Z$'])(\$?[A-Z]{1,3}\$?\d+)(?![\d(])")


def _qualify(formula: str, sheet: str) -> str:
    """Prefix bare A1 references with `sheet` (refs already qualified are left alone)."""
    out, last = [], 0
    for m in re.finditer(r"'[^']+'![$A-Z0-9:]+|Inputs_Scenarios![$A-Z0-9:]+", formula):
        out.append(_REF.sub(lambda x: f"'{sheet}'!{x.group(1)}", formula[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(_REF.sub(lambda x: f"'{sheet}'!{x.group(1)}", formula[last:]))
    return "".join(out)


def inputs_frame(con: duckdb.DuckDBPyConnection) -> tuple[pd.DataFrame, list[drivers.Driver]]:
    """One row per column (history as of the cutoff, then the forecast quarters) with every input.

    History columns carry the as-of-cutoff SEC values the model is built on. Forecast columns
    carry only what the BvA tab needs: actuals as finally reported and guidance known at the cutoff.
    """
    from saas_metrics import backtest

    hist = drivers.history(con)
    cal = con.execute("SELECT * FROM cohort_calibration WHERE vintage = 'as_of_cutoff'").df().set_index("period_end")
    hc = drivers.headcount_history(con, hist).set_index("period_end")
    kpi = con.execute("""SELECT period_end, value FROM kpi_disclosures WHERE kpi = ? AND filed <= ?""",
                      [config.ACTIVE.raw["cohort_ledger"]["nrr_kpi"], config.CUTOFF_DATE]).df()
    nrr = kpi.set_index("period_end")["value"] / 100
    rep = con.execute("SELECT period, value, known_at_cutoff FROM reported_non_gaap "
                      "WHERE metric = 'non_gaap_operating_income'").df()
    h = hist.set_index("period_end")
    v = pd.DataFrame({
        **{sg.key: h[sg.line] for sg in segments.active()},
        "cost_sub": h.cost_subscription, "cost_svc": h.cost_services,
        **{d: h[d] for d in DEPTS}, **{f"sbc_{s}": h[f"sbc_{s}"] for s in model.SBC_LINES},
        "op_inc_reported": h.operating_income, "amort": h.amort_intangibles, "cfo": h.cfo, "capex": h.capex,
        "customers": cal.total_customers.reindex(h.index), "nrr": nrr.reindex(h.index),
        "arr_ledger": cal.arr_proxy.reindex(h.index), "new_logos_ledger": cal.new_logos.reindex(h.index),
        "headcount": hc.headcount.reindex(h.index), "hc_basis": hc.hc_tag.reindex(h.index),
        "ng_company": h.fiscal_label.map(rep[rep.known_at_cutoff].set_index("period")["value"]),
    })
    act = backtest.actual_rows(con)
    fq = model.forecast_quarters()
    labels = [model.periods.label(e) for e in fq]
    assert labels[0].startswith("Q3") and labels[1].startswith("Q4"), "guidance layout assumes T1=Q3, T2=Q4"
    guide = con.execute("SELECT period, metric, mid FROM guidance WHERE known_at_cutoff AND filed = "
                        "(SELECT max(filed) FROM guidance WHERE known_at_cutoff)").df()
    gmid = {(r.period, r.metric): r.mid for r in guide.itertuples()}
    fy = labels[0].split()[1]
    f = pd.DataFrame(index=fq)
    for k in act_inputs():
        src = {"hc_basis": "hc_basis"}.get(k, k)
        f[f"act_{k}"] = act[src].reindex(fq) if src in act else None
    f["ng_company"] = [rep.set_index("period")["value"].get(lbl) for lbl in labels]
    f["guide_rev"] = [gmid.get((labels[0], "revenue")), gmid.get((fy, "revenue")), None, None]
    f["guide_ng"] = [gmid.get((labels[0], "non_gaap_operating_income")),
                     gmid.get((fy, "non_gaap_operating_income")), None, None]
    allv = pd.concat([v, f])
    allv.index.name = "period_end"
    return allv.reset_index(), drivers.derive(con)


def build(con: duckdb.DuckDBPyConnection, out: Path | None = None) -> Path:
    from saas_metrics import roi

    out = out or config.WORKBOOK

    values, drv = inputs_frame(con)
    hist = drivers.history(con)
    fc = model.forecast_quarters()
    b = Builder(hist, len(fc))
    b.inputs(drv, roi.roi_inputs(con), [damped_inputs(con)])
    labels = list(hist.fiscal_label) + [model.periods.label(e) for e in fc]
    ends = list(hist.period_end) + fc
    kinds = ["Actual"] * len(hist) + ["Forecast"] * len(fc)
    days = [model.days_in(e) for e in ends]
    fq = [int(lbl[1]) for lbl in labels]
    guide_filed = con.execute("SELECT max(filed) FROM guidance WHERE known_at_cutoff").fetchone()[0]
    sheets = [*model_sheets(), bva_sheet(str(guide_filed) if guide_filed else None), ue_sheet()]
    for sh in sheets:
        b.model_sheet(sh, values, labels, ends, kinds, days, fq)
    b.render_all(sheets)
    b.roi_block("Unit_Economics_ROI", roi.months())
    b.checks()
    src = con.execute("""SELECT line, fiscal_label, period_end, value, derivation, source_accn, filed
                         FROM quarterly_financials WHERE vintage = 'as_of_cutoff' ORDER BY line, period_end""").df()
    b.sources(src)
    out.parent.mkdir(parents=True, exist_ok=True)
    b.wb.save(out)
    return out


# ------------------------------------------------------------------------------------------------
# Phase 4 sheets: budget-vs-actual backtest, unit economics and the GTM case.
# saas_metrics.backtest and saas_metrics.roi are the reference implementations.
# ------------------------------------------------------------------------------------------------
def _hdr(key: str, label: str) -> Row:
    return Row(key, label, bold=True)


def act_inputs() -> dict[str, tuple[str, str | None]]:
    """Actual row key -> (label, history link template or None). Revenue segments first; the yoy
    segments link their history (their forecast needs the same quarter a year earlier)."""
    seg = {sg.key: (sg.act_label or sg.label, None if sg.method == "arr_residual" else f"={{Operating_Model:{sg.key}}}")
           for sg in segments.active()}
    return {**seg, **ACT_OTHER}


ACT_OTHER = {
    "cost_sub": ("Cost of subscription", None),
    "cost_svc": ("Cost of services", None),
    "sbc_cost_subscription": ("SBC in cost of subscription", None),
    "sbc_cost_services": ("SBC in cost of services", None),
    "rnd": ("Research and development", None),
    "snm": ("Sales and marketing", None),
    "gna": ("General and administrative", None),
    "sbc_rnd": ("SBC in R&D", None),
    "sbc_snm": ("SBC in S&M", None),
    "sbc_gna": ("SBC in G&A", None),
    "amort": ("Amortization of intangibles", None),
    "cfo": ("Operating cash flow", None),
    "capex": ("Capital expenditures", None),
    "customers": ("Total customers", "={Cohort_ARR_Build:customers}"),
    "nrr": ("Net ARR expansion rate", None),
    "headcount": ("Employees", None),
    "hc_basis": ("Headcount basis", None),
}


def bva_sheet(guide_filed: str | None) -> Sheet:
    """`guide_filed`: filing date of the guidance known at the cutoff; None leaves out the guidance rows."""
    from saas_metrics.backtest import explains, group_labels

    GROUP_LABELS = group_labels()
    GROUPS, EXPLAINS = tuple(GROUP_LABELS), explains()
    res, ttm, svc, rec = segments.residual(), segments.ttm(), segments.nonrecurring(), segments.recurring()
    sv = svc.key
    rows = [_hdr("h_act", "A. Actuals as finally reported (latest filings) [Sourced - SEC Filing]")]
    for k, (label, link) in act_inputs().items():
        fmt = PCT if k == "nrr" else (NUM if k in ("customers", "headcount") else ("@" if k == "hc_basis" else MONEY))
        rows.append(Row(f"act_{k}", label, link, f"input:act_{k}", fmt, min_col=3 if k == "customers" else 0))
    rows += [
        Row("act_sub", "Subscription revenue", None, "=" + "+".join(f"{{act_{sg.key}}}" for sg in rec)),
        Row("act_revenue", "Total revenue", None, f"={{act_sub}}+{{act_{sv}}}", bold=True),
        *(Row(f"act_{t.key}_ttm", f"{t.short} revenue, trailing 4 quarters", None, f"=SUM({{act_{t.key}@-3..0}})")
          for t in ttm),
        Row("act_arr", "ARR proxy", "={Cohort_ARR_Build:arr}",
            f"={{act_{res.key}}}*{{#days_year}}/{{days}}" + "".join(f"+{{act_{t.key}_ttm}}" for t in ttm), min_col=3),
    ]
    for d in DEPTS:
        rows += [Row(f"act_ng_{d}", f"{d.upper()} non-GAAP spend", None, f"={{act_{d}}}-{{act_sbc_{d}}}"),
                 Row(f"act_cph_{d}", f"{d.upper()} non-GAAP spend per employee", None,
                     f"={{act_ng_{d}}}/{{act_headcount}}", "$#,##0")]
    rows += [
        Row("act_ng_op", "Non-GAAP operating income (model definition)", None,
            "={act_revenue}-{act_cost_sub}-{act_cost_svc}+{act_sbc_cost_subscription}+{act_sbc_cost_services}"
            "-{act_ng_rnd}-{act_ng_snm}-{act_ng_gna}+{act_amort}", bold=True),
        Row("act_fcf", "Free cash flow", None, "={act_cfo}-{act_capex}", bold=True),
        Row("act_conv", "FCF / non-GAAP operating income", None, "={act_fcf}/{act_ng_op}", "0.00x"),
        _hdr("h_imp", "B. Actual-implied drivers (what the model needed to reproduce the actuals)"),
        Row("imp_g", "Quarterly net expansion of the existing book", None,
            "=MIN({act_nrr}^(1/{#q_per_year})-1,{act_arr}/{act_arr@-1}-1)", "0.00%"),
        Row("imp_new_logos", "Gross new logos", None, "={act_customers}-{act_customers@-1}*(1-{$churn_q})", NUM),
        Row("imp_arpl", "ARR per new logo", None, "=({act_arr}-{act_arr@-1}*(1+{imp_g}))/{imp_new_logos}", "$#,##0"),
        *(Row(f"imp_{sg.key}_yoy", f"{sg.short} revenue growth, YoY", None,
              f"={{act_{sg.key}}}/{{act_{sg.key}@-4}}-1", PCT) for sg in segments.yoy()),
        Row("imp_sub_gm", "Subscription gross margin (GAAP)", None, "=1-{act_cost_sub}/{act_sub}", PCT),
        Row("imp_svc_gm", f"{svc.short} gross margin (GAAP)", None, f"=1-{{act_cost_svc}}/{{act_{sv}}}", PCT),
        Row("imp_sbc_cogs", "SBC in cost of revenue, % of revenue", None,
            "=({act_sbc_cost_subscription}+{act_sbc_cost_services})/{act_revenue}", "0.00%"),
    ]

    def block(k: int) -> list[Row]:
        use = set(GROUPS[:k])
        p = f"s{k}_"

        def pick(group: str, actual: str, plan: str) -> str:
            return actual if group in use else plan

        def yoy(sg) -> str:
            return f"={{{p}{sg.key}@-4}}*(1+{pick(sg.bridge_group, f'{{imp_{sg.key}_yoy}}', f'{{${sg.key}_yoy}}')})"
        title = "plan" if k == 0 else f"plan with actual {', '.join(GROUP_LABELS[g].lower() for g in GROUPS[:k])}"
        out = [_hdr(f"{p}h", f"Step {k}: {title}")]
        out += [
            Row(f"{p}g", "Quarterly net expansion", None,
                pick("expansion", "={imp_g}", "={$nrr}^(1/{#q_per_year})-1"), "0.00%"),
            Row(f"{p}nl", "Gross new logos", None, pick("new_logo_volume", "={imp_new_logos}", "={$new_logos}"), NUM),
            Row(f"{p}arpl", "ARR per new logo", None,
                pick("new_logo_arr", "={imp_arpl}", "={$arr_per_new_logo}"), "$#,##0"),
            Row(f"{p}arr", "ARR proxy", "={Cohort_ARR_Build:arr}",
                f"={{{p}arr@-1}}*(1+{{{p}g}})+{{{p}nl}}*{{{p}arpl}}", min_col=3),
        ]
        for sg in ttm:
            out += [
                Row(f"{p}{sg.key}", f"{sg.short} revenue", f"={{Operating_Model:{sg.key}}}", yoy(sg)),
                Row(f"{p}{sg.key}_ttm", f"{sg.short} trailing 4Q", None, f"=SUM({{{p}{sg.key}@-3..0}})"),
            ]
        out += [
            Row(f"{p}{res.key}", f"{res.short} revenue", None,
                f"=({{{p}arr}}" + "".join(f"-{{{p}{t.key}_ttm}}" for t in ttm) + ")*{days}/{#days_year}"),
            *(Row(f"{p}{sg.key}", f"{sg.short} revenue", f"={{Operating_Model:{sg.key}}}", yoy(sg))
              for sg in segments.yoy() if not sg.is_ttm),
            Row(f"{p}sub", "Subscription revenue", None, "=" + "+".join(f"{{{p}{sg.key}}}" for sg in rec)),
            Row(f"{p}revenue", "Total revenue", None, f"={{{p}sub}}+{{{p}{sv}}}", bold=True),
            Row(f"{p}ng_gp", "Non-GAAP gross profit", None,
                f"={{{p}revenue}}-{{{p}sub}}*(1-{pick('gross_margin', '{imp_sub_gm}', '{$sub_gm}')})"
                f"-{{{p}{sv}}}*(1-{pick('gross_margin', '{imp_svc_gm}', '{$svc_gm}')})"
                f"+{{{p}revenue}}*({pick('gross_margin', '{imp_sbc_cogs}', '{$sbc_pct_cost_subscription}+{$sbc_pct_cost_services}')})"),
            Row(f"{p}hc", "Employees", "={Headcount_Opex_Build:headcount}",
                pick("headcount_pace", "={act_headcount}", f"={{{p}hc@-1}}+{{$net_hires_q}}"), NUM, min_col=3),
        ]
        for d in DEPTS:
            out.append(Row(f"{p}cph_{d}", f"{d.upper()} spend per employee", f"={{Headcount_Opex_Build:cph_{d}}}",
                           pick("cost_per_head", f"={{act_cph_{d}}}", f"={{{p}cph_{d}@-1}}*(1+{{$cph_growth_{d}}})"),
                           "$#,##0", min_col=3))
        out += [
            Row(f"{p}ng_opex", "Non-GAAP operating expenses", None,
                f"={{{p}hc}}*({{{p}cph_rnd}}+{{{p}cph_snm}}+{{{p}cph_gna}})"),
            Row(f"{p}amort", "Amortization", None, pick("amortization", "={act_amort}", "={$amort_q}")),
            Row(f"{p}ng_op", "Non-GAAP operating income", None, f"={{{p}ng_gp}}-{{{p}ng_opex}}+{{{p}amort}}",
                bold=True),
            Row(f"{p}fcf", "Free cash flow", None,
                pick("fcf_conversion", f"={{{p}ng_op}}*{{act_conv}}", f"={{{p}ng_op}}*INDEX({{FCFCONV}},{{fiscal_q}})"),
                bold=True),
        ]
        return out

    bridge_rows = [_hdr("h_br", "C. Budget-vs-actual bridge (sequential substitution in the order shown; "
                               "the attribution depends on that order)")]
    act_key = {"revenue": "act_revenue", "ng_op_inc": "act_ng_op", "fcf": "act_fcf"}
    blk_key = {"revenue": "revenue", "ng_op_inc": "ng_op", "fcf": "fcf"}
    names = {"revenue": "Revenue", "ng_op_inc": "Non-GAAP operating income", "fcf": "Free cash flow"}
    for m, n in EXPLAINS.items():
        b = blk_key[m]
        bridge_rows.append(Row(f"br_{m}_plan", f"{names[m]}: frozen plan", None, f"={{s0_{b}}}", bold=True,
                               total="sum"))
        for k in range(1, n + 1):
            g = GROUPS[k - 1]
            bridge_rows.append(Row(f"br_{m}_{g}", f"  {GROUP_LABELS[g]}", None, f"={{s{k}_{b}}}-{{s{k - 1}_{b}}}",
                                   total="sum"))
        bridge_rows += [
            Row(f"br_{m}_actual", f"{names[m]}: actual", None, f"={{{act_key[m]}}}", bold=True, total="sum"),
            Row(f"br_{m}_check", f"{names[m]}: plan + components - actual (should be 0)", None,
                f"={{s{n}_{b}}}-{{{act_key[m]}}}", total="sum"),
        ]

    lab = [model.periods.label(e) for e in model.forecast_quarters()]
    fy = lab[0].split()[1]
    sc = [
        _hdr("h_sc", "D. Forecast scorecard: driver model vs naive baselines vs management guidance"),
        Row("sc_actual", "Revenue, actual", None, "={act_revenue}", bold=True, total="sum"),
        Row("sc_model", "Revenue, driver model (selected scenario)", None, "={Operating_Model:revenue}", total="sum"),
        Row("sc_g_ttm", "Trailing-12-month revenue growth (baseline input)",
            "=SUM({Operating_Model:revenue@-3..0})/SUM({Operating_Model:revenue@-7..-4})-1", None, PCT, min_col=7),
        Row("sc_seasonal", "Revenue, seasonal naive (same quarter last year x TTM growth)", None,
            "={Operating_Model:revenue@-4}*(1+{sc_g_ttm@T0})", total="sum"),
        Row("sc_g_q", "Average q/q revenue growth over the last year (baseline input)",
            "=({Operating_Model:revenue}/{Operating_Model:revenue@-4})^(1/{#q_per_year})-1", None, PCT, min_col=4),
        Row("sc_trend", "Revenue, trend naive (T0 x average q/q growth)", "={Operating_Model:revenue}",
            "={sc_trend@-1}*(1+{sc_g_q@T0})", total="sum"),
        Row("sc_guide_input", f"Guidance midpoint known at cutoff (Q3 column: {lab[0]}; Q4 column: full year {fy})",
            None, "input:guide_rev", tag=f"[Sourced - SEC Filing] 8-K {guide_filed}"),
        Row("sc_guidance", "Revenue, management guidance (Q4 implied = FY - Q1 - Q2 - Q3 guide)", None,
            ["={sc_guide_input}",
             "={sc_guide_input}-{Operating_Model:revenue@-3}-{Operating_Model:revenue@-2}-{sc_guidance@-1}",
             None, None]),
        Row("sc_err_model", "Error: driver model", None, "={sc_model}/{sc_actual}-1", PCT, total="mape"),
        Row("sc_err_seasonal", "Error: seasonal naive", None, "={sc_seasonal}/{sc_actual}-1", PCT, total="mape"),
        Row("sc_err_trend", "Error: trend naive", None, "={sc_trend}/{sc_actual}-1", PCT, total="mape"),
        Row("sc_err_guidance", "Error: management guidance", None,
            ["={sc_guidance}/{sc_actual}-1", "={sc_guidance}/{sc_actual}-1", None, None], PCT, total="mape"),
        Row("ng_actual_model", "Non-GAAP op. income, actual (model definition: + SBC + amortization)", None,
            "={act_ng_op}", total="sum"),
        Row("ng_model", "Non-GAAP op. income, driver model", None, "={Operating_Model:ng_op_inc}", total="sum"),
        Row("ng_err_model", "Error: driver model vs actual (model definition)", None,
            "={ng_model}/{ng_actual_model}-1", PCT, total="mape"),
        Row("ng_actual_company", f"Non-GAAP op. income, as reported by {config.ACTIVE.short_name} (company definition)",
            "input:ng_company", "input:ng_company", tag="[Sourced - SEC Filing] 8-K", min_col=0, total="sum"),
        Row("ng_guide_input", f"Guidance midpoint known at cutoff (Q3 column: {lab[0]}; Q4 column: {fy})", None,
            "input:guide_ng", tag=f"[Sourced - SEC Filing] 8-K {guide_filed}"),
        Row("ng_guidance", "Non-GAAP op. income, management guidance (company definition)", None,
            ["={ng_guide_input}",
             "={ng_guide_input}-{ng_actual_company@-3}-{ng_actual_company@-2}-{ng_guidance@-1}", None, None]),
        Row("ng_err_guidance", "Error: management guidance vs reported", None,
            ["={ng_guidance}/{ng_actual_company}-1", "={ng_guidance}/{ng_actual_company}-1", None, None], PCT,
            total="mape"),
    ]
    if guide_filed is None:
        sc = [r for r in sc if "guid" not in r.key]
    # E. damped-trend baseline, appended after the scorecard so earlier cell addresses never move
    rev = "{Operating_Model:revenue}"
    damped = [
        _hdr("h_dt", "E. Damped-trend baseline (Holt, damped slope; parameters fitted to T0 on Inputs_Scenarios)"),
        Row("dt_level", "Level", f"={{$dt_alpha}}*{rev}+(1-{{$dt_alpha}})*({{dt_level@-1}}+{{$dt_phi}}*{{dt_trend@-1}})",
            None, start=f"={rev}"),
        Row("dt_trend", "Trend (per quarter)",
            "={$dt_beta}*({dt_level}-{dt_level@-1})+(1-{$dt_beta})*{$dt_phi}*{dt_trend@-1}", None,
            start="={Operating_Model:revenue@1}-{Operating_Model:revenue}"),
        Row("dt_h", "Quarters ahead", None, ["=1", "={dt_h@-1}+1", "={dt_h@-1}+1", "={dt_h@-1}+1"], "0"),
        Row("sc_damped", "Revenue, damped trend (level at T0 + damped trend)", "={dt_level}",
            "={sc_damped@-1}+{$dt_phi}^{dt_h}*{dt_trend@T0}", total="sum"),
        Row("sc_err_damped", "Error: damped trend", None, "={sc_damped}/{sc_actual}-1", PCT, total="mape"),
    ]
    rows = rows + [r for k in range(len(GROUPS) + 1) for r in block(k)] + bridge_rows + sc + damped
    return Sheet("BvA_Variance_Backtest", "Budget vs actual: the frozen forecast scored against reported results",
                 rows)


def damped_inputs(con: duckdb.DuckDBPyConnection) -> tuple[str, list]:
    """Fitted damped-trend parameters (saas_metrics.backtest.damped_fit), for the appended Inputs block."""
    from saas_metrics import backtest
    from saas_metrics.roi import RoiInput

    rev = list(model.run(drivers.history(con), *_run_inputs(con), "Base").query("kind == 'Actual'").revenue)
    p = backtest.damped_fit(rev)
    note = "Grid search on quarterly revenue to T0, minimising one-step-ahead squared error."
    tag = "[Historical Derived]"
    return ("Damped-trend baseline (BvA scorecard, section E); flat across scenarios", [
        RoiInput("dt_alpha", "Level smoothing (alpha)", p["alpha"], tag, note, "0.00"),
        RoiInput("dt_beta", "Trend smoothing (beta)", p["beta"], tag, note, "0.00"),
        RoiInput("dt_phi", "Trend damping per quarter (phi)", p["phi"], tag, note, "0.00"),
    ])


def _run_inputs(con: duckdb.DuckDBPyConnection) -> tuple:
    hist = drivers.history(con)
    cal = con.execute("SELECT * FROM cohort_calibration WHERE vintage = 'as_of_cutoff'").df().set_index("period_end")
    hc = drivers.headcount_history(con, hist).set_index("period_end")["headcount"]
    return cal, hc, drivers.derive(con)


def ue_sheet() -> Sheet:
    o, c = "Operating_Model", "Cohort_ARR_Build"
    return Sheet("Unit_Economics_ROI", "Unit economics (trailing 4 quarters) and the GTM capital-allocation case", [
        _hdr("h_ue", "A. SaaS efficiency scorecard (history: actuals as of cutoff; forecast: selected scenario)"),
        Row("ue_snm_ng", "Non-GAAP sales and marketing", f"={{{o}:snm}}-{{{o}:sbc_snm}}", f"={{{o}:snm}}-{{{o}:sbc_snm}}"),
        Row("ue_snm_ng_ttm", "Non-GAAP S&M, trailing 4Q", "=SUM({ue_snm_ng@-3..0})", "=SUM({ue_snm_ng@-3..0})",
            min_col=3),
        Row("ue_sub_ttm", "Subscription revenue, trailing 4Q", f"=SUM({{{o}:sub@-3..0}})", f"=SUM({{{o}:sub@-3..0}})",
            min_col=3),
        Row("ue_rev_ttm", "Revenue, trailing 4Q", f"=SUM({{{o}:revenue@-3..0}})", f"=SUM({{{o}:revenue@-3..0}})",
            min_col=3),
        Row("ue_fcf_ttm", "FCF, trailing 4Q", f"=SUM({{{o}:fcf@-3..0}})", f"=SUM({{{o}:fcf@-3..0}})", min_col=3),
        Row("ue_sub_gm_ng", "Non-GAAP subscription gross margin",
            f"=({{{o}:sub}}-{{{o}:cost_sub}}+{{{o}:sbc_cost_subscription}})/{{{o}:sub}}",
            f"=({{{o}:sub}}-{{{o}:cost_sub}}+{{{o}:sbc_cost_subscription}})/{{{o}:sub}}", PCT),
        Row("ue_arr", "ARR proxy", f"={{{c}:arr}}", f"={{{c}:arr}}", min_col=3),
        Row("ue_nnarr", "Net new ARR, trailing 4Q", "={ue_arr}-{ue_arr@-4}", "={ue_arr}-{ue_arr@-4}", min_col=7),
        Row("ue_cac_payback", "CAC payback (months)",
            "={ue_snm_ng_ttm@-1}/({ue_nnarr}*{ue_sub_gm_ng})*{#months_year}",
            "={ue_snm_ng_ttm@-1}/({ue_nnarr}*{ue_sub_gm_ng})*{#months_year}", "0.0", min_col=7),
        Row("ue_magic", "Magic number", "=({ue_sub_ttm}-{ue_sub_ttm@-4})/{ue_snm_ng_ttm@-1}",
            "=({ue_sub_ttm}-{ue_sub_ttm@-4})/{ue_snm_ng_ttm@-1}", "0.00", min_col=7),
        Row("ue_rule40", "Rule of 40 (revenue growth YoY + FCF margin TTM)",
            f"={{{o}:rev_yoy}}+{{ue_fcf_ttm}}/{{ue_rev_ttm}}", f"={{{o}:rev_yoy}}+{{ue_fcf_ttm}}/{{ue_rev_ttm}}",
            PCT, min_col=4),
        Row("ue_burn", "Burn multiple (only while FCF is negative)",
            '=IF({ue_fcf_ttm}<0,-{ue_fcf_ttm}/{ue_nnarr},"n/m")', '=IF({ue_fcf_ttm}<0,-{ue_fcf_ttm}/{ue_nnarr},"n/m")',
            "0.00", min_col=7),
        Row("ue_nrr", "Net ARR expansion rate", f"={{{c}:nrr}}", f"={{{c}:nrr}}", PCT, min_col=3),
        Row("ue_new_logos_ttm", "New logos, trailing 4Q [Calibrated Synthetic]", f"=SUM({{{c}:new_logos@-3..0}})",
            f"=SUM({{{c}:new_logos@-3..0}})", NUM, min_col=7),
        Row("ue_cac_logo", "Non-GAAP S&M per new logo", "={ue_snm_ng_ttm@-1}/{ue_new_logos_ttm}",
            "={ue_snm_ng_ttm@-1}/{ue_new_logos_ttm}", "$#,##0", min_col=7),
        Row("ue_ltv", "LTV per customer [Illustrative: assumed churn]",
            f"={{{c}:arr}}/{{{c}:customers}}*{{ue_sub_gm_ng}}/(1-(1-{{$churn_q}})^{{#q_per_year}})",
            f"={{{c}:arr}}/{{{c}:customers}}*{{ue_sub_gm_ng}}/(1-(1-{{$churn_q}})^{{#q_per_year}})", "$#,##0",
            min_col=3),
        Row("ue_ltv_cac", "LTV : CAC [Illustrative]", "={ue_ltv}/{ue_cac_logo}", "={ue_ltv}/{ue_cac_logo}", "0.0",
            min_col=7),
    ])
