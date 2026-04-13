# Piano Practice Agent — Telegram Bot Module

## Overview

Add a piano practice coaching module to the existing calorie-tracker Telegram bot.
The module has two responsibilities:
1. **Daily coaching & habit tracking** — motivational check-ins, practice session logging, streak tracking, repertoire management
2. **Recording analysis** — user sends a voice/audio recording, a dedicated sub-agent analyzes it and returns structured feedback

This is implemented as a separate set of handlers and services inside the existing project, following the same patterns (async, aiosqlite + postgres sync, same `.env` config, same provider factory).

---

## Model Selection Strategy

Use **two separate model tiers** — do not use the same model for everything:

### Tier 1 — Lightweight (coaching dialog, reminders, streak messages)
- Fast, cheap, no vision/audio needed
- Recommended: `google/gemma-3-12b` (local) or `google/gemini-flash-1.5` (OpenRouter)
- Configured via `PIANO_CHAT_MODEL` in `.env`
- Used for: check-in conversations, session summaries, practice suggestions, streak feedback

### Tier 2 — Capable audio/reasoning model (recording analysis sub-agent)
- Needs strong reasoning; audio transcription handled separately (see below)
- Recommended: `anthropic/claude-3-5-sonnet` or `openai/gpt-4o` via OpenRouter
- Configured via `PIANO_ANALYSIS_MODEL` in `.env`
- Used only when user submits a recording — keep cost low by invoking rarely

### `.env` additions

```
PIANO_CHAT_MODEL=google/gemini-flash-1.5
PIANO_ANALYSIS_MODEL=anthropic/claude-3-5-sonnet
PIANO_CHECKIN_TIME=19:00          # daily check-in reminder HH:MM
```

Model selection must use the same provider factory as the rest of the bot (`get_llm_client()`), extended to accept an optional `model_override` parameter so piano handlers can request a specific model tier without changing the global active model.

---

## Project Structure additions

```
bot/
├── handlers/
│   └── piano.py              # all /piano commands
├── services/
│   └── piano/
│       ├── coach.py          # coaching dialog logic, streak calc
│       ├── audio_agent.py    # recording analysis sub-agent
│       └── repertoire.py     # piece tracking helpers
```

---

## Database Schema additions

```sql
CREATE TABLE IF NOT EXISTS piano_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id BIGINT NOT NULL,
    practiced_at DATE NOT NULL,
    duration_minutes INTEGER,
    notes TEXT,                          -- user's own description of the session
    pieces_practiced TEXT,               -- JSON array of piece names
    logged_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS piano_pieces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id BIGINT NOT NULL,
    title TEXT NOT NULL,
    composer TEXT,
    status TEXT NOT NULL DEFAULT 'learning',  -- learning | polishing | mastered | needs_review
    added_at DATE NOT NULL DEFAULT CURRENT_DATE,
    last_practiced_at DATE,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS piano_recordings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id BIGINT NOT NULL,
    piece_id INTEGER REFERENCES piano_pieces(id),
    recorded_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    file_path TEXT,                      -- local path to saved audio file
    duration_seconds INTEGER,
    feedback_summary TEXT,               -- LLM output stored as plain text
    raw_analysis TEXT                    -- full JSON from analysis sub-agent
);

CREATE TABLE IF NOT EXISTS piano_streak (
    owner_user_id BIGINT PRIMARY KEY,
    current_streak INTEGER NOT NULL DEFAULT 0,
    longest_streak INTEGER NOT NULL DEFAULT 0,
    last_practiced_date DATE
);
```

---

## Bot Commands

### `/piano` — main menu
Show a summary: current streak 🔥, pieces in progress, last session date, quick tips.

### `/piano log [N min] [<piece1>, <piece2>]`
Log a practice session for today.
- `N min` — duration in minutes (e.g. `/piano log 30 min Chopin Nocturne, scales`)
- If no arguments, bot starts a short conversational check-in (3 questions max: how long, what did you practice, how did it feel)
- Update `piano_sessions`, update `piano_streak`, update `last_practiced_at` on relevant pieces
- Reply with encouragement + streak info: `"🔥 Day 5 in a row! Keep it up."`

### `/piano checkin`
Trigger the daily coaching dialog manually (same as the scheduled one).
Lightweight model asks:
1. Did you practice today?
2. If yes — what and how long? Any difficulties?
3. Based on session history and piece statuses, suggest what to focus on next session (max 3 bullet points)

### `/piano pieces`
List all pieces with their status (emoji per status: 📖 learning, 🔧 polishing, ✅ mastered, 🔄 needs review).

### `/piano piece add <title> [by <composer>]`
Add a new piece to the repertoire, status defaults to `learning`.

### `/piano piece status <title> <status>`
Update piece status. Valid statuses: `learning`, `polishing`, `mastered`, `needs_review`.

### `/piano piece note <title> <text>`
Add a free-text note to a piece (e.g. "left hand bars 12-16 still shaky").

### `/piano analyze` + audio/voice message
Trigger the **recording analysis sub-agent** (see below).

### `/piano history [N]`
Show last N practice sessions (default 7) with dates, durations, pieces practiced.

### `/piano stats`
- Total sessions logged
- Total practice time (hours + minutes)
- Current and longest streak
- Pieces by status count
- Most practiced piece

---

## Daily Check-in Scheduler

- At `PIANO_CHECKIN_TIME` each day, bot sends a proactive message: `"🎹 Time for your daily piano check-in! Did you practice today? /piano log or tell me about it."`
- Use APScheduler CronTrigger, same pattern as supplement reminders
- If user already logged a session today, skip the reminder

---

## Recording Analysis Sub-Agent

This is a **dedicated sub-agent** invoked only when the user sends `/piano analyze` with an attached voice or audio message.

### Flow

1. User sends `/piano analyze` with an attached Telegram voice message or audio file
2. Bot downloads the file using `bot.get_file()` and saves it temporarily to `/tmp/`
3. **Transcription step**: send audio to OpenAI Whisper-compatible endpoint (use `openrouter` or local `whisper.cpp` via `LOCAL_WHISPER_URL` env var if set). If no whisper endpoint available, skip transcription and proceed with audio description only.
4. **Analysis step**: send to `PIANO_ANALYSIS_MODEL` with:
   - Transcribed text (if available)
   - User's note about which piece it is (ask before analysis if not provided)
   - Piece history from `piano_pieces` and previous recordings for context
   - System prompt (see below)
5. Parse structured JSON response, save to `piano_recordings`
6. Reply with formatted feedback message

### Analysis sub-agent system prompt

```
You are an expert piano teacher assistant. Analyze the provided piano practice recording transcription and context.
Return ONLY valid JSON, no markdown:
{
  "overall_impression": str,          // 1-2 sentences
  "tempo": {
    "assessment": str,                // "steady" | "rushing" | "dragging" | "uneven"
    "notes": str
  },
  "rhythm": {
    "assessment": str,                // "accurate" | "minor_errors" | "significant_errors"
    "notes": str
  },
  "dynamics": {
    "assessment": str,
    "notes": str
  },
  "problem_areas": [str],             // list of specific bars or passages to work on
  "strengths": [str],
  "next_session_focus": [str],        // max 3 actionable suggestions
  "progress_vs_last": str             // "improved" | "similar" | "regressed" | "first_recording"
}
Always be encouraging but honest. Never refuse to analyze.
```

### `.env` addition for Whisper

```
LOCAL_WHISPER_URL=          # optional, e.g. http://localhost:9000/asr (whisper.cpp server)
                            # if empty, use OpenRouter/OpenAI Whisper API
```

### Audio handling notes

- Telegram voice messages are `.ogg` (opus codec) — convert to `.mp3` or `.wav` using `pydub` + `ffmpeg` before sending to Whisper
- Max file size to process: 25MB (Telegram bot API limit)
- After analysis, delete the temp file from `/tmp/`
- If transcription fails or returns empty, ask the user to describe the recording in text and proceed with text-only analysis

---

## Coaching Dialog Logic (`coach.py`)

The lightweight model handles all conversational interactions. Keep system prompt concise to minimize tokens:

```
You are a friendly piano practice coach. Be encouraging, brief, and practical.
User context: {streak} day streak, currently learning: {pieces_in_progress}.
Recent sessions: {last_3_sessions_summary}.
Keep responses under 150 words. Use emojis sparingly.
```

Inject fresh context from DB on every call — no conversation memory needed between sessions (stateless per check-in).

---

## Error Handling

- Audio download fails → ask user to resend
- Whisper transcription fails → proceed with text-only analysis, notify user
- Analysis model returns malformed JSON → retry once, then return raw text response with a note
- User sends audio without `/piano analyze` prefix → bot detects voice message and asks: `"🎹 Is this a piano recording? Reply /piano analyze to get feedback."`

---

## Out of Scope

- Sheet music recognition (OCR on scores)
- MIDI file analysis
- Real-time audio streaming
- Integration with external practice apps (Playground Sessions, Simply Piano, etc.)