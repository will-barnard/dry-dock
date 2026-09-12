"""Pilot modes — the two-choice abstraction over pools and models.

Pilot asks the user exactly one question: how much thought should this get?

    light  ("Lightweight") — fast, cheap, whatever is always-on
    deep   ("Thoughtful")  — the big model, and the only mode that gets tools

Everything else — which pool, which model tag, whether the web access is a
pre-flight injection or an agentic loop — is configuration, resolved here.

LATE BINDING IS THE POINT
-------------------------
A conversation stores only its `mode`. The (pool, model) pair is resolved from
`app_settings` at *dispatch* time, never frozen onto the row at creation. That
buys three things the old Operator could not do:

  * Changing a mode's model in settings heals every existing thread. Before
    this, `conversation.model` was written once at creation with no UI to edit
    it, so retiring a model permanently bricked every thread pinned to it.
  * A thread can switch modes mid-conversation without losing its history.
  * Retiring or renaming a worker is a settings edit, not a data migration.

NOT THE SAME AS THE FLEET SETTINGS PAGE
---------------------------------------
The role→model pins on /settings are a *hard filter applied by the task
router* (`router.py::_worker_compatible`): a worker without that exact tag is
dropped before priority is considered. Pilot chat never goes through the
router — `chat.dispatch_turn` reads the registry directly — so nothing here
filters workers. Identical-looking dropdowns, different machinery. Keep them
straight when debugging.
"""
from __future__ import annotations

from dataclasses import dataclass

import structlog

from app.orchestrator import settings_service
from app.orchestrator.pools import KNOWN_POOLS
from app.orchestrator.registry import registry

log = structlog.get_logger()

LIGHT = "light"
DEEP = "deep"
MODES: tuple[str, ...] = (LIGHT, DEEP)
DEFAULT_MODE = DEEP

MODE_LABELS: dict[str, str] = {
    LIGHT: "Lightweight",
    DEEP: "Thoughtful",
}

MODE_BLURBS: dict[str, str] = {
    LIGHT: "Quick answers from an always-on machine. No tools.",
    DEEP: "The big model. Slower, and the only mode that can use tools.",
}


# Shipped defaults, used until the settings page writes something. `light`
# points at the pool that lives on always-on hardware; `deep` at the pool on
# the box with the most RAM. Model unset means "whatever that worker's
# DEFAULT_MODEL is" — the safest possible default, since a worker can never
# 404 on its own configured model.
_DEFAULTS: dict[str, "ModeConfig"] = {}


@dataclass(frozen=True)
class ModeConfig:
    """Where a mode's work goes, and whether it may call tools."""

    mode: str
    pool: str
    model: str | None
    tools: bool

    @property
    def label(self) -> str:
        return MODE_LABELS.get(self.mode, self.mode)


_DEFAULTS[LIGHT] = ModeConfig(mode=LIGHT, pool="researcher", model=None, tools=False)
_DEFAULTS[DEEP] = ModeConfig(mode=DEEP, pool="coder", model=None, tools=True)


# ── tool capability ────────────────────────────────────────────────
#
# Ollama silently IGNORES the `tools` field on a model that can't do function
# calling — the request succeeds and the model answers from memory, so a
# mis-set model produces a confidently wrong answer rather than an error.
# That silent-failure shape is why this check exists at all.
#
# This is a family allowlist, which is a knowingly cheap stand-in for the
# truthful version: have the worker call Ollama's /api/show per installed
# model at register time and report a `tool_capable` list on RegisterMsg.
# That is additive on the message (pydantic defaults it for old workers) but
# it IS a worker change, which means rebuilding and restarting every worker on
# every machine by hand. Worth doing on a day the fleet is already being
# walked; until then, this list plus the warning on the settings page.

TOOL_CAPABLE_PREFIXES: tuple[str, ...] = (
    "qwen2.5",
    "qwen3",
    "llama3.1",
    "llama3.2",
    "llama3.3",
    "mistral-nemo",
    "mistral-small",
    "mistral-large",
    "devstral",
    "command-r",
    "hermes3",
    "granite3",
    "firefunction",
)


def is_tool_capable(model: str | None) -> bool | None:
    """True / False for a known model family, None when we can't tell.

    None means the mode has no pinned model, so the worker will use its own
    DEFAULT_MODEL and we have no name to check. Callers should treat None as
    "allow, but say so" rather than as a failure.
    """
    if not model:
        return None
    name = model.strip().lower()
    return name.startswith(TOOL_CAPABLE_PREFIXES)


# ── mode configuration ─────────────────────────────────────────────


def normalize_mode(value: str | None) -> str:
    m = (value or "").strip().lower()
    return m if m in MODES else DEFAULT_MODE


def _key(mode: str, field: str) -> str:
    return f"pilot.{mode}.{field}"


async def get_mode_config(mode: str) -> ModeConfig:
    """Resolve a mode to its pool / model / tools setting.

    Falls back to the shipped default field by field, so a half-configured
    mode still resolves rather than erroring.
    """
    mode = normalize_mode(mode)
    default = _DEFAULTS[mode]

    pool = await settings_service.get_raw(_key(mode, "pool"))
    if pool not in KNOWN_POOLS:
        pool = default.pool

    model = await settings_service.get_raw(_key(mode, "model")) or None

    raw_tools = await settings_service.get_raw(_key(mode, "tools"))
    if raw_tools is None:
        tools = default.tools
    else:
        tools = raw_tools == "1"

    return ModeConfig(mode=mode, pool=pool, model=model, tools=tools)


async def get_all_configs() -> dict[str, ModeConfig]:
    return {mode: await get_mode_config(mode) for mode in MODES}


async def set_mode_config(
    mode: str, pool: str, model: str | None, tools: bool
) -> None:
    """Persist one mode's configuration. Raises ValueError on a bad pool."""
    mode = normalize_mode(mode)
    if pool not in KNOWN_POOLS:
        raise ValueError(f"unknown pool: {pool}")
    # Tools belong to Thoughtful and nowhere else. Enforced here as well as in
    # the route so the invariant survives a future caller that forgets it —
    # the whole light/deep split rests on it.
    tools = bool(tools) and mode == DEEP
    await settings_service.set_raw(_key(mode, "pool"), pool)
    await settings_service.set_raw(_key(mode, "model"), (model or "").strip() or None)
    await settings_service.set_raw(_key(mode, "tools"), "1" if tools else "0")
    log.info("pilot.mode_configured", mode=mode, pool=pool, model=model, tools=tools)


# ── web access ─────────────────────────────────────────────────────


def web_mechanism(config: ModeConfig, web_enabled: bool) -> str:
    """Map (mode config, web on/off) to the mechanism dispatch should use.

    "off"    — no web
    "search" — pre-flight SearXNG injection; works on any model
    "tools"  — agentic web_search / fetch_url loop; needs function calling

    The degrade path matters: a mode with tools enabled but a model that
    can't call them falls back to "search" rather than handing the model a
    `tools` field it will silently ignore. Worse answers are acceptable;
    silently-no-web answers that look researched are not.
    """
    if not web_enabled:
        return "off"
    if not config.tools:
        return "search"
    if is_tool_capable(config.model) is False:
        return "search"
    return "tools"


# ── live status (for the settings page and composer preflight) ─────


async def mode_status(config: ModeConfig) -> dict:
    """What this mode would actually do right now, per the live registry.

    The registry is the truthful source — `worker` rows in Postgres are only
    set offline by the WS disconnect handler and can be stale ghosts.
    """
    workers = await registry.by_pool(config.pool)
    installed: set[str] = {m for w in workers for m in (w.installed_models or ())}

    if config.model is None:
        model_ok: bool | None = None
    else:
        model_ok = config.model in installed

    capable = is_tool_capable(config.model)

    return {
        "mode": config.mode,
        "label": config.label,
        "pool": config.pool,
        "model": config.model,
        "tools": config.tools,
        "worker_count": len(workers),
        "worker_names": sorted(w.name for w in workers),
        "available_models": sorted(installed),
        # None = no model pinned, so the worker's own default applies and
        # there is nothing to verify.
        "model_installed": model_ok,
        "tool_capable": capable,
        # What tools mode would actually resolve to if the user turns web on.
        "effective_web": web_mechanism(config, True),
        "online": len(workers) > 0,
    }


async def all_mode_statuses() -> list[dict]:
    configs = await get_all_configs()
    return [await mode_status(configs[mode]) for mode in MODES]
