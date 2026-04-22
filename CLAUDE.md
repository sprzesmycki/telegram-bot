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

# Run tests (always do this after making changes)
uv run pytest

# Create a new migration
uv run alembic revision -m "describe change"

# One-shot import from a legacy SQLite file (Europe/Warsaw assumed for naive datetimes)
SQLITE_PATH=./data/caloriebot.db uv run python scripts/migrate_sqlite_to_pg.py
```

## Tests rule

After making any code change, run `uv run pytest` and confirm all tests pass before reporting the task as done. The test suite covers pure utility and parsing functions — no DB or Telegram connection required. If a change breaks a test, fix the test or the code (whichever is wrong) before finishing.

## Docs rule

When you add, remove, or rename a user-facing feature (a command, a flow, a scheduled job, or a new config key that a user must set), **always update `README.md` in the same change**. The commands table, the relevant sample-commands section, and any config mention must stay in sync with the code. If the feature introduces new architectural context (new DB tables, new LLM tier, new handler pipeline, new scheduler job), also update the matching "Architecture" section in this file. Docs drift is a blocker — treat it as part of the feature, not an afterthought.

## Environment

- `main.py` calls `load_dotenv` twice with `override=False`: first against **`~/.config/telegrambot/.env`** (host workflow), then against the process env / repo-root `.env` that docker compose injects via `env_file`. Host values win when both are present. Neither file is committed; `.env.example` documents every key.
- `.env` holds **secrets only** — API keys, DB credentials, bot token. All structured feature config lives in `config.yaml`.
- Minimum required keys: `TELEGRAM_BOT_TOKEN`, `OPENROUTER_API_KEY` (or the equivalent for `local` / `custom` providers), and a reachable `DATABASE_URL`.
- `DATABASE_URL` uses plain `postgresql://…` — Alembic's env.py and the asyncpg pool both rewrite it to `postgresql+asyncpg://` / strip the driver suffix as needed, so keep a single canonical URL in env.
- Inside docker compose the `app` / `migrate` services talk to Postgres on the compose network (`postgres:5432`); host workflows use the port-mapped `127.0.0.1:5432`. `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` feed both the postgres service and the `DATABASE_URL` the app reads.
- Runtime data lives under `./data/` (photos + rotating logs — gitignored). The DB is no longer on disk; it lives in the `postgres_data` docker volume or whatever Postgres you point `DATABASE_URL` at.

## Architecture

### Configuration: config.yaml + bot/config.py

`config.yaml` (committed) is the source of truth for all structured feature config: timezone, logging, LLM provider defaults, storage paths, module enable/disable flags, and schedule times. `bot/config.py` reads it and exposes a typed `AppConfig` singleton via `get_config()`.

`_env(key, yaml_fallback)` in `bot/config.py` applies `.env` overlays for legacy env vars — this lets existing deployments keep working without touching `config.yaml`. Don't add new env-var config; add it to `config.yaml` instead.

Key dataclass tree: `AppConfig → LLMConfig, StorageConfig, LoggingConfig, ModulesConfig → CaloriesModuleConfig, PianoModuleConfig, InvoicesModuleConfig, GmailModuleConfig`. All module-level code reads config via `get_config()` — never via `os.getenv()` for anything other than secrets.

### Module system (bot/modules/)

Features are organised as optional modules in `bot/modules/`. Each module directory exposes a `module` singleton (a `*Module` class instance) with:
- `ENABLED: bool` — read from `get_config().modules.<name>.enabled`
- `COMMANDS: list[tuple[str, str]]` — bot commands contributed by this module
- `register(app)` — attaches handlers to the `Application`
- `register_scheduled(scheduler, bot)` — registers cron jobs owned by this module

`bot/modules/__init__.py::load_enabled_modules()` always loads `CoreModule` (profiles, reminders, model) and conditionally loads `PianoModule`, `CaloriesModule`, `InvoicesModule`, and `GmailModule` (in that order) based on their `enabled` flags. `main.py` calls `load_enabled_modules()` and loops over the result — no explicit handler imports in `main.py`.

Module order matters for text-handler dispatch: piano is registered before calories so `piano_text_dispatch` gets first crack at plain-text messages (see Piano section).

**Adding a new module:** create `bot/modules/<name>/`, add a `*Module` class, add the config key to `config.yaml` + `ModulesConfig`, add it to `load_enabled_modules()`.

### Agent files and agent_runner

Every LLM system prompt lives in an `.md` file with YAML frontmatter:

```yaml
---
name: meal-analyzer
model: null          # null = follow active /model; "model-id" = override on current provider; "provider:model-id" = dedicated client
tools: []            # MCP-style tool names (empty = no tools)
---
<system prompt body>
```

Agent files per module:
- `bot/modules/calories/agents/` — `meal_analyzer.md`, `liquid_analyzer.md`, `recipe_analyzer.md`, `day_reviewer.md`
- `bot/modules/piano/agents/` — `practice_coach.md` (`model: google/gemini-flash-1.5`), `recording_analyzer.md` (`model: google/gemini-2.0-flash-001`)
- `bot/modules/invoices/agents/` — `invoice_reader.md` (`model: local:gemma4:26b`)

`bot/services/agent_runner.py` provides:
- `load_agent(path: str) -> AgentDefinition` — parses frontmatter + body; `@lru_cache` so each file is read once; paths resolved relative to project root
- `run_agent(agent, messages, *, response_format=None, temperature=0.3) -> str` — resolves LLM client from `agent.model`, prepends system prompt, calls API, returns content string

Model spec resolution in `_resolve_client(model_spec)`:
- `None` → `get_llm_client()` (follows active `/model`)
- `"model-id"` → `get_llm_client(model_override="model-id")` (current provider, different model)
- `"provider:model-id"` → build a dedicated `AsyncOpenAI` client for that provider

`llm.py`'s `analyze_meal/liquid/recipe` and `review_day` are thin wrappers that load the corresponding agent file and call `run_agent`. Handler code is unchanged.

### MCP-style tool registry (bot/tools/)

`bot/tools/__init__.py` holds a `_REGISTRY: dict[str, ToolDefinition]` of callable tools. Currently empty — no tools are registered for any existing agent. The registry is in place so agent files can list `tools: [tool-name]` and `run_agent` will automatically fetch schemas and pass them as `tools=` to the API when the registry is populated, without any runner changes.

### Entry point and handler registration

`main.py` calls `load_enabled_modules()`, loops over the returned module list, and calls `mod.register(app)` for each. The catch-all `MessageHandler(filters.COMMAND, unknown_cmd)` is registered **last** — order matters. Startup/shutdown work (DB init, LLM init, scheduler boot, loading reminders) happens in `post_init` / `post_shutdown` hooks. In `post_init`, `mod.register_scheduled(scheduler, bot)` is called per module after the scheduler starts.

### Database: PostgreSQL-only via asyncpg + Alembic

`bot/services/db.py` owns a single module-level `asyncpg.Pool`. `init_db()` builds it with `server_settings={"timezone": "Europe/Warsaw"}` so `CURRENT_DATE` and `col::date` always mean "Warsaw-local day" without per-query casts; Python-side, `_to_dt` coerces naive datetimes to `ZoneInfo("Europe/Warsaw")` before they hit TIMESTAMPTZ columns. There is no fallback or mirror — if the pool can't connect, the bot fails to start, which is intentional.

Range queries use half-open intervals (`col >= $1 AND col < $2`) rather than `BETWEEN`, because Postgres `BETWEEN` is inclusive on both ends (SQLite string-lex `BETWEEN` happened to work because of date-string ordering). All queries filter by `owner_user_id`; see the Owner isolation section.

Schema changes go through Alembic. `alembic/env.py` is async-configured (`async_engine_from_config` + `connection.run_sync(do_run_migrations)`) and reads `DATABASE_URL` directly, rewriting `postgresql://` → `postgresql+asyncpg://` so the same env var works for runtime asyncpg *and* Alembic. Migrations are **raw SQL via `op.execute()`** — no SQLAlchemy models in the repo. `alembic/versions/0001_initial_schema.py` creates every table + index; add a new revision per change. The compose `migrate` service runs `alembic upgrade head` on boot and `app` depends on `service_completed_successfully` so runtime always sees a current schema.

`scripts/migrate_sqlite_to_pg.py` is a one-shot import for legacy SQLite files. It `TRUNCATE ... RESTART IDENTITY CASCADE`s every table, executemany-inserts with preserved ids, then `setval(pg_get_serial_sequence(...), MAX(id), true)` to reset the SERIAL sequences. Naive SQLite datetimes are attached to `ZoneInfo("Europe/Warsaw")` before insertion. Not idempotent — re-running wipes the target.

### Owner isolation
Every query filters by `owner_user_id` (the Telegram user ID). Profiles are namespaced per owner — two users can both have a profile called "Me". Don't write queries that read `profiles`, `meals`, `supplements`, or `supplement_logs` without this filter.

### LLM provider singleton
`bot/services/llm.py` holds a module-level `(client, model, provider)` triple. `init_llm()` seeds it from `get_config().llm.*` (API keys still from `os.getenv`); `switch_provider()` swaps it in place; `get_llm_client()` returns the current pair. The `/model` command mutates this at runtime — no restart. All three providers (`openrouter`, `local`, `custom`) speak the OpenAI API via `AsyncOpenAI`, so only the `base_url` / `api_key` / `model` differ.

Two bespoke exceptions drive handler UX: `VisionNotSupportedError` (raised when an image is sent to a non-vision model — we map `openai.BadRequestError` to this) and `LLMParseError` (raised after one retry of JSON parsing fails). `_handle_llm_error` in `bot/modules/calories/handlers/calories.py` centralizes the user-facing messages.

### Deleting logged entries
`/today` (in `bot/modules/calories/handlers/calories.py`) lists meals + liquids for today per profile and attaches inline `❌ N` buttons whose `callback_data` encodes the row type and DB id (`delm:<meal_id>` / `dell:<liquid_id>`). The callback handler (`today_delete_callback`, registered via `CallbackQueryHandler(pattern=r"^del[ml]:")`) hard-deletes via `db.delete_meal` / `db.delete_liquid` and re-renders the message. Ownership is enforced by passing `owner_user_id` into the DELETE `WHERE` clause — never trust the callback payload alone. Delete is **hard** (no `active` column on `meals` / `liquids`); if we later need an audit trail, add the column via a new Alembic revision and switch the DELETE to `UPDATE ... SET active = 0`.

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
`APScheduler`'s `AsyncIOScheduler` is created in `post_init`, stored on `app.bot_data["scheduler"]`, and started there. Core supplement/reminder infrastructure lives in `bot/services/scheduler.py`. On boot, `load_all_reminders` reads every active supplement from Postgres and registers a `CronTrigger` per `HH:MM` (job ID format `supplement_<id>`). `/supplement add` and `/supplement remove` incrementally add/remove jobs on this same scheduler.

Scheduled cron jobs for features are registered by each module's `register_scheduled(scheduler, bot)` call in `post_init`:
- **Calories** (`bot/modules/calories/scheduled.py`): `daily_summary` (at `modules.calories.daily_summary_time`) and `daily_review` (at `modules.calories.daily_review_time`). Both iterate distinct profile owners and skip profiles with nothing logged.
- **Piano** (`bot/modules/piano/scheduled.py`): `piano_checkin` (at `modules.piano.checkin_time`). Iterates piano owners and skips days already logged.
- **Invoices** (`bot/modules/invoices/__init__.py`): one-shot `invoice_pending_cleanup` fires 5 s after startup (via `DateTrigger`) to delete stale `pending_invoices` rows and remove their tmp files.
- **Gmail** (`bot/modules/gmail/scheduled.py`): `gmail_check` polls for new unread mail every `modules.gmail.check_interval_minutes` minutes (IntervalTrigger). Compares against a module-level `_last_seen_count`; when count rises it fetches the new messages and broadcasts to all profile owners.

All "iterate distinct owners" paths share the `db.get_distinct_profile_owner_ids()` helper — don't write raw `SELECT DISTINCT owner_user_id` queries in the scheduler.

### Daily AI review (`/review`)
`/review [@name] [YYYY-MM-DD]` (in `bot/modules/calories/handlers/review.py`) gathers the day's meals, liquids, totals, goal, hydration, and (for today only) supplement compliance, then hands the payload to `llm.review_day` which loads `bot/modules/calories/agents/day_reviewer.md` and calls `run_agent`. The review is plain text with three fixed sections — `✅ Wins`, `⚠️ Concerns`, `➡️ Tomorrow` — each bullet in the form `<English> / <Polish>`. Unlike piano, there's no pinned model tier; the review uses whatever `/model` is active. `send_daily_review` is the scheduler entrypoint and returns silently for empty days. Supplement compliance uses `get_supplement_logs_today` (today-only helper), so for past-date reviews we pass empty supplement lists — if you add a per-date supplement-log query, wire it into `_gather_day_data` in `handlers/review.py`.

### Piano practice coach
Piano is **owner-scoped**, not profile-scoped (piano is personal; you don't share it between `Me` / `Wife`). Four tables: `piano_sessions`, `piano_pieces`, `piano_recordings`, `piano_streak`. `pieces_practiced` is stored as JSONB — the `_session_row_to_dict` helper in `db.py` decodes the value on read (asyncpg returns it as either a Python list or a JSON string depending on the driver path).

Piano services live in `bot/modules/piano/services/` (coach, audio_agent, repertoire, streaks). The piano handler is at `bot/modules/piano/handlers/piano.py`.

Two LLM tiers, configured in agent file frontmatter (not env vars):
- `bot/modules/piano/agents/practice_coach.md` — `model: google/gemini-flash-1.5` — used for `/piano checkin` and `/piano log` encouragement (cheap/fast)
- `bot/modules/piano/agents/recording_analyzer.md` — `model: google/gemini-2.0-flash-001` — used only for `/piano analyze`

Both are resolved via `agent_runner._resolve_client`, so `/model` state is not perturbed.

Voice/audio pipeline: `MessageHandler(filters.VOICE | filters.AUDIO, piano_voice_handler)` handles any audio message. If caption starts with `/piano analyze`, analysis runs inline; otherwise the `file_id`/`duration`/`kind`/`extension` is stashed in `context.user_data["pending_piano_audio"]` and the bot prompts "Is this a piano recording?". `audio_agent.analyze_recording` base64-encodes the raw audio and sends it as an OpenAI-style `input_audio` content block — there is **no** whisper/transcription step, because whisper transcribes speech and piano is notes. The model must accept audio input; `google/gemini-2.0-flash-001` accepts ogg/opus directly. No ffmpeg/pydub — raw `.ogg` bytes go straight to the provider.

Text-handler dispatch: `/piano log` with no args stashes `pending_piano_log=True` and prompts "reply like `30 min Chopin, scales`". The next plain-text message triggers `calories.refine_handler`, which calls `piano.piano_text_dispatch` **first** (behind a config guard — only if `get_config().modules.piano.enabled`); if that returns `True`, the calorie refine path is skipped. This is why piano is registered before calories in `load_enabled_modules()`.

Streak rules (`bot/modules/piano/services/streaks.py::compute_and_update_streak`): same-day re-log is idempotent; `delta_days == 1` increments; `delta_days > 1` resets to 1; `delta_days <= 0` (backdated) keeps current. Always updates `longest = max(longest, current)`.

### Invoices module
Invoices are **owner-scoped**. The module is in `bot/modules/invoices/`.

Flow: receive file (photo with `/invoice` caption, or any PDF document) → `analyze_invoice` (local `gemma4:26b` via `bot/modules/invoices/agents/invoice_reader.md`) → `pending_invoices` row + preview card → ✅ Save / ❌ Discard. On confirm, `find_duplicate_invoice` checks by `invoice_number` + original filename; if a duplicate exists the user gets a Replace / Skip choice before the row is written to `invoices`.

Commands:
- `/invoice` — usage help
- `/invoices [N]` — list last N saved invoices (default 10)
- `/invoices month [YYYY-MM]` — monthly summary: totals, by-category breakdown, recurring vs one-time split, top vendors, month-over-month delta
- `/invoices avg [N]` — N-month average effective cost (default 6), per-category breakdown, month-by-month table
- `/scan [dir]` — scan `storage.invoice_catalog_dir` (or an explicit path) for unprocessed PDF/image files, present each one for confirm/discard in sequence
- `/scan_stop` — abort an in-progress catalog scan

**Billing period**: `invoice_reader.md` extracts `billing_period_months` (1, 3, or 12). `summary.py::_effective()` divides the total by this to compute a per-month effective cost used by `/invoices month` and `/invoices avg`.

**Gmail integration**: the Gmail handler injects "Process as invoice" buttons for PDF attachments (`inv_email:<key>` callback) and for Apple email-body invoices (`inv_body:<email_id>` callback). If `modules.gmail.auto_process_invoices: true`, Apple invoices are processed automatically on `/emails` without a confirm dialog — a sequential queue drains one item at a time and sends a batch summary on completion.

### Gmail module
`bot/modules/gmail/` — optional (`modules.gmail.enabled`). Requires OAuth 2.0; run `scripts/gmail_auth.py` once to get `credentials.json`; set `GMAIL_CREDENTIALS_PATH` in `.env`.

Config keys (`GmailModuleConfig`): `enabled`, `check_interval_minutes`, `max_results`, `label` (Gmail label to filter), `auto_process_invoices` (boolean).

Commands: `/emails [N]` — fetch up to N unread messages (default `max_results`). Each email is displayed with a "Read more" button if the body is truncated. If the invoices module is enabled, PDF attachments get an "Invoice: <filename>" button and Apple body-invoices get a "Parse Apple invoice" button (or are auto-queued if `auto_process_invoices` is on).

Scheduled: `gmail_check` polls every `check_interval_minutes` using an `IntervalTrigger`. It compares the current unread count against `_last_seen_count`; if count grew it fetches the delta and sends each new email to all profile owners.

### Logging
`bot/utils/logging_config.setup_logging()` must be called **before** any project imports that create module-level loggers. It reads `get_config().logging` for level, file path, and debug flag. It wires a stdout `StreamHandler` and a `RotatingFileHandler` at `./data/logs/bot.log` (5 MB × 10 files). Setting `logging.debug: true` in `config.yaml` (or `DEBUG=1` in env) flips both the root level and unmutes the noisy library loggers listed in `_NOISY_LOGGERS`.
