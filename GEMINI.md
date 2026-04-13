# Telegram Calorie & Supplement Tracker

A sophisticated Telegram bot for tracking daily calorie intake (via text/photo) and managing supplement reminders. It supports multiple profiles per user and uses LLMs (OpenRouter/Ollama/Custom) for intelligent vision and text analysis of meals and recipes.

## Project Overview

*   **Primary Technologies:** Python 3.14+, `python-telegram-bot`, `uv` package manager.
*   **LLM Integration:** OpenAI-compatible API (OpenRouter, local Ollama, or custom endpoints) for meal analysis, recipe parsing, and bilingual description generation (EN/PL).
*   **Architecture:** Modular handler-based system with a dual-database mirror pattern (SQLite source of truth + PostgreSQL mirror).
*   **Key Features:**
    *   **Meal Tracking:** `/cal <desc> [@name|@both] [at HH:MM]` with photo/text vision analysis.
    *   **Recipe Analysis:** `/recipe <URL|text> [for N] [@name|@both]`.
    *   **Interactive Flow:** Analysis results are presented as previews; users can refine via text or confirm with `/yes`.
    *   **Multi-Profile Support:** Manage profiles with `/profile`; target specific profiles or all (`@both`) in commands.
    *   **Supplements:** Reminders and logs via `/supplement`.
    *   **Reports:** Daily summaries and weekly reports via `/summary`, `/week`, and `/report`.
    *   **Nutrition Stats:** `/stats [@name]` calculates BMR, TDEE, and macro targets based on profile data.

## Building and Running

### Prerequisites
*   Python 3.14+
*   [uv](https://docs.astral.sh/uv/) package manager
*   Telegram Bot Token
*   LLM API Key (OpenRouter or equivalent)

### Setup
1.  **Install Dependencies:**
    ```bash
    uv sync
    ```
2.  **Environment Configuration:**
    Create a `.env` file at `~/.config/telegrambot/.env`. See `.env.example` for available options.
    ```env
    TELEGRAM_BOT_TOKEN=your_token
    OPENROUTER_API_KEY=your_key
    ```
3.  **Run the Bot:**
    ```bash
    uv run python main.py
    ```

### Testing
There is currently no test suite or linter configured for this project.

## Development Conventions

### Architecture & Patterns
*   **Dual-DB Mirror:** SQLite (`aiosqlite`) is the primary database. PostgreSQL (`asyncpg`) is a best-effort mirror. All write operations must call `db_sqlite.log_*` followed by a silent `db_postgres.mirror_*` call.
*   **Entry Point:** `main.py` initializes the `Application`, registers handlers from `bot/handlers/`, and manages startup/shutdown via `post_init`/`post_shutdown` hooks.
*   **Owner Isolation:** Every database query MUST filter by `owner_user_id` to ensure data privacy between different Telegram users.
*   **Profile Targeting:** Use `get_target_profiles(owner_id, text)` from `bot/handlers/profiles.py` to resolve `@name` or `@both` syntax.
*   **Extended Profiles:** Profiles include height, weight, age, gender, and activity level, set via `/profile set`.
*   **Nutrition Logic:** `bot/utils/nutrition.py` uses Mifflin-St Jeor for BMR/TDEE and standard macro splits (Protein: 2g/kg, Fat: 1g/kg).
*   **Meal Analysis Flow:** `/cal` or `/recipe` stashes a `pending_meal` in `context.user_data`. Plain text responses trigger refinement; `/yes` triggers final logging.
*   **Bilingual Content:** LLM prompts are designed to return bilingual descriptions (`en / pl`). These are stored as a single string in the database.
*   **Photo Processing:** Photos are compressed (Pillow) to ≤1920px and ≤512KB before being sent to the LLM or saved to `./data/photos/`.

### Directory Structure
*   `bot/handlers/`: Telegram command and message handlers.
*   `bot/services/`: Database (SQLite/PG), LLM client, and Scheduler logic.
*   `bot/utils/`: Formatting, logging, nutrition calculations, and storage utilities.
*   `migrations/`: SQL initialization scripts.
*   `data/`: Runtime data (SQLite DB, photos, logs) - gitignored.

### LLM Providers
The bot supports runtime switching between `openrouter`, `local` (Ollama), and `custom` providers using the `/model` command. All providers use the OpenAI-compatible `AsyncOpenAI` client.
