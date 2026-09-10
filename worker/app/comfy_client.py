"""ComfyUI client + workflow templating for the imager worker.

ComfyUI's API takes a whole node graph, not a prompt string, so the worker
ships *named templates* and the orchestrator sends parameters. Each template
is a JSON file in `app/workflows/` shaped like:

    {
      "description": "...",
      "map": {"prompt": ["6", "text"], "seed": ["3", "seed"], ...},
      "graph": { "<node id>": {"class_type": ..., "inputs": {...}}, ... }
    }

`map` is what keeps the parameter names in this repo from having to know
ComfyUI node ids — it lives next to the graph it describes, so exporting a new
workflow from ComfyUI ("Save (API Format)") and writing five lines of map is
the whole cost of adding one. Templates are discovered at startup and
advertised in the register message, so the Darkroom UI can only ever offer
what this machine actually has.

The API surface used here:
    POST /prompt                 queue a graph        → {"prompt_id": ...}
    GET  /history/{prompt_id}    poll for completion
    GET  /view?filename=…        fetch the PNG bytes
    GET  /object_info/…          what's installed
    GET  /system_stats           liveness
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx
import structlog

from app.config import get_settings

log = structlog.get_logger()

WORKFLOW_DIR = Path(__file__).resolve().parent / "workflows"


class ComfyError(Exception):
    """Anything that should fail the image job with a legible message."""


# ── workflow templates ─────────────────────────────────────────────


def load_workflows() -> dict[str, dict]:
    """Read every template in app/workflows/. A malformed file is skipped with
    a warning rather than taking the worker down — one bad JSON shouldn't cost
    you the whole imager."""
    out: dict[str, dict] = {}
    if not WORKFLOW_DIR.is_dir():
        return out
    for path in sorted(WORKFLOW_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text())
            if "graph" not in data:
                raise ValueError("template has no 'graph' key")
            out[path.stem] = data
        except Exception as exc:  # noqa: BLE001
            log.warning("comfy.bad_workflow", file=path.name, error=str(exc))
    return out


def render_graph(template: dict, params: dict[str, Any]) -> dict:
    """Apply parameters to a template's graph via its `map`.

    A map entry is either a single target `["6", "text"]` or a list of them,
    `[["3", "seed"], ["11", "seed"]]` — multi-pass workflows need one parameter
    to land on several nodes, so a hires template can drive both of its
    samplers from one `seed`.

    A None parameter leaves the template's own default in place, so a template
    can carry sensible values for anything the caller doesn't care about.
    """
    graph = json.loads(json.dumps(template["graph"]))  # deep copy
    mapping: dict[str, list] = template.get("map", {})
    for name, value in params.items():
        if value is None:
            continue
        target = mapping.get(name)
        if not target:
            continue  # this template doesn't expose that parameter
        # Normalise single-target and multi-target forms to one shape.
        targets = target if isinstance(target[0], (list, tuple)) else [target]
        for node_id, input_name in targets:
            node = graph.get(str(node_id))
            if node is None:
                log.warning("comfy.map_points_nowhere", param=name, node=node_id)
                continue
            node.setdefault("inputs", {})[input_name] = value
    return graph


# ── client ─────────────────────────────────────────────────────────


class ComfyClient:
    def __init__(self, base_url: str | None = None) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.comfyui_base_url).rstrip("/")
        self.client_id = str(uuid.uuid4())

    async def health(self) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self.base_url}/system_stats")
                r.raise_for_status()
                return r.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("comfy.health_failed", url=self.base_url, error=str(exc))
            return None

    async def list_checkpoints(self) -> list[str]:
        """What's actually on this machine. Advertised at register time so the
        UI's dropdown can't offer a checkpoint that isn't installed."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{self.base_url}/object_info/CheckpointLoaderSimple")
                r.raise_for_status()
                data = r.json()
            options = (
                data["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
            )
            return [str(o) for o in options]
        except Exception as exc:  # noqa: BLE001
            log.warning("comfy.list_checkpoints_failed", error=str(exc))
            return []

    async def list_samplers(self) -> tuple[list[str], list[str]]:
        """The sampler and scheduler names this ComfyUI build actually offers.

        Advertised at register time for the same reason checkpoints are: a
        hardcoded list in the orchestrator goes stale the moment ComfyUI adds
        a sampler, and offering one the box doesn't have produces a failure
        deep in the render rather than a greyed-out option.
        """
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{self.base_url}/object_info/KSampler")
                r.raise_for_status()
                req = r.json()["KSampler"]["input"]["required"]
            return (
                [str(x) for x in req["sampler_name"][0]],
                [str(x) for x in req["scheduler"][0]],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("comfy.list_samplers_failed", error=str(exc))
            return [], []

    async def upload_image(self, data: bytes, filename: str) -> str:
        """Push an image into ComfyUI's input folder and return its name.

        ComfyUI's LoadImage node takes a *filename*, not bytes — it reads from
        its own input directory. So an img2img job is always two steps: upload,
        then reference the returned name in the graph. `overwrite` keeps the
        input folder from filling up with one file per render.
        """
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.post(
                    f"{self.base_url}/upload/image",
                    files={"image": (filename, data, "image/png")},
                    data={"overwrite": "true"},
                )
                r.raise_for_status()
                body = r.json()
        except httpx.HTTPError as exc:
            raise ComfyError(f"Couldn't upload the source image to ComfyUI: {exc}") from exc

        name = body.get("name")
        if not name:
            raise ComfyError(f"ComfyUI accepted the upload but named no file: {body}")
        subfolder = body.get("subfolder") or ""
        # LoadImage addresses a file in a subfolder as "sub/name".
        return f"{subfolder}/{name}" if subfolder else str(name)

    async def submit(self, graph: dict) -> str:
        payload = {"prompt": graph, "client_id": self.client_id}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(f"{self.base_url}/prompt", json=payload)
        except httpx.HTTPError as exc:
            raise ComfyError(f"ComfyUI unreachable at {self.base_url}: {exc}") from exc

        if r.status_code >= 400:
            # ComfyUI returns a structured validation error here, and it's the
            # single most useful message in the whole pipeline — a missing
            # checkpoint or a bad sampler name lands exactly here.
            try:
                body = r.json()
                detail = body.get("error") or body
                node_errors = body.get("node_errors")
                if node_errors:
                    detail = f"{detail} · node errors: {node_errors}"
            except Exception:  # noqa: BLE001
                detail = r.text[:500]
            raise ComfyError(f"ComfyUI rejected the workflow: {detail}")

        prompt_id = r.json().get("prompt_id")
        if not prompt_id:
            raise ComfyError("ComfyUI accepted the workflow but returned no prompt_id.")
        return str(prompt_id)

    async def wait_for_images(
        self,
        prompt_id: str,
        timeout: float,
        on_progress: Callable[[int, int], Any] | None = None,
    ) -> list[bytes]:
        """Poll /history until the prompt finishes, then fetch its PNGs.

        Polling rather than ComfyUI's progress WebSocket: one HTTP call a
        second against a service on the same host is cheap, and it keeps this
        client free of a second socket lifecycle to get wrong.
        """
        deadline = time.monotonic() + timeout
        entry: dict | None = None
        polls = 0
        async with httpx.AsyncClient(timeout=15.0) as client:
            while time.monotonic() < deadline:
                await asyncio.sleep(1.0)
                polls += 1
                try:
                    r = await client.get(f"{self.base_url}/history/{prompt_id}")
                    r.raise_for_status()
                    history = r.json()
                except httpx.HTTPError as exc:
                    log.warning("comfy.history_poll_failed", error=str(exc))
                    continue

                entry = history.get(prompt_id)
                if entry:
                    status = (entry.get("status") or {})
                    if status.get("status_str") == "error" or status.get("completed") is False:
                        messages = status.get("messages") or []
                        raise ComfyError(f"ComfyUI reported an error: {messages}")
                    if entry.get("outputs"):
                        break
                if on_progress is not None:
                    try:
                        on_progress(polls, int(timeout))
                    except Exception:  # noqa: BLE001
                        pass
                entry = None

            if entry is None:
                raise ComfyError(
                    f"ComfyUI didn't finish within {timeout:.0f}s. The checkpoint "
                    "may still be loading on a cold start — try again."
                )

            refs: list[dict] = []
            for node_output in (entry.get("outputs") or {}).values():
                refs.extend(node_output.get("images") or [])
            if not refs:
                raise ComfyError(
                    "ComfyUI finished but produced no images — the workflow "
                    "probably has no SaveImage node."
                )

            images: list[bytes] = []
            for ref in refs:
                if ref.get("type") == "temp":
                    continue  # previews, not the real output
                rr = await client.get(
                    f"{self.base_url}/view",
                    params={
                        "filename": ref.get("filename"),
                        "subfolder": ref.get("subfolder", ""),
                        "type": ref.get("type", "output"),
                    },
                )
                rr.raise_for_status()
                images.append(rr.content)

            if not images:
                raise ComfyError("ComfyUI produced only preview images, no saved output.")
            return images
