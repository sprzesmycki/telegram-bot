# Gmail Module Implementation Plan

## Context

The sebson-bot already has a modular Telegram bot system (calories, piano, invoices). This plan adds a Gmail module following the exact same patterns, enabling the user to fetch unread emails via `/emails`, receive auto-notifications when new mail arrives, and handle attachments. The Gmail API uses OAuth 2.0 with token auto-refresh.

The project has **no `requirements.txt`** — dependencies go in `pyproject.toml`.

---

## Files to Create

### `bot/services/gmail.py`
Core Gmail API service (independent of Telegram layer):
- `load_gmail_service(credentials_path: str) -> Resource` — builds an authenticated `googleapiclient.discovery.Resource` using token.json (auto-refresh, saved back on change)
- `@dataclass EmailData` — `id, sender, subject, date, body_text, attachments: list[tuple[str, int]]` (filename, bytes)
- `@dataclass AttachmentInfo` — `filename: str, size_bytes: int, local_path: str`
- `fetch_unread(service, label: str, limit: int, sender_filter: str | None) -> list[EmailData]` — calls `messages.list`, then `messages.get(format='full')` per message; parses MIME parts (prefer `text/plain`, fallback `text/html` stripped); saves attachments to `attachments_dir`; marks each as read via `messages.modify(removeLabelIds=['UNREAD'])`
- `get_unread_count(service, label: str) -> int` — lightweight check for scheduler

SCOPES: `['https://www.googleapis.com/auth/gmail.modify']`
token.json stored alongside credentials.json (derive path from `GMAIL_CREDENTIALS_PATH`).

### `bot/modules/gmail/__init__.py`
`GmailModule` class (same interface as `CaloriesModule`, `PianoModule`):
```python
class GmailModule:
    @property
    def ENABLED(self) -> bool: return get_config().modules.gmail.enabled
    COMMANDS = [("emails", "Fetch unread Gmail messages")]
    def register(self, app) -> None: ...
    def register_scheduled(self, scheduler, bot) -> None: ...
module = GmailModule()
```

### `bot/modules/gmail/handlers/__init__.py`
Empty.

### `bot/modules/gmail/handlers/emails.py`
- `COMMANDS = [("emails", "Fetch unread Gmail messages")]`
- `emails_cmd(update, context)` — parses `/emails [N]`; loads gmail service; calls `fetch_unread(limit=N or cfg.gmail.max_results)`; formats and sends each email; truncates body to ~500 chars with inline "Read more" button if longer
- `read_more_callback(update, context)` — `CallbackQueryHandler(pattern=r'^gmail_read:')` — sends full body text as follow-up message
- `register(app)` — `CommandHandler("emails", emails_cmd)` + `CallbackQueryHandler(read_more_callback, pattern=r'^gmail_read:')`
- Error handling: catches `google.auth.exceptions.TransportError` / `HttpError` / missing credentials with user-friendly messages

### `bot/modules/gmail/scheduled.py`
- `register_all(scheduler, bot)` — adds interval job: every `cfg.gmail.check_interval_minutes` minutes
- Job: `_check_new_mail(bot)` — gets service; calls `get_unread_count()`; compares against a module-level `_last_seen_count: int`; if count increased, calls `fetch_unread()` and sends formatted emails to each profile owner ID (same pattern as `calories/scheduled.py` using `db.get_distinct_profile_owner_ids()`)
- Uses `APScheduler` `IntervalTrigger(minutes=interval)` (not cron — interval is user-configurable)

### `scripts/gmail_auth.py`
Standalone one-shot script for initial OAuth setup:
- Reads `GMAIL_CREDENTIALS_PATH` from env (defaults to `./credentials.json`)
- Runs `google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file(...).run_local_server(port=0)`
- Saves resulting token to `token.json` next to credentials.json
- Prints confirmation and instructions

---

## Files to Modify

### `bot/config.py`
1. Add `@dataclass GmailModuleConfig`:
   ```python
   @dataclass
   class GmailModuleConfig:
       enabled: bool
       check_interval_minutes: int
       max_results: int
       label: str
   ```
2. Add `gmail: GmailModuleConfig` field to `ModulesConfig`
3. Add `gmail_attachments_dir: str` to `StorageConfig`
4. In `load_config()`: parse `mod_sec.get("gmail", {})` and `stor_sec.get("gmail_attachments_dir", "./data/gmail_attachments")`; read `GMAIL_CREDENTIALS_PATH` env var for secrets

### `config.yaml`
Add under `storage:`:
```yaml
  gmail_attachments_dir: "./data/gmail_attachments"
```
Add under `modules:`:
```yaml
  gmail:
    enabled: false
    check_interval_minutes: 5
    max_results: 10
    label: "INBOX"
```

### `bot/modules/__init__.py`
Add after the invoices block:
```python
if cfg.modules.gmail.enabled:
    from bot.modules.gmail import module as gmail_module
    modules.append(gmail_module)
    logger.info("Module loaded: gmail")
else:
    logger.info("Module disabled: gmail")
```

### `pyproject.toml`
Add to `dependencies`:
```
"google-auth>=2.0",
"google-auth-oauthlib>=1.0",
"google-api-python-client>=2.0",
```

### `.env.example`
Add:
```
GMAIL_CREDENTIALS_PATH=./credentials.json
```

### `.gitignore`
Add:
```
credentials.json
token.json
```

### `README.md`
Add a **Gmail API Setup** section covering:
1. Create GCP project + enable Gmail API
2. Create OAuth 2.0 credentials (Desktop app type) → download `credentials.json`
3. Run `python scripts/gmail_auth.py` once to generate `token.json`
4. Set `GMAIL_CREDENTIALS_PATH` in `.env`
5. Enable module in `config.yaml`: `modules.gmail.enabled: true`

---

## Implementation Notes

- `fetch_unread` marks emails as read immediately after fetching (per requirements). The `/emails` command and the scheduler both do this.
- `body_text` prefers `text/plain` MIME part; falls back to stripping HTML tags from `text/html` using stdlib `html.parser` (beautifulsoup4 already in project but stdlib is simpler here).
- Attachments saved to `./data/gmail_attachments/<msg_id>_<filename>` using the existing `StorageConfig.gmail_attachments_dir`.
- token.json path is derived from `credentials_path` by replacing the filename: `Path(credentials_path).parent / "token.json"`.
- The `read_more_callback` stores the full email body in `context.user_data` (keyed by msg_id) during the `/emails` command to avoid re-fetching from Gmail API.
- The scheduler only sends notifications when `unread_count > _last_seen_count`, minimizing API calls. On bot restart, `_last_seen_count` resets to 0, so all current unread emails will be surfaced.

---

## Verification

1. Copy `credentials.json` to project root, run `python scripts/gmail_auth.py` → confirm `token.json` created
2. Set `modules.gmail.enabled: true` in `config.yaml`, start bot
3. Send yourself an email, run `/emails` → confirm email appears with sender/subject/body (truncated if long)
4. Run `/emails 3` → confirm limit respected
5. If email body > 500 chars, confirm "Read more" button appears and shows full text on click
6. Confirm email is marked as read in Gmail after `/emails`
7. Wait for scheduler interval → confirm auto-notification arrives in Telegram
8. Confirm `token.json` is gitignored (`git status` shows it as untracked, not staged)
