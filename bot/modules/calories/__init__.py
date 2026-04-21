"""Calories module — optional, controlled by config.yaml modules.calories.enabled.

Covers: meal logging, liquid logging, recipes, goals, supplements,
daily summary, weekly overview, reports, and AI-powered day reviews.
"""
from __future__ import annotations

from bot.config import get_config
from bot.modules.calories.handlers.calories import COMMANDS as _CAL_COMMANDS
from bot.modules.calories.handlers.calories import register as _reg_calories
from bot.modules.calories.handlers.goals import COMMANDS as _GOAL_COMMANDS
from bot.modules.calories.handlers.goals import register as _reg_goals
from bot.modules.calories.handlers.liquids import COMMANDS as _LIQ_COMMANDS
from bot.modules.calories.handlers.liquids import register as _reg_liquids
from bot.modules.calories.handlers.review import register as _reg_review
from bot.modules.calories.handlers.summary import COMMANDS as _SUM_COMMANDS
from bot.modules.calories.handlers.summary import register as _reg_summary


class CaloriesModule:
    @property
    def ENABLED(self) -> bool:
        return get_config().modules.calories.enabled

    COMMANDS: list[tuple[str, str]] = (
        _CAL_COMMANDS
        + _LIQ_COMMANDS
        + _GOAL_COMMANDS
        + _SUM_COMMANDS
        + [("review", "AI-powered daily nutrition review")]
    )

    def register(self, app) -> None:
        _reg_goals(app)
        _reg_calories(app)
        _reg_liquids(app)
        _reg_summary(app)
        _reg_review(app)

    def register_scheduled(self, scheduler, bot) -> None:
        from bot.modules.calories.scheduled import register_all
        register_all(scheduler, bot)


module = CaloriesModule()
