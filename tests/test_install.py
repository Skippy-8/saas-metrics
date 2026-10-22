"""What a pip-installed copy needs: bundled configs and memo template, a writable work root outside
site-packages, and an app that runs on a fresh host (no store, no SEC contact)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from saas_metrics import company

REPO = Path(__file__).resolve().parents[1]


def test_work_root_rules(monkeypatch, tmp_path):
    monkeypatch.setenv("SAAS_METRICS_HOME", str(tmp_path))
    assert company.work_root() == tmp_path.resolve()
    monkeypatch.delenv("SAAS_METRICS_HOME")
    assert company.work_root() == REPO                          # a source checkout keeps its data/ folder
    monkeypatch.setattr(company, "_is_checkout", lambda: False)
    root = company.work_root()
    assert "site-packages" not in str(root) and root.name == "saas-metrics"


def test_wheel_bundles_configs_and_memo_template():
    force = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert force == {"configs": "saas_metrics/configs", "memos/memo_template.md": "saas_metrics/memo_template.md"}
    assert (REPO / "configs" / "MDB.yaml").is_file() and (REPO / "memos" / "memo_template.md").is_file()
    assert {"MDB", "DDOG", "SNOW", "TTD"} <= set(company.available())


def test_extras_named_in_the_readme_exist():
    extras = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["optional-dependencies"]
    readme = (REPO / "README.md").read_text()
    for extra in ("web", "mcp"):
        assert f'saas-metrics[{extra}]' in readme and extra in extras


def test_app_on_a_fresh_host(tmp_path):
    pytest.importorskip("streamlit")
    script = (
        "import json\n"
        "from streamlit.testing.v1 import AppTest\n"
        f"at = AppTest.from_file({str(REPO / 'src' / 'saas_metrics' / 'app.py')!r}, default_timeout=120).run()\n"
        "print(json.dumps({'exc': [e.message for e in at.exception], 'tabs': [t.label for t in at.tabs],\n"
        "                  'warn': [w.value for w in at.warning], 'err': [e.value for e in at.error]}))\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "SEC_USER_AGENT"}
    env.update(SAAS_METRICS_HOME=str(tmp_path), SEC_USER_AGENT="")
    out = subprocess.run([sys.executable, "-c", script], env=env, cwd=tmp_path, capture_output=True, text=True,
                         timeout=300)
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res["exc"] == [] and res["err"] == []
    assert res["tabs"] == ["SaaS SEC Comps", "ARR Waterfall & Cohort Retention"]   # no store: no backtest tab
    assert any("SEC_USER_AGENT" in w for w in res["warn"])


def test_app_command_passes_streamlit_options(monkeypatch):
    from saas_metrics import cli
    seen = []
    monkeypatch.setattr(cli, "cmd_app", lambda a: seen.append(a.streamlit_args))
    cli.main(["app", "--server.headless", "true", "--server.port", "8599"])
    assert seen == [["--server.headless", "true", "--server.port", "8599"]]
    with pytest.raises(SystemExit):
        cli.main(["sample", "out.csv", "--no-such-flag"])        # other commands still reject unknown options
