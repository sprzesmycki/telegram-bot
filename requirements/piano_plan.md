# Piano Practice Coach — Implementation Plan

## Context

`requirements/piano_prompt.md` adds a piano-practice coaching module to the existing calorie-tracker bot. It provides daily habit tracking (streaks, session logging, repertoire management) plus an on-demand recording-analysis sub-agent that accepts voice/audio messages. The module reuses every existing pattern (async, aiosqlite + Postgres mirror, APScheduler, OpenAI-compatible LLM factory) — no new architecture. Two model tiers are introduced so the chatty coaching dialog can run on a cheap/fast model while only the rare analysis call pays for a capable model.

Why it's worth building on top of this codebase: the patterns (handler `register(app)`, pending-state in `user_data`, scheduler job IDs like `entity_<id>`, silent Postgres mirror) are already doing what the piano flows need. The only cross-cutting change is a small extension to `get_llm_client()` to accept a `model_override` so the global `/model` state is not perturbed by piano calls.

---

## Architecture overview

- **Two LLM tiers via `model_override`**: `PIANO_CHAT_MODEL` for coach dialog / check-ins / log replies; `PIANO_ANALYSIS_MODEL` only when running the recording analyzer. Both routed through the existing provider singleton — no new client.
- **Four new tables** (owner-scoped, no `profile_id` — piano is personal, not shared between profiles like meals are).
- **One new scheduler job** (`piano_checkin`, cron from `PIANO_CHECKIN_TIME`). Skip send if today's session already logged.
- **One new voice/audio pipeline**: `MessageHandler(filters.VOICE | filters.AUDIO)` stashes audio in `context.user_data["pending_piano_audio"]`; `/piano analyze` consumes it. Transcription tries `LOCAL_WHISPER_URL` first, falls back to OpenAI's `/v1/audio/transcriptions` endpoint with `OPENAI_API_KEY` if set, else silently skips to text-only analysis.
- **Single-prompt log refinement**: `/piano log` with no args prompts "reply with `30 min Chopin, scales`" and stashes `pending_piano_log`; the next plain-text message is parsed. This piggybacks on the existing text-handler (see "Text-handler dispatch" below).

---

## Files to create

### `bot/handlers/piano.py`
All `/piano` commands + voice handler. Exposes `register(app)`.

Subcommand router on `args[0].lower()` (same pattern as `supplements.py`):
- no args → `_piano_summary` (streak, in-progress pieces, last session, quick tips)
- `log` → `_piano_log` (direct `N min Pieces…` parse or stash `pending_piano_log`)
- `checkin` → `_piano_checkin` (single LLM call with DB context)
- `pieces` → `_piano_pieces_list`
- `piece add|status|note|remove` → `_piano_piece_*`
- `analyze` → `_piano_analyze` (consumes `pending_piano_audio`)
- `history [N]` → `_piano_history` (default 7)
- `stats` → `_piano_stats`

Voice handler `piano_voice_handler`:
- triggered on `filters.VOICE | filters.AUDIO`
- if caption is `/piano analyze` → run analysis inline
- else stash `{file_id, duration, kind}` in `context.user_data["pending_piano_audio"]` and reply: `"🎹 Is this a piano recording? Reply /piano analyze to get feedback."`

Text-handler dispatch for `pending_piano_log`: implemented by piano exposing `piano_text_dispatch(update, context) -> bool` that `refine_handler` in `calories.py` calls first. If it returns `True`, calorie refine exits early. Keeps both features in their own modules without needing PTB handler groups.

### `bot/services/piano/__init__.py`
Empty package marker.

### `bot/services/piano/coach.py`
- `build_coach_context(owner_id) -> dict` — loads streak, in-progress pieces, last 3 sessions summary for system prompt injection
- `async run_checkin(owner_id) -> str` — single stateless call using `PIANO_CHAT_MODEL` via `get_llm_client(model_override=...)`. Returns formatted text.
- `async summarize_log(owner_id, session) -> str` — post-log encouragement text
- `format_streak(streak: int) -> str` → `"🔥 Day N in a row!"`

### `bot/services/piano/repertoire.py`
- `status_emoji(status) -> str` (📖/🔧/✅/🔄)
- `parse_piece_title(text) -> tuple[str, str|None]` — splits `"<title> by <composer>"`
- `format_pieces_list(pieces: list[dict]) -> str`
- `async find_piece_by_title(owner_id, title) -> dict | None` — case-insensitive LIKE

### `bot/services/piano/audio_agent.py`
- `async transcribe(audio_bytes: bytes, filename: str = "audio.ogg") -> str | None` — tiered:
  1. If `LOCAL_WHISPER_URL` set → POST multipart to that endpoint. On success return text; on failure fall through.
  2. Else/then if `OPENAI_API_KEY` set → build an `AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))` (direct, not via `get_llm_client`, because whisper lives at api.openai.com and the project's `LLM_PROVIDER` may be OpenRouter) and call `client.audio.transcriptions.create(model="whisper-1", file=(filename, audio_bytes))`.
  3. Else → return `None` (caller proceeds with text-only analysis).
  Wrap each tier in try/except; log failures; never raise (transcription is best-effort).
- `async analyze_recording(owner_id, piece: dict | None, transcript: str | None, user_note: str) -> dict` — builds system prompt from spec §"Analysis sub-agent system prompt", calls `get_llm_client(model_override=os.getenv("PIANO_ANALYSIS_MODEL"))`, parses JSON with one retry (reuse `_parse_json_response` from `llm.py` — expose by simply importing the underscore name).
- `format_feedback(analysis: dict) -> str` — pretty-print feedback for Telegram reply.
- **No pydub/ffmpeg conversion.** Telegram voice (`.ogg`/opus) is accepted directly by whisper.cpp and by OpenAI's whisper-1 endpoint, so we pass raw bytes. Avoids a native-dependency install.

### `bot/services/piano/streaks.py` *(optional split — could live in `coach.py`)*
- `async compute_and_update_streak(owner_id, practiced_at: date) -> dict` — reads `piano_streak`, updates per rules (same-day no-op, consecutive-day increment, gap reset-to-1), writes back, returns new streak dict. Called from `_piano_log`.

---

## Files to modify

### `migrations/init.sql`
Append 4 tables exactly as spec §"Database Schema additions" dictates:
```sql
CREATE TABLE IF NOT EXISTS piano_sessions ( ... );
CREATE TABLE IF NOT EXISTS piano_pieces ( ... );
CREATE TABLE IF NOT EXISTS piano_recordings ( ... );
CREATE TABLE IF NOT EXISTS piano_streak ( ... );
```
Match existing style: `INTEGER PRIMARY KEY AUTOINCREMENT`, `BIGINT` for owner IDs, `BOOLEAN DEFAULT 1` where needed. Store `pieces_practiced` as TEXT holding `json.dumps(list)`. Idempotent — `CREATE TABLE IF NOT EXISTS` handles both fresh installs and existing DBs.

### `bot/services/db_sqlite.py`
New CRUD (all `async def`, follow existing argument order: first entity-id args, `owner_id` as authorization context, `await db.commit()` after writes):

- `log_piano_session(owner_id, practiced_at: date, duration_minutes: int|None, notes: str|None, pieces_practiced: list[str]) -> int` — JSON-dumps `pieces_practiced`
- `list_piano_sessions(owner_id, limit: int = 7) -> list[dict]` — ORDER BY practiced_at DESC, parse JSON back
- `get_piano_session_today(owner_id) -> dict | None` — for checkin-skip logic
- `piano_total_stats(owner_id) -> dict` — total_sessions, total_minutes
- `add_piano_piece(owner_id, title, composer) -> int`
- `remove_piano_piece(owner_id, piece_id) -> None`
- `list_piano_pieces(owner_id, status: str | None = None) -> list[dict]`
- `find_piano_piece_by_title(owner_id, title) -> dict | None` — case-insensitive (SQLite `COLLATE NOCASE` or `LOWER()` LIKE)
- `update_piano_piece_status(owner_id, piece_id, status) -> None`
- `update_piano_piece_note(owner_id, piece_id, notes) -> None`
- `touch_piano_piece_last_practiced(owner_id, piece_id, practiced_at: date) -> None`
- `most_practiced_piece(owner_id) -> dict | None` — count joins via JSON; simplest implementation counts occurrences by iterating sessions in Python rather than JSON-querying SQLite (fine for small tables)
- `get_piano_streak(owner_id) -> dict` — returns row or defaults `{current:0, longest:0, last_practiced_date:None}`
- `upsert_piano_streak(owner_id, current, longest, last_practiced_date)` — INSERT OR REPLACE
- `add_piano_recording(owner_id, piece_id, file_path, duration_seconds, feedback_summary, raw_analysis) -> int`
- `list_piano_recordings(owner_id, piece_id: int | None = None, limit: int = 10) -> list[dict]` — used by analysis for "previous recordings" context

No changes to `_apply_migrations` — tables are added via the `CREATE TABLE IF NOT EXISTS` block in `init.sql` and are net-new, so no `ALTER TABLE` needed.

### `bot/services/db_postgres.py`
- Extend `_get_pg_schema()` with Postgres variants of the 4 tables (SERIAL, TIMESTAMPTZ where applicable). Keep DATE columns as DATE.
- New `mirror_*` functions paralleling the sqlite writes, passing the SQLite-generated `id` as first arg (same convention as `mirror_log_meal`). All wrapped in `try/except` with `logger.error(...)` and early-return if `_pool is None`:
  - `mirror_log_piano_session`
  - `mirror_add_piano_piece`
  - `mirror_remove_piano_piece`
  - `mirror_update_piano_piece_status`
  - `mirror_update_piano_piece_note`
  - `mirror_touch_piano_piece_last_practiced`
  - `mirror_upsert_piano_streak` (ON CONFLICT (owner_user_id) DO UPDATE)
  - `mirror_add_piano_recording`

### `bot/services/llm.py`
Extend `get_llm_client`:
```python
def get_llm_client(model_override: str | None = None) -> tuple[AsyncOpenAI, str]:
    if _current_client is None:
        init_llm()
    return _current_client, (model_override or _current_model)
```
Non-breaking: callers that pass no arg behave unchanged. Also promote `_parse_json_response` to a public-ish helper (rename to `parse_json_response` or simply import by underscore name from `audio_agent.py`). No other LLM changes.

### `bot/services/scheduler.py`
Add alongside `register_daily_summary`:
```python
def register_piano_checkin(scheduler, bot) -> None:
    time_str = os.getenv("PIANO_CHECKIN_TIME", "19:00")
    # parse HH:MM; cron job id="piano_checkin"; iterate all owners from profiles table;
    # skip owners whose get_piano_session_today(owner) is not None;
    # send "🎹 Time for your daily piano check-in! /piano log or tell me about it."
```
Use the same "iterate all profile owners" pattern already present in `_send_summaries`.

### `main.py`
- Import `piano` handler module and `register_piano_checkin` from scheduler.
- In `post_init`, after `register_daily_summary(scheduler, app.bot)`, call `register_piano_checkin(scheduler, app.bot)`.
- In `main()`, add `piano.register(app)` **before** `calories.register(app)` so the voice/audio MessageHandler doesn't get shadowed by anything (photo vs voice are distinct filters so order isn't strictly required, but keeping piano high avoids future collisions). Still before the `MessageHandler(filters.COMMAND, unknown_cmd)` catch-all.

### `bot/handlers/calories.py`
Minimal change to `refine_handler`: at the top, call `piano.piano_text_dispatch(update, context)`; if it returns `True` (meaning the text belonged to a piano pending state), return early. Keeps refine_handler's existing meal-only logic otherwise untouched.

### `.env.example`
Append:
```
# Piano practice coach
PIANO_CHAT_MODEL=google/gemini-flash-1.5
PIANO_ANALYSIS_MODEL=openai/gpt-4o
PIANO_CHECKIN_TIME=19:00

# Transcription for /piano analyze (tiered; both optional)
LOCAL_WHISPER_URL=              # e.g. http://localhost:9000/asr (whisper.cpp server)
OPENAI_API_KEY=                 # fallback to api.openai.com/v1/audio/transcriptions if LOCAL_WHISPER_URL fails/unset
```
`OPENAI_API_KEY` is used **only** for whisper transcription; the main LLM client continues to use `OPENROUTER_API_KEY` / `LLM_PROVIDER`. Both whisper env vars are optional — if neither is set, analysis proceeds with text-only context.

---

## Recording-analysis flow (`/piano analyze`)

1. Resolve audio: first check `context.user_data["pending_piano_audio"]`; else if current update has a voice/audio attachment, use it; else reply "send a voice note first, then /piano analyze".
2. Download via `bot.get_file()` → `file.download_as_bytearray()`.
3. Save to `./data/piano_recordings/<ts>_<owner>_<uuid>.ogg` (follow `save_meal_photo`'s pattern; add `save_piano_recording` to `bot/utils/storage.py`).
4. Ask user for piece context if not already specified in `/piano analyze <piece title>` args; else `None`.
5. Transcribe via `audio_agent.transcribe()` — may return `None`.
6. Load recording history for this piece (last 3 via `list_piano_recordings`).
7. Call `audio_agent.analyze_recording()` with `PIANO_ANALYSIS_MODEL` override.
8. Persist with `add_piano_recording` + `mirror_add_piano_recording`.
9. Reply with `format_feedback(analysis)`.
10. If JSON parse fails after retry, fall back to raw text reply with a header note (spec §Error Handling).

Audio file NOT deleted immediately (spec says delete from `/tmp/`, but we save under `./data/piano_recordings/` for audit, same as photos). Can add a retention policy later.

---

## Streak computation

In `streaks.compute_and_update_streak(owner_id, practiced_at)`:
- Read current row via `get_piano_streak`.
- `last = row["last_practiced_date"]`
- If `last == practiced_at`: no-op (same-day re-log), return current.
- If `last is None or (practiced_at - last).days > 1`: reset current = 1.
- If `(practiced_at - last).days == 1`: current += 1.
- `longest = max(longest, current)`, `last = practiced_at`.
- `upsert_piano_streak(...)` + mirror.

---

## Critical files to modify (summary)

- `migrations/init.sql` — 4 new tables
- `bot/services/db_sqlite.py` — ~16 CRUD functions
- `bot/services/db_postgres.py` — `_get_pg_schema` + 8 mirror functions
- `bot/services/llm.py` — `get_llm_client(model_override=None)`
- `bot/services/scheduler.py` — `register_piano_checkin`
- `bot/utils/storage.py` — `save_piano_recording` (parallels `save_meal_photo`)
- `bot/handlers/calories.py` — 3-line dispatch hook at top of `refine_handler`
- `bot/handlers/piano.py` — new file
- `bot/services/piano/{__init__,coach,repertoire,audio_agent}.py` — new files
- `main.py` — 2 lines (import + register)
- `.env.example` — 4 new env vars

## Critical existing utilities to reuse

- `bot/services/llm.py::_parse_json_response` — JSON-with-retry pattern; expose for audio_agent
- `bot/services/llm.py::get_llm_client` — provider singleton
- `bot/utils/storage.py::save_meal_photo` — template for `save_piano_recording`
- `bot/utils/formatting.py::strip_command_args` — if piano adopts any of the meal-command parsers
- `bot/services/scheduler.py::register_daily_summary` — template for `register_piano_checkin`
- `bot/services/db_postgres.py` silent-no-op pattern — copy exactly for all new mirrors

---

## Verification

1. `uv sync` to pick up any new deps (none expected unless whisper.cpp integration needs `httpx` — already pulled in by `openai`, so no new deps).
2. `uv run python main.py` — confirm bot boots, logs "Scheduler started", "PostgreSQL mirror initialised" (or the graceful skip), and `Loaded N supplement reminders`.
3. Manual test matrix in Telegram with a real bot token:
   - `/piano` — summary reply (empty state).
   - `/piano piece add Chopin Nocturne by Chopin` — piece created.
   - `/piano pieces` — shows 📖 Chopin Nocturne.
   - `/piano piece status Chopin Nocturne polishing` — status change.
   - `/piano log 30 min Chopin Nocturne, scales` — logs session, streak day 1, encouragement.
   - `/piano log 20 min Chopin Nocturne` next day — streak day 2. Skip a day, log again — streak resets to 1.
   - `/piano log` (no args) → usage prompt. Reply `25 min Chopin` → refine_handler dispatches to piano and logs.
   - `/piano checkin` — single LLM reply referencing streak + last sessions.
   - `/piano history`, `/piano stats` — correct numbers.
   - Wait until `PIANO_CHECKIN_TIME` (or set it to the next minute) — proactive message arrives. Log a session, run again — message is skipped.
   - Send a voice note — bot prompts "Is this a piano recording?". Reply `/piano analyze` — analysis runs. Verify each tier separately: (a) with `LOCAL_WHISPER_URL` set and a whisper.cpp server running, (b) with `LOCAL_WHISPER_URL` unset but `OPENAI_API_KEY` set, (c) with both unset — should gracefully produce analysis based on user note + piece history and include a "transcription unavailable" note in the reply.
   - Send voice with caption `/piano analyze Chopin Nocturne` — analysis runs inline.
   - Restart bot — scheduler reloads `piano_checkin` job, streak persists.
4. Verify Postgres mirror: if `DATABASE_URL` set and reachable, `SELECT * FROM piano_sessions` after a log matches the SQLite row. If `DATABASE_URL` unset, confirm bot still functions silently.
5. Confirm `/model` still works and does NOT change which model piano uses (because piano passes `model_override`).
