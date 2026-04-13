"""File storage helpers for meal photos."""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def get_photos_dir() -> Path:
    """Return the directory where meal photos are stored, creating it if needed."""
    path = Path(os.getenv("PHOTOS_DIR", "./data/photos"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_meal_photo(photo_bytes: bytes, owner_id: int) -> str:
    """Save a meal photo and return its on-disk path as a string.

    Filename format: ``YYYYMMDD_HHMMSS_{owner_id}_{uuid8}.jpg``
    -- sorts chronologically, identifies owner, avoids collisions.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:8]
    filename = f"{ts}_{owner_id}_{suffix}.jpg"
    path = get_photos_dir() / filename
    path.write_bytes(photo_bytes)
    logger.debug("Saved meal photo to %s (%d bytes)", path, len(photo_bytes))
    return str(path)


def get_piano_recordings_dir() -> Path:
    """Return the directory where piano recordings are stored, creating it if needed."""
    path = Path(os.getenv("PIANO_RECORDINGS_DIR", "./data/piano_recordings"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_piano_recording(
    audio_bytes: bytes, owner_id: int, extension: str = "ogg"
) -> str:
    """Save a piano recording and return its on-disk path as a string.

    Filename format: ``YYYYMMDD_HHMMSS_{owner_id}_{uuid8}.{ext}``.
    Extension is determined by caller based on the Telegram audio kind
    (voice messages are opus-in-ogg; audio files may be mp3/m4a/etc.).
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:8]
    ext = extension.lstrip(".") or "bin"
    filename = f"{ts}_{owner_id}_{suffix}.{ext}"
    path = get_piano_recordings_dir() / filename
    path.write_bytes(audio_bytes)
    logger.debug("Saved piano recording to %s (%d bytes)", path, len(audio_bytes))
    return str(path)
