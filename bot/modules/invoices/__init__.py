"""Invoices module — optional, controlled by config.yaml modules.invoices.enabled.

Uses local gemma4:26b (via Ollama) to read and classify invoice documents.
Enable with:  modules.invoices.enabled: true  in config.yaml.
"""
from __future__ import annotations

from bot.config import get_config
from bot.modules.invoices.handlers.invoices import COMMANDS as _INV_COMMANDS
from bot.modules.invoices.handlers.invoices import register as _reg_invoices


class InvoicesModule:
    @property
    def ENABLED(self) -> bool:
        return get_config().modules.invoices.enabled

    COMMANDS: list[tuple[str, str]] = _INV_COMMANDS

    def register(self, app) -> None:
        _reg_invoices(app)

    def register_scheduled(self, scheduler, bot) -> None:
        pass  # no scheduled jobs yet


module = InvoicesModule()
