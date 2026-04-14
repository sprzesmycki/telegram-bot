# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Python 3.14+, managed with `uv`. PostgreSQL is required. The canonical dev loop is docker compose; host-only runs are fine too.

```bash
# Docker (preferred) — spins up postgres, runs Alembic once, then starts the bot.
docker compose up --build

# Host run — you bring your own Postgres (compose exposes 127.0.0.1:5432).
uv sync
uv run alembic upgrade head         # apply schema
uv run python main.py               # run the bot (polling mode)

# Create a new migration
uv run alembic revision -m "describe change"

# One-shot import from a legacy SQLite file (Europe/Warsaw assumed for naive datetimes)
SQLITE_PATH=./data/caloriebot.db uv run python scripts/migrate_sqlite_to_pg.py
```

There is no test suite, linter, or formatter configured — don't invent commands for them.

## Docs rule

When you add, remove, or rename a user-facing feature (a command, a flow, a scheduled job, or a new env var that a user must set), **always update `README.md` in the same change**. The commands table, the relevant sample-commands section, and any env-var mention must stay in sync with the code. If the feature introduces new architectural context (new DB tables, new LLM tier, new handler pipeline, new scheduler job), also update the matching "Architecture" section in this file. Docs drift is a blocker — treat it as part of the feature, not an afterthought.

## Environment

- `main.py` calls `load_dotenv` twice with `override=False`: first against **`~/.config/telegrambot/.env`** (host workflow), then against the process env / repo-root `.env` that docker compose injects via `env_file`. Host values win when both are present. Neither file is committed; `.env.example` documents every key.
- Minimum required keys: `TELEGRAM_BOT_TOKEN`, `OPENROUTER_API_KEY` (or the equivalent for `local` / `custom` providers), and a reachable `DATABASE_URL`.
- `DATABASE_URL` uses plain `postgresql://…` — Alembic's env.py and the asyncpg pool both rewrite it to `postgresql+asyncpg://` / strip the driver suffix as needed, so keep a single canonical URL in env.
- Inside docker compose the `app` / `migrate` services talk to Postgres on the compose network (`postgres:5432`); host workflows use the port-mapped `127.0.0.1:5432`. `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` feed both the postgres service and the `DATABASE_URL` the app reads.
- Runtime data lives under `./data/` (photos + rotating logs — gitignored). The DB is no longer on disk; it lives in the `postgres_data` docker volume or whatever Postgres you point `DATABASE_URL` at.

## Architecture

### Entry point and handler registration
`main.py` builds a `telegram.ext.Application`, then each handler module exposes `register(app)` that attaches its own `CommandHandler`/`MessageHandler`. The catch-all `MessageHandler(filters.COMMAND, unknown_cmd)` is registered **last** — order matters. Startup/shutdown work (DB init, LLM init, scheduler boot, loading reminders) happens in `post_init` / `post_shutdown` hooks, not in `main()`.

### Database: PostgreSQL-only via asyncpg + Alembic
`bot/services/db.py` owns a single module-level `asyncpg.Pool`. `init_db()` builds it with `server_settings={"timezone": "Europe/Warsaw"}` so `CURRENT_DATE` and `col::date` always mean "Warsaw-local day" without per-query casts; Python-side, `_to_dt` coerces naive datetimes to `ZoneInfo("Europe/Warsaw")` before they hit TIMESTAMPTZ columns. There is no fallback or mirror — if the pool can't connect, the bot fails to start, which is intentional.

Range queries use half-open intervals (`col >= $1 AND col < $2`) rather than `BETWEEN`, because Postgres `BETWEEN` is inclusive on both ends (SQLite string-lex `BETWEEN` happened to work because of date-string ordering). All queries filter by `owner_user_id`; see the Owner isolation section.

Schema changes go through Alembic. `alembic/env.py` is async-configured (`async_engine_from_config` + `connection.run_sync(do_run_migrations)`) and reads `DATABASE_URL` directly, rewriting `postgresql://` → `postgresql+asyncpg://` so the same env var works for runtime asyncpg *and* Alembic. Migrations are **raw SQL via `op.execute()`** — no SQLAlchemy models in the repo. `alembic/versions/0001_initial_schema.py` creates every table + index; add a new revision per change. The compose `migrate` service runs `alembic upgrade head` on boot and `app` depends on `service_completed_successfully` so runtime always sees a current schema.

`scripts/migrate_sqlite_to_pg.py` is a one-shot import for legacy SQLite files. It `TRUNCATE ... RESTART IDENTITY CASCADE`s every table, executemany-inserts with preserved ids, then `setval(pg_get_serial_sequence(...), MAX(id), true)` to reset the SERIAL sequences. Naive SQLite datetimes are attached to `ZoneInfo("Europe/Warsaw")` before insertion. Not idempotent — re-running wipes the target.

### Owner isolation
Every query filters by `owner_user_id` (the Telegram user ID). Profiles are namespaced per owner — two users can both have a profile called "Me". Don't write queries that read `profiles`, `meals`, `supplements`, or `supplement_logs` without this filter.

### LLM provider singleton
`bot/services/llm.py` holds a module-level `(client, model, provider)` triple. `init_llm()` seeds it from env; `switch_provider()` swaps it in place; `get_llm_client()` returns the current pair. The `/model` command mutates this at runtime — no restart. All three providers (`openrouter`, `local`, `custom`) speak the OpenAI API via `AsyncOpenAI`, so only the `base_url` / `api_key` / `model` differ.

Two bespoke exceptions drive handler UX: `VisionNotSupportedError` (raised when an image is sent to a non-vision model — we map `openai.BadRequestError` to this) and `LLMParseError` (raised after one retry of JSON parsing fails). `_handle_llm_error` in `calories.py` centralizes the user-facing messages.

### Deleting logged entries
`/today` (in `calories.py`) lists meals + liquids for today per profile and attaches inline `❌ N` buttons whose `callback_data` encodes the row type and DB id (`delm:<meal_id>` / `dell:<liquid_id>`). The callback handler (`today_delete_callback`, registered via `CallbackQueryHandler(pattern=r"^del[ml]:")`) hard-deletes via `db.delete_meal` / `db.delete_liquid` and re-renders the message. Ownership is enforced by passing `owner_user_id` into the DELETE `WHERE` clause — never trust the callback payload alone. Delete is **hard** (no `active` column on `meals` / `liquids`); if we later need an audit trail, add the column via a new Alembic revision and switch the DELETE to `UPDATE ... SET active = 0`.

### Meal analysis: preview → refine → confirm
`/cal` and `/recipe` **never** log immediately. They stash a `pending_meal` dict in `context.user_data` and reply with a preview. Two things can happen next:
- `/yes` → `yes_cmd` reads the pending dict and writes to Postgres via `db.log_meal` / `db.log_liquid`.
- Any plain text → `refine_handler` (registered as `MessageHandler(filters.TEXT & ~filters.COMMAND, ...)`) appends the remark to the accumulated description and re-runs the LLM. This is why plain text in a chat has meaning only when a pending meal exists; otherwise it's a silent no-op.

LLM output is bilingual: `description_en` + `description_pl` (meals) or `dish_name_en` + `dish_name_pl` (recipes) are post-processed into a single `"<en> / <pl>"` string and stored as `description` / `dish_name`. Keep this split intact in prompts — the schema is load-bearing for both DB storage and message formatting.

### Photo pipeline
Photo messages are handled by `photo_handler` regardless of caption. `compress_image` (Pillow) resizes to ≤1920 px longest side, then JPEG-compresses until under 512 KB. The compressed bytes are base64'd for the vision API and also written to `./data/photos/` via `save_meal_photo` (filename sorts chronologically and includes owner ID + uuid suffix). `photo_path` flows into the `meals` row.

### Profile targeting syntax (`@name` / `@both`)
Every user-facing handler that operates on a profile routes through `get_target_profiles(owner_id, text)` in `bot/handlers/profiles.py`. It parses `@both` → all profiles, `@SomeName` → that one profile, otherwise the active profile (auto-creating "Me" on first use via `ensure_default_profile`). `parse_target` / `parse_time` / `parse_servings` / `strip_command_args` in `utils/formatting.py` are the canonical parsers — reuse them instead of writing ad-hoc regex in handlers. Profile names are **case-sensitive** (`@wife` does not match `Wife`).

The first `/profile add <Name>` (when `Name != "Me"`) auto-creates a `Me` profile alongside the user-supplied one — intentional behaviour, see `profiles.profile_cmd` under `sub == "add"`. Separately, `ensure_default_profile` creates `Me` lazily on any first command that targets an active profile.

### Scheduler
`APScheduler`'s `AsyncIOScheduler` is created in `post_init`, stored on `app.bot_data["scheduler"]`, and started there. On boot, `load_all_reminders` reads every active supplement from Postgres and registers a `CronTrigger` per `HH:MM` (job ID format `supplement_<id>`). `/supplement add` and `/supplement remove` incrementally add/remove jobs on this same scheduler. `register_daily_summary` adds one cron job at `DAILY_SUMMARY_TIME` that iterates all owners and sends `format_summary` per profile. `register_daily_review` adds job `daily_review` at `DAILY_REVIEW_TIME` (default 22:00) that iterates profile owners and calls `review.send_daily_review` per profile; it silently skips profiles with no meals/drinks logged today so we don't nag users who were off. `register_piano_checkin` adds a cron job `piano_checkin` at `PIANO_CHECKIN_TIME` (default 19:00) that iterates owners from `db.get_piano_owners()` (plus `db.get_distinct_profile_owner_ids()`) and skips anyone with a session already logged today. All three "iterate distinct owners" paths share the `db.get_distinct_profile_owner_ids()` helper — don't write raw `SELECT DISTINCT owner_user_id` queries in the scheduler.

### Daily AI review (`/review`)
`/review [@name] [YYYY-MM-DD]` (in `handlers/review.py`) gathers the day's meals, liquids, totals, goal, hydration, and (for today only) supplement compliance, then hands the payload to `llm.review_day` which calls the *currently-selected* `/model` provider with a bilingual coach prompt. The review is plain text with three fixed sections — `✅ Wins`, `⚠️ Concerns`, `➡️ Tomorrow` — each bullet in the form `<English> / <Polish>`. Unlike piano, there's no pinned model tier; the review uses whatever `/model` is active. `send_daily_review` is the scheduler entrypoint and returns silently for empty days. Supplement compliance uses `get_supplement_logs_today` (today-only helper), so for past-date reviews we pass empty supplement lists — if you add a per-date supplement-log query, wire it into `_gather_day_data` in `handlers/review.py`.

### Piano practice coach
Piano is **owner-scoped**, not profile-scoped (piano is personal; you don't share it between `Me` / `Wife`). Four tables: `piano_sessions`, `piano_pieces`, `piano_recordings`, `piano_streak`. `pieces_practiced` is stored as JSONB — the `_session_row_to_dict` helper in `db.py` decodes the value on read (asyncpg returns it as either a Python list or a JSON string depending on the driver path).

Two LLM tiers via `get_llm_client(model_override=...)`: `PIANO_CHAT_MODEL` for `/piano checkin` and `/piano log` encouragement (cheap/fast), `PIANO_ANALYSIS_MODEL` only for `/piano analyze`. The override means `/model` state is not perturbed by piano calls.

Voice/audio pipeline: `MessageHandler(filters.VOICE | filters.AUDIO, piano_voice_handler)` handles any audio message. If caption starts with `/piano analyze`, analysis runs inline; otherwise the `file_id`/`duration`/`kind`/`extension` is stashed in `context.user_data["pending_piano_audio"]` and the bot prompts "Is this a piano recording?". `audio_agent.analyze_recording` base64-encodes the raw audio and sends it as an OpenAI-style `input_audio` content block to `PIANO_ANALYSIS_MODEL` — there is **no** whisper/transcription step, because whisper transcribes speech and piano is notes. The model must accept audio input (e.g. `google/gemini-2.0-flash-001`, which accepts ogg/opus directly; `openai/gpt-4o-audio-preview` accepts only wav/mp3 so isn't a drop-in default for Telegram voice messages). No ffmpeg/pydub — raw `.ogg` bytes go straight to the provider.

Text-handler dispatch: `/piano log` with no args stashes `pending_piano_log=True` and prompts "reply like `30 min Chopin, scales`". The next plain-text message triggers `calories.refine_handler`, which calls `piano.piano_text_dispatch` **first**; if that returns `True`, the calorie refine path is skipped. This is why `piano.register(app)` runs before `calories.register(app)` in `main.py` — not strictly required (voice and text filters don't collide), but keeps the mental model consistent.

Streak rules (`bot/services/piano/streaks.py::compute_and_update_streak`): same-day re-log is idempotent; `delta_days == 1` increments; `delta_days > 1` resets to 1; `delta_days <= 0` (backdated) keeps current. Always updates `longest = max(longest, current)`.

### Logging
`bot/utils/logging_config.setup_logging()` must be called **before** any project imports that create module-level loggers. It wires a stdout `StreamHandler` and a `RotatingFileHandler` at `./data/logs/bot.log` (5 MB × 10 files). `DEBUG=1` (or any truthy value) flips both the root level and unmutes the noisy library loggers listed in `_NOISY_LOGGERS`.
