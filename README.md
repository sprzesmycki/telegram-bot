# Calorie, Supplement & Piano Practice Tracker — Telegram Bot

A Telegram bot that tracks daily calorie intake (via photo or text), manages supplement reminders, and coaches piano practice (streaks, repertoire, voice-note analysis). Supports multiple profiles per account.

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
| `/today [@name\|@both]` | List today's meals & drinks with inline ❌ delete buttons |
| `/summary [@name]` | Today's meal summary |
| `/week [@name]` | Last 7 days overview |
| `/report [@name] [YYYY-MM-DD]` | Dietitian-ready daily report |
| `/goal <kcal> [@name]` | Set daily calorie target (resets macros) |
| `/stats [@name]` | Calculate BMR, TDEE and macro goals from profile data |
| `/profile add\|list\|switch\|delete\|set <name>` | Manage profiles and attributes (height, weight, etc.) |
| `/supplement add\|list\|done\|remove <name> [HH:MM]` | Manage supplements |
| `/piano` | Piano summary: streak, pieces in progress, last session |
| `/piano log [N min] [pieces…]` | Log today's practice; updates streak |
| `/piano checkin [note]` | LLM coaching check-in (cheap model) |
| `/piano pieces` | List your repertoire grouped by status |
| `/piano piece add\|status\|note\|remove <title>` | Manage pieces |
| `/piano analyze [piece title]` | Analyse the last voice note you sent (feedback) |
| `/piano history [N]` / `/piano stats` | Recent sessions / totals |
| `/model [openrouter\|local\|custom] [model-name]` | View or switch LLM provider |

## Sample Commands

A realistic end-to-end flow. Run these in order against your bot in Telegram:

**1. Create and configure profiles**

```
/profile add Seba
/profile add Wife
/profile list
/profile switch Wife
```

Set up physical attributes to auto-calculate nutritional needs:

```
/profile set height 180 @Seba
/profile set weight 85 @Seba
/profile set age 30 @Seba
/profile set gender male @Seba
/profile set activity moderate @Seba

/stats @Seba
```

The `/stats` command uses the Mifflin-St Jeor equation to calculate BMR and TDEE, then sets daily targets: 2.0g/kg protein, 1.0g/kg fat, and remaining calories from carbs.

The first `/profile add` auto-creates a default `Me` profile alongside yours, so after `add Seba` you'll have both `Me` and `Seba`. Names are case-sensitive — `@wife` won't match `Wife`.

**2. Set calorie goals**

```
/goal 2200
/goal 1800 @Wife
```

Setting a manual `/goal` resets macro targets to "untracked" mode (calories only).

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
Scrambled eggs with toast
Jajecznica z tostem
420 kcal | P: 22g | C: 35g | F: 18g

Reply /yes to log, or send a remark to refine.
Example: "actually larger portion" or "add a tablespoon of butter".
```

Descriptions are always bilingual (English / Polish) in separate lines. To approve, reply `/yes`. To adjust, send a plain-text remark and the bot re-analyses:

```
you: /cal scrambled eggs
bot: Preview — ... Scrambled eggs ... 180 kcal ...
you: that's 3 eggs with butter and a slice of cheddar
bot: Preview — ... Scrambled eggs with butter and cheddar ... 350 kcal ...
you: /yes
bot: [Me] Logged: Scrambled eggs with butter and cheddar
Jajecznica z masłem i cheddarem ...
Daily: 350 / 2200 kcal (1850 remaining)
P: 25 / 170g | C: 5 / 230g | F: 25 / 85g
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
/today
/today @both
/summary
/summary @Wife
/week
/report @Seba 2026-04-11
```

`/today` lists every meal and drink logged today for the targeted profile, numbered and ordered by time, with an inline `❌ N` button per entry. Tapping one hard-deletes that row (from SQLite and the Postgres mirror) and re-renders the message with updated totals — useful for backing out a mistaken `/cal` or recipe log.

**6. Supplements**

```
/supplement add Vitamin_D 09:00
/supplement add Omega_3 21:00
/supplement list
/supplement done Vitamin_D
/supplement remove Omega_3
```

Supplement names cannot contain spaces — use underscores (`Vitamin_D`, not `Vitamin D`). The reminder fires daily at the given `HH:MM`.

**7. Piano practice**

```
/piano piece add Chopin Nocturne by Chopin
/piano piece add Bach Invention 1 by Bach
/piano pieces
/piano log 30 min Chopin Nocturne, scales
/piano log                           # prompts: reply "25 min Bach" to log
/piano checkin
/piano history
/piano stats
```

Send a voice note of your playing, then reply `/piano analyze` (optionally with a piece title) — the bot sends the raw audio to a multimodal model which listens directly and returns structured feedback (tempo, rhythm, dynamics, problem areas, next-session focus). Daily practice fires a check-in at `PIANO_CHECKIN_TIME` (default 19:00), skipping days you've already logged.

Two LLM tiers keep costs low: `PIANO_CHAT_MODEL` handles check-ins and log encouragement (cheap/fast text), and `PIANO_ANALYSIS_MODEL` is only called by `/piano analyze`. The analysis model **must accept audio input** via OpenAI-style `input_audio` content blocks — defaults to `google/gemini-2.0-flash-001` (which accepts Telegram's ogg/opus directly). Both are set independently from `/model` so regular `/cal` flow is unaffected.

**8. Switch LLM provider**

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
- **Scheduler:** APScheduler for supplement reminders, daily calorie summaries, and daily piano check-ins
