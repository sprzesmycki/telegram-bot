"""MCP-style tool registry.

Tools registered here can be referenced by name in agent .md files under the
``tools:`` frontmatter key. The agent runner fetches their OpenAI-compatible
schema and passes it to the LLM at call time.

Currently empty — wired for future multimodel agent integration.

Usage::

    from bot.tools import register_tool, get_tools_schema, call_tool

    register_tool(
        name="get_meals_today",
        description="Return today's logged meals for a user profile.",
        parameters={
            "type": "object",
            "properties": {
                "owner_id": {"type": "integer"},
                "profile_id": {"type": "integer"},
            },
            "required": ["owner_id", "profile_id"],
        },
        fn=db.get_meals_today,
    )
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]   # JSON Schema object
    fn: Callable                 # async Python function


_REGISTRY: dict[str, ToolDefinition] = {}


def register_tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
    fn: Callable,
) -> None:
    """Register a callable as an LLM-callable tool."""
    _REGISTRY[name] = ToolDefinition(
        name=name,
        description=description,
        parameters=parameters,
        fn=fn,
    )
    logger.debug("Registered tool: %s", name)


def get_tools_schema() -> list[dict[str, Any]]:
    """Return all registered tools in OpenAI function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in _REGISTRY.values()
    ]


def get_tools_for_names(names: list[str]) -> list[dict[str, Any]]:
    """Return OpenAI tool schemas for a specific list of tool names."""
    result = []
    for name in names:
        if name in _REGISTRY:
            t = _REGISTRY[name]
            result.append({
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            })
        else:
            logger.warning("Tool '%s' referenced in agent file but not registered", name)
    return result


async def call_tool(name: str, args: dict[str, Any]) -> Any:
    """Invoke a registered tool by name with the given arguments."""
    if name not in _REGISTRY:
        raise KeyError(f"Tool '{name}' is not registered")
    return await _REGISTRY[name].fn(**args)
