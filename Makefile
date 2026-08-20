# MarketPulse — common tasks.
.DEFAULT_GOAL := help
PY := ./.venv/bin/python
PIP := ./.venv/bin/pip
PGBIN := /opt/homebrew/opt/postgresql@16/bin

.PHONY: help setup db migrate api worker frontend backfill backfill-trades \
        test redteam evals bench lint fmt up down logs clean stack

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	 awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

setup: ## Create the venv, install deps, install frontend deps
	python3.12 -m venv .venv || /opt/homebrew/opt/python@3.12/bin/python3.12 -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"
	cd frontend && npm install
	@test -f .env || cp .env.example .env
	@echo "setup complete -- next: make db && make migrate"

db: ## Create the local database and roles
	./scripts/bootstrap_db.sh

migrate: ## Apply sql/*.sql migrations
	$(PY) scripts/migrate.py

api: ## Run the API with reload
	./.venv/bin/uvicorn app.main:app --reload --port 8000

worker: ## Run the ingestion worker
	$(PY) -m app.ingest.worker

frontend: ## Run the dashboard dev server
	cd frontend && npm run dev

backfill: ## Backfill 1-minute candles (make backfill DAYS=365)
	$(PY) -m app.ingest.backfill klines --days $(or $(DAYS),90)

backfill-trades: ## Backfill raw trades (make backfill-trades HOURS=12)
	$(PY) -m app.ingest.backfill trades --hours $(or $(HOURS),6)

test: ## Run the test suite
	$(PY) -m pytest tests/ -v

redteam: ## Attack the AI guardrails (no API key needed)
	$(PY) -m scripts.redteam

evals: ## Run the AI eval suite (needs ANTHROPIC_API_KEY)
	$(PY) -m evals.run

bench: ## Benchmark the indexes with EXPLAIN ANALYZE
	$(PY) -m bench.explain --runs 7 --markdown

lint: ## Lint
	./.venv/bin/ruff check app tests scripts bench evals

fmt: ## Format
	./.venv/bin/ruff format app tests scripts bench evals

up: ## Start the whole stack in Docker
	docker compose up -d --build

down: ## Stop the Docker stack
	docker compose down

logs: ## Tail Docker logs
	docker compose logs -f api worker

stack: ## Run API + worker + dashboard locally, all at once
	@echo "starting api, worker and dashboard -- Ctrl-C stops all three"
	@trap 'kill 0' INT TERM; \
	 ./.venv/bin/uvicorn app.main:app --port 8000 & \
	 $(PY) -m app.ingest.worker & \
	 (cd frontend && npm run dev) & \
	 wait

clean: ## Remove build artefacts
	rm -rf .pytest_cache **/__pycache__ frontend/dist evals/reports
