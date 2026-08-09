# aeroQ — common tasks.
#
#   make run     start the backend (serves the API and the built frontend)
#   make help    everything else

PORT ?= 8000
# Loopback by default: the app should not be reachable from the LAN. Override
# to expose it on the Tailscale interface, e.g.
#   make run HOST=$(shell tailscale ip -4 2>/dev/null | head -1)
HOST ?= 127.0.0.1
PY   := $(CURDIR)/backend/.venv/bin/python
PIP  := $(CURDIR)/backend/.venv/bin/pip

.DEFAULT_GOAL := help
.PHONY: help install run dev web build test check probe quota health tailnet clean

help: ## Show this help
	@echo "aeroQ"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "  Override the port with:  make run PORT=8080"
	@echo "  Expose on Tailscale:     make tailnet"

install: ## Create the venv and install backend + frontend dependencies
	test -d backend/.venv || python3 -m venv backend/.venv
	$(PIP) install -q -r backend/requirements.txt
	cd frontend && npm install
	cd frontend && npm run build

run: ## Start the backend (default port 8000)
	@test -x $(PY) || { echo "No venv found — run 'make install' first."; exit 1; }
	@echo "→ http://$(HOST):$(PORT)"
	cd backend && $(PY) -m uvicorn app.main:app --host $(HOST) --port $(PORT)

dev: ## Start the backend with auto-reload
	@test -x $(PY) || { echo "No venv found — run 'make install' first."; exit 1; }
	cd backend && $(PY) -m uvicorn app.main:app --reload --host $(HOST) --port $(PORT)

web: ## Start the Vite dev server on :5173 (proxies /api to the backend)
	cd frontend && npm run dev

build: ## Rebuild the frontend into frontend/dist
	cd frontend && npm run build

test: ## Run the backend test suite
	cd backend && $(PY) -m pytest -q

check: test build ## Tests plus a frontend build — run before committing

tailnet: ## Run bound to this machine's Tailscale IP (reachable by your devices)
	@test -n "$$(tailscale ip -4 2>/dev/null)" || { echo "Tailscale is not up."; exit 1; }
	@$(MAKE) run HOST=$$(tailscale ip -4 | head -1) PORT=$(PORT)

probe: ## Verify a live provider's response shape (spends 2 API calls, asks first)
	cd backend && $(PY) scripts/probe.py $(FLIGHT) $(DATE)

quota: ## Show the current API budget ledger
	@curl -fsS http://127.0.0.1:$(PORT)/api/quota | $(PY) -m json.tool

health: ## Show provider states and corpus size
	@curl -fsS http://127.0.0.1:$(PORT)/api/health | $(PY) -m json.tool

clean: ## Remove build output and caches (keeps the database)
	rm -rf frontend/dist frontend/node_modules/.vite
	find backend -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf backend/.pytest_cache
