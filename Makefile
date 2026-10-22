# saas-fpa-backtest developer targets. `make help` lists them.
PY ?= .venv/bin/python
.DEFAULT_GOAL := help
.PHONY: help install fetch build qc workbook backtest memo sources benchmark unit-econ cohorts-sample test lint check

help:  ## list the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-9s %s\n", $$1, $$2}'

install:  ## create the venv with dev extras
	uv venv --python 3.12 && uv pip install --python $(PY) -e ".[dev]"

fetch:  ## download MongoDB filings from SEC into data/raw (needs SEC_USER_AGENT in .env; CONFIG=... for another company)
	$(PY) -m saas_metrics.cli $(if $(CONFIG),--config $(CONFIG)) fetch

build:  ## build data/saas_metrics.duckdb from data/raw
	$(PY) -m saas_metrics.cli $(if $(CONFIG),--config $(CONFIG)) build

qc:  ## pre-flight checks on the built store
	$(PY) -m saas_metrics.cli $(if $(CONFIG),--config $(CONFIG)) qc

workbook:  ## write models/saas_operating_model.xlsx from the store
	$(PY) -m saas_metrics.cli $(if $(CONFIG),--config $(CONFIG)) workbook

backtest:  ## print the budget-vs-actual bridge and the forecast scorecard
	$(PY) -m saas_metrics.cli $(if $(CONFIG),--config $(CONFIG)) backtest

memo:  ## render memos/qbr_and_gtm_roi_memo.md and .html from the model
	$(PY) -m saas_metrics.cli memo

sources:  ## write docs/sources.md (filings used, with accession numbers)
	$(PY) -m saas_metrics.cli sources

benchmark:  ## peer comp table from SEC XBRL (MDB DDOG SNOW TTD)
	$(PY) -m saas_metrics.cli benchmark --tickers MDB DDOG SNOW TTD

unit-econ:  ## GTM case: AE hiring vs product attach, from the config defaults
	$(PY) -m saas_metrics.cli unit-econ

cohorts-sample:  ## cohort engine on the synthetic sample billing tape
	$(PY) -m saas_metrics.cli cohorts --input tests/fixtures/sample_billing.csv.gz

test:  ## offline pytest suite
	$(PY) -m pytest -q

lint:  ## ruff
	$(PY) -m ruff check src tests scripts

check: lint test  ## lint + tests
