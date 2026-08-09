# aeroQ — common tasks.
#
#   make run     start the backend (serves the API and the built frontend)
#   make help    everything else

PORT ?= 8000
PY   := $(CURDIR)/backend/.venv/bin/python
PIP  := $(CURDIR)/backend/.venv/bin/pip

.DEFAULT_GOAL := help
.PHONY: help install run dev web build test check quota health clean

help: ## Show this help
	@echo "aeroQ"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "  Override the port with:  make run PORT=8080"

install: ## Create the venv and install backend + frontend dependencies
	test -d backend/.venv || python3 -m venv backend/.venv
	$(PIP) install -q -r backend/requirements.txt
	cd frontend && npm install
	cd frontend && npm run build

run: ## Start the backend (default port 8000)
	@test -x $(PY) || { echo "No venv found — run 'make install' first."; exit 1; }
	@echo "→ http://127.0.0.1:$(PORT)"
	cd backend && $(PY) -m uvicorn app.main:app --port $(PORT)

dev: ## Start the backend with auto-reload
	@test -x $(PY) || { echo "No venv found — run 'make install' first."; exit 1; }
	cd backend && $(PY) -m uvicorn app.main:app --reload --port $(PORT)

web: ## Start the Vite dev server on :5173 (proxies /api to the backend)
	cd frontend && npm run dev

build: ## Rebuild the frontend into frontend/dist
	cd frontend && npm run build

test: ## Run the backend test suite
	cd backend && $(PY) -m pytest -q

check: test build ## Tests plus a frontend build — run before committing

quota: ## Show the current API budget ledger
	@curl -fsS http://127.0.0.1:$(PORT)/api/quota | $(PY) -m json.tool

health: ## Show provider states and corpus size
	@curl -fsS http://127.0.0.1:$(PORT)/api/health | $(PY) -m json.tool

clean: ## Remove build output and caches (keeps the database)
	rm -rf frontend/dist frontend/node_modules/.vite
	find backend -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf backend/.pytest_cache
