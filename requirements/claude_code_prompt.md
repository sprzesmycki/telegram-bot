# Calorie & Supplement Tracker — Telegram Bot

## Project Overview

Build a Telegram bot in Python that helps the user track daily calorie intake (via photo or text description) and reminds them to take supplements according to a custom schedule.

The bot supports **multiple profiles** per Telegram account (e.g. "Me" and "Wife") so one person can log meals for several people independently. It also supports logging meals **with a custom time** (for retroactive entries) and can generate a **dietitian-ready daily report** as a formatted plain-text export.

---

## Tech Stack

- **Language:** Python 3.11+
- **Telegram:** `python-telegram-bot` (v20+, async)
- **LLM:** OpenRouter API (OpenAI-compatible), use `openai` Python SDK pointed at OpenRouter base URL
- **Vision model:** Use a model with vision support available on OpenRouter (e.g. `anthropic/claude-3-5-sonnet`) — make it configurable via `.env`
- **Primary DB:** SQLite (local, via `aiosqlite`)
- **Backup DB:** PostgreSQL (via `asyncpg`) — sync after every write to SQLite
- **Scheduler:** `APScheduler` (AsyncIOScheduler) for supplement reminders and daily summary

---

## Project Structure

```
calorie-bot/
├── bot/
│   ├── __init__.py
│   ├── handlers/
│   │   ├── calories.py       # /cal command handler
│   │   ├── supplements.py    # /supplement commands
│   │   ├── summary.py        # /summary, /week, /report commands
│   │   ├── goals.py          # /goal command
│   │   └── profiles.py       # /profile commands
│   ├── services/
│   │   ├── llm.py            # OpenRouter API calls
│   │   ├── db_sqlite.py      # SQLite async operations
│   │   ├── db_postgres.py    # PostgreSQL async operations
│   │   └── scheduler.py      # APScheduler setup
│   └── utils/
│       └── formatting.py     # Message formatting helpers
├── migrations/
│   └── init.sql              # DB schema
├── .env.example
├── requirements.txt
└── main.py
```

---

## Configuration

Config is loaded from `~/.config/aidevs4/.env` using `python-dotenv`. Do **not** store any `.env` file in the repository. Add `.env` to `.gitignore`.

Load it at startup in `main.py`:

```python
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path.home() / ".config" / "aidevs4" / ".env")
```

### `.env` file location: `~/.config/aidevs4/.env`

```
TELEGRAM_BOT_TOKEN=
OPENROUTER_API_KEY=
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_MODEL=anthropic/claude-3-5-sonnet
DATABASE_URL=postgresql://user:password@localhost:5432/caloriebot
SQLITE_PATH=./data/caloriebot.db
DAILY_SUMMARY_TIME=21:00
```

Include a `.env.example` file in the repo with the same keys but empty values, as documentation.

---

## Database Schema

### SQLite (primary) + PostgreSQL (mirror — identical schema)

```sql
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT 1,
    UNIQUE(owner_user_id, name)
);

CREATE TABLE IF NOT EXISTS active_profile (
    user_id BIGINT PRIMARY KEY,
    profile_id INTEGER NOT NULL REFERENCES profiles(id)
);

CREATE TABLE IF NOT EXISTS meals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id),
    owner_user_id BIGINT NOT NULL,
    eaten_at DATETIME NOT NULL,
    logged_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    description TEXT NOT NULL,
    calories INTEGER,
    protein_g REAL,
    carbs_g REAL,
    fat_g REAL,
    raw_llm_response TEXT
);

CREATE TABLE IF NOT EXISTS goals (
    profile_id INTEGER PRIMARY KEY REFERENCES profiles(id),
    daily_calories INTEGER NOT NULL DEFAULT 2000
);

CREATE TABLE IF NOT EXISTS supplements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id),
    owner_user_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    reminder_time TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS supplement_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplement_id INTEGER NOT NULL REFERENCES supplements(id),
    profile_id INTEGER NOT NULL,
    taken_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

---

## Bot Commands & Behavior

### Profile Management

#### `/profile add <name>`
- Create a new profile linked to this Telegram user (e.g. `/profile add Wife`)
- On first use (no profiles exist), automatically create a default "Me" profile and set it active before creating the new one

#### `/profile list`
- Show all profiles; mark active one with ✅

#### `/profile switch <name>`
- Switch active profile; all subsequent commands apply to it

#### `/profile delete <name>`
- Soft-delete profile (keep data, set active=0); cannot delete last remaining profile

---

### `/cal <description> [@name|@both] [at HH:MM]` or `/cal` + photo

- **Active profile** is used by default
- `@name` targets a specific profile (e.g. `/cal salad @Wife`)
- `@both` logs the same meal to **all profiles** at once (e.g. `/cal pizza @both`)
- `at HH:MM` sets the actual meal time retroactively (e.g. `/cal eggs at 08:30`); if omitted, use current time as `eaten_at`
- If message contains a **photo**: attach image as base64 to LLM vision call
- LLM system prompt: `You are a nutrition assistant. Always return valid JSON only, no markdown. Schema: {"calories": int, "protein_g": float, "carbs_g": float, "fat_g": float, "description": str}. Always estimate, never refuse.`
- Save to `meals` table (SQLite + Postgres sync); if `@both`, insert one row per profile
- Reply with: profile name(s), meal description, kcal + macros, running daily total vs goal

### `/recipe <url> [for <N>] [@name|@both]` or `/recipe` + pasted text + `[for <N>]`

- **URL mode**: bot fetches the page content (use `httpx` with a browser-like User-Agent), extracts the recipe text, then passes it to the LLM
- **Paste mode**: user pastes raw recipe text directly in the message after `/recipe`
- **`for N`** specifies how many servings the recipe makes (e.g. `/recipe https://... for 4`); if omitted, LLM tries to detect serving count from the recipe text; if still unclear, bot asks the user
- LLM calculates total kcal + macros for the whole recipe, then divides by N to get per-serving values
- LLM system prompt addition: `Given this recipe, calculate: total calories and macros (protein_g, carbs_g, fat_g) for the whole dish, then divide by servings. Return JSON: {"total": {"calories": int, "protein_g": float, "carbs_g": float, "fat_g": float}, "per_serving": {"calories": int, "protein_g": float, "carbs_g": float, "fat_g": float}, "servings": int, "dish_name": str}. Always estimate, never refuse.`
- Bot replies with a summary: dish name, per-serving kcal + macros, total for whole recipe
- Then asks: `"Log this as a meal for [active profile]? Reply /yes to confirm or /cal <description> to adjust."`
- If user confirms with `/yes`, log it as a meal entry using current time (or `at HH:MM` if provided) — reuse existing `/cal` save logic
- `@name` / `@both` targeting works the same as in `/cal`

**URL fetching notes:**
- Use `httpx` with `follow_redirects=True` and a realistic User-Agent header
- Strip HTML tags, extract only visible text (use `BeautifulSoup` with `get_text()`)
- If the page is behind a paywall or returns no useful content, reply with an error asking the user to paste the recipe text directly instead
- Limit extracted text to 8000 characters before sending to LLM to stay within token budget


- Show today's meals for active/named profile
- Each meal: time (`eaten_at`), description, kcal
- Totals: kcal consumed, remaining vs goal, protein/carbs/fat

### `/week [@name]`
- Last 7 days for active/named profile
- Per-day: date, total kcal, vs goal (over/under)
- Weekly average kcal

### `/report [@name] [YYYY-MM-DD]`
- Generate a **dietitian-ready plain-text report** for the given date (default: today)
- Content:
  - Header: profile name + date
  - Each meal: time, description, kcal, protein/carbs/fat
  - Daily totals and macros breakdown
  - Supplement adherence: list of scheduled supplements, which were logged as taken
- Send as plain-text Telegram message — easy to copy or forward directly to a dietitian

### `/goal <kcal> [@name]`
- Set daily calorie target for active/named profile
- Upsert into `goals` table by `profile_id`

---

### Supplement Commands

All supplement commands operate on the **active profile** unless `@name` is appended.

- `/supplement add <name> <HH:MM> [@profile]` — add supplement, register APScheduler CronTrigger job
- `/supplement list [@profile]` — show active supplements with times
- `/supplement done <name> [@profile]` — log as taken today in `supplement_logs`
- `/supplement remove <name> [@profile]` — soft-delete, remove scheduler job

---

## Supplement Reminder Logic

- On bot startup: load all active supplements from DB, register APScheduler CronTrigger jobs per HH:MM per profile
- At reminder time: send message to owner: `💊 [ProfileName] Reminder: time to take {name}! Reply /supplement done {name} @{profile} when done.`
- At `DAILY_SUMMARY_TIME`: automatically send `/summary` output for each profile to the owner

---

## LLM Provider System

The bot must support **multiple LLM providers** with easy switching via bot command or `.env`. All providers expose an OpenAI-compatible API, so a single async client wrapper handles all of them.

### Supported providers

| Provider | base_url | API key env var | Notes |
|---|---|---|---|
| `openrouter` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` | Default; cloud models |
| `local` | `http://localhost:11434/v1` | `LOCAL_API_KEY=ollama` | Ollama (e.g. Gemma 4) |
| `custom` | `CUSTOM_BASE_URL` | `CUSTOM_API_KEY` | Any OpenAI-compat endpoint |

### `.env` additions

```
# Active provider: openrouter | local | custom
LLM_PROVIDER=openrouter

# Model to use per provider (can be overridden live via /model command)
OPENROUTER_MODEL=anthropic/claude-3-5-sonnet
LOCAL_MODEL=gemma3:27b
CUSTOM_BASE_URL=
CUSTOM_MODEL=

LOCAL_API_KEY=ollama
CUSTOM_API_KEY=
```

### Provider factory (`bot/services/llm.py`)

```python
from openai import AsyncOpenAI
import os

def get_llm_client() -> tuple[AsyncOpenAI, str]:
    provider = os.getenv("LLM_PROVIDER", "openrouter")
    if provider == "local":
        client = AsyncOpenAI(
            api_key=os.getenv("LOCAL_API_KEY", "ollama"),
            base_url=os.getenv("LOCAL_BASE_URL", "http://localhost:11434/v1"),
        )
        model = os.getenv("LOCAL_MODEL", "gemma3:27b")
    elif provider == "custom":
        client = AsyncOpenAI(
            api_key=os.getenv("CUSTOM_API_KEY"),
            base_url=os.getenv("CUSTOM_BASE_URL"),
        )
        model = os.getenv("CUSTOM_MODEL")
    else:  # openrouter
        client = AsyncOpenAI(
            api_key=os.getenv("OPENROUTER_API_KEY"),
            base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        )
        model = os.getenv("OPENROUTER_MODEL", "anthropic/claude-3-5-sonnet")
    return client, model
```

Store active provider + model override in a simple in-memory singleton (reset on bot restart); DB persistence is not required.

### `/model` command (admin/owner only)

- `/model` — show current provider and model name
- `/model openrouter [model-name]` — switch to OpenRouter, optionally change model
- `/model local [model-name]` — switch to local Ollama
- `/model custom [model-name]` — switch to custom endpoint
- Changes take effect immediately for all subsequent LLM calls without restart
- Bot replies with confirmation: current provider, base URL, model name

### Vision handling

When sending a photo, always pass image as base64 `image_url`. Local models that do not support vision (e.g. text-only variants) should return a graceful error: `"⚠️ Current model does not support image analysis. Please describe the meal in text or switch to a vision-capable model."`

Detect vision support failure by catching a 400/422 response or an exception from the client and replying with the above message — do not crash.

---

## PostgreSQL Sync Strategy

- After every successful SQLite write, immediately replicate the same record to PostgreSQL
- If PostgreSQL is unavailable, log the error but do NOT fail the user interaction — SQLite is the source of truth
- On startup, check if PostgreSQL is reachable; log warning if not

---

## Error Handling

- LLM returns malformed JSON → retry once with stricter prompt, then reply asking user to rephrase
- Photo too large for API → compress with Pillow before sending
- PostgreSQL connection failure → silent fallback to SQLite only, log warning
- Unknown command → friendly help message listing all commands

---

## Additional Requirements

- All handlers must be async
- Owner isolation: all DB queries must filter by `owner_user_id` (Telegram user ID)
- Include a `requirements.txt` with pinned versions
- Include a `README.md` with setup instructions (Telegram bot token, `.env` setup, running migrations, starting the bot)
- Add basic logging (Python `logging` module) to stdout

---

## Out of Scope (do not implement)

- Web dashboard
- Authentication / registration flow
- Payment / subscription logic
- Multi-language support
