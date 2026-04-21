"""Gmail module — optional, controlled by config.yaml modules.gmail.enabled.

Fetches unread Gmail messages on demand (/emails) and sends auto-notifications
when new mail arrives. Requires OAuth 2.0 setup (see scripts/gmail_auth.py).
Enable with:  modules.gmail.enabled: true  in config.yaml.
"""
from __future__ import annotations

from bot.config import get_config
from bot.modules.gmail.handlers.emails import COMMANDS as _GMAIL_COMMANDS
from bot.modules.gmail.handlers.emails import register as _reg_emails


class GmailModule:
    @property
    def ENABLED(self) -> bool:
        return get_config().modules.gmail.enabled

    COMMANDS: list[tuple[str, str]] = _GMAIL_COMMANDS

    def register(self, app) -> None:
        _reg_emails(app)

    def register_scheduled(self, scheduler, bot) -> None:
        from bot.modules.gmail.scheduled import register_all
        register_all(scheduler, bot)


module = GmailModule()
