#!/usr/bin/env bash
# Run NexGuard without Docker: the web dashboard + the relay bridge, in one terminal.
#
#   ./run.sh            # dev: Django runserver on :8000 + relay (auto-reload web)
#   ./run.sh prod       # gunicorn on 127.0.0.1:8000 + relay (collectstatic first)
#   PORT=9000 ./run.sh  # pick another port (HOST=0.0.0.0 to open it to your LAN)
#
# First run creates ./venv, installs requirements and copies main/.env.example to
# main/.env (set SECRET_KEY and REDIS_URL there). Ctrl-C stops everything.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:-dev}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

log() { printf '\033[36m[run]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[run]\033[0m %s\n' "$*" >&2; exit 1; }

case "$MODE" in dev|prod) ;; *) die "usage: $0 [dev|prod]" ;; esac

# --- virtualenv -------------------------------------------------------------
if [[ ! -x "$ROOT/venv/bin/python" ]]; then
  log "Creating virtualenv in ./venv"
  python3 -m venv "$ROOT/venv"
  "$ROOT/venv/bin/pip" install --upgrade pip
  "$ROOT/venv/bin/pip" install -r "$ROOT/main/requirements-dev.txt"
fi
# shellcheck disable=SC1091
source "$ROOT/venv/bin/activate"

# --- config -----------------------------------------------------------------
if [[ ! -f "$ROOT/main/.env" ]]; then
  cp "$ROOT/main/.env.example" "$ROOT/main/.env"
  die "Created main/.env from the example. Set SECRET_KEY and REDIS_URL, then run again."
fi
grep -Eq '^REDIS_URL=.+' "$ROOT/main/.env" || log "Warning: REDIS_URL is empty in main/.env; the relay will not connect."

cd "$ROOT/main"

# --- database ---------------------------------------------------------------
log "Applying migrations"
python manage.py migrate --noinput

# --- processes --------------------------------------------------------------
PIDS=()
cleanup() {
  trap - INT TERM EXIT
  log "Stopping..."
  for pid in "${PIDS[@]:-}"; do [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

log "Starting relay bridge"
python manage.py relay &
PIDS+=($!)

if [[ "$MODE" == "prod" ]]; then
  log "Collecting static files"
  python manage.py collectstatic --noinput >/dev/null
  log "Starting gunicorn on http://$HOST:$PORT"
  gunicorn config.wsgi:application --bind "$HOST:$PORT" \
    --workers 1 --threads 8 --timeout 30 --access-logfile - &
else
  log "Starting dev server on http://$HOST:$PORT"
  python manage.py runserver "$HOST:$PORT" &
fi
PIDS+=($!)

# If either process dies, stop the other too (portable: macOS ships bash 3.2, no `wait -n`).
while true; do
  for pid in "${PIDS[@]}"; do
    kill -0 "$pid" 2>/dev/null || die "A process exited; shutting down."
  done
  sleep 1
done
