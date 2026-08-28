#!/usr/bin/env bash
# ollama-host-setup.sh — configure the HOST Ollama on a Mac running
# dry-dock workers. Run this on the Mac itself; it touches launchd and
# restarts Ollama, so it cannot be applied remotely.
#
#   ./ollama-host-setup.sh          # show what it would do, then confirm
#   ./ollama-host-setup.sh -y       # no prompt
#   ./ollama-host-setup.sh --show   # just print current values and exit
#   ./ollama-host-setup.sh --serve-agent   # headless Macs — see below
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

# ── --serve-agent: the headless path ────────────────────────────────
# Instead of setting session-wide env vars, run ollama serve ourselves from
# a LaunchAgent with EnvironmentVariables baked in. The settings travel with
# the process, so there is no launchd domain to be denied.
if [[ "${1:-}" == "--serve-agent" ]]; then
  SERVE_PLIST="$HOME/Library/LaunchAgents/com.drydock.ollama-serve.plist"
  OLLAMA_BIN="$(command -v ollama || true)"
  [[ -n "$OLLAMA_BIN" ]] || { echo "error: ollama not on PATH" >&2; exit 1; }

  # Preserve the current bind address. Workers reach Ollama from inside
  # Docker via host.docker.internal, which cannot reach a 127.0.0.1 bind —
  # so default to all interfaces if nothing is set. Do not narrow this on a
  # machine whose workers are already connecting.
  HOST_BIND="${OLLAMA_HOST:-$(launchctl getenv OLLAMA_HOST 2>/dev/null || true)}"
  HOST_BIND="${HOST_BIND:-0.0.0.0:11434}"

  echo "ollama binary : $OLLAMA_BIN"
  echo "bind address  : $HOST_BIND"
  echo "keep alive    : $KEEP_ALIVE"
  echo "plist         : $SERVE_PLIST"
  echo
  echo "This takes over serving. Anything already bound to 11434 (Ollama.app,"
  echo "brew services, a stray 'ollama serve') must stop first or the agent"
  echo "will fail to bind."
  echo
  read -r -p "Stop those and proceed? [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]] || { echo "aborted"; exit 0; }

  osascript -e 'quit app "Ollama"' 2>/dev/null || true
  brew services stop ollama 2>/dev/null || true
  pkill -f "ollama serve" 2>/dev/null || true
  sleep 2

  mkdir -p "$(dirname "$SERVE_PLIST")"
  {
    echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    echo '<plist version="1.0"><dict>'
    echo '  <key>Label</key><string>com.drydock.ollama-serve</string>'
    echo '  <key>ProgramArguments</key><array>'
    echo "    <string>${OLLAMA_BIN}</string><string>serve</string>"
    echo '  </array>'
    echo '  <key>EnvironmentVariables</key><dict>'
    echo "    <key>OLLAMA_HOST</key><string>${HOST_BIND}</string>"
    for i in "${!KEYS[@]}"; do
      echo "    <key>${KEYS[$i]}</key><string>${VALS[$i]}</string>"
    done
    echo '  </dict>'
    echo '  <key>RunAtLoad</key><true/>'
    echo '  <key>KeepAlive</key><true/>'
    echo '  <key>StandardOutPath</key><string>/tmp/drydock-ollama.log</string>'
    echo '  <key>StandardErrorPath</key><string>/tmp/drydock-ollama.err</string>'
    echo '</dict></plist>'
  } > "$SERVE_PLIST"
  echo "wrote $SERVE_PLIST"

  launchctl unload "$SERVE_PLIST" 2>/dev/null || true
  launchctl load "$SERVE_PLIST"
  sleep 3

  echo
  if curl -fsS "http://127.0.0.1:11434/api/tags" >/dev/null 2>&1; then
    echo "Ollama is up with the new environment."
    echo "Verify:  ollama ps        (UNTIL column reflects keep-alive)"
    echo "Logs:    tail -f /tmp/drydock-ollama.err"
  else
    echo "Ollama did not answer on 11434. Check /tmp/drydock-ollama.err —"
    echo "the usual cause is something else still holding the port."
  fi
  echo
  echo "To undo:  launchctl unload \"$SERVE_PLIST\" && rm \"$SERVE_PLIST\""
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
# launchctl setenv targets the CALLER's launchd domain. Over SSH (or under
# sudo) that is the system domain, which SIP refuses:
#
#   Could not set environment: 150: Operation not permitted while System
#   Integrity Protection is engaged
#
# That is not a broken script and not something to disable SIP over — it
# means this mechanism is the wrong one for a headless box. Fall through
# to --serve-agent below, which attaches the environment to the ollama
# process itself and never touches a launchd domain.
SETENV_OK=1
for i in "${!KEYS[@]}"; do
  if launchctl setenv "${KEYS[$i]}" "${VALS[$i]}" 2>/dev/null; then
    echo "set ${KEYS[$i]}=${VALS[$i]}"
  else
    SETENV_OK=0
  fi
done

if [[ "$SETENV_OK" -eq 0 ]]; then
  cat <<'SIPNOTE'

launchctl setenv was refused (SIP blocks the system domain). You are almost
certainly running this over SSH or with sudo.

  If this Mac has a display and you can open Terminal.app on it:
      re-run this script there, WITHOUT sudo, and it will work.

  If this Mac is headless (a Mac mini serving workers), that is the normal
  case and launchctl setenv is the wrong tool. Run:

      ./ollama-host-setup.sh --serve-agent

  which installs a LaunchAgent that starts `ollama serve` with these
  variables baked into the process environment. Survives reboot, needs no
  GUI session, and SIP is not involved.

SIPNOTE
  exit 1
fi

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
