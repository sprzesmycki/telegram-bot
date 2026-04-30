"""Optional feature module loader.

Each module exposes a singleton ``module`` object with:
- ``ENABLED: bool`` property
- ``register(app: Application) -> None`` — attach handlers
- ``register_scheduled(scheduler, bot) -> None`` — attach cron jobs
- ``COMMANDS: list[tuple[str, str]]`` — flat list of (name, description)

Registration order matters: piano must be loaded before food because
``food.refine_handler`` conditionally calls ``piano.piano_text_dispatch``.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def load_enabled_modules():
    """Return a list of enabled module singletons in registration order."""
    from bot.config import get_config
    cfg = get_config()

    from bot.modules.core import module as core_module
    modules = [core_module]

    # Piano before food (text-dispatch dependency)
    if cfg.modules.piano.enabled:
        from bot.modules.piano import module as piano_module
        modules.append(piano_module)
        logger.info("Module loaded: piano")
    else:
        logger.info("Module disabled: piano")

    if cfg.modules.food.enabled:
        from bot.modules.food import module as food_module
        modules.append(food_module)
        logger.info("Module loaded: food")
    else:
        logger.info("Module disabled: food")

    if cfg.modules.supplements.enabled:
        from bot.modules.supplements import module as supplements_module
        modules.append(supplements_module)
        logger.info("Module loaded: supplements")
    else:
        logger.info("Module disabled: supplements")

    if cfg.modules.subscriptions.enabled:
        from bot.modules.subscriptions import module as subscriptions_module
        modules.append(subscriptions_module)
        logger.info("Module loaded: subscriptions")
    else:
        logger.info("Module disabled: subscriptions")

    if cfg.modules.invoices.enabled:
        from bot.modules.invoices import module as invoices_module
        modules.append(invoices_module)
        logger.info("Module loaded: invoices")
    else:
        logger.info("Module disabled: invoices")

    if cfg.modules.gmail.enabled:
        from bot.modules.gmail import module as gmail_module
        modules.append(gmail_module)
        logger.info("Module loaded: gmail")
    else:
        logger.info("Module disabled: gmail")

    return modules
