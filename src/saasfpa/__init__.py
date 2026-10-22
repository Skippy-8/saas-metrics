"""Backwards-compatibility shim: the ``saasfpa`` package is now ``saas_metrics``.

Every module moved into ``saas_metrics`` under the same name, so existing code keeps working:

    from saasfpa import build               # -> saas_metrics.build
    from saasfpa.cli import main            # -> saas_metrics.cli.main
    saasfpa benchmark --tickers MDB DDOG    # -> the `saasfpa` console script

New code should import ``saas_metrics`` directly. Nothing is re-implemented here: each name below is
the same module object, so state (for example the active company in ``config``) is shared.
"""
from __future__ import annotations

import importlib
import sys

_MODULES = (
    "adapters", "backtest", "benchmark", "build", "calendar", "cli", "cohort", "cohort_engine", "company",
    "config", "contracts", "drivers", "engine", "excel", "fetch", "fiscal", "fx", "guidance", "inputs", "kpis",
    "lines", "memo", "model", "periods", "qc", "quality", "roi", "sample", "sec_client", "segments", "sources",
    "views", "workbook", "xbrl",
)

for _name in _MODULES:
    _module = importlib.import_module(f"saas_metrics.{_name}")
    sys.modules[f"{__name__}.{_name}"] = _module
    setattr(sys.modules[__name__], _name, _module)

# re-export the public SDK names so `from saasfpa import analyze` also works
from saas_metrics import *  # noqa: E402,F401,F403
from saas_metrics import __all__ as _sdk_all  # noqa: E402

del _name, _module
__all__ = [*_MODULES, *_sdk_all]
