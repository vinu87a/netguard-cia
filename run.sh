#!/usr/bin/env bash
#
# NetGuard-CIA one-command start:
#   1. ensures a Python venv with deps
#   2. brings up the Batfish Docker stack and waits for it to be healthy
#   3. launches the Streamlit app
#
# Usage:   ./run.sh            (port 8501)
#          PORT=8600 ./run.sh  (custom port)
#
set -euo pipefail

# --- locate repo root (this script's directory) -----------------------------
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

PORT="${PORT:-8501}"
COMPOSE_FILE="docker/docker-compose.yml"
VENV="$REPO_DIR/.venv"

log() { printf '\033[1;34m[run]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[run] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# --- 1/5 prerequisites ------------------------------------------------------
log "[1/5] checking prerequisites (docker, .env) ..."
command -v docker >/dev/null 2>&1 || die "docker not found — install Docker Desktop and start it."
docker info >/dev/null 2>&1 || die "docker daemon not running — start Docker Desktop."
[ -f .env ] || die ".env not found in $REPO_DIR — create it with your COMMOTION_* keys (see README)."
log "      prerequisites OK."

# --- 2/5 python venv --------------------------------------------------------
if [ ! -x "$VENV/bin/python" ]; then
  log "[2/5] creating virtualenv (.venv) ..."
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  log "      virtualenv created."
else
  log "[2/5] virtualenv present (.venv)."
fi

# --- 3/5 dependencies (cheap idempotent check: is streamlit importable?) -----
if ! "$VENV/bin/python" -c "import streamlit" >/dev/null 2>&1; then
  log "[3/5] installing Python dependencies from requirements.txt ..."
  "$VENV/bin/pip" install --quiet -r requirements.txt
  log "      dependencies installed."
else
  log "[3/5] dependencies already installed."
fi

# --- 4/5 Batfish Docker stack -----------------------------------------------
log "[4/5] starting Batfish stack (docker compose) ..."
docker compose -f "$COMPOSE_FILE" up -d

log "      waiting for the Batfish engine to become healthy (up to ~90s) ..."
for i in $(seq 1 30); do
  status="$(docker inspect --format '{{.State.Health.Status}}' netguard-batfish 2>/dev/null || echo missing)"
  if [ "$status" = "healthy" ]; then
    log "      engine healthy."
    break
  fi
  if [ "$i" -eq 30 ]; then
    die "engine did not become healthy in time. Check: docker logs netguard-batfish"
  fi
  sleep 3
done

# --- 5/5 launch the app -----------------------------------------------------
if lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  die "port $PORT is already in use (another Streamlit instance?). Stop it first, or run with PORT=<other> ./run.sh"
fi

log "[5/5] launching Streamlit on http://localhost:$PORT  (Ctrl-C to stop)"
exec "$VENV/bin/streamlit" run app/streamlit_app.py \
     --server.headless true --server.port "$PORT"
