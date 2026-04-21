"""Gmail API service layer.

Handles OAuth 2.0 token refresh, email fetching, attachment saving, and
unread-count polling. All functions are synchronous — callers must run them
in an executor from async contexts.
"""
from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build, Resource
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


@dataclass
class AttachmentInfo:
    filename: str
    size_bytes: int
    local_path: str


@dataclass
class EmailData:
    id: str
    sender: str
    subject: str
    date: str
    body_text: str
    attachments: list[AttachmentInfo] = field(default_factory=list)


class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(self._parts)


def _strip_html(html: str) -> str:
    s = _HTMLStripper()
    s.feed(html)
    return s.get_text()


def _token_path(credentials_path: str) -> Path:
    return Path(credentials_path).parent / "token.json"


def load_gmail_service(credentials_path: str) -> Resource:
    """Build an authenticated Gmail API resource, auto-refreshing token.json.

    Raises FileNotFoundError if token.json is missing — run scripts/gmail_auth.py first.
    Raises RuntimeError if the token is invalid and cannot be refreshed.
    """
    token_file = _token_path(credentials_path)

    if not token_file.exists():
        raise FileNotFoundError(
            f"token.json not found at {token_file}. "
            "Run scripts/gmail_auth.py to generate it."
        )

    creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_file.write_text(creds.to_json())
        else:
            raise RuntimeError(
                "Gmail token is invalid and cannot be refreshed. "
                "Re-run scripts/gmail_auth.py."
            )

    return build("gmail", "v1", credentials=creds)


def _decode_base64(data: str) -> str:
    return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")


def _extract_body_and_attachments(payload: dict) -> tuple[str, list[dict]]:
    """Recursively extract (body_text, attachment_parts) from a message payload."""
    mime_type = payload.get("mimeType", "")
    parts = payload.get("parts", [])

    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return (_decode_base64(data) if data else "", [])

    if mime_type == "text/html":
        data = payload.get("body", {}).get("data", "")
        return (_strip_html(_decode_base64(data)) if data else "", [])

    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachment_parts: list[dict] = []

    for part in parts:
        sub_mime = part.get("mimeType", "")
        body = part.get("body", {})
        filename = part.get("filename", "")

        if filename:
            attachment_parts.append(part)
            continue

        if sub_mime == "text/plain":
            data = body.get("data", "")
            if data:
                plain_parts.append(_decode_base64(data))
        elif sub_mime == "text/html":
            data = body.get("data", "")
            if data:
                html_parts.append(_strip_html(_decode_base64(data)))
        elif sub_mime.startswith("multipart/"):
            sub_body, sub_atts = _extract_body_and_attachments(part)
            if sub_body:
                plain_parts.append(sub_body)
            attachment_parts.extend(sub_atts)

    body_text = " ".join(plain_parts) or " ".join(html_parts)
    return body_text, attachment_parts


def fetch_unread(
    service: Resource,
    label: str,
    limit: int,
    sender_filter: str | None,
    attachments_dir: str,
) -> list[EmailData]:
    """Fetch unread emails, save attachments locally, and mark each as read."""
    query = "is:unread"
    if sender_filter:
        query += f" from:{sender_filter}"

    try:
        result = (
            service.users()
            .messages()
            .list(userId="me", labelIds=[label], q=query, maxResults=limit)
            .execute()
        )
    except HttpError as e:
        logger.error("Gmail messages.list error: %s", e)
        raise

    messages = result.get("messages", [])
    att_dir = Path(attachments_dir)
    att_dir.mkdir(parents=True, exist_ok=True)
    emails: list[EmailData] = []

    for msg_meta in messages:
        msg_id = msg_meta["id"]
        try:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=msg_id, format="full")
                .execute()
            )
        except HttpError as e:
            logger.error("Gmail messages.get %s error: %s", msg_id, e)
            continue

        headers = {
            h["name"].lower(): h["value"]
            for h in msg["payload"].get("headers", [])
        }
        sender = headers.get("from", "")
        subject = headers.get("subject", "(no subject)")
        date_str = headers.get("date", "")

        body_text, att_parts = _extract_body_and_attachments(msg["payload"])

        attachments: list[AttachmentInfo] = []
        for att_part in att_parts:
            filename = att_part.get("filename", "attachment")
            att_body = att_part.get("body", {})
            size_bytes = att_body.get("size", 0)
            att_id = att_body.get("attachmentId")
            saved_path = ""

            if att_id:
                try:
                    att_data = (
                        service.users()
                        .messages()
                        .attachments()
                        .get(userId="me", messageId=msg_id, id=att_id)
                        .execute()
                    )
                    raw = base64.urlsafe_b64decode(att_data["data"] + "==")
                    safe_name = re.sub(r"[^\w.\-]", "_", filename)
                    local_path = att_dir / f"{msg_id}_{safe_name}"
                    local_path.write_bytes(raw)
                    size_bytes = len(raw)
                    saved_path = str(local_path)
                    logger.info("Saved attachment: %s (%d bytes)", local_path, size_bytes)
                except HttpError as e:
                    logger.error("Failed to fetch attachment %s: %s", att_id, e)

            attachments.append(AttachmentInfo(filename=filename, size_bytes=size_bytes, local_path=saved_path))

        emails.append(
            EmailData(
                id=msg_id,
                sender=sender,
                subject=subject,
                date=date_str,
                body_text=body_text.strip(),
                attachments=attachments,
            )
        )

        try:
            service.users().messages().modify(
                userId="me",
                id=msg_id,
                body={"removeLabelIds": ["UNREAD"]},
            ).execute()
        except HttpError as e:
            logger.error("Failed to mark message %s as read: %s", msg_id, e)

    return emails


def get_unread_count(service: Resource, label: str) -> int:
    """Return the current unread message count for the given label."""
    try:
        result = service.users().labels().get(userId="me", id=label).execute()
        return result.get("messagesUnread", 0)
    except HttpError as e:
        logger.error("Gmail labels.get error: %s", e)
        return 0
