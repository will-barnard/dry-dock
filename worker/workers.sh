#!/usr/bin/env bash
# workers.sh — manage every dry-dock worker on this Mac as one fleet.
#
#   ./workers.sh up                 start every worker defined in envs/*.env
#   ./workers.sh down               stop them all
#   ./workers.sh restart            stop + start (re-reads env files)
#   ./workers.sh rebuild            rebuild image, then recreate every stack
#   ./workers.sh status             list running workers + their state
#   ./workers.sh logs [name]        tail logs (all workers, or one by name)
#   ./workers.sh ps                 quick docker ps filtered to dry-dock
#
# Each .env file in envs/ becomes its own compose project named
# `drydock-<basename>`, so containers, networks, and bind mounts stay
# isolated. The bind-mounted ./worktrees directory is shared (workers
# use random temp subdirs inside it, so they don't collide).

set -euo pipefail

cd "$(dirname "$0")"

COMPOSE_FILE="docker-compose.example.yml"
OVERRIDE_FILE="docker-compose.override.yml"
ENV_DIR="envs"

# Compose -f flags: always the base, plus an override if present.
compose_files() {
  printf -- '-f\0%s\0' "$COMPOSE_FILE"
  if [[ -f "$OVERRIDE_FILE" ]]; then
    printf -- '-f\0%s\0' "$OVERRIDE_FILE"
  fi
}

# ── helpers ─────────────────────────────────────────────────────────

die() { echo "error: $*" >&2; exit 1; }

envs() {
  shopt -s nullglob
  local files=("$ENV_DIR"/*.env)
  if (( ${#files[@]} == 0 )); then
    die "no env files found in $ENV_DIR/ — create one per worker before running this"
  fi
  printf '%s\n' "${files[@]}"
}

project_name_for() {
  local f="$1"
  echo "drydock-$(basename "$f" .env)"
}

compose_for() {
  local env_file="$1"
  local project="$2"
  shift 2
  local files=(-f "$COMPOSE_FILE")
  if [[ -f "$OVERRIDE_FILE" ]]; then
    files+=(-f "$OVERRIDE_FILE")
  fi
  docker compose --env-file "$env_file" -p "$project" "${files[@]}" "$@"
}

# Build the image once. Compose builds use whatever env file we give it
# (only the build args matter, not the runtime vars); we just need one.
build_image() {
  local sentinel; sentinel="$(envs | head -1)"
  echo "›› building drydock-worker:latest (one-time per code change)"
  local files=(-f "$COMPOSE_FILE")
  if [[ -f "$OVERRIDE_FILE" ]]; then
    files+=(-f "$OVERRIDE_FILE")
  fi
  docker compose --env-file "$sentinel" "${files[@]}" build
}

# ── commands ────────────────────────────────────────────────────────

cmd_up() {
  local need_build=1
  if docker image inspect drydock-worker:latest >/dev/null 2>&1; then
    need_build=0
  fi
  if (( need_build )); then
    build_image
  fi
  while read -r f; do
    local p; p="$(project_name_for "$f")"
    echo "›› up   $p"
    compose_for "$f" "$p" up -d
  done < <(envs)
  echo
  cmd_ps
}

cmd_down() {
  while read -r f; do
    local p; p="$(project_name_for "$f")"
    echo "›› down $p"
    compose_for "$f" "$p" down --remove-orphans
  done < <(envs)
}

cmd_restart() {
  cmd_down
  cmd_up
}

cmd_rebuild() {
  build_image
  while read -r f; do
    local p; p="$(project_name_for "$f")"
    echo "›› recreate $p"
    compose_for "$f" "$p" up -d --force-recreate
  done < <(envs)
  echo
  cmd_ps
}

cmd_status() {
  echo "configured workers (envs/):"
  while read -r f; do
    local name; name="$(basename "$f" .env)"
    local pool model
    pool=$(grep -E '^WORKER_POOL=' "$f" | head -1 | cut -d= -f2-)
    model=$(grep -E '^DEFAULT_MODEL=' "$f" | head -1 | cut -d= -f2-)
    printf "  %-30s pool=%-12s model=%s\n" "$name" "$pool" "$model"
  done < <(envs)
  echo
  cmd_ps
}

cmd_ps() {
  echo "running:"
  docker ps \
    --filter "name=drydock-" \
    --format "  {{.Names}}\t{{.Status}}" \
    | column -ts $'\t' || true
}

cmd_logs() {
  local name="${1:-}"
  if [[ -n "$name" ]]; then
    local f="$ENV_DIR/${name}.env"
    [[ -f "$f" ]] || die "no env file for $name (looked at $f)"
    local p; p="$(project_name_for "$f")"
    compose_for "$f" "$p" logs -f --tail=200
  else
    # Stream every worker's logs together. Each line is prefixed with the
    # container name via docker's built-in formatting.
    local names
    names=$(docker ps --filter "name=drydock-" --format '{{.Names}}' | tr '\n' ' ')
    [[ -n "$names" ]] || die "no running workers"
    # shellcheck disable=SC2086
    docker logs -f --tail=50 $names
  fi
}

# ── dispatch ────────────────────────────────────────────────────────

cmd="${1:-status}"
shift || true

case "$cmd" in
  up)       cmd_up ;;
  down)     cmd_down ;;
  restart)  cmd_restart ;;
  rebuild)  cmd_rebuild ;;
  status)   cmd_status ;;
  ps)       cmd_ps ;;
  logs)     cmd_logs "$@" ;;
  *)        die "unknown command: $cmd (try: up | down | restart | rebuild | status | ps | logs [name])" ;;
esac
