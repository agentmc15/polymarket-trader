# Polymarket Trader Backend Makefile
# ===================================
#
# Common commands for development and data management.
#
# Usage:
#   make <target>
#
# Run `make help` to see all available targets.

.PHONY: help dev run test lint format migrate celery beat \
        sync-markets collect-prices backfill-data \
        db-shell redis-shell clean

# Default Python interpreter
PYTHON ?= python

# ===================================
# Help
# ===================================

help:
	@echo "Polymarket Trader Backend"
	@echo "========================="
	@echo ""
	@echo "Development:"
	@echo "  make dev              Start development server"
	@echo "  make run              Run production server"
	@echo "  make test             Run tests"
	@echo "  make lint             Run linting"
	@echo "  make format           Format code with black"
	@echo ""
	@echo "Database:"
	@echo "  make migrate          Run database migrations"
	@echo "  make migrate-create   Create new migration (MSG=description)"
	@echo "  make db-shell         Open database shell"
	@echo ""
	@echo "Celery:"
	@echo "  make celery           Start Celery worker"
	@echo "  make beat             Start Celery beat scheduler"
	@echo "  make celery-flower    Start Flower monitoring"
	@echo ""
	@echo "Data Management:"
	@echo "  make sync-markets     Sync markets from Polymarket"
	@echo "  make collect-prices   Start continuous price collection"
	@echo "  make collect-once     Collect prices once"
	@echo "  make backfill         Backfill historical data (START/END required)"
	@echo ""
	@echo "Examples:"
	@echo "  make backfill START=2024-01-01 END=2024-06-01"
	@echo "  make backfill START=2024-01-01 END=2024-12-01 MARKETS=abc,def"

# ===================================
# Development
# ===================================

dev:
	uvicorn app.main:app --reload --port 8000

run:
	uvicorn app.main:app --host 0.0.0.0 --port 8000

test:
	pytest -v

test-cov:
	pytest --cov=app --cov-report=html --cov-report=term

lint:
	ruff check app/
	mypy app/

format:
	black app/
	ruff check --fix app/

# ===================================
# Database
# ===================================

migrate:
	alembic upgrade head

migrate-create:
	@if [ -z "$(MSG)" ]; then \
		echo "Error: MSG is required. Usage: make migrate-create MSG='description'"; \
		exit 1; \
	fi
	alembic revision --autogenerate -m "$(MSG)"

migrate-down:
	alembic downgrade -1

db-shell:
	@echo "Connecting to database..."
	psql $${DATABASE_URL:-postgresql://postgres:postgres@localhost:5432/polymarket}

# ===================================
# Celery
# ===================================

celery:
	celery -A app.tasks worker --loglevel=info

celery-debug:
	celery -A app.tasks worker --loglevel=debug

beat:
	celery -A app.tasks beat --loglevel=info

celery-flower:
	celery -A app.tasks flower --port=5555

redis-shell:
	redis-cli

# ===================================
# Data Management Scripts
# ===================================

# Sync all active markets from Polymarket
sync-markets:
	$(PYTHON) -m app.scripts.sync_markets

# Sync all markets including inactive
sync-markets-all:
	$(PYTHON) -m app.scripts.sync_markets --all

# Preview market sync without saving
sync-markets-dry:
	$(PYTHON) -m app.scripts.sync_markets --dry-run --verbose

# Start continuous price collection (Ctrl+C to stop)
collect-prices:
	$(PYTHON) -m app.scripts.collect_prices

# Collect prices with custom interval
collect-prices-fast:
	$(PYTHON) -m app.scripts.collect_prices --interval 30

# Collect prices once
collect-once:
	$(PYTHON) -m app.scripts.collect_prices --once

# Backfill historical data
# Usage: make backfill START=2024-01-01 END=2024-06-01
# Optional: MARKETS=abc,def (comma-separated market IDs)
backfill:
	@if [ -z "$(START)" ] || [ -z "$(END)" ]; then \
		echo "Error: START and END are required."; \
		echo "Usage: make backfill START=2024-01-01 END=2024-06-01"; \
		exit 1; \
	fi
	@if [ -n "$(MARKETS)" ]; then \
		$(PYTHON) -m app.scripts.backfill_data --start $(START) --end $(END) --markets "$(MARKETS)" --all --verbose; \
	else \
		$(PYTHON) -m app.scripts.backfill_data --start $(START) --end $(END) --all --verbose; \
	fi

# Backfill only trades
backfill-trades:
	@if [ -z "$(START)" ] || [ -z "$(END)" ]; then \
		echo "Error: START and END are required."; \
		exit 1; \
	fi
	$(PYTHON) -m app.scripts.backfill_data --start $(START) --end $(END) --trades --verbose

# Backfill only prices (from existing trades)
backfill-prices:
	@if [ -z "$(START)" ] || [ -z "$(END)" ]; then \
		echo "Error: START and END are required."; \
		exit 1; \
	fi
	$(PYTHON) -m app.scripts.backfill_data --start $(START) --end $(END) --prices --verbose

# ===================================
# Backtesting
# ===================================

# Run a backtest (requires Celery worker)
run-backtest:
	@if [ -z "$(STRATEGY)" ]; then \
		echo "Error: STRATEGY is required."; \
		echo "Usage: make run-backtest STRATEGY=catalyst_momentum START=2024-01-01 END=2024-06-01"; \
		exit 1; \
	fi
	@echo "Note: Ensure Celery worker is running (make celery)"
	curl -X POST http://localhost:8000/api/v1/backtests \
		-H "Content-Type: application/json" \
		-d '{"strategy": "$(STRATEGY)", "start_date": "$(START)T00:00:00Z", "end_date": "$(END)T00:00:00Z", "initial_capital": 10000}'

# ===================================
# Cleanup
# ===================================

clean:
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -delete
	find . -type d -name ".pytest_cache" -delete
	find . -type d -name ".mypy_cache" -delete
	find . -type d -name "*.egg-info" -delete
	rm -rf .coverage htmlcov/

# ===================================
# Installation
# ===================================

install:
	pip install -r requirements.txt

install-dev:
	pip install -r requirements.txt
	pip install -r requirements-dev.txt

# ===================================
# Docker
# ===================================

docker-build:
	docker build -t polymarket-backend .

docker-run:
	docker run -p 8000:8000 --env-file .env polymarket-backend
