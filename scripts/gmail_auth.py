#!/usr/bin/env python3
"""One-shot OAuth 2.0 setup for the Gmail module.

Run this once to generate token.json before enabling the Gmail module:
    python scripts/gmail_auth.py

Set GMAIL_CREDENTIALS_PATH in .env (or the environment) to point to your
credentials.json downloaded from Google Cloud Console. Defaults to ./credentials.json.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def main() -> None:
    credentials_path = os.getenv("GMAIL_CREDENTIALS_PATH", "./credentials.json")
    creds_file = Path(credentials_path)

    if not creds_file.exists():
        print(f"ERROR: credentials file not found at {creds_file.resolve()}")
        print(
            "Download OAuth 2.0 credentials (Desktop app type) from Google Cloud Console "
            "and save as credentials.json, or set GMAIL_CREDENTIALS_PATH."
        )
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), SCOPES)
    creds = flow.run_local_server(port=0)

    token_path = creds_file.parent / "token.json"
    token_path.write_text(creds.to_json())

    print(f"✓ token.json saved to {token_path.resolve()}")
    print()
    print("Next steps:")
    print("  1. Set GMAIL_CREDENTIALS_PATH in .env (if not already done)")
    print("  2. Enable the module in config.yaml:")
    print("       modules:")
    print("         gmail:")
    print("           enabled: true")


if __name__ == "__main__":
    main()
