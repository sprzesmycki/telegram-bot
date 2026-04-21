"""Subscriptions module — tracks recurring/one-time payments without invoice files.

Enable with:  modules.subscriptions.enabled: true  in config.yaml.
"""
from __future__ import annotations

from bot.config import get_config
from bot.modules.subscriptions.handlers.subscriptions import COMMANDS as _SUB_COMMANDS
from bot.modules.subscriptions.handlers.subscriptions import register as _reg_subscriptions


class SubscriptionsModule:
    @property
    def ENABLED(self) -> bool:
        return get_config().modules.subscriptions.enabled

    COMMANDS: list[tuple[str, str]] = _SUB_COMMANDS

    def register(self, app) -> None:
        _reg_subscriptions(app)

    def register_scheduled(self, scheduler, bot) -> None:
        pass


module = SubscriptionsModule()
