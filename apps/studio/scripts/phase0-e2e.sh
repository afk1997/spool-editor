#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
cd "$REPO_ROOT"

RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/spool-phase0-e2e.XXXXXX")"
ENGINE_LOG="$RUN_DIR/engine.log"
STUDIO_LOG="$RUN_DIR/studio.log"
ENGINE_PID=""
STUDIO_PID=""
HARNESS_SUCCEEDED=0
TERM_GRACE_ATTEMPTS=20
KILL_GRACE_ATTEMPTS=20
LOOPBACK_NO_PROXY="127.0.0.1,localhost,::1"
test -z "${NO_PROXY:-}" || LOOPBACK_NO_PROXY="$LOOPBACK_NO_PROXY,$NO_PROXY"
test -z "${no_proxy:-}" || LOOPBACK_NO_PROXY="$LOOPBACK_NO_PROXY,$no_proxy"
export NO_PROXY="$LOOPBACK_NO_PROXY"
export no_proxy="$LOOPBACK_NO_PROXY"

collect_process_tree() {
  local pid="${1:-}"
  local child
  local children
  test -n "$pid" || return 0
  kill -0 "$pid" 2>/dev/null || return 0
  children="$(pgrep -P "$pid" 2>/dev/null || true)"
  for child in $children; do
    collect_process_tree "$child"
  done
  echo "$pid"
}

pid_is_running() {
  local pid="${1:-}"
  local state
  test -n "$pid" || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  state="$(ps -o stat= -p "$pid" 2>/dev/null | tr -d '[:space:]')"
  test -n "$state" || return 1
  case "$state" in
    Z*) return 1 ;;
  esac
  return 0
}

wait_for_tree_exit() {
  local pids="$1"
  local attempts="$2"
  local attempt
  local pid
  local running
  for ((attempt = 0; attempt < attempts; attempt += 1)); do
    running=0
    for pid in $pids; do
      if pid_is_running "$pid"; then
        running=1
        break
      fi
    done
    test "$running" -eq 1 || return 0
    sleep 0.1
  done
  return 1
}

stop_process_tree() {
  local root="${1:-}"
  local pids
  local current
  local pid
  test -n "$root" || return 0
  pid_is_running "$root" || {
    wait "$root" 2>/dev/null || true
    return 0
  }

  pids="$(collect_process_tree "$root")"
  for pid in $pids; do
    kill -TERM "$pid" 2>/dev/null || true
  done
  if ! wait_for_tree_exit "$pids" "$TERM_GRACE_ATTEMPTS"; then
    current="$(collect_process_tree "$root")"
    pids="$pids $current"
    for pid in $pids; do
      kill -KILL "$pid" 2>/dev/null || true
    done
    if ! wait_for_tree_exit "$pids" "$KILL_GRACE_ATTEMPTS"; then
      echo "Phase 0 E2E teardown could not stop process tree rooted at $root" >&2
      return 1
    fi
  fi

  wait "$root" 2>/dev/null || true
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  set +e
  if test "$status" -eq 0 && test "$HARNESS_SUCCEEDED" -ne 1; then
    status=1
  fi
  if ! stop_process_tree "$STUDIO_PID"; then
    status=1
  fi
  if ! stop_process_tree "$ENGINE_PID"; then
    status=1
  fi

  if test "$status" -eq 0; then
    rm -rf "$RUN_DIR"
  else
    echo "Phase 0 E2E failed; preserved failure artifacts at $RUN_DIR" >&2
    echo "--- engine.log (last 200 lines) ---" >&2
    tail -n 200 "$ENGINE_LOG" 2>/dev/null >&2 || true
    echo "--- studio.log (last 200 lines) ---" >&2
    tail -n 200 "$STUDIO_LOG" 2>/dev/null >&2 || true
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

fail() {
  echo "Phase 0 E2E prerequisite failed: $*" >&2
  return 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "missing command: $1"
}

loopback_curl() {
  curl --noproxy '*' "$@"
}

run_teardown_probe() {
  local ready="$RUN_DIR/teardown-probe.ready"
  local pid
  local attempt
  (
    trap '' TERM
    : >"$ready"
    exec sleep 30
  ) &
  pid=$!
  for ((attempt = 0; attempt < 100; attempt += 1)); do
    test -f "$ready" && break
    kill -0 "$pid" 2>/dev/null || fail "teardown probe exited before readiness"
    sleep 0.01
  done
  test -f "$ready" || fail "teardown probe did not become ready"
  stop_process_tree "$pid" || fail "bounded teardown probe failed"
  pid_is_running "$pid" && fail "bounded teardown probe left its child running"
  echo "Phase 0 bounded teardown probe passed"
}

if test "${SPOOL_PHASE0_E2E_TEARDOWN_PROBE:-0}" = 1; then
  run_teardown_probe
  HARNESS_SUCCEEDED=1
  exit 0
fi

assert_port_free() {
  local port="$1"
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | grep -q .; then
    fail "TCP port $port is already in use"
  fi
}

assert_loopback_listener() {
  local port="$1"
  local listeners
  listeners="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  echo "$listeners" | grep -Eq "TCP 127\\.0\\.0\\.1:${port} .*\\(LISTEN\\)" \
    || fail "TCP port $port is not listening on IPv4 loopback"
  if echo "$listeners" | grep -Eq "TCP (\\*|0\\.0\\.0\\.0|\\[::\\]):${port} .*\\(LISTEN\\)"; then
    fail "TCP port $port is exposed beyond loopback"
  fi
}

wait_for_http() {
  local pid="$1"
  local url="$2"
  local token="${3:-}"
  local attempt
  for attempt in $(seq 1 120); do
    kill -0 "$pid" 2>/dev/null || fail "server exited before readiness: $url"
    if test -n "$token"; then
      if loopback_curl -fsS --connect-timeout 1 --max-time 3 \
        -H "Authorization: Bearer $token" "$url" >/dev/null 2>&1; then
        return 0
      fi
    elif loopback_curl -fsS --connect-timeout 1 --max-time 3 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.25
  done
  fail "server did not become ready: $url"
}

require_command curl
require_command ffmpeg
require_command ffprobe
require_command lsof
require_command openssl
require_command pgrep
require_command pnpm
test -x engine/.venv/bin/python || fail "engine/.venv/bin/python is missing"
test -x engine/.venv/bin/yt-dlp || fail "engine/.venv/bin/yt-dlp is missing"
test -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  || fail "system Google Chrome is missing"
test -L engine/models || fail "engine/models must be the documented active-model symlink"
test -f engine/models/ACTIVE || fail "engine/models/ACTIVE is missing"
ACTIVE_MODEL="$(tr -d '\r\n' <engine/models/ACTIVE)"
test -n "$ACTIVE_MODEL" || fail "engine/models/ACTIVE is empty"
test -f "engine/models/$ACTIVE_MODEL" || fail "active Whisper model $ACTIVE_MODEL is missing"
assert_port_free 8899
assert_port_free 3000

TOKEN="phase0-e2e-$(openssl rand -hex 16)"

pnpm --filter @spool/studio build

(
  cd engine
  exec env \
    HOST=127.0.0.1 \
    PORT=8899 \
    TROVE_TOKEN="$TOKEN" \
    TROVE_RATE_LIMIT=0 \
    TROVE_DOWNLOAD_DIR="$RUN_DIR/data" \
    NO_PROXY="$LOOPBACK_NO_PROXY" \
    no_proxy="$LOOPBACK_NO_PROXY" \
    SPOOL_OFFLINE=0 \
    SPOOL_LLM_PROVIDER=CoDeX \
    SPOOL_LLM_EGRESS_CONSENT=YES \
    SPOOL_WATCH_INTERVAL=0 \
    .venv/bin/python app.py
) >"$ENGINE_LOG" 2>&1 &
ENGINE_PID=$!

wait_for_http "$ENGINE_PID" "http://127.0.0.1:8899/api/v1/doctor" "$TOKEN"
wait_for_http "$ENGINE_PID" "http://127.0.0.1:8899/api/v1/jobs" "$TOKEN"
assert_loopback_listener 8899

DOCTOR_JSON="$(loopback_curl -fsS --connect-timeout 1 --max-time 5 \
  -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8899/api/v1/doctor)"
CAPABILITIES_JSON="$(loopback_curl -fsS --connect-timeout 1 --max-time 5 \
  http://127.0.0.1:8899/api/v1/capabilities)"
engine/.venv/bin/python -c \
  'import json,sys; data=json.load(sys.stdin); assert data.get("ok") is True, data' \
  <<<"$DOCTOR_JSON"
engine/.venv/bin/python -c \
  'import json,sys; data=json.load(sys.stdin); assert data.get("auth_required") is True, data' \
  <<<"$CAPABILITIES_JSON"

(
  cd apps/studio
  exec env \
    NEXT_TELEMETRY_DISABLED=1 \
    NO_PROXY="$LOOPBACK_NO_PROXY" \
    no_proxy="$LOOPBACK_NO_PROXY" \
    SPOOL_ENGINE_URL=http://127.0.0.1:8899 \
    SPOOL_ENGINE_TOKEN="$TOKEN" \
    ./node_modules/.bin/next start --hostname 127.0.0.1 --port 3000
) >"$STUDIO_LOG" 2>&1 &
STUDIO_PID=$!

wait_for_http "$STUDIO_PID" "http://127.0.0.1:3000/import"
wait_for_http "$STUDIO_PID" "http://127.0.0.1:3000/api/engine/api/v1/jobs"
assert_loopback_listener 3000

AUTH_CODE="$(loopback_curl -sS --connect-timeout 1 --max-time 5 -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8899/api/v1/jobs)"
NO_AUTH_CODE="$(loopback_curl -sS --connect-timeout 1 --max-time 5 -o /dev/null -w '%{http_code}' \
  http://127.0.0.1:8899/api/v1/jobs)"
PROXY_CODE="$(loopback_curl -sS --connect-timeout 1 --max-time 5 -o /dev/null -w '%{http_code}' \
  http://127.0.0.1:3000/api/engine/api/v1/jobs)"
test "$AUTH_CODE" = 200 || fail "authenticated Flask readiness returned $AUTH_CODE"
test "$NO_AUTH_CODE" = 401 || fail "unauthenticated Flask readiness returned $NO_AUTH_CODE"
test "$PROXY_CODE" = 200 || fail "authenticated Studio proxy readiness returned $PROXY_CODE"

NO_PROXY="$LOOPBACK_NO_PROXY" \
no_proxy="$LOOPBACK_NO_PROXY" \
E2E_ENGINE_API_URL="http://127.0.0.1:8899/api/v1" \
TROVE_TOKEN="$TOKEN" \
SPOOL_STUDIO_URL="http://127.0.0.1:3000" \
  pnpm --filter @spool/studio e2e -- e2e/url-to-clip.spec.ts

echo "Phase 0 authenticated proxy E2E passed"
HARNESS_SUCCEEDED=1
