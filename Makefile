.PHONY: help up down restart build logs ps test test-server test-watchdog test-dashboard test-liva create-device devices sim-enroll sim-collect sim-beat sim-list prune psql shell-api shell-dash shell-liva clean

COMPOSE := docker compose

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

up: ## Start the stack (builds if needed)
	$(COMPOSE) up -d --build
	@echo
	@echo "  dashboard  http://localhost:$${DASHBOARD_PORT:-8501}"
	@echo "  api docs   http://localhost:$${API_PORT:-8000}/docs"
	@echo

down: ## Stop the stack, keeping the database volume
	$(COMPOSE) down

restart: ## Restart every service
	$(COMPOSE) restart

build: ## Rebuild images
	$(COMPOSE) build

logs: ## Follow logs
	$(COMPOSE) logs -f

ps: ## Show service status
	$(COMPOSE) ps

test: test-server test-watchdog test-dashboard test-liva ## Run every test suite

test-server: ## Run server tests against a scratch database
	$(COMPOSE) exec -T api sh -lc 'pip install -q -r requirements-dev.txt && python -m pytest tests -q'

test-watchdog: ## Run watchdog tests
	$(COMPOSE) exec -T api sh -lc 'pip install -q pytest && cd /app/watchdog && PYTHONPATH=/app/watchdog python -m pytest tests -q'

test-dashboard: ## Run dashboard tests
	$(COMPOSE) exec -T dashboard sh -lc 'pip install -q pytest && cd /app && python -m pytest tests -q'

test-liva: ## Run Liva tests
	$(COMPOSE) exec -T liva sh -lc 'pip install -q -r requirements-dev.txt && python -m pytest tests -q'

sim-enroll: ## Create devices for the sim containers and install the watchdog in them
	python3 scripts/enroll_sim.py

sim-collect: ## Force inventory collection on the sim devices now
	@for c in sim-device-1 sim-device-2; do \
		echo "--- $$c ---"; \
		$(COMPOSE) exec -T $$c runuser -u sentinel -- sentinel-watchdog collect || true; \
	done

sim-beat: ## Run one beat on the sim devices now
	@for c in sim-device-1 sim-device-2; do \
		echo "--- $$c ---"; \
		$(COMPOSE) exec -T $$c runuser -u sentinel -- sentinel-watchdog -v beat || true; \
	done

sim-list: ## Show what the sim devices report locally
	@for c in sim-device-1 sim-device-2; do \
		echo "--- $$c ---"; \
		$(COMPOSE) exec -T $$c runuser -u sentinel -- sentinel-watchdog status 2>/dev/null || echo "not enrolled"; \
	done

create-device: ## Register a device and save its installer locally (NAME=web-01)
	@test -n "$(NAME)" || { echo "usage: make create-device NAME=web-01"; exit 1; }
	@$(COMPOSE) exec -T api python -m app.cli create-device --name "$(NAME)" --stdout \
		> "sentinel-install-$(NAME).sh"
	@chmod 600 "sentinel-install-$(NAME).sh"
	@echo "  wrote sentinel-install-$(NAME).sh (mode 600)"

devices: ## Show the fleet and its watchdog versions
	@$(COMPOSE) exec -T api python -m app.cli list-devices

prune: ## Apply the retention policy now
	$(COMPOSE) exec -T api python -m app.cli prune

psql: ## Open a database shell
	$(COMPOSE) exec db psql -U $${POSTGRES_USER:-sentinel} -d $${POSTGRES_DB:-sentinel}

shell-api: ## Shell in the API container
	$(COMPOSE) exec api bash

shell-dash: ## Shell in the dashboard container
	$(COMPOSE) exec dashboard bash

shell-liva: ## Shell in the Liva container
	$(COMPOSE) exec liva bash

clean: ## Stop the stack and DELETE the database volume
	$(COMPOSE) down -v
