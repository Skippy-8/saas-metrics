"""Render the QBR and capital-allocation memo from the model.

memos/memo_template.md holds the prose with {{placeholders}}; every number in the memo is a
placeholder filled here from saas_metrics.backtest / saas_metrics.roi. The template itself may contain no
digits except fiscal-period labels and form names (tested), so no figure can drift from the model.

Outputs: memos/qbr_and_gtm_roi_memo.md and memos/qbr_and_gtm_roi_memo.html (tearsheet with
inline SVG waterfalls; no external assets).
"""
from __future__ import annotations

import html as _html
import re
from pathlib import Path

import duckdb
import pandas as pd

from saas_metrics import backtest, config, drivers, model, roi

MEMO_DIR = config.ROOT / "memos"
# the template ships inside the wheel (saas_metrics/memo_template.md); a checkout keeps it in memos/
_PACKAGED = Path(__file__).resolve().parent / "memo_template.md"
TEMPLATE = _PACKAGED if _PACKAGED.is_file() else config._company.SOURCE_ROOT / "memos" / "memo_template.md"
OUT_MD = MEMO_DIR / "qbr_and_gtm_roi_memo.md"
OUT_HTML = MEMO_DIR / "qbr_and_gtm_roi_memo.html"


# ---- formatting ----------------------------------------------------------------------------------
def m(v: float, signed: bool = False) -> str:
    s = f"${abs(v) / 1e6:,.1f}M"
    if signed:
        return ("+" if v >= 0 else "-") + s
    return ("-" if v < 0 else "") + s


def pct(v: float, signed: bool = False, dp: int = 1) -> str:
    return f"{v * 100:+.{dp}f}%" if signed else f"{v * 100:.{dp}f}%"


def num(v: float) -> str:
    return f"{v:,.0f}"


def mult(v: float) -> str:
    return f"{v:.2f}"


def _table(header: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" if i == 0 else "---:" for i in range(len(header))) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


# ---- facts ----------------------------------------------------------------------------------------
def facts(con: duckdb.DuckDBPyConnection) -> dict[str, str]:
    f: dict[str, str] = {}
    fq = model.forecast_quarters()
    lab = [model.periods.label(e) for e in fq]
    f["cutoff"] = config.CUTOFF_DATE
    f["t0"] = model.periods.label(config.T0_QUARTER_END)
    f["window"] = f"{lab[0]} to {lab[-1]}"
    f["n_quarters"] = num(len(fq))
    f["n_filings"] = num(con.execute("SELECT count(*) FROM filings").fetchone()[0])
    f["n_releases"] = num(con.execute("SELECT count(DISTINCT accn) FROM guidance").fetchone()[0])

    long, steps = backtest.bridge(con)
    four = long.groupby(["metric", "component"])["value"].sum()
    q = long.pivot_table(index=["metric", "component"], columns="period_end", values="value")
    names = {"revenue": "Revenue", "ng_op_inc": "Non-GAAP operating income", "fcf": "Free cash flow"}
    rows = []
    for key, name in names.items():
        plan, act = four[(key, "plan")], four[(key, "actual")]
        f[f"{key}_plan"], f[f"{key}_act"] = m(plan), m(act)
        f[f"{key}_var"], f[f"{key}_var_pct"] = m(act - plan, True), pct(act / plan - 1, True)
        f[f"{key}_var_abs"] = m(abs(act - plan))
        rows.append([name, m(plan), m(act), m(act - plan, True), pct(act / plan - 1, True)])
    f["table_plan_vs_actual"] = _table(["4 quarters, " + f["window"], "Frozen plan", "Actual", "Variance", "%"], rows)
    for metric in backtest.EXPLAINS:
        for g in backtest.GROUPS[:backtest.EXPLAINS[metric]]:
            f[f"br_{metric}_{g}"] = m(four[(metric, g)], True)
    for i, e in enumerate(fq):
        f[f"br_rev_ea_{lab[i][:2].lower()}"] = m(q.loc[("revenue", "ea_mix_timing"), e], True)
    f["br_revenue_expansion_abs"] = m(abs(four[("revenue", "expansion")]))
    f["br_ng_people_net"] = m(four[("ng_op_inc", "headcount_pace")] + four[("ng_op_inc", "cost_per_head")], True)

    def bridge_table(metric: str) -> str:
        comps = backtest.GROUPS[:backtest.EXPLAINS[metric]]
        r = [["Frozen plan", *[m(q.loc[(metric, "plan"), e]) for e in fq], m(four[(metric, "plan")])]]
        for g in comps:
            r.append([backtest.GROUP_LABELS[g], *[m(q.loc[(metric, g), e], True) for e in fq], m(four[(metric, g)], True)])
        r.append(["**Actual**", *[f"**{m(q.loc[(metric, 'actual'), e])}**" for e in fq],
                  f"**{m(four[(metric, 'actual')])}**"])
        return _table(["", *lab, "4Q"], r)
    f["table_rev_bridge"] = bridge_table("revenue")
    f["table_ng_bridge"] = bridge_table("ng_op_inc")

    # scorecard
    sc = backtest.scorecard(con)
    err = {"Driver model (Base)": "model_base", "Driver model (Upside)": "model_upside",
           "Trend naive": "trend_naive", "Seasonal naive": "seasonal_naive", "Management guidance": "guidance"}
    rows = []
    stats = {}
    for name, col in err.items():
        e = (sc[col] / sc.actual_revenue - 1)
        mape = e.abs().mean()
        known = sc[col].notna()
        tot = sc.loc[known, col].sum() / sc.loc[known, "actual_revenue"].sum() - 1
        stats[col] = (mape, tot, int(known.sum()))
        rows.append([name, *[pct(v, True) if pd.notna(v) else "n/a" for v in e], pct(mape), pct(tot, True)])
    f["table_errors"] = _table(["Revenue forecast error", *lab, "Mean abs. error", "Total error"], rows)
    for col, key in (("model_base", "model"), ("model_upside", "upside"), ("trend_naive", "trend"),
                     ("seasonal_naive", "seasonal"), ("guidance", "guidance")):
        f[f"mape_{key}"], f[f"tot_{key}"] = pct(stats[col][0]), pct(stats[col][1], True)
    f["n_guided"] = num(stats["guidance"][2])
    above = int((sc.actual_revenue > sc.model_upside).sum())
    below = int((sc.actual_revenue < sc.model_downside).sum())
    f["n_above"], f["n_below"], f["n_inside"] = num(above), num(below), num(len(sc) - above - below)
    pretty = {"Driver model (Base)": "driver model's Base case", "Driver model (Upside)": "driver model's Upside case",
              "Trend naive": "trend naive", "Seasonal naive": "seasonal naive", "Management guidance": "guidance"}
    best = min(err, key=lambda name: stats[err[name]][0])
    f["best_forecast"], f["best_mape"] = pretty[best], pct(stats[err[best]][0])
    f["q1_label"] = lab[0]
    f["threshold_100k"] = "$100k"
    f["err_q3_model"] = pct(sc.model_base.iloc[0] / sc.actual_revenue.iloc[0] - 1, True)
    f["err_q3_downside"] = pct(sc.model_downside.iloc[0] / sc.actual_revenue.iloc[0] - 1, True)
    f["guide_q3"] = m(sc.guidance.iloc[0])
    f["act_q3"] = m(sc.actual_revenue.iloc[0])
    f["guide_ng_q3"] = m(sc.guidance_ng_op.iloc[0])
    f["act_ng_q3_company"] = m(sc.actual_ng_op_company_def.iloc[0])
    f["g_ttm"] = pct(sc.attrs["g_ttm"])
    f["g_q"] = pct(sc.attrs["g_q"])

    # drivers and actual-implied values
    drv = {d.key: d for d in drivers.derive(con)}
    act = backtest.actual_rows(con)
    plan = {k: d.base for k, d in drv.items()}
    imp = backtest.implied_drivers(act, plan)
    f["nrr_t0"] = pct(drv["nrr"].base, dp=0)
    f["nrr_latest"] = pct(act.nrr.iloc[-1], dp=0)
    f["ea_yoy_plan"] = pct(plan["ea_yoy"])
    f["ea_yoy_act_range"] = f"{pct(imp.ea_yoy.min(), True)} to {pct(imp.ea_yoy.max(), True)}"
    f["net_hires_plan"] = num(plan["net_hires_q"])
    hist = drivers.history(con)
    hc_cut = drivers.headcount_history(con, hist).set_index("period_end")["headcount"]
    f["hc_t0_est"] = num(hc_cut.loc[config.T0_QUARTER_END])
    f["hc_t0_act"] = num(act.at[config.T0_QUARTER_END, "headcount"])
    emp = con.execute("SELECT period_end, value FROM kpi_disclosures WHERE kpi='employees' ORDER BY period_end").df()
    f["hc_fy25"], f["hc_fy26"] = num(emp.value.iloc[-2]), num(emp.value.iloc[-1])
    f["hires_fy26"] = num(emp.value.iloc[-1] - emp.value.iloc[-2])
    f["hc_fy26_label"] = model.periods.label(emp.period_end.iloc[-1])
    f["conv_plan_q3"], f["conv_plan_q4"] = mult(plan["fcf_conv_q3"]) + "x", mult(plan["fcf_conv_q4"]) + "x"
    f["conv_act_q3"], f["conv_act_q4"] = mult(imp.fcf_conv.iloc[0]) + "x", mult(imp.fcf_conv.iloc[1]) + "x"

    # unit economics (actuals)
    nl = con.execute("SELECT period_end, new_logos FROM cohort_calibration WHERE vintage='latest'").df()
    ue = roi.scorecard(act, nl.set_index("period_end").new_logos, plan["churn_q"])
    t0, last = config.T0_QUARTER_END, fq[-1]
    f["latest"] = lab[-1]
    rows = []
    for label, col, fmt in (("Revenue growth, YoY", None, None), ("FCF margin, trailing 4Q", None, None),
                            ("Rule of 40", "rule_of_40", "pct"), ("CAC payback (months)", "cac_payback_months", "mo"),
                            ("Magic number", "magic_number", "x"), ("Net ARR expansion rate", None, None),
                            ("LTV : CAC [Illustrative: assumed churn]", "ltv_cac", "x1")):
        vals = []
        for e in (t0, last):
            if label.startswith("Revenue growth"):
                v = pct(act.at[e, "revenue"] / act.iloc[act.index.get_loc(e) - 4].revenue - 1)
            elif label.startswith("FCF margin"):
                v = pct(ue.at[e, "fcf_ttm"] / ue.at[e, "revenue_ttm"])
            elif label.startswith("Net ARR"):
                v = pct(act.at[e, "nrr"], dp=0)
            elif fmt == "pct":
                v = pct(ue.at[e, col])
            elif fmt == "mo":
                v = f"{ue.at[e, col]:.1f}"
            elif fmt == "x1":
                v = f"{ue.at[e, col]:.1f}x"
            else:
                v = mult(ue.at[e, col])
            vals.append(v)
        rows.append([label, *vals])
    f["table_unit_econ"] = _table(["", f["t0"] + " (cutoff)", lab[-1] + " (latest)"], rows)
    f["rule40_t0"], f["rule40_latest"] = pct(ue.at[t0, "rule_of_40"]), pct(ue.at[last, "rule_of_40"])
    f["cac_t0"], f["cac_latest"] = f"{ue.at[t0, 'cac_payback_months']:.1f}", f"{ue.at[last, 'cac_payback_months']:.1f}"
    f["magic_t0"], f["magic_latest"] = mult(ue.at[t0, "magic_number"]), mult(ue.at[last, "magic_number"])
    f["rev_growth_latest"] = pct(act.at[last, "revenue"] / act.iloc[act.index.get_loc(last) - 4].revenue - 1)

    # GTM case
    inp = {i.key: i.value for i in roi.roi_inputs(con)}
    cases = roi.gtm_cases(inp)
    s = roi.summary(cases, inp)
    co_magic = ue.at[t0, "magic_number"]
    att_co = roi.attainment_at_efficiency(inp, co_magic)
    low = {**inp, "roi_attain": att_co}
    s_low = roi.summary(roi.gtm_cases(low), low)
    be = roi.breakeven_attainment(inp)
    f.update({
        "roi_n_ae": num(inp["roi_n_ae"]), "roi_quota": m(inp["roi_quota"]), "roi_attain": pct(inp["roi_attain"], dp=0),
        "roi_ramp": num(inp["roi_ramp"]), "roi_months": num(roi.MONTHS), "roi_discount": pct(inp["roi_discount"], dp=0),
        "roi_ae_cost": f"${inp['roi_ae_cost'] / 1e3:,.0f}k", "roi_attach": pct(inp["roi_attach"], dp=0),
        "roi_target_customers": num(inp["roi_target_customers"]),
        "a_eff": mult(roi.ae_efficiency(inp)), "co_magic": mult(co_magic), "att_co": pct(att_co, dp=0),
        "breakeven_att": pct(be, dp=0), "pilot_n": num(round(inp["roi_n_ae"] / 3)),
    })

    def payback(v) -> str:
        return f"month {num(v)}" if pd.notna(v) else f"not within {num(roi.MONTHS)} months"
    rows = []
    for name, row in (("A. Hire Enterprise AEs, plan assumptions", s.loc["A"]),
                      ("A. Same, at MongoDB's current efficiency", s_low.loc["A"]),
                      ("B. Product attach to >$100k customers", s.loc["B"])):
        irr = row.irr_annual
        rows.append([name, payback(row.payback_month), m(row.npv), pct(irr) if pd.notna(irr) else "n/a",
                     m(row.trough), m(row.arr_month_36)])
    f["table_roi"] = _table(["Case (36 months)", "Payback", "NPV", "IRR", "Peak investment", "ARR at month 36"], rows)
    f["a_payback"], f["a_npv"] = payback(s.at["A", "payback_month"]), m(s.at["A", "npv"])
    f["b_payback"], f["b_npv"] = payback(s.at["B", "payback_month"]), m(s.at["B", "npv"])
    f["a_low_payback"], f["a_low_npv"] = payback(s_low.at["A", "payback_month"]), m(s_low.at["A", "npv"])
    return f


# ---- rendering ------------------------------------------------------------------------------------
_PH = re.compile(r"\{\{([a-z0-9_]+)\}\}")


def render(con: duckdb.DuckDBPyConnection, template: Path = TEMPLATE) -> str:
    f = facts(con)
    text = template.read_text()
    missing = sorted({k for k in _PH.findall(text) if k not in f})
    if missing:
        raise KeyError(f"memo template uses unknown facts: {missing}")
    return _PH.sub(lambda mt: f[mt.group(1)], text)


def _waterfall_svg(title: str, start: tuple[str, float], steps: list[tuple[str, float]], end: tuple[str, float]) -> str:
    """A plain SVG waterfall: start bar, signed steps, end bar. Values in dollars."""
    bars = [start, *steps, end]
    w, h, pad, bw = 760, 300, 40, 760 // (len(bars) + 1)
    run, lo, hi = 0.0, float("inf"), float("-inf")
    spans = []
    for i, (_, v) in enumerate(bars):
        if i == 0 or i == len(bars) - 1:
            a, b = 0.0, v
            run = v
        else:
            a, b = run, run + v
            run = b
        spans.append((min(a, b), max(a, b)))
        lo, hi = min(lo, a, b), max(hi, a, b)
    lo = min(lo, 0.0)
    base = min(s[0] for s in spans[1:-1]) * 0.98 if len(spans) > 2 else lo
    lo = max(lo, base) if base > 0 else lo   # zoom the axis onto the moving part
    scale = (h - 2 * pad) / (hi - lo)
    y = lambda v: h - pad - (v - lo) * scale  # noqa: E731
    if lo > 0:  # the axis is zoomed onto the moving part; say so rather than let bar heights mislead
        title = f"{title} (axis starts at {m(lo)}, not zero)"
    parts = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{_html.escape(title)}" class="wf">',
             f'<text x="{pad}" y="20" class="wf-title">{_html.escape(title)}</text>']
    for i, ((label, v), (a, b)) in enumerate(zip(bars, spans, strict=True)):
        x = pad + i * bw
        top, bot = y(b), y(max(a, lo))
        cls = "total" if i in (0, len(bars) - 1) else ("up" if v >= 0 else "down")
        parts.append(f'<rect x="{x}" y="{top:.1f}" width="{bw * 0.7:.1f}" height="{max(bot - top, 1):.1f}" class="{cls}"/>')
        txt = m(v) if cls == "total" else m(v, True)
        parts.append(f'<text x="{x + bw * 0.35:.1f}" y="{top - 4:.1f}" class="v">{txt}</text>')
        parts.append(f'<text x="{x + bw * 0.35:.1f}" y="{h - pad + 14}" class="l">{_html.escape(label)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def to_html(md_text: str, con: duckdb.DuckDBPyConnection) -> str:
    import markdown

    long, _ = backtest.bridge(con)
    four = long.groupby(["metric", "component"])["value"].sum()
    short = {"expansion": "Expansion", "new_logo_volume": "Logo volume", "new_logo_arr": "ARR/logo",
             "ea_mix_timing": "EA timing", "services": "Services", "gross_margin": "GM rate",
             "headcount_pace": "Headcount", "cost_per_head": "Cost/head", "amortization": "Amort."}
    svgs = []
    for metric, title in (("revenue", "Revenue: frozen plan to actual, 4 quarters"),
                          ("ng_op_inc", "Non-GAAP operating income: frozen plan to actual, 4 quarters")):
        comps = backtest.GROUPS[:backtest.EXPLAINS[metric]]
        svgs.append(_waterfall_svg(title, ("Plan", four[(metric, "plan")]),
                                   [(short[g], four[(metric, g)]) for g in comps],
                                   ("Actual", four[(metric, "actual")])))
    body = markdown.markdown(md_text, extensions=["tables"])
    body = body.replace("<!-- WATERFALLS -->", "".join(f'<figure>{s}</figure>' for s in svgs))
    css = """
:root{--fg:#1b1f24;--muted:#5b6672;--rule:#d9dee3;--up:#2e7d4f;--down:#b23b3b;--tot:#3550c8;--bg:#fff}
@media (prefers-color-scheme:dark){:root{--fg:#e6e9ed;--muted:#9aa5b1;--rule:#39424b;--bg:#14181c;--tot:#7d93f0}}
body{font:15px/1.5 Georgia,'Times New Roman',serif;color:var(--fg);background:var(--bg);max-width:860px;margin:32px auto;padding:0 16px}
h1{font-size:24px;margin-bottom:4px}h2{font-size:18px;border-bottom:1px solid var(--rule);padding-bottom:4px;margin-top:28px}
table{border-collapse:collapse;width:100%;font:13px/1.4 -apple-system,Segoe UI,Helvetica,sans-serif;margin:10px 0 16px;display:block;overflow-x:auto}
th,td{border-bottom:1px solid var(--rule);padding:4px 8px;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}
figure{margin:12px 0}.wf{width:100%;height:auto;font:11px -apple-system,Segoe UI,Helvetica,sans-serif}
.wf .up{fill:var(--up)}.wf .down{fill:var(--down)}.wf .total{fill:var(--tot)}.wf text{fill:var(--fg)}
.wf .v,.wf .l{text-anchor:middle}.wf .wf-title{font-weight:600}
em,.muted{color:var(--muted)}"""
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><title>MongoDB QBR backtest memo</title>"
            f"<style>{css}</style></head><body>{body}</body></html>")


def write(con: duckdb.DuckDBPyConnection) -> tuple[Path, Path]:
    md_text = render(con)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text(md_text)
    OUT_HTML.write_text(to_html(md_text, con))
    return OUT_MD, OUT_HTML
