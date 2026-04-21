Add a Gmail module to the existing Telegram bot (Python).

## Requirements

**Gmail Authorization:**
- Use Gmail API with OAuth 2.0 (`google-auth`, `google-api-python-client`)
- Store credentials in `credentials.json` (downloaded from Google Cloud Console)
- Auto-refresh token and persist it in `token.json`

**Module functionality (`gmail_module.py`):**
- Fetch unread emails from INBOX
- Parse: sender, subject, date, body (plain text + HTML fallback)
- Handle attachments – save locally + report filename and size
- Mark emails as read after fetching
- Filter by label or sender (optional parameters)
- Configurable fetch limit per call (default 10)

**Telegram bot integration:**
- `/emails` command – fetch and send unread emails to chat
- `/emails 5` – fetch last N emails
- Auto-notifications every X minutes (configurable) when new mail arrives
- Truncate long emails to ~500 chars with a "read more" option
- Handle API errors with clear messages

**Config (`config.py` or `.env`):**
- `GMAIL_CHECK_INTERVAL` – polling interval in minutes
- `GMAIL_MAX_RESULTS` – default fetch limit
- `GMAIL_LABEL` – label filter (default: `INBOX`)
- `GMAIL_CREDENTIALS_PATH` – path to credentials.json


**Additional:**
- Add all new dependencies to `requirements.txt`
- Update `.gitignore` with `credentials.json` and `token.json`
- Add a "Gmail API Setup" section to README.md covering: GCP project creation, enabling Gmail API, downloading credentials
- Use type hints throughout and log via the `logging` module

## Context

Review the existing project structure and match the code style, naming conventions, and handler registration pattern of the existing bot. Do not overwrite existing files – extend them or import the new module where appropriate.