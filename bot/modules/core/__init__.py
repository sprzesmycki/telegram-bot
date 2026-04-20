"""Core module — always enabled.

Registers the fundamental handlers: profiles, reminders, model switcher.
"""
from __future__ import annotations

from telegram.ext import Application

from bot.handlers.model import COMMANDS as _MODEL_COMMANDS
from bot.handlers.model import register as _reg_model
from bot.handlers.profiles import COMMANDS as _PROFILE_COMMANDS
from bot.handlers.profiles import register as _reg_profiles
from bot.handlers.reminders import COMMANDS as _REMINDER_COMMANDS
from bot.handlers.reminders import register as _reg_reminders


class CoreModule:
    ENABLED = True  # always on; not user-configurable

    COMMANDS: list[tuple[str, str]] = (
        _PROFILE_COMMANDS + _REMINDER_COMMANDS + _MODEL_COMMANDS
    )

    def register(self, app: Application) -> None:
        _reg_profiles(app)
        _reg_reminders(app)
        _reg_model(app)

    def register_scheduled(self, scheduler, bot) -> None:
        # Generic reminders are loaded by main.py via load_all_reminders()
        pass


module = CoreModule()
