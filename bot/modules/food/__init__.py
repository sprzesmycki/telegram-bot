"""Food module — optional, controlled by config.yaml modules.food.enabled.

Covers: meal logging, liquid logging, recipes, goals,
daily summary, weekly overview, reports, and AI-powered day reviews.
"""
from __future__ import annotations

from bot.config import get_config
from bot.modules.food.handlers.goals import COMMANDS as _GOAL_COMMANDS
from bot.modules.food.handlers.goals import register as _reg_goals
from bot.modules.food.handlers.meals import COMMANDS as _MEAL_COMMANDS
from bot.modules.food.handlers.meals import register as _reg_meals
from bot.modules.food.handlers.review import register as _reg_review
from bot.modules.food.handlers.summary import COMMANDS as _SUM_COMMANDS
from bot.modules.food.handlers.summary import register as _reg_summary


class FoodModule:
    @property
    def ENABLED(self) -> bool:
        return get_config().modules.food.enabled

    COMMANDS: list[tuple[str, str]] = (
        _MEAL_COMMANDS
        + _GOAL_COMMANDS
        + _SUM_COMMANDS
        + [("review", "AI-powered daily nutrition review")]
    )

    def register(self, app) -> None:
        _reg_goals(app)
        _reg_meals(app)
        _reg_summary(app)
        _reg_review(app)

    def register_scheduled(self, scheduler, bot) -> None:
        from bot.modules.food.scheduled import register_all
        register_all(scheduler, bot)


module = FoodModule()
