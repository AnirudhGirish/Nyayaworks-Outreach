"""Delivery backend abstraction + Resend HTTPS API & IMAP polling.

Uses the official Resend Python SDK for email dispatch (with Idempotency-Key
header support) and status polling. Retains IMAP reading via ssl.create_default_context()
for detecting human replies landing in the mailbox.
"""
from __future__ import annotations

import email
from abc import ABC, abstractmethod
import imaplib
import ssl
from typing import Any, cast

import config
import resend


class SendProvider(ABC):
    """Interface implemented by every delivery backend."""

    @abstractmethod
    def send(
        self,
        to_address: str,
        subject: str,
        html_body: str,
        text_body: str,
        row_id: str = "",
        attempts: int = 1,
        reply_to: str | None = None,
    ) -> str:
        """Send a message. Return the provider email ID for correlation."""

    @abstractmethod
    def fetch_unread(self) -> list[dict[str, Any]]:
        """Return new/unseen messages as parsed dicts for status polling."""

    @abstractmethod
    def get_email_status(self, email_id: str) -> str | None:
        """Retrieve the last_event status string from Resend for an email ID."""


class ResendProvider(SendProvider):
    """Resend API send & status polling + IMAP reply fetch."""

    def __init__(
        self,
        api_key: str | None = None,
        imap_host: str | None = None,
        imap_port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        from_address: str | None = None,
        from_name: str | None = None,
        reply_to_address: str | None = None,
    ) -> None:
        self.api_key = api_key or config.RESEND_API_KEY
        self.imap_host = imap_host or config.IMAP_HOST
        self.imap_port = imap_port or config.IMAP_PORT
        self.user = user or config.EMAIL_USER
        self.password = password or config.EMAIL_PASSWORD
        self.from_address = from_address or config.FROM_ADDRESS
        self.from_name = from_name or config.FROM_NAME
        self.reply_to_address = reply_to_address or getattr(config, "REPLY_TO_ADDRESS", "founder@nyayaworks.in")

        resend.api_key = self.api_key
        self._ctx = ssl.create_default_context()
        self._imap_timeout = config.IMAP_TIMEOUT

    # -- Resend HTTP API ---------------------------------------------------
    def send(
        self,
        to_address: str,
        subject: str,
        html_body: str,
        text_body: str,
        row_id: str = "",
        attempts: int = 1,
        reply_to: str | None = None,
    ) -> str:
        if not resend.api_key:
            raise RuntimeError("RESEND_API_KEY is not configured")

        reply_to_target = reply_to or self.reply_to_address

        params: dict[str, Any] = {
            "from": f"{self.from_name} <{self.from_address}>",
            "to": [to_address],
            "subject": subject,
            "html": html_body,
            "text": text_body,
        }
        if reply_to_target:
            params["reply_to"] = reply_to_target

        idempotency_key = f"{row_id}_{attempts}" if row_id else ""
        options = {"idempotency_key": idempotency_key} if idempotency_key else None

        try:
            resp = resend.Emails.send(
                cast(resend.Emails.SendParams, params),
                options=cast(resend.Emails.SendOptions, options) if options else None,
            )
            email_id = resp.get("id") if hasattr(resp, "get") else getattr(resp, "id", None)
            if not email_id:
                raise RuntimeError(f"Resend response missing 'id': {resp!r}")
            return str(email_id)
        except Exception as exc:
            raise RuntimeError(f"Resend send failed: {exc}") from exc

    def get_email_status(self, email_id: str) -> str | None:
        if not resend.api_key:
            raise RuntimeError("RESEND_API_KEY is not configured")

        try:
            resp = resend.Emails.get(email_id)
            last_event = resp.get("last_event") if hasattr(resp, "get") else getattr(resp, "last_event", None)
            return str(last_event) if last_event is not None else None
        except Exception as exc:
            raise RuntimeError(f"Resend status lookup failed ({email_id}): {exc}") from exc

    # -- IMAP (Inbound Reply Polling) --------------------------------------
    def fetch_unread(self) -> list[dict[str, Any]]:
        """Fetch unseen messages as parsed dicts (raw + mime)."""
        messages: list[dict[str, Any]] = []
        if not self.password:
            return messages

        try:
            with imaplib.IMAP4_SSL(
                self.imap_host,
                self.imap_port,
                ssl_context=self._ctx,
            ) as imap:
                imap.socket().settimeout(self._imap_timeout)
                imap.login(self.user, self.password)
                imap.select("INBOX")
                status, data = imap.search(None, "UNSEEN")
                if status != "OK" or not data or not data[0]:
                    return messages
                for num in data[0].split():
                    _, msg_data = imap.fetch(num, "(RFC822)")
                    if not msg_data or not isinstance(msg_data[0], tuple):
                        continue
                    raw = msg_data[0][1]
                    if not isinstance(raw, (bytes, bytearray)):
                        continue
                    mime = email.message_from_bytes(raw)
                    messages.append(
                        {
                            "uid": num.decode(),
                            "raw": raw,
                            "mime": mime,
                            "from": mime.get("From", ""),
                            "subject": mime.get("Subject", ""),
                            "message_id": mime.get("Message-ID", ""),
                            "content_type": mime.get_content_type(),
                        }
                    )
                    imap.store(num, "+FLAGS", "\\Seen")
        except (imaplib.IMAP4.error, OSError) as exc:
            raise RuntimeError(f"IMAP fetch failed: {exc}") from exc
        return messages