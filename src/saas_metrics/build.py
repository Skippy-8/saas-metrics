"""Build data/saas_metrics.duckdb from the cached SEC files.

Tables
  filings               every 10-Q/10-K used (accession, filing date, period)
  facts                 every selected XBRL fact, one row per (filing, concept, period, member)
  kpi_disclosures       operating KPIs parsed from filing text, with the source sentence
  quarterly_financials  derived quarterly values per vintage:
                          'as_of_cutoff' = only filings on or before the cutoff date
                          'latest'       = all filings (used to score the forecast)
"""
from __future__ import annotations

import json
from datetime import date

import duckdb
import pandas as pd

from saas_metrics import cohort, config, guidance, kpis, periods, xbrl
from saas_metrics import lines as _lines
from saas_metrics.fetch import filings as list_filings


def load_facts() -> tuple[pd.DataFrame, pd.DataFrame]:
    fl = list_filings(("10-Q", "10-K"), config.FILINGS_FROM, config.LAST_QUARTER_END, by="reportDate")
    frows, rows = [], []
    for f in fl:
        accn = f["accessionNumber"]
        frows.append({"accn": accn, "form": f["form"], "filed": f["filingDate"], "period_end": f["reportDate"],
                      "document": f["primaryDocument"]})
        for x in xbrl.parse(str(config.RAW / "filings" / accn / "instance.xml"), _lines.CONCEPTS):
            # multi-axis facts are stored with "|"-joined axes and members (dims are sorted by axis)
            axis = "|".join(a for a, _ in x.dims)
            member = "|".join(m for _, m in x.dims)
            rows.append({"accn": accn, "filed": f["filingDate"], "form": f["form"], "concept": x.concept,
                         "axis": axis, "member": member, "start": x.start, "end": x.end, "value": x.value,
                         "unit": x.unit, "decimals": x.decimals})
    facts = pd.DataFrame(rows).drop_duplicates()
    facts["in_scope"] = _in_scope(facts, pd.DataFrame(frows))
    key = ["accn", "concept", "axis", "member", "start", "end"]
    clash = facts[facts.duplicated(key, keep=False)]
    if len(clash):
        # two different values under one key means a dimension was not read; never pick one silently
        raise ValueError(f"conflicting facts for one key:\n{clash.head(10).to_string()}")
    return pd.DataFrame(frows), facts


# A filing may only speak for its own period and its standard comparatives: a 10-Q for the
# quarter/YTD and the same periods a year earlier; a 10-K for three fiscal years. A fact dated
# outside that span is treated as a tagging error and never used. (Found in the wild: the Q2 FY26
# 10-Q carries a $0.1M amortization fact dated May-Jul 2023, against $2.3M in the original filing.)
def scope_months() -> dict[str, int]:
    """The active company's filing-scope rule (configs/<TICKER>.yaml sec.scope_months)."""
    return dict(config.ACTIVE.raw["sec"]["scope_months"])


def _in_scope(facts: pd.DataFrame, filings_df: pd.DataFrame) -> pd.Series:
    report_end = facts["accn"].map(filings_df.set_index("accn")["period_end"])
    back = (pd.to_datetime(report_end).dt.to_period("M") - pd.to_datetime(facts["end"]).dt.to_period("M")).apply(
        lambda d: d.n)
    return back <= facts["form"].map(scope_months())


def load_kpis(filings_df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for f in filings_df.itertuples():
        text = kpis.text_of((config.RAW / "filings" / f.accn / f.document).read_text(errors="ignore"))
        for k in kpis.parse(text):
            rows.append({"accn": f.accn, "form": f.form, "filed": f.filed, "period_end": f.period_end,
                         "kpi": k.kpi, "value": k.value, "unit": k.unit, "qualifier": k.qualifier,
                         "snippet": k.snippet, "method": "text_parse"})
    return pd.DataFrame(rows)


def load_guidance() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Guidance and company-defined non-GAAP actuals from each earnings release.

    `known_at_cutoff` marks what was public by the cutoff date.
    """
    rows, actuals = [], []
    for f in list_filings(("8-K",), config.EIGHT_K_FROM, config.EIGHT_K_TO, by="filingDate"):
        folder = config.RAW / "8k" / f["accessionNumber"]
        for doc in sorted(folder.glob("*.htm*")) if folder.exists() else []:
            text = kpis.text_of(doc.read_text(errors="ignore"))
            for a in guidance.parse_actuals(text):
                actuals.append({"accn": f["accessionNumber"], "filed": f["filingDate"], "period": a.period,
                                "metric": a.metric, "value": a.value, "snippet": a.snippet,
                                "known_at_cutoff": f["filingDate"] <= config.CUTOFF_DATE})
            for g in guidance.parse(text):
                rows.append({"accn": f["accessionNumber"], "filed": f["filingDate"], "issued": g.issued,
                             "period": g.period, "metric": g.metric, "low": g.low, "high": g.high,
                             "mid": (g.low + g.high) / 2, "snippet": g.snippet,
                             "known_at_cutoff": f["filingDate"] <= config.CUTOFF_DATE})
    return pd.DataFrame(rows), pd.DataFrame(actuals)


def _known(facts: pd.DataFrame, as_of: str | None) -> pd.DataFrame:
    """Each fact's value as known on `as_of`: the most recent filing on or before it."""
    f = facts[facts["in_scope"]]
    f = f if as_of is None else f[f["filed"] <= as_of]
    f = f.sort_values("filed")
    return f.drop_duplicates(["concept", "axis", "member", "start", "end"], keep="last")


def derive_quarterly(facts: pd.DataFrame, as_of: str | None, vintage: str) -> pd.DataFrame:
    known = _known(facts, as_of)
    idx, by_axis = {}, {}
    for r in known.itertuples():
        start = r.start if isinstance(r.start, str) else None
        idx[(r.concept, r.axis, r.member, start, r.end)] = r
        by_axis.setdefault((r.concept, r.axis, start, r.end), []).append(r)

    def get(line, start, end):
        for axis, member in line.keys():
            if member == "*":
                hits = by_axis.get((line.concept, axis, start, end), [])
                if len(hits) > 1:
                    raise ValueError(f"{line.name}: {len(hits)} facts on {axis} for {end}; expected one")
                r = hits[0] if hits else None
            else:
                r = idx.get((line.concept, axis, member, start, end))
            if r is not None:
                return r
        return None

    out = []
    for q_end in periods.quarter_ends(config.PREWINDOW_FROM, config.LAST_QUARTER_END):
        e, qs, fys = q_end.isoformat(), periods.quarter_start(q_end).isoformat(), periods.fy_start(q_end).isoformat()
        prev = periods.prev_quarter_end(q_end).isoformat()
        _, q = periods.fiscal(q_end)
        for ln in _lines.LINES:
            val = src = how = filed = None
            if ln.kind == "instant":
                r = get(ln, None, e)
                if r is not None:
                    val, src, how, filed = r.value, r.accn, "reported_instant", r.filed
            else:
                r = get(ln, qs, e)
                if r is not None:
                    val, src, how, filed = r.value, r.accn, "reported_3m", r.filed
                elif q > 1:
                    ytd, ytd_prev = get(ln, fys, e), get(ln, fys, prev)
                    if ytd is not None and ytd_prev is not None:
                        val = ytd.value - ytd_prev.value
                        src = f"{ytd.accn} - {ytd_prev.accn}"
                        how = "fy_minus_9m" if q == 4 else "ytd_diff"
                        filed = max(ytd.filed, ytd_prev.filed)
            if val is None:
                continue
            fy, _ = periods.fiscal(q_end)
            out.append({"vintage": vintage, "period_end": e, "fiscal_label": periods.label(q_end),
                        "fiscal_year": fy, "fiscal_quarter": q, "line": ln.name, "value": val,
                        "in_window": e >= config.FIRST_QUARTER_END,
                        "derivation": how, "source_accn": src, "filed": filed})
    cols = ["vintage", "period_end", "fiscal_label", "fiscal_year", "fiscal_quarter", "line", "value",
            "in_window", "derivation", "source_accn", "filed"]
    df = pd.DataFrame(out, columns=cols)
    if as_of is not None:
        # a quarter is only "known" if every input it used was filed by the cutoff
        df = df[df["filed"] <= as_of]
    return df


def _write(con: duckdb.DuckDBPyConnection, name: str, df: pd.DataFrame) -> None:
    con.register("_frame", df)
    con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM _frame")
    con.unregister("_frame")


SOURCE_TABLES = ("filings", "facts", "kpi_disclosures", "guidance", "reported_non_gaap")


def load_raw() -> dict[str, pd.DataFrame]:
    """Parse the cached SEC files into the source tables (the only step that reads data/raw)."""
    filings_df, facts = load_facts()
    guide_df, ng_actual_df = load_guidance()
    return {"filings": filings_df, "facts": facts, "kpi_disclosures": load_kpis(filings_df),
            "guidance": guide_df, "reported_non_gaap": ng_actual_df}


def assemble(con: duckdb.DuckDBPyConnection, src: dict[str, pd.DataFrame]) -> dict:
    """Everything downstream of the source tables: quarterly derivation, cohort ledger, views."""
    from saas_metrics import views

    for name in SOURCE_TABLES:
        _write(con, name, src[name])
    facts, kpi_df = src["facts"], src["kpi_disclosures"]
    q = pd.concat([derive_quarterly(facts, config.CUTOFF_DATE, "as_of_cutoff"),
                   derive_quarterly(facts, None, "latest")], ignore_index=True)
    _write(con, "quarterly_financials", q)
    con.execute("CREATE OR REPLACE TABLE meta AS SELECT * FROM (VALUES "
                f"('ticker', '{config.TICKER}'), ('cutoff_date', '{config.CUTOFF_DATE}'), "
                f"('t0_quarter_end', '{config.T0_QUARTER_END}'), ('built_at', '{date.today()}')) t(key, value)")
    views.create(con, upto="01")  # q_wide, needed to build the cohort ledger

    # calibrated cohort ledger, once per data vintage, each from its own information set
    parts: dict[str, list[pd.DataFrame]] = {"cohort_arr": [], "cohort_logos": [], "cohort_calibration": []}
    for vintage, as_of in (("as_of_cutoff", config.CUTOFF_DATE), ("latest", None)):
        qw = con.execute("SELECT * FROM q_wide WHERE vintage = ?", [vintage]).df()
        kp = kpi_df if as_of is None else kpi_df[kpi_df["filed"] <= as_of]
        for name, df in zip(parts, cohort.build(cohort.inputs(qw, kp)), strict=True):
            parts[name].append(df.assign(vintage=vintage))
    for name, dfs in parts.items():
        _write(con, name, pd.concat(dfs, ignore_index=True))
    views.create(con)
    return {name: len(src[name]) for name in SOURCE_TABLES} | {
        "quarterly_rows": json.loads(q.groupby("vintage").size().to_json())}


def run() -> dict:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(config.DB_PATH))
    try:
        return assemble(con, load_raw())
    finally:
        con.close()
