"""Send provider abstraction + Titan (GoDaddy Professional Email) SMTP/IMAP.

Both SMTP and IMAP use ssl.create_default_context() with no plaintext fallback
(non-negotiable, §3/§9). The :class:`SendProvider` ABC means a future
provider (e.g. Smartlead) can be swapped in without touching business logic.

Every external call has an explicit timeout and catches the specific network
exception so the caller (state_machine) can log it without crashing the run.
"""
from __future__ import annotations

import email
import email.utils
import imaplib
import smtplib
import ssl
from abc import ABC, abstractmethod
from email.message import EmailMessage
from typing import Any

import config


class SendProvider(ABC):
    """Interface implemented by every delivery backend."""

    @abstractmethod
    def send(self, to_address: str, subject: str, body: str) -> str:
        """Send a message. Return the server Message-ID for correlation."""

    @abstractmethod
    def fetch_unread(self) -> list[dict[str, Any]]:
        """Return new/unseen messages as parsed dicts for status polling."""


class SMTPProvider(SendProvider):
    """Raw SSL SMTP send + IMAP poll against Titan endpoints."""

    def __init__(
        self,
        smtp_host: str | None = None,
        smtp_port: int | None = None,
        imap_host: str | None = None,
        imap_port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        from_address: str | None = None,
        from_name: str | None = None,
    ) -> None:
        self.smtp_host = smtp_host or config.SMTP_HOST
        self.smtp_port = smtp_port or config.SMTP_PORT
        self.imap_host = imap_host or config.IMAP_HOST
        self.imap_port = imap_port or config.IMAP_PORT
        self.user = user or config.EMAIL_USER
        self.password = password or config.EMAIL_PASSWORD
        self.from_address = from_address or config.FROM_ADDRESS
        self.from_name = from_name or config.FROM_NAME
        self._ctx = ssl.create_default_context()  # shared, SSL-only context
        self._smtp_timeout = config.SMTP_TIMEOUT
        self._imap_timeout = config.IMAP_TIMEOUT

        if not self.password:
            raise RuntimeError("EMAIL_PASSWORD is not configured")

    # -- SMTP --------------------------------------------------------------
    def send(self, to_address: str, subject: str, body: str) -> str:
        msg = EmailMessage()
        msg["From"] = f"{self.from_name} <{self.from_address}>"
        msg["To"] = to_address
        msg["Subject"] = subject
        msg.set_content(body)

        # Explicitly generate a deterministic Message-ID header before sending
        domain = self.from_address.split("@")[-1] if "@" in self.from_address else "nyayaworks.in"
        msg_id = email.utils.make_msgid(domain=domain)
        msg["Message-ID"] = msg_id

        try:
            with smtplib.SMTP_SSL(
                self.smtp_host,
                self.smtp_port,
                context=self._ctx,
                timeout=self._smtp_timeout,
            ) as server:
                server.login(self.user, self.password)
                server.send_message(msg)
        except smtplib.SMTPException as exc:
            raise RuntimeError(f"SMTP send failed: {exc}") from exc
        except OSError as exc:
            raise RuntimeError(f"SMTP socket error: {exc}") from exc

        # Return the generated msg_id so state_machine records it in Column L!
        return msg_id

    # -- IMAP --------------------------------------------------------------
    def fetch_unread(self) -> list[dict[str, Any]]:
        """Fetch unseen messages as parsed dicts (raw + mime)."""
        messages: list[dict[str, Any]] = []
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