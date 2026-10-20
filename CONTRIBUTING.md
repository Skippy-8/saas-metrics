# Contributing

Issues and pull requests are welcome. Especially useful:
- exports from a billing system that don't map cleanly (send the column headers, not your data);
- a definition you think is wrong or ambiguous;
- metrics people commonly ask for.

Any change to a definition updates `docs/definitions.md` and its test in the same pull request.

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/ruff check . && .venv/bin/pytest
```
