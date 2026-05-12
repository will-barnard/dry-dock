#!/usr/bin/env python3
"""dry-dock host agent — runs on the Mac mini OUTSIDE Docker.

Why this exists: the dry-dock backend runs in a Docker container on Beachhead.
Containers can't broadcast Wake-on-LAN packets to the host's LAN, and pinning
SSH credentials into a container is awkward. This is a tiny HTTP daemon that
runs as the user on the host, with its own LAN routing + the user's SSH config
already in place, and the backend calls it over host.docker.internal.

Endpoints (all require `Authorization: Bearer <token>`):
  POST /wake?mac=<mac>&broadcast=<ip>     → runs `wakeonlan -i <broadcast> <mac>`
  POST /shutdown?host=<ssh-alias>          → runs `ssh <host> "shutdown /s /t 0"`
  GET  /status?host=<host-or-ip>           → pings; returns {"online": bool, …}
  GET  /healthz                            → always 200; sanity check

Bind: 0.0.0.0:8088 by default so the Docker bridge can reach it. The shared
secret is the only line of defence — keep DRYDOCK_HOST_AGENT_TOKEN strong and
treat it like the worker secret. On a trusted home LAN this is fine; if you
want stricter, run it behind a `pf` rule that drops everything that isn't from
the Docker bridge IP.

Configuration (env):
  DRYDOCK_HOST_AGENT_TOKEN   required — must match backend's value
  DRYDOCK_HOST_AGENT_PORT    optional, default 8088
  DRYDOCK_HOST_AGENT_BIND    optional, default 0.0.0.0
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse


TOKEN = os.environ.get("DRYDOCK_HOST_AGENT_TOKEN", "")
PORT = int(os.environ.get("DRYDOCK_HOST_AGENT_PORT", "8088"))
BIND = os.environ.get("DRYDOCK_HOST_AGENT_BIND", "0.0.0.0")

# Defensive validators — values come from a trusted source (backend env) but
# we still want the agent to refuse anything that doesn't look like the right
# shape, so a typo or compromised secret can't easily turn this into an
# arbitrary command runner.
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(?:[:\-][0-9A-Fa-f]{2}){5}$")
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9._\-@]+$")
_IP_RE = re.compile(r"^[0-9.]+$")


def _run(cmd: list[str], *, timeout: int = 10) -> dict:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"exit": r.returncode, "stdout": r.stdout[-2000:], "stderr": r.stderr[-2000:]}
    except subprocess.TimeoutExpired:
        return {"exit": 124, "stdout": "", "stderr": "timeout"}
    except FileNotFoundError as e:
        return {"exit": 127, "stdout": "", "stderr": str(e)}


class Handler(BaseHTTPRequestHandler):
    server_version = "drydock-host-agent/0.1"

    def _auth_ok(self) -> bool:
        if not TOKEN:
            # Fail closed — running without a token would expose a remote
            # command runner on the LAN, which is never what we want.
            return False
        return self.headers.get("Authorization", "") == f"Bearer {TOKEN}"

    def _reply(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ─ routes ────────────────────────────────────────────────────────

    def _wake(self, qs: dict) -> None:
        mac = (qs.get("mac", [""])[0] or "").strip()
        broadcast = (qs.get("broadcast", ["255.255.255.255"])[0] or "").strip()
        if not _MAC_RE.match(mac):
            return self._reply(400, {"error": "invalid mac"})
        if not _IP_RE.match(broadcast):
            return self._reply(400, {"error": "invalid broadcast"})
        return self._reply(200, _run(["wakeonlan", "-i", broadcast, mac]))

    def _shutdown(self, qs: dict) -> None:
        host = (qs.get("host", [""])[0] or "").strip()
        if not _HOSTNAME_RE.match(host):
            return self._reply(400, {"error": "invalid host"})
        # We invoke ssh with a fixed command — never substitute arbitrary
        # strings from the caller. If you want a different shutdown style
        # (hibernate, sleep), change this constant here.
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
               host, "shutdown /s /t 0"]
        return self._reply(200, _run(cmd, timeout=15))

    def _status(self, qs: dict) -> None:
        host = (qs.get("host", [""])[0] or "").strip()
        if not _HOSTNAME_RE.match(host):
            return self._reply(400, {"error": "invalid host"})
        # macOS ping flags: -c count, -W timeout (ms)
        r = _run(["ping", "-c", "1", "-W", "1000", host], timeout=5)
        return self._reply(200, {"online": r["exit"] == 0, **r})

    # ─ wire ──────────────────────────────────────────────────────────

    def do_POST(self) -> None:
        if not self._auth_ok():
            return self._reply(401, {"error": "unauthorized"})
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        if url.path == "/wake":
            return self._wake(qs)
        if url.path == "/shutdown":
            return self._shutdown(qs)
        return self._reply(404, {"error": "unknown path"})

    def do_GET(self) -> None:
        if self.path.startswith("/healthz"):
            return self._reply(200, {"status": "ok"})
        if not self._auth_ok():
            return self._reply(401, {"error": "unauthorized"})
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        if url.path == "/status":
            return self._status(qs)
        return self._reply(404, {"error": "unknown path"})

    def log_message(self, fmt: str, *args) -> None:  # type: ignore[override]
        sys.stdout.write(f"{self.address_string()} - {fmt % args}\n")
        sys.stdout.flush()


def main() -> None:
    if not TOKEN:
        sys.stderr.write(
            "DRYDOCK_HOST_AGENT_TOKEN is not set — refusing to start "
            "without a shared secret. Generate one with `openssl rand -hex 32` "
            "and set it in this process's environment AND in the backend "
            "container's env (DRYDOCK_HOST_AGENT_TOKEN).\n"
        )
        sys.exit(1)
    server = HTTPServer((BIND, PORT), Handler)
    print(f"dry-dock host agent listening on {BIND}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
