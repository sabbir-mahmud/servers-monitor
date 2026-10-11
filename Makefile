.DEFAULT_GOAL := help
PY := $(CURDIR)/venv/bin/python
MANAGE := cd main && $(PY) manage.py

.PHONY: help install run worker sweep test lint format check migrate superuser agent-dev docker-up

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Create the virtualenv and install everything
	python3 -m venv venv
	venv/bin/pip install -r main/requirements-dev.txt

migrate: ## Apply migrations (also syncs the Viewer/Operator/Admin roles)
	$(MANAGE) migrate

superuser: ## Create an admin account
	$(MANAGE) createsuperuser

run: ## Development server on :8000
	$(MANAGE) runserver

worker: ## Celery worker + beat (needs REDIS_URL)
	cd main && ../venv/bin/celery -A config worker -B -l info

sweep: ## Offline detection without Celery, every 15 s (development)
	$(MANAGE) monitor_sweep --loop 15

agent-dev: ## Run the agent locally against the dev server (needs agent/dev.toml)
	$(PY) agent/server_monitor_agent.py -c agent/dev.toml -v

test: ## Run main + agent tests
	cd main && $(PY) -m pytest
	$(PY) -m pytest agent/tests -q

lint: ## Lint and check formatting
	venv/bin/ruff check .
	venv/bin/ruff format --check .

format: ## Reformat
	venv/bin/ruff format .
	venv/bin/ruff check --fix .

check: ## Django system checks incl. the deployment audit
	$(MANAGE) check
	$(MANAGE) makemigrations --check --dry-run
	cd main && DEBUG=False CELERY_BROKER_URL=redis://localhost:6379/0 SECRET_KEY=$$(python3 -c 'import secrets;print(secrets.token_urlsafe(50))') \
	  ALLOWED_HOSTS=monitor.example.com $(PY) manage.py check --deploy --fail-level WARNING

docker-up: ## Postgres + Redis + web + worker
	docker compose up --build
