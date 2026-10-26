#!/usr/bin/env bash
# Porneste backend-ul (FastAPI) si frontend-ul (Vite) intr-un singur terminal.
# Ctrl+C opreste ambele procese.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [ ! -d backend/.venv ]; then
  echo "Lipseste backend/.venv. Ruleaza mai intai:"
  echo "  cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

if [ ! -f backend/.env ]; then
  echo "Creez backend/.env din .env.example (schimba INGEST_TOKEN inainte sa expui public)."
  cp backend/.env.example backend/.env
fi

pids=()
cleanup() {
  echo ""
  echo "Opresc procesele..."
  for pid in "${pids[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "→ Backend  : http://127.0.0.1:8000"
( cd backend && exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 ) &
pids+=($!)

sleep 2

if [ -d frontend/node_modules ]; then
  echo "→ Dashboard: http://127.0.0.1:5173"
  ( cd frontend && exec npm run dev ) &
  pids+=($!)
else
  echo "! frontend/node_modules lipseste - rulez doar backend-ul."
  echo "  Instaleaza cu: cd frontend && npm install"
fi

wait
