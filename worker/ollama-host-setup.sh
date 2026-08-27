#!/usr/bin/env bash
# ollama-host-setup.sh — configure the HOST Ollama on a Mac running
# dry-dock workers. Run this on the Mac itself; it touches launchd and
# restarts Ollama, so it cannot be applied remotely.
#
#   ./ollama-host-setup.sh          # show what it would do, then confirm
#   ./ollama-host-setup.sh -y       # no prompt
#   ./ollama-host-setup.sh --show   # just print current values and exit
#
#   KEEP_ALIVE=-1 ./ollama-host-setup.sh -y    # dedicated worker box
#
# KEEP_ALIVE defaults to 2h — right for a machine you also use yourself.
# Use -1 on a box that does nothing but serve dry-dock.
#
# ── Why ─────────────────────────────────────────────────────────────
# worker/envs/ holds five worker containers that all run on this one
# machine and share one Ollama. Without these settings they contend:
#
#   OLLAMA_MAX_LOADED_MODELS=1   Two 20 GB models will not both fit
#                                alongside their KV cache. Pinning to 1
#                                makes Ollama queue instead of evict.
#   OLLAMA_NUM_PARALLEL=1        KV cache is allocated as
#                                num_ctx * parallel_slots. On the default
#                                multi-slot setting, num_ctx=32768
#                                silently reserves a 131k-token cache.
#   OLLAMA_KEEP_ALIVE            How long a model stays resident after its
#                                last request. Ollama's default is 5m, which
#                                is shorter than a single build pause in the
#                                engineer loop — so every task pays a ~20 GB
#                                reload. -1 means never unload; 2h means a
#                                whole working session stays warm and the
#                                memory comes back when you walk away. On a
#                                daily-driver Mac, 2h. On a dedicated worker
#                                box, -1.
#   OLLAMA_FLASH_ATTENTION=1     Required for KV quantization below.
#   OLLAMA_KV_CACHE_TYPE=q8_0    Halves KV cache memory. Roughly doubles
#                                the context you can afford.
#
# See docs/ENGINEER-REBUILD.md section 05 (Fleet, finding F3).

set -euo pipefail

PLIST="$HOME/Library/LaunchAgents/com.drydock.ollama-env.plist"
LABEL="com.drydock.ollama-env"

KEYS=(
  OLLAMA_MAX_LOADED_MODELS
  OLLAMA_NUM_PARALLEL
  OLLAMA_KEEP_ALIVE
  OLLAMA_FLASH_ATTENTION
  OLLAMA_KV_CACHE_TYPE
)
# Override on the command line: KEEP_ALIVE=-1 ./ollama-host-setup.sh
KEEP_ALIVE="${KEEP_ALIVE:-2h}"

VALS=(
  1
  1
  "$KEEP_ALIVE"
  1
  q8_0
)

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "error: this script is macOS-only. On Windows, set these as system" >&2
  echo "       environment variables and restart the Ollama service." >&2
  exit 1
fi

show_current() {
  echo "Current values (as launchd sees them):"
  for k in "${KEYS[@]}"; do
    v="$(launchctl getenv "$k" || true)"
    printf '  %-28s %s\n' "$k" "${v:-<unset>}"
  done
}

if [[ "${1:-}" == "--show" ]]; then
  show_current
  exit 0
fi

echo "This will:"
echo "  1. launchctl setenv each of the five variables below (takes effect now)"
echo "  2. write $PLIST so they survive a reboot"
echo "  3. restart Ollama so the running server picks them up"
echo
for i in "${!KEYS[@]}"; do
  printf '     %-28s = %s\n' "${KEYS[$i]}" "${VALS[$i]}"
done
echo
show_current
echo

if [[ "${1:-}" != "-y" ]]; then
  read -r -p "Proceed? [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]] || { echo "aborted"; exit 0; }
fi

# ── 1. immediate ────────────────────────────────────────────────────
for i in "${!KEYS[@]}"; do
  launchctl setenv "${KEYS[$i]}" "${VALS[$i]}"
  echo "set ${KEYS[$i]}=${VALS[$i]}"
done

# ── 2. persist across reboot ────────────────────────────────────────
mkdir -p "$(dirname "$PLIST")"
{
  echo '<?xml version="1.0" encoding="UTF-8"?>'
  echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
  echo '<plist version="1.0"><dict>'
  echo "  <key>Label</key><string>${LABEL}</string>"
  echo '  <key>ProgramArguments</key><array>'
  echo '    <string>/bin/sh</string><string>-c</string>'
  printf '    <string>'
  for i in "${!KEYS[@]}"; do
    printf 'launchctl setenv %s %s; ' "${KEYS[$i]}" "${VALS[$i]}"
  done
  printf '</string>\n'
  echo '  </array>'
  echo '  <key>RunAtLoad</key><true/>'
  echo '</dict></plist>'
} > "$PLIST"
echo "wrote $PLIST"

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "loaded $LABEL"

# ── 3. restart Ollama ───────────────────────────────────────────────
# Env vars are read at server start, so a running server ignores them.
if pgrep -qx "Ollama" 2>/dev/null; then
  echo "restarting Ollama.app…"
  osascript -e 'quit app "Ollama"' 2>/dev/null || true
  sleep 3
  open -a Ollama
elif pgrep -qf "ollama serve" 2>/dev/null; then
  echo
  echo "NOTE: Ollama is running as a bare 'ollama serve' process. launchctl"
  echo "      env vars do NOT reach a process started from your shell."
  echo "      Stop it and restart it from a shell that exports them, or add"
  echo "      these to ~/.zshrc:"
  echo
  for i in "${!KEYS[@]}"; do
    echo "        export ${KEYS[$i]}=${VALS[$i]}"
  done
  echo
  echo "      then:  pkill -f 'ollama serve' && ollama serve &"
else
  echo "Ollama does not appear to be running — start it and it will pick these up."
fi

# ── 4. verify ───────────────────────────────────────────────────────
cat <<'VERIFY'

Verify once Ollama is back up:

  ollama ps                      # after a task runs: must say 100% GPU
  ./workers.sh restart           # workers re-read envs/*.env

If `ollama ps` shows any CPU split for qwen2.5-coder:32b at 32k, the
model + KV no longer fit. Drop MAX_CONTEXT in every worker/envs/*.env
together — they must stay in lockstep or Ollama reloads the runner each
time work moves between pools.
VERIFY
