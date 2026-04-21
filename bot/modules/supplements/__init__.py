"""Supplements module — standalone tracking and reminders.
"""
from __future__ import annotations

from bot.config import get_config
from bot.modules.supplements.handlers.supplements import COMMANDS, register as _reg_supplements


class SupplementsModule:
    @property
    def ENABLED(self) -> bool:
        return get_config().modules.supplements.enabled

    COMMANDS: list[tuple[str, str]] = COMMANDS

    def register(self, app) -> None:
        _reg_supplements(app)

    def register_scheduled(self, scheduler, bot) -> None:
        # Reminders are registered globally in bot.services.scheduler.load_all_reminders
        pass


module = SupplementsModule()
