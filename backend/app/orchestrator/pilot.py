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

import re
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


# ── choosing a model ───────────────────────────────────────────────
#
# The raw dropdown was a flat alphabetical list of Ollama tags, which asks the
# user to know things the app already knows: that llama3 and llama3.1 are
# different models with different tool support, that a 70B answer is minutes
# not seconds, that a tag offered by the pool may live on only one of its
# machines. Everything below turns those into text on the option itself, so
# the appropriate choice is the obvious one rather than the informed one.

_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*b(?:\b|$)", re.I)


def model_size_b(tag: str) -> float | None:
    """Parameter count in billions, parsed from the tag, or None if untagged.

    Reads the part after the colon so a name like `llama3:70b` gives 70 and
    `qwen2.5-coder:32b` gives 32. `:latest` and bare names give None — common,
    and treated as "unknown" rather than guessed.
    """
    suffix = tag.split(":", 1)[1] if ":" in tag else tag
    match = _SIZE_RE.search(suffix)
    return float(match.group(1)) if match else None


def describe_model(tag: str, *, installed_on: int, worker_total: int) -> dict:
    """Annotate one model tag with everything that should affect the choice."""
    size = model_size_b(tag)
    tools = is_tool_capable(tag)
    everywhere = worker_total > 0 and installed_on >= worker_total

    bits: list[str] = []
    bits.append(f"{size:g}B" if size is not None else "size unknown")
    bits.append("tools" if tools else "no tools")
    if "coder" in tag.lower():
        bits.append("code-tuned")
    if not everywhere:
        bits.append(f"on {installed_on} of {worker_total} workers")

    return {
        "tag": tag,
        "size_b": size,
        "tools": bool(tools),
        "specialist": "coder" in tag.lower(),
        "installed_on": installed_on,
        "everywhere": everywhere,
        "label": f"{tag} — {' · '.join(bits)}",
    }


# Group keys, in the order they should appear. The copy lives in the template.
GROUP_GOOD = "good"
GROUP_NO_TOOLS = "no_tools"
GROUP_PARTIAL = "partial"


def _group_for(mode: str, d: dict) -> str:
    if not d["everywhere"]:
        return GROUP_PARTIAL
    # Tool support only sorts models for the mode that can actually use them.
    if mode == DEEP and not d["tools"]:
        return GROUP_NO_TOOLS
    return GROUP_GOOD


def _sort_key(mode: str, d: dict):
    """Order within a group. Unknown size always sorts last — it is not guessed.

    The two modes want opposite things, and neither wants a code model by
    default: Pilot is a general chat surface, so a `*-coder` tag is demoted
    below any general-purpose alternative. It stays selectable and is labelled
    as such — ranking it lower just stops "smallest" from recommending a
    fill-in-the-middle code model as a conversationalist.
    """
    unknown = 0 if d["size_b"] is not None else 1
    specialist = 1 if d["specialist"] else 0
    if mode == DEEP:
        # Bigger is better where "thoughtful" is the whole point; size wins
        # over general-vs-code, since a large code-tuned model still reasons
        # better than a small general one.
        return (unknown, -(d["size_b"] or 0), specialist, d["tag"])
    # Lightweight wants a snappy general chat model, so general first, then
    # smallest — a 6.7B code model is fast and useless for this.
    return (specialist, unknown, d["size_b"] or 0, d["tag"])


def rank_models(mode: str, models: list[str], coverage: dict[str, int],
                worker_total: int) -> tuple[list[dict], str | None]:
    """Annotate, group and order the pool's models for this mode.

    Returns (options, recommended_tag). The recommendation is simply the top
    of the first group — the model that is present on every worker, can do
    what the mode needs, and is biggest (Thoughtful) or smallest
    (Lightweight). None when no model qualifies, in which case the UI should
    keep pointing at the worker default.
    """
    described = [
        describe_model(t, installed_on=coverage.get(t, 0), worker_total=worker_total)
        for t in models
    ]
    for d in described:
        d["group"] = _group_for(mode, d)

    options: list[dict] = []
    for group in (GROUP_GOOD, GROUP_NO_TOOLS, GROUP_PARTIAL):
        members = [d for d in described if d["group"] == group]
        members.sort(key=lambda d: _sort_key(mode, d))
        options.extend(members)

    best = next((d["tag"] for d in options if d["group"] == GROUP_GOOD), None)
    return options, best


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
    coverage = {
        tag: sum(1 for w in workers if tag in (w.installed_models or ()))
        for tag in installed
    }
    options, recommended = rank_models(
        config.mode, sorted(installed), coverage, len(workers)
    )

    # How many of the pool's online workers actually have the pinned model.
    # The available-model list is a UNION, so "some" is a real state in a
    # multi-machine pool — and dispatch only sends to a worker that has it,
    # so "some" means reduced capacity rather than failure. Workers that
    # registered with an empty list (Ollama unreachable at startup) count as
    # not having it, which is the truth.
    if config.model is None:
        model_ok: bool | None = None
        model_worker_count = len(workers)
    else:
        model_worker_count = sum(
            1 for w in workers if config.model in (w.installed_models or ())
        )
        model_ok = model_worker_count > 0

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
        # The same models, annotated, grouped and ordered best-first for this
        # mode, plus the one the UI should suggest.
        "model_options": options,
        "recommended": recommended,
        # None = no model pinned, so the worker's own default applies and
        # there is nothing to verify.
        "model_installed": model_ok,
        "model_worker_count": model_worker_count,
        # Workers that registered without a model list — their Ollama was
        # unreachable at startup. They fail every pinned model silently.
        "blind_workers": sorted(w.name for w in workers if not w.installed_models),
        "tool_capable": capable,
        # What tools mode would actually resolve to if the user turns web on.
        "effective_web": web_mechanism(config, True),
        "online": len(workers) > 0,
    }


async def pool_options() -> list[dict]:
    """Every pool, described by the hardware actually behind it right now.

    "coder" is not a meaningful choice; "coder — macbook · 64 GB · 1 online"
    is. Pools map one-to-one to machines, so the registry can say which.
    """
    out: list[dict] = []
    for pool in KNOWN_POOLS:
        workers = await registry.by_pool(pool)
        classes = sorted({w.hardware_class for w in workers if w.hardware_class})
        ram = max((w.ram_gb or 0) for w in workers) if workers else 0
        bits: list[str] = []
        if classes:
            bits.append(" + ".join(classes))
        if ram:
            bits.append(f"{ram} GB")
        bits.append(
            f"{len(workers)} online" if workers else "nothing online"
        )
        out.append({
            "pool": pool,
            "online": bool(workers),
            "worker_count": len(workers),
            "label": f"{pool} — {' · '.join(bits)}",
        })
    return out


async def all_mode_statuses() -> list[dict]:
    configs = await get_all_configs()
    return [await mode_status(configs[mode]) for mode in MODES]
