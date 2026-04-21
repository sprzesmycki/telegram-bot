"""Unified configuration loader.

Reads config.yaml for structured feature config and overlays .env values for
secrets and per-environment overrides. Both sources are optional — reasonable
defaults are baked in.

Usage::

    from bot.config import get_config
    cfg = get_config()
    cfg.modules.calories.enabled   # True/False
    cfg.llm.provider               # "openrouter" | "local" | "custom"
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------


@dataclass
class LLMProviderConfig:
    base_url: str
    model: str


@dataclass
class LLMConfig:
    provider: str
    openrouter: LLMProviderConfig
    local: LLMProviderConfig
    custom: LLMProviderConfig
    compare_models: list[str]


@dataclass
class StorageConfig:
    photos_dir: str
    piano_recordings_dir: str
    invoices_dir: str
    invoice_catalog_dir: str
    gmail_attachments_dir: str


@dataclass
class LoggingConfig:
    level: str
    file: str
    debug: bool
    rotation: str = "daily"  # "daily" | "hourly"
    keep_days: int = 30


@dataclass
class CaloriesModuleConfig:
    enabled: bool
    daily_summary_time: str
    daily_review_time: str


@dataclass
class SupplementsModuleConfig:
    enabled: bool


@dataclass
class PianoModuleConfig:
    enabled: bool
    checkin_time: str


@dataclass
class InvoicesModuleConfig:
    enabled: bool


@dataclass
class SubscriptionsModuleConfig:
    enabled: bool


@dataclass
class GmailModuleConfig:
    enabled: bool
    check_interval_minutes: int
    max_results: int
    label: str
    auto_process_invoices: bool


@dataclass
class ModulesConfig:
    calories: CaloriesModuleConfig
    supplements: SupplementsModuleConfig
    piano: PianoModuleConfig
    invoices: InvoicesModuleConfig
    subscriptions: SubscriptionsModuleConfig
    gmail: GmailModuleConfig


@dataclass
class AppConfig:
    timezone: str
    logging: LoggingConfig
    llm: LLMConfig
    storage: StorageConfig
    modules: ModulesConfig


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _env(key: str, fallback: str = "") -> str:
    """Return the env var value if non-empty, else *fallback*."""
    v = os.getenv(key, "").strip()
    return v if v else fallback


def load_config(path: Path | None = None) -> AppConfig:
    """Load config.yaml and overlay .env overrides on top."""
    p = path or Path("config.yaml")
    raw: dict[str, Any] = {}
    if p.exists():
        with p.open() as f:
            raw = yaml.safe_load(f) or {}

    bot_sec = raw.get("bot", {})
    log_sec = raw.get("logging", {})
    llm_sec = raw.get("llm", {})
    stor_sec = raw.get("storage", {})
    mod_sec = raw.get("modules", {})
    cal_sec = mod_sec.get("calories", {})
    pia_sec = mod_sec.get("piano", {})
    inv_sec = mod_sec.get("invoices", {})
    sub_sec = mod_sec.get("subscriptions", {})
    gml_sec = mod_sec.get("gmail", {})
    sup_sec = mod_sec.get("supplements", {})

    debug = (
        os.getenv("DEBUG", "").strip().lower() in ("1", "true", "yes", "on", "debug")
        or bool(log_sec.get("debug", False))
    )

    # COMPARE_MODELS: env var wins (comma-separated); fall back to yaml list
    compare_raw = _env("COMPARE_MODELS", "")
    if compare_raw:
        compare_models = [m.strip() for m in compare_raw.split(",") if m.strip()]
    else:
        compare_models = llm_sec.get("compare_models", [])

    return AppConfig(
        timezone=_env("TZ", bot_sec.get("timezone", "Europe/Warsaw")),
        logging=LoggingConfig(
            level=_env("LOG_LEVEL", "DEBUG" if debug else log_sec.get("level", "INFO")).upper(),
            file=_env("LOG_FILE", log_sec.get("file", "./data/logs/bot.log")),
            debug=debug,
            rotation=log_sec.get("rotation", "daily"),
            keep_days=int(log_sec.get("keep_days", 30)),
        ),
        llm=LLMConfig(
            provider=_env("LLM_PROVIDER", llm_sec.get("provider", "openrouter")),
            openrouter=LLMProviderConfig(
                base_url=_env(
                    "OPENROUTER_BASE_URL",
                    llm_sec.get("openrouter", {}).get("base_url", "https://openrouter.ai/api/v1"),
                ),
                model=_env(
                    "OPENROUTER_MODEL",
                    llm_sec.get("openrouter", {}).get("model", "anthropic/claude-sonnet-4.5"),
                ),
            ),
            local=LLMProviderConfig(
                base_url=_env(
                    "LOCAL_BASE_URL",
                    llm_sec.get("local", {}).get("base_url", "http://localhost:11434/v1"),
                ),
                model=_env(
                    "LOCAL_MODEL",
                    llm_sec.get("local", {}).get("model", "gemma4:26b"),
                ),
            ),
            custom=LLMProviderConfig(
                base_url=_env("CUSTOM_BASE_URL", llm_sec.get("custom", {}).get("base_url", "")),
                model=_env("CUSTOM_MODEL", llm_sec.get("custom", {}).get("model", "")),
            ),
            compare_models=compare_models,
        ),
        storage=StorageConfig(
            photos_dir=_env("PHOTOS_DIR", stor_sec.get("photos_dir", "./data/photos")),
            piano_recordings_dir=_env(
                "PIANO_RECORDINGS_DIR",
                stor_sec.get("piano_recordings_dir", "./data/piano_recordings"),
            ),
            invoices_dir=stor_sec.get("invoices_dir", "./data/invoices"),
            invoice_catalog_dir=stor_sec.get("invoice_catalog_dir", "./data/invoice_catalog"),
            gmail_attachments_dir=stor_sec.get(
                "gmail_attachments_dir", "./data/gmail_attachments"
            ),
        ),
        modules=ModulesConfig(
            calories=CaloriesModuleConfig(
                enabled=cal_sec.get("enabled", True),
                daily_summary_time=cal_sec.get("schedules", {}).get("daily_summary_time", "21:00"),
                daily_review_time=cal_sec.get("schedules", {}).get("daily_review_time", "22:00"),
            ),
            supplements=SupplementsModuleConfig(
                enabled=sup_sec.get("enabled", True),
            ),
            piano=PianoModuleConfig(
                enabled=pia_sec.get("enabled", True),
                checkin_time=pia_sec.get("schedules", {}).get("checkin_time", "19:00"),
            ),
            invoices=InvoicesModuleConfig(
                enabled=inv_sec.get("enabled", False),
            ),
            subscriptions=SubscriptionsModuleConfig(
                enabled=sub_sec.get("enabled", False),
            ),
            gmail=GmailModuleConfig(
                enabled=gml_sec.get("enabled", False),
                check_interval_minutes=gml_sec.get("check_interval_minutes", 5),
                max_results=gml_sec.get("max_results", 10),
                label=gml_sec.get("label", "INBOX"),
                auto_process_invoices=gml_sec.get("auto_process_invoices", False),
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_config: AppConfig | None = None


def get_config() -> AppConfig:
    """Return the singleton AppConfig, loading it on first call."""
    global _config
    if _config is None:
        _config = load_config()
    return _config
