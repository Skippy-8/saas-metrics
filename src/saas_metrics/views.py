"""Create the analytical SQL views from sql/*.sql, in file-name order."""
from __future__ import annotations

import duckdb

from saas_metrics import config
from saas_metrics import lines as _lines


def create(con: duckdb.DuckDBPyConnection, upto: str | None = None) -> list[str]:
    """Run sql/*.sql in name order; `upto="01"` stops after files whose name starts <= "01"."""
    done = []
    for path in sorted(config.SQL_DIR.glob("*.sql")):
        if upto is not None and path.name[:len(upto)] > upto:
            break
        con.execute(path.read_text().replace("{LINES}", ", ".join(f"'{ln.name}'" for ln in _lines.LINES)))
        done.append(path.name)
    return done
