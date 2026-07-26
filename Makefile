PYTHON ?= python
BASELINE_RUN_ID ?=
AGENT_RUN_ID ?=
RUN_ID ?=

.PHONY: doctor setup prepare baseline agent compare dashboard demo test lint submission

doctor:
	$(PYTHON) -m ecoloop doctor

setup:
	uv sync --extra dev

prepare:
	$(PYTHON) -m ecoloop prepare-model

baseline:
	$(PYTHON) -m ecoloop run baseline --period smoke

agent:
	$(PYTHON) -m ecoloop run agent --period smoke

compare:
	$(PYTHON) -m ecoloop compare $(BASELINE_RUN_ID) $(AGENT_RUN_ID)

dashboard:
	$(PYTHON) -m ecoloop dashboard

demo:
	$(PYTHON) -m ecoloop demo

test:
	pytest

lint:
	ruff check .
	ruff format --check .
	mypy src

submission:
	$(PYTHON) -m ecoloop package-submission
