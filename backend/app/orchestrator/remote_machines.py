"""Remote machine control — wake / shutdown / status.

Machines are configured via REMOTE_MACHINES_JSON (a JSON array). Each entry:

    {
      "name": "windows-rig",
      "display_name": "Windows (RTX 3080)",
      "mac": "D8:BB:C1:51:84:DF",
      "ssh_host": "windows",
      "broadcast": "192.168.1.255",
      "hardware_class": "windows-rtx3080"
    }

`hardware_class` is the value the worker advertises in its `register` message;
we cross-reference against the live registry so a machine that has workers
connected shows as online without needing a ping.

The actual side effects live in the host-agent (see host-agent/agent.py) which
runs OUTSIDE Docker on the Mac mini. This module is just an HTTP client.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from app.config import get_settings
from app.orchestrator.registry import registry

log = structlog.get_logger()


@dataclass
class RemoteMachine:
    name: str
    display_name: str
    mac: str
    ssh_host: str
    broadcast: str
    hardware_class: str | None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RemoteMachine":
        return cls(
            name=d["name"],
            display_name=d.get("display_name") or d["name"],
            mac=d["mac"],
            ssh_host=d.get("ssh_host", ""),
            broadcast=d.get("broadcast", "255.255.255.255"),
            hardware_class=d.get("hardware_class"),
        )


def configured_machines() -> list[RemoteMachine]:
    settings = get_settings()
    try:
        raw = json.loads(settings.remote_machines_json or "[]")
    except json.JSONDecodeError as exc:
        log.warning("remote_machines.bad_json", error=str(exc))
        return []
    if not isinstance(raw, list):
        return []
    out: list[RemoteMachine] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            out.append(RemoteMachine.from_dict(entry))
        except KeyError as exc:
            log.warning("remote_machines.bad_entry", missing=str(exc), entry=entry)
    return out


def find_machine(name: str) -> RemoteMachine | None:
    for m in configured_machines():
        if m.name == name:
            return m
    return None


async def machine_status(machine: RemoteMachine) -> dict[str, Any]:
    """Decide whether the machine is online. Two signals:

    1. Live workers from this hardware_class → definitely online.
    2. Host agent's ping result against the SSH host → fallback.

    Returns ``{"online": bool, "source": "workers"|"ping"|"unknown", …}``.
    """
    if machine.hardware_class:
        live = await registry.all()
        if any(w.hardware_class == machine.hardware_class for w in live):
            return {"online": True, "source": "workers"}

    settings = get_settings()
    if not machine.ssh_host or not settings.host_agent_token:
        return {"online": False, "source": "unknown"}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"{settings.host_agent_url}/status",
                params={"host": machine.ssh_host},
                headers={"Authorization": f"Bearer {settings.host_agent_token}"},
            )
            if r.status_code >= 300:
                return {"online": False, "source": "ping", "error": r.text}
            data = r.json()
            return {"online": bool(data.get("online")), "source": "ping"}
    except httpx.HTTPError as exc:
        log.warning("remote_machines.status_failed", machine=machine.name, error=str(exc))
        return {"online": False, "source": "ping", "error": str(exc)}


async def wake_machine(machine: RemoteMachine) -> dict[str, Any]:
    settings = get_settings()
    if not settings.host_agent_token:
        return {"ok": False, "error": "DRYDOCK_HOST_AGENT_TOKEN not configured on backend"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{settings.host_agent_url}/wake",
                params={"mac": machine.mac, "broadcast": machine.broadcast},
                headers={"Authorization": f"Bearer {settings.host_agent_token}"},
            )
    except httpx.HTTPError as exc:
        log.warning("remote_machines.wake_failed", machine=machine.name, error=str(exc))
        return {"ok": False, "error": f"agent unreachable: {exc}"}
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    ok = r.status_code < 300 and body.get("exit") == 0
    log.info("remote_machines.wake", machine=machine.name, ok=ok, body=body)
    return {"ok": ok, **body}


async def shutdown_machine(machine: RemoteMachine) -> dict[str, Any]:
    settings = get_settings()
    if not settings.host_agent_token:
        return {"ok": False, "error": "DRYDOCK_HOST_AGENT_TOKEN not configured on backend"}
    if not machine.ssh_host:
        return {"ok": False, "error": "machine has no ssh_host configured"}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                f"{settings.host_agent_url}/shutdown",
                params={"host": machine.ssh_host},
                headers={"Authorization": f"Bearer {settings.host_agent_token}"},
            )
    except httpx.HTTPError as exc:
        log.warning("remote_machines.shutdown_failed", machine=machine.name, error=str(exc))
        return {"ok": False, "error": f"agent unreachable: {exc}"}
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    # `shutdown /s /t 0` typically kills the SSH session before responding so
    # exit codes are unreliable. Treat HTTP 2xx as success.
    ok = r.status_code < 300
    log.info("remote_machines.shutdown", machine=machine.name, ok=ok, body=body)
    return {"ok": ok, **body}
