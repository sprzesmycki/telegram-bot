"""Agent file loader and runner.

Agent files are Markdown files with a YAML frontmatter block::

    ---
    name: meal-analyzer
    model: google/gemini-flash-1.5          # model-only override (current provider)
    # or:
    # model: local:gemma4:26b               # provider:model — builds a dedicated client
    tools: []                               # MCP tool names (empty = no tools)
    ---
    You are a nutrition assistant...

The ``model`` field is optional:
- Absent / empty  → use the currently active provider + model (respects ``/model`` switching)
- ``"model_id"``  → current-provider client with this model override
- ``"provider:model_id"`` → dedicated client built for that provider + model

``tools`` is a list of names registered in ``bot.tools``.  An empty list means
no tool-calling.  When tools are present the runner appends their OpenAI schemas
to the API call — the LLM may then emit ``tool_calls`` which are passed back to
the caller as-is (the caller is responsible for the execution loop when needed).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent.parent


# ---------------------------------------------------------------------------
# Agent definition
# ---------------------------------------------------------------------------


@dataclass
class AgentDefinition:
    name: str
    model: str | None           # None | "model_id" | "provider:model_id"
    tools: list[str]
    system_prompt: str


# ---------------------------------------------------------------------------
# Loader (cached)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=None)
def load_agent(path: str) -> AgentDefinition:
    """Parse an agent .md file and return an AgentDefinition.

    *path* may be absolute or relative to the project root.
    Results are cached forever — agent files are not re-read at runtime.
    """
    p = Path(path)
    if not p.is_absolute():
        p = _PROJECT_ROOT / p

    text = p.read_text(encoding="utf-8")

    meta: dict[str, Any] = {}
    body = text

    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            meta = yaml.safe_load(parts[1]) or {}
            body = parts[2]

    return AgentDefinition(
        name=meta.get("name", p.stem),
        model=meta.get("model") or None,
        tools=meta.get("tools") or [],
        system_prompt=body.strip(),
    )


# ---------------------------------------------------------------------------
# Client resolution
# ---------------------------------------------------------------------------


def _resolve_client(model_spec: str | None):  # -> tuple[AsyncOpenAI, str]
    """Map an agent model spec to (client, resolved_model_name).

    Formats:
    - None / ""                → global default via get_llm_client()
    - "model_id"               → get_llm_client(model_override="model_id")
    - "provider:model_id"      → _build_client(provider, model_id)
    """
    from bot.services.llm import _build_client, get_llm_client

    if not model_spec:
        return get_llm_client()

    # Detect "provider:model" only when the part before the first colon is a
    # known provider name, not a URL (avoid matching "https://...").
    _PROVIDERS = {"openrouter", "local", "custom"}
    first, _, rest = model_spec.partition(":")
    if first in _PROVIDERS and rest:
        client, resolved_model, _ = _build_client(first, rest)
        return client, resolved_model

    # Plain model name — override model on current provider client
    return get_llm_client(model_override=model_spec)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def run_agent(
    agent: AgentDefinition,
    messages: list[dict[str, Any]],
    *,
    response_format: dict[str, Any] | None = None,
    temperature: float = 0.3,
) -> str:
    """Execute an agent: prepend system prompt, call LLM, return content.

    *messages* should NOT include the system message — this function prepends
    ``agent.system_prompt`` as ``{"role": "system", ...}`` automatically.

    If ``agent.tools`` is non-empty, the registered tool schemas are passed to
    the API.  Tool-call responses are returned raw (the caller handles the loop
    when multi-turn execution is required).

    Raises whatever the underlying ``openai`` client raises (e.g.
    ``openai.BadRequestError``, ``openai.APIConnectionError``).
    """
    client, model = _resolve_client(agent.model)

    full_messages: list[dict[str, Any]] = [
        {"role": "system", "content": agent.system_prompt},
        *messages,
    ]

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": full_messages,
        "temperature": temperature,
    }

    if response_format:
        kwargs["response_format"] = response_format

    if agent.tools:
        from bot.tools import get_tools_for_names
        tool_schemas = get_tools_for_names(agent.tools)
        if tool_schemas:
            kwargs["tools"] = tool_schemas

    logger.debug("run_agent: name=%s model=%s tools=%s", agent.name, model, agent.tools)

    response = await client.chat.completions.create(**kwargs)
    return (response.choices[0].message.content or "").strip()
