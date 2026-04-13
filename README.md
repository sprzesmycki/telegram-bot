# Calorie & Supplement Tracker — Telegram Bot

A Telegram bot that tracks daily calorie intake (via photo or text) and manages supplement reminders with a custom schedule. Supports multiple profiles per account.

## Prerequisites

- Python 3.14+
- [uv](https://docs.astral.sh/uv/) package manager
- Telegram Bot Token (from [@BotFather](https://t.me/BotFather))
- OpenRouter API key (or local Ollama / custom LLM endpoint)
- PostgreSQL (optional — used as mirror backup)

## Setup

1. **Install dependencies:**

   ```bash
   uv sync
   ```

2. **Configure environment:**

   Create or edit `~/.config/telegrambot/.env` with your keys (see `.env.example` for all options):

   ```
   TELEGRAM_BOT_TOKEN=your-token-here
   OPENROUTER_API_KEY=your-key-here
   ```

3. **Run the bot:**

   ```bash
   uv run python main.py
   ```

   SQLite database is created automatically at `./data/caloriebot.db`. PostgreSQL mirror is optional — if `DATABASE_URL` is not set or the server is unreachable, the bot runs on SQLite only.

## Commands

| Command | Description |
|---------|-------------|
| `/cal <description> [@name\|@both] [at HH:MM]` | Analyse a meal (text or photo); shows preview |
| `/recipe <URL or text> [for N] [@name\|@both]` | Analyse a recipe; shows preview |
| `/yes` | Confirm and log the pending meal/recipe |
| _(plain text)_ | Refine the pending preview (e.g. "add butter", "larger portion") |
| `/summary [@name]` | Today's meal summary |
| `/week [@name]` | Last 7 days overview |
| `/report [@name] [YYYY-MM-DD]` | Dietitian-ready daily report |
| `/goal <kcal> [@name]` | Set daily calorie target |
| `/profile add\|list\|switch\|delete <name>` | Manage profiles |
| `/supplement add\|list\|done\|remove <name> [HH:MM]` | Manage supplements |
| `/model [openrouter\|local\|custom] [model-name]` | View or switch LLM provider |

## Sample Commands

A realistic end-to-end flow. Run these in order against your bot in Telegram:

**1. Create profiles**

```
/profile add Seba
/profile add Wife
/profile list
/profile switch Wife
```

The first `/profile add` auto-creates a default `Me` profile alongside yours, so after `add Seba` you'll have both `Me` and `Seba`. Names are case-sensitive — `@wife` won't match `Wife`.

**2. Set calorie goals**

```
/goal 2200
/goal 1800 @Wife
```

**3. Log meals (preview → approve or refine)**

```
/cal scrambled eggs with toast and orange juice
/cal banana at 07:30
/cal chicken salad @Wife
/cal slice of pizza @both
```

Or send a photo with a caption (or no caption at all). The bot downloads, compresses, and runs vision analysis.

Every `/cal` (text or photo) replies with a **preview**, not a log — nothing is saved until you confirm:

```
Preview — will log to: Me at 13:05
Scrambled eggs with toast / Jajecznica z tostem
420 kcal | P: 22g | C: 35g | F: 18g

Reply /yes to log, or send a remark to refine.
Example: "actually larger portion" or "add a tablespoon of butter".
```

Descriptions are always bilingual (English / Polish). To approve, reply `/yes`. To adjust, send a plain-text remark and the bot re-analyses:

```
you: /cal scrambled eggs
bot: Preview — ... Scrambled eggs / Jajecznica ... 180 kcal ...
you: that's 3 eggs with butter and a slice of cheddar
bot: Preview — ... Scrambled eggs with butter and cheddar / ... 350 kcal ...
you: /yes
bot: [Me] Logged: Scrambled eggs with butter and cheddar / Jajecznica z masłem i cheddarem ...
```

Caption examples for photos:

- `/cal` — just analyse the photo
- `/cal at 13:00` — analyse photo, backdate to 13:00
- `/cal toast with potato, cheese and ham` — the caption is treated as an authoritative hint about ingredients, which is especially useful for dishes where the layers or fillings aren't visible (sandwiches, wraps, stuffed pastries, casseroles)
- `/cal chicken curry with rice @Wife at 12:30` — full combo: hint + target profile + time

**4. Log a recipe**

```
/recipe https://www.seriouseats.com/perfect-scrambled-eggs-recipe for 2
/yes
```

`/recipe` shows per-serving macros with a bilingual dish name; `/yes` logs one serving to your active profile. Same as `/cal`, you can also send a plain-text remark to refine (e.g. "use half the oil", "add 200g chicken breast") before confirming.

**5. Check progress**

```
/summary
/summary @Wife
/week
/report @Seba 2026-04-11
```

**6. Supplements**

```
/supplement add Vitamin_D 09:00
/supplement add Omega_3 21:00
/supplement list
/supplement done Vitamin_D
/supplement remove Omega_3
```

Supplement names cannot contain spaces — use underscores (`Vitamin_D`, not `Vitamin D`). The reminder fires daily at the given `HH:MM`.

**7. Switch LLM provider**

```
/model
/model openrouter anthropic/claude-sonnet-4.5
/model local gemma3:27b
```

First form shows the current provider/model; the others switch at runtime.

## Multi-Profile Support

- `@name` targets a specific profile (e.g., `/cal salad @Wife`)
- `@both` logs to all profiles at once
- Default profile "Me" is created automatically on first use

## LLM Providers

| Provider | Base URL | Notes |
|----------|----------|-------|
| `openrouter` | `https://openrouter.ai/api/v1` | Default; cloud models with vision |
| `local` | `http://localhost:11434/v1` | Ollama (e.g., Gemma 4) |
| `custom` | Configurable | Any OpenAI-compatible endpoint |

Switch at runtime with `/model local gemma3:27b` — no restart needed.

## Architecture

```
bot/
  handlers/    — Telegram command handlers (one file per feature)
  services/    — LLM client, SQLite, PostgreSQL, APScheduler
  utils/       — Message formatting and text parsing
migrations/    — Database schema
main.py        — Entry point
```

- **Primary DB:** SQLite (async via aiosqlite)
- **Mirror DB:** PostgreSQL (async via asyncpg) — every write is replicated; failures are silent
- **Scheduler:** APScheduler for supplement reminders and automatic daily summaries
