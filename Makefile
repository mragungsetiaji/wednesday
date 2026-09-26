# Wednesday: common tasks on macOS and Linux. Needs uv and Node 20+.
# `make` lists the targets. Pass extra CLI flags with ARGS, e.g. make scan ARGS="--source synthetic".

SHELL := /bin/bash
.DEFAULT_GOAL := help

HOST ?= 127.0.0.1
PORT ?= 8000
ARGS ?=
UV   ?= uv
NPM  ?= npm
WEB  := web

UI_BUILD   := $(WEB)/dist/index.html
UI_SOURCES := $(shell find $(WEB)/src -type f 2>/dev/null) $(WEB)/index.html $(WEB)/vite.config.ts $(WEB)/package.json

.PHONY: help doctor setup install ui serve demo dev dev-api dev-ui scan check test typecheck telegram-chats telegram-test clean

help: ## Show the targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

doctor: ## Check that uv and Node are installed
	@command -v $(UV) >/dev/null || { echo "uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
	@command -v node >/dev/null || { echo "Node not found. Install Node 20+ (macOS: brew install node)"; exit 1; }
	@echo "uv $$($(UV) --version | cut -d' ' -f2), node $$(node --version)"

setup: doctor .env install ui ## First run: create .env, install dependencies, build the dashboard

.env:
	cp .env.example .env
	@echo "Created .env from .env.example. Edit it for MT5 or Telegram; the default source is Yahoo Finance."

install: $(WEB)/node_modules ## Install Python and dashboard dependencies
	$(UV) sync --extra llm --extra ml

$(WEB)/node_modules: $(WEB)/package.json $(WEB)/package-lock.json
	cd $(WEB) && $(NPM) install
	@touch $@

ui: $(UI_BUILD) ## Build the dashboard (only when its sources changed)

$(UI_BUILD): $(WEB)/node_modules $(UI_SOURCES)
	cd $(WEB) && $(NPM) run build

serve: $(UI_BUILD) ## Scan loop + dashboard on http://HOST:PORT (source from Settings or .env)
	$(UV) run wednesday --serve --host $(HOST) --port $(PORT) $(ARGS)

demo: $(UI_BUILD) ## Dashboard on demo data, no market data needed
	$(UV) run wednesday --serve --source synthetic --host $(HOST) --port $(PORT) $(ARGS)

dev: $(WEB)/node_modules ## Demo API + Vite dev server with hot reload on http://localhost:5173
	$(MAKE) -j2 dev-api dev-ui

dev-api:
	$(UV) run wednesday --serve --source synthetic --port $(PORT) $(ARGS)

dev-ui:
	cd $(WEB) && XAU_API=http://127.0.0.1:$(PORT) $(NPM) run dev

scan: ## One scan in the console
	$(UV) run wednesday --once $(ARGS)

check: ## Test the data feed connection
	$(UV) run wednesday --check $(ARGS)

test: ## Run the Python tests
	$(UV) run pytest -q

typecheck: $(WEB)/node_modules ## Type-check the dashboard
	cd $(WEB) && $(NPM) run typecheck

telegram-chats: ## List chats that messaged your bot, to find TELEGRAM_CHAT_ID
	$(UV) run wednesday --telegram-chats

telegram-test: ## Send a Telegram test message
	$(UV) run wednesday --telegram-test

clean: ## Remove build output and caches (keeps data/ and .env)
	rm -rf $(WEB)/dist .pytest_cache
	find . -path ./.venv -prune -o -path ./$(WEB)/node_modules -prune -o -name __pycache__ -type d -exec rm -rf {} +
