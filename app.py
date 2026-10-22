"""Streamlit Community Cloud entry point. The workbench itself ships in the package (saas_metrics/app.py);
installed users run it with `saas-metrics app`."""
import runpy
from pathlib import Path

import saas_metrics

runpy.run_path(str(Path(saas_metrics.__file__).parent / "app.py"), run_name="__main__")
