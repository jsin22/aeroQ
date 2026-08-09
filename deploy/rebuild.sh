#!/usr/bin/env bash
# Rebuild and restart aeroQ in place on the GPD.
#
#   ./deploy/rebuild.sh
#
# The frontend is built before the service restarts so there is no window where
# FastAPI serves a half-written dist/.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> Backend dependencies"
backend/.venv/bin/pip install -q -r backend/requirements.txt

echo "==> Backend tests"
(cd backend && .venv/bin/python -m pytest -q)

echo "==> Frontend build"
(cd frontend && npm ci --silent && npm run build)

if systemctl list-unit-files 2>/dev/null | grep -q '^aeroq\.service'; then
  echo "==> Restarting service"
  sudo systemctl restart aeroq
  sleep 2
  systemctl --no-pager --lines=0 status aeroq || true
else
  echo "==> Service not installed; skipping restart"
  echo "    sudo cp deploy/aeroq.service /etc/systemd/system/"
  echo "    sudo systemctl daemon-reload && sudo systemctl enable --now aeroq"
fi

echo "==> Health"
curl -fsS http://127.0.0.1:8000/api/health | head -c 400 || echo "(service not responding)"
echo
