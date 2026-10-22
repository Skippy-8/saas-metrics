"""README headline figures and docs/sources.md stay in sync with the model."""
from __future__ import annotations

from saas_metrics import config, memo, qc, sources

README = (config.ROOT / "README.md").read_text()


def test_sources_page_is_in_sync(db):
    assert config.SOURCES.read_text() == sources.render(db), "run `saas-metrics sources` to regenerate docs/sources.md"


def test_readme_headline_figures_match_the_model(db, companyfacts_path):
    f = memo.facts(db)
    for key in ("revenue_var_abs", "revenue_var_pct", "mape_trend", "mape_model", "roi_n_ae", "roi_months"):
        assert f[key] in README, (key, f[key])
    assert f"{f['revenue_var_abs']} ({f['revenue_var_pct']})" in README
    assert f["n_inside"] == "1" and "only one quarter" in README
    ratio = float(f["a_eff"]) / float(f["co_magic"])
    assert 2.25 <= ratio < 2.75 and "roughly\n  2.5 times" in README
    assert f"{len(qc.run(db, companyfacts_path))} pre-flight checks" in README
    assert config.CUTOFF_DATE in README


def test_readme_peer_table_matches_the_benchmark():
    import gzip
    import json
    from pathlib import Path

    from saas_metrics import benchmark
    fix = Path(__file__).parent / "fixtures"
    tickers = ["MDB", "DDOG", "SNOW", "TTD"]
    table, _ = benchmark.comps(tickers, offline={t: json.load(gzip.open(fix / f"benchmark_{t}.json.gz", "rt"))
                                                 for t in tickers})
    for r in table.itertuples():
        row = (f"| {r.ticker} | {r.fiscal_label} | ${r.revenue_ttm / 1e6:,.0f}M | {r.revenue_growth_yoy:.1%} | "
               f"{r.gross_margin:.1%} | {r.snm_pct:.1%} | {r.gaap_op_margin:.1%} | {r.non_gaap_op_margin_approx:.1%} | "
               f"{r.fcf_margin:.1%} | {r.rule_of_40:.1%} |")
        assert row in README, row


def test_readme_damped_trend_figure_matches_the_scorecard(db):
    from saas_metrics import backtest
    sc = backtest.scorecard(db)
    mape = lambda c: (sc[c] / sc.actual_revenue - 1).abs().mean()  # noqa: E731
    assert f"({mape('damped_trend'):.1%} mean absolute error)" in README
    assert mape("damped_trend") > max(mape("trend_naive"), mape("seasonal_naive"))   # "did worse than both"


def test_readme_commands_parse():
    """Every documented CLI line in the README's code blocks is accepted by the parser.

    Both entry points are documented: `saas-metrics` (the package) and `saasfpa` (the legacy alias).
    """
    import re
    import shlex

    from saas_metrics import cli
    blocks = re.findall(r"```bash\n(.*?)```", README, re.S)
    joined = [re.sub(r"\\\n\s*", " ", b) for b in blocks]      # shell line continuations
    prefixes = ("saas-metrics ", "saasfpa ")
    cmds = [ln.split("#")[0].strip() for b in joined for ln in b.splitlines()
            if ln.strip().startswith(prefixes)]
    assert len(cmds) >= 14
    for c in cmds:
        cli.parser().parse_args(shlex.split(c)[1:])


def test_readme_config_kinds_are_accurate():
    from saas_metrics import company
    for t in ("DDOG", "SNOW", "TTD"):
        c = company.load(t)
        assert not c.has_model and len(c.path.read_text().splitlines()) <= 25, t
    assert company.load("MDB").has_model
    assert company.load(config.ROOT / "configs" / "examples" / "SYNTHCO.yaml").has_model
    assert "3-statement" not in README.lower() and "three-statement" not in README.lower()   # income statement + FCF only
