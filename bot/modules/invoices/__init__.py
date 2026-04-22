"""Invoices module — optional, controlled by config.yaml modules.invoices.enabled.

Uses local gemma4:26b (via Ollama) to read and classify invoice documents.
Enable with:  modules.invoices.enabled: true  in config.yaml.
"""
from __future__ import annotations

import logging
from pathlib import Path

from bot.config import get_config
from bot.modules.invoices.handlers.invoices import COMMANDS as _INV_COMMANDS
from bot.modules.invoices.handlers.invoices import register as _reg_invoices

logger = logging.getLogger(__name__)


async def _cleanup_stale_pending() -> None:
    from bot.services import db
    paths = await db.cleanup_stale_pending_invoices()
    for p in paths:
        Path(p).unlink(missing_ok=True)
    if paths:
        logger.info("Cleaned up %d stale pending invoice(s)", len(paths))


class InvoicesModule:
    @property
    def ENABLED(self) -> bool:
        return get_config().modules.invoices.enabled

    COMMANDS: list[tuple[str, str]] = _INV_COMMANDS

    def register(self, app) -> None:
        _reg_invoices(app)

    def register_scheduled(self, scheduler, bot) -> None:
        from datetime import datetime, timedelta

        from apscheduler.triggers.date import DateTrigger
        scheduler.add_job(
            _cleanup_stale_pending,
            trigger=DateTrigger(run_date=datetime.now() + timedelta(seconds=5)),
            id="invoice_pending_cleanup",
            replace_existing=True,
        )


module = InvoicesModule()
