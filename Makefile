.DEFAULT_GOAL := help
PY := $(CURDIR)/venv/bin/python
MANAGE := cd main && $(PY) manage.py

.PHONY: help install run relay dev test lint format check migrate superuser agent-dev device-agent phone-app phone-app-apk

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Create the virtualenv and install everything
	python3 -m venv venv
	venv/bin/pip install -r main/requirements-dev.txt

migrate: ## Apply migrations
	$(MANAGE) migrate

superuser: ## Create an admin account
	$(MANAGE) createsuperuser

run: ## Dashboard on :8000 (development server)
	$(MANAGE) runserver

relay: ## Relay bridge: agent messages, job retries, offline sweep (needs REDIS_URL)
	$(MANAGE) relay

dev: ## Dashboard on :8000 AND the relay bridge, in one terminal (Ctrl-C stops both)
	@trap 'kill 0' INT TERM EXIT; cd main && $(PY) manage.py relay & cd main && $(PY) manage.py runserver

agent-dev: ## Run the agent locally (needs agent/dev.toml with a redis_url)
	$(PY) agent/server_monitor_agent.py -c agent/dev.toml -v

test: ## Run main + agent tests
	cd main && $(PY) -m pytest
	$(PY) -m pytest agent/tests -q

device-agent: ## Test the Go device agent and build it for every system into device-agent/dist/
	cd device-agent && go vet ./... && go test ./... && ./build.sh

phone-app: ## Run the NexGuard phone app with Expo Go (installs npm packages the first time)
	cd phone-app && ([ -d node_modules ] || npm install) && npx expo start

phone-app-apk: ## Build the Android app on EAS and save the .apk where the dashboard serves it
	cd phone-app && ([ -d node_modules ] || npm install) && ./scripts/android-apk.sh

lint: ## Lint and check formatting
	venv/bin/ruff check .
	venv/bin/ruff format --check .

format: ## Reformat
	venv/bin/ruff format .
	venv/bin/ruff check --fix .

check: ## Django system checks incl. the deployment audit (as if behind HTTPS)
	$(MANAGE) check
	$(MANAGE) makemigrations --check --dry-run
	cd main && DEBUG=False HTTPS=True REDIS_URL=rediss://check@localhost:6379 SECRET_KEY=$$(python3 -c 'import secrets;print(secrets.token_urlsafe(50))') \
	  ALLOWED_HOSTS=monitor.example.com $(PY) manage.py check --deploy --fail-level WARNING
