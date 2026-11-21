# Wednesday: common tasks on macOS and Linux. Needs Rust (cargo), Node 20+ and uv.
# The server is Rust (server-rs); Python stays as the reference that writes tests/golden/ and as
# `wednesday --serve` / `make scan` from source.
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

.PHONY: help doctor setup install ui serve serve-py dev dev-api dev-ui scan check test test-ui typecheck telegram-chats telegram-test clean release tauri test-tauri serve-rs

help: ## Show the targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

doctor: ## Check that cargo, uv and Node are installed
	@command -v cargo >/dev/null || { echo "cargo not found. Install Rust: https://rustup.rs"; exit 1; }
	@command -v $(UV) >/dev/null || { echo "uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
	@command -v node >/dev/null || { echo "Node not found. Install Node 20+ (macOS: brew install node)"; exit 1; }
	@echo "$$(cargo --version), uv $$($(UV) --version | cut -d' ' -f2), node $$(node --version)"

setup: doctor .env install ui ## First run: create .env, install dependencies, build the dashboard

.env:
	cp .env.example .env
	@echo "Created .env from .env.example. Edit it for Telegram or the data source; the default source is TradingView."

install: $(WEB)/node_modules tradingview/node_modules ## Install Python, dashboard and TradingView helper dependencies
	$(UV) sync

tradingview/node_modules: tradingview/package.json tradingview/package-lock.json
	cd tradingview && $(NPM) install
	@touch $@

$(WEB)/node_modules: $(WEB)/package.json $(WEB)/package-lock.json
	cd $(WEB) && $(NPM) install
	@touch $@

ui: $(UI_BUILD) ## Build the dashboard (only when its sources changed)

$(UI_BUILD): $(WEB)/node_modules $(UI_SOURCES)
	cd $(WEB) && $(NPM) run build

serve: $(UI_BUILD) ## The Rust server: scan engine + dashboard on http://HOST:PORT (data in this folder, source from Settings or .env)
	cd server-rs && cargo run --release -- --host $(HOST) --port $(PORT) --home $(CURDIR) --ui-dir $(CURDIR)/$(WEB)/dist $(ARGS)

serve-py: $(UI_BUILD) ## The Python server from source (wednesday --serve), the reference implementation
	$(UV) run wednesday --serve --host $(HOST) --port $(PORT) $(ARGS)

dev: $(WEB)/node_modules ## Rust server on synthetic data + Vite dev server with hot reload on http://localhost:5173
	$(MAKE) -j2 dev-api dev-ui

dev-api:
	cd server-rs && cargo run -- --source synthetic --port $(PORT) --home $(CURDIR) $(ARGS)

dev-ui:
	cd $(WEB) && XAU_API=http://127.0.0.1:$(PORT) $(NPM) run dev

tauri: $(UI_BUILD) ## The Tauri desktop app (src-tauri), with the Rust server in its process
	cd src-tauri && cargo run

test-tauri: ## Run the Rust tests: every crate of the workspace (shell, server, scan), against tests/golden
	cargo test --workspace

serve-rs: ## Compare: the Rust server on PORT in front of a running Python one on PORT+1 (make serve-py PORT=8001 first; ARGS=--engine runs the engine)
	cd server-rs && cargo run -- --port $(PORT) --backend $$(( $(PORT) + 1 )) --home $(CURDIR) $(ARGS)

scan: ## One scan in the console (Python, from source)
	$(UV) run wednesday --once $(ARGS)

check: ## Test the data feed connection (Python, from source)
	$(UV) run wednesday --check $(ARGS)

test: ## Run the Python tests (they also write and check tests/golden, which the Rust tests read)
	$(UV) run pytest -q

test-ui: $(WEB)/node_modules ## Run the dashboard unit tests (Node 22.18+)
	cd $(WEB) && $(NPM) test

typecheck: $(WEB)/node_modules ## Type-check the dashboard
	cd $(WEB) && $(NPM) run typecheck

telegram-chats: ## List chats that messaged your bot, to find TELEGRAM_CHAT_ID
	$(UV) run wednesday --telegram-chats

telegram-test: ## Send a Telegram test message
	$(UV) run wednesday --telegram-test

# make release v0.3.2: the version is the goal after `release` (VERSION=0.3.2 works too).
ifeq (release,$(firstword $(MAKECMDGOALS)))
  RELEASE_VERSION := $(or $(word 2,$(MAKECMDGOALS)),$(VERSION))
  ifneq ($(word 2,$(MAKECMDGOALS)),)
    $(eval $(word 2,$(MAKECMDGOALS)):;@:)
  endif
endif

release: ## Tag main and push it, which builds and publishes the Windows installer: make release v0.3.2
	@scripts/release.sh "$(RELEASE_VERSION)"

clean: ## Remove build output and caches (keeps data/ and .env)
	rm -rf $(WEB)/dist .pytest_cache dist build
	find . -path ./.venv -prune -o -path ./$(WEB)/node_modules -prune -o -name __pycache__ -type d -exec rm -rf {} +
