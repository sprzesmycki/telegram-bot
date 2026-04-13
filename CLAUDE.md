# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Python 3.14+, managed with `uv`.

```bash
uv sync                     # install deps from uv.lock
uv run python main.py       # run the bot (polling mode)
```

There is no test suite, linter, or formatter configured — don't invent commands for them.

## Environment

- `.env` lives at **`~/.config/telegrambot/.env`** (loaded by `main.py` before any project imports). It is *not* read from the repo root. `.env.example` documents every key.
- Minimum required keys: `TELEGRAM_BOT_TOKEN`, `OPENROUTER_API_KEY` (or the equivalent for `local` / `custom` providers).
- `DATABASE_URL` is optional — if unset or unreachable, the PostgreSQL mirror silently disables itself (see below).
- Runtime data lives under `./data/` (SQLite DB, photos, rotating logs) — gitignored.

## Architecture

### Entry point and handler registration
`main.py` builds a `telegram.ext.Application`, then each handler module exposes `register(app)` that attaches its own `CommandHandler`/`MessageHandler`. The catch-all `MessageHandler(filters.COMMAND, unknown_cmd)` is registered **last** — order matters. Startup/shutdown work (DB init, LLM init, scheduler boot, loading reminders) happens in `post_init` / `post_shutdown` hooks, not in `main()`.

### Dual-database mirror pattern
SQLite (via `aiosqlite`) is the **source of truth**; PostgreSQL (via `asyncpg`) is a best-effort mirror. Every mutating operation writes SQLite first, then calls the matching `db_postgres.mirror_*` function. The mirror functions are *deliberately silent* on failure — catch broadly, log, and return. If `DATABASE_URL` is unset or the pool can't be built, `_pool` stays `None` and mirror calls become no-ops. When adding a new write operation, add the corresponding `mirror_*` and call it from the handler.

Schema lives in two places that must stay in sync: `migrations/init.sql` (SQLite) and `db_postgres._get_pg_schema()` (PostgreSQL, SERIAL/TIMESTAMPTZ variants). Additive column migrations are applied idempotently in `db_sqlite._apply_migrations` and an `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` block in `db_postgres.init_pg`.

### Owner isolation
Every query filters by `owner_user_id` (the Telegram user ID). Profiles are namespaced per owner — two users can both have a profile called "Me". Don't write queries that read `profiles`, `meals`, `supplements`, or `supplement_logs` without this filter.

### LLM provider singleton
`bot/services/llm.py` holds a module-level `(client, model, provider)` triple. `init_llm()` seeds it from env; `switch_provider()` swaps it in place; `get_llm_client()` returns the current pair. The `/model` command mutates this at runtime — no restart. All three providers (`openrouter`, `local`, `custom`) speak the OpenAI API via `AsyncOpenAI`, so only the `base_url` / `api_key` / `model` differ.

Two bespoke exceptions drive handler UX: `VisionNotSupportedError` (raised when an image is sent to a non-vision model — we map `openai.BadRequestError` to this) and `LLMParseError` (raised after one retry of JSON parsing fails). `_handle_llm_error` in `calories.py` centralizes the user-facing messages.

### Meal analysis: preview → refine → confirm
`/cal` and `/recipe` **never** log immediately. They stash a `pending_meal` dict in `context.user_data` and reply with a preview. Two things can happen next:
- `/yes` → `yes_cmd` reads the pending dict and writes to the DB(s).
- Any plain text → `refine_handler` (registered as `MessageHandler(filters.TEXT & ~filters.COMMAND, ...)`) appends the remark to the accumulated description and re-runs the LLM. This is why plain text in a chat has meaning only when a pending meal exists; otherwise it's a silent no-op.

LLM output is bilingual: `description_en` + `description_pl` (meals) or `dish_name_en` + `dish_name_pl` (recipes) are post-processed into a single `"<en> / <pl>"` string and stored as `description` / `dish_name`. Keep this split intact in prompts — the schema is load-bearing for both DB storage and message formatting.

### Photo pipeline
Photo messages are handled by `photo_handler` regardless of caption. `compress_image` (Pillow) resizes to ≤1920 px longest side, then JPEG-compresses until under 512 KB. The compressed bytes are base64'd for the vision API and also written to `./data/photos/` via `save_meal_photo` (filename sorts chronologically and includes owner ID + uuid suffix). `photo_path` flows into the `meals` row.

### Profile targeting syntax (`@name` / `@both`)
Every user-facing handler that operates on a profile routes through `get_target_profiles(owner_id, text)` in `bot/handlers/profiles.py`. It parses `@both` → all profiles, `@SomeName` → that one profile, otherwise the active profile (auto-creating "Me" on first use via `ensure_default_profile`). `parse_target` / `parse_time` / `parse_servings` / `strip_command_args` in `utils/formatting.py` are the canonical parsers — reuse them instead of writing ad-hoc regex in handlers. Profile names are **case-sensitive** (`@wife` does not match `Wife`).

The first `/profile add <Name>` (when `Name != "Me"`) auto-creates a `Me` profile alongside the user-supplied one — intentional behaviour, see `profiles.profile_cmd` under `sub == "add"`. Separately, `ensure_default_profile` creates `Me` lazily on any first command that targets an active profile.

### Scheduler
`APScheduler`'s `AsyncIOScheduler` is created in `post_init`, stored on `app.bot_data["scheduler"]`, and started there. On boot, `load_all_reminders` reads every active supplement from SQLite and registers a `CronTrigger` per `HH:MM` (job ID format `supplement_<id>`). `/supplement add` and `/supplement remove` incrementally add/remove jobs on this same scheduler. `register_daily_summary` adds one cron job at `DAILY_SUMMARY_TIME` that iterates all owners and sends `format_summary` per profile.

### Logging
`bot/utils/logging_config.setup_logging()` must be called **before** any project imports that create module-level loggers. It wires a stdout `StreamHandler` and a `RotatingFileHandler` at `./data/logs/bot.log` (5 MB × 10 files). `DEBUG=1` (or any truthy value) flips both the root level and unmutes the noisy library loggers listed in `_NOISY_LOGGERS`.
