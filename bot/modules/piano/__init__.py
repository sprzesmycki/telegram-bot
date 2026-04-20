"""Piano practice coach module — optional, controlled by config.yaml modules.piano.enabled."""
from __future__ import annotations

from bot.config import get_config
from bot.modules.piano.handlers.piano import COMMANDS as _PIANO_COMMANDS
from bot.modules.piano.handlers.piano import register as _reg_piano


class PianoModule:
    @property
    def ENABLED(self) -> bool:
        return get_config().modules.piano.enabled

    COMMANDS: list[tuple[str, str]] = _PIANO_COMMANDS

    def register(self, app) -> None:
        _reg_piano(app)

    def register_scheduled(self, scheduler, bot) -> None:
        from bot.modules.piano.scheduled import register_all
        register_all(scheduler, bot)


module = PianoModule()
