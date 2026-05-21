"""Send weekly report emails through Resend."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

LOGGER = logging.getLogger(__name__)
RESEND_API_URL = "https://api.resend.com/emails"
DEFAULT_TIMEOUT_SECONDS = float(os.getenv("RESEND_TIMEOUT_SECONDS", "30"))


class SendAlreadyRecordedError(RuntimeError):
    """Raised when a caller attempts to send a week that was already recorded."""


class SendRecorder(Protocol):
    """Minimal DB adapter expected by this module."""

    def email_already_sent(self, week_start: str) -> bool: ...

    def record_email_sent(
        self,
        *,
        week_start: str,
        week_end: str,
        recipient: str,
        message_id: str | None,
    ) -> None: ...


@dataclass(frozen=True)
class DatabaseSendRecorder:
    """Adapter for the ``src.db`` email send idempotency functions."""

    database_path: str | Path

    def email_already_sent(self, week_start: str) -> bool:
        from src.db import has_email_been_sent

        return has_email_been_sent(self.database_path, week_start)

    def record_email_sent(
        self,
        *,
        week_start: str,
        week_end: str,
        recipient: str,
        message_id: str | None,
    ) -> None:
        from src.db import record_email_sent

        recorded = record_email_sent(
            self.database_path,
            week_start,
            week_end,
            recipient,
            message_id=message_id,
        )
        if not recorded:
            LOGGER.info("Email send for week %s was already recorded", week_start)


@dataclass(frozen=True)
class EmailSendResult:
    sent: bool
    skipped: bool
    message_id: str | None = None
    error: str | None = None


def send_weekly_email(
    *,
    html: str,
    subject: str,
    sender: str | None = None,
    recipients: str | Sequence[str] | None = None,
    api_key: str | None = None,
    week_start: str | None = None,
    week_end: str | None = None,
    recorder: SendRecorder | None = None,
    force: bool = False,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    config: Any | None = None,
    idempotency_key: str | None = None,
) -> EmailSendResult:
    """Send an HTML email and record it only after Resend accepts the request."""

    resolved_sender = sender or _config_value(config, "email_from") or os.getenv("EMAIL_FROM")
    resolved_recipients = normalize_recipients(
        recipients or _config_value(config, "email_to") or os.getenv("EMAIL_TO")
    )
    resolved_api_key = api_key or _config_value(config, "resend_api_key") or os.getenv("RESEND_API_KEY")
    resolved_timeout = float(_config_value(config, "resend_timeout_seconds", default=timeout_seconds))
    resolved_force = bool(force or _config_value(config, "force", default=False))

    if recorder and week_start and not resolved_force and recorder.email_already_sent(week_start):
        return EmailSendResult(sent=False, skipped=True, error="email already sent for this week")

    _validate_email_request(
        html=html,
        subject=subject,
        sender=resolved_sender,
        recipients=resolved_recipients,
        api_key=resolved_api_key,
    )

    message_id = _send_with_resend_http(
        api_key=resolved_api_key or "",
        sender=resolved_sender or "",
        recipients=resolved_recipients,
        subject=subject,
        html=html,
        idempotency_key=idempotency_key,
        timeout_seconds=resolved_timeout,
    )

    if recorder and week_start and week_end:
        recorder.record_email_sent(
            week_start=week_start,
            week_end=week_end,
            recipient=",".join(resolved_recipients),
            message_id=message_id,
        )

    return EmailSendResult(sent=True, skipped=False, message_id=message_id)


def normalize_recipients(recipients: str | Sequence[str] | None) -> list[str]:
    if recipients is None:
        return []
    if isinstance(recipients, str):
        parts = recipients.replace(";", ",").split(",")
    else:
        parts = list(recipients)
    return [str(part).strip() for part in parts if str(part).strip()]


def _validate_email_request(
    *,
    html: str,
    subject: str,
    sender: str | None,
    recipients: Sequence[str],
    api_key: str | None,
) -> None:
    missing = []
    if not api_key:
        missing.append("RESEND_API_KEY")
    if not sender:
        missing.append("EMAIL_FROM")
    if not recipients:
        missing.append("EMAIL_TO")
    if not subject:
        missing.append("subject")
    if not html:
        missing.append("html")
    if missing:
        raise ValueError(f"Missing required email settings: {', '.join(missing)}")


def _send_with_resend_http(
    *,
    api_key: str,
    sender: str,
    recipients: Sequence[str],
    subject: str,
    html: str,
    idempotency_key: str | None,
    timeout_seconds: float,
) -> str | None:
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("requests package is not installed") from exc

    payload = {
        "from": sender,
        "to": list(recipients),
        "subject": subject,
        "html": html,
    }
    response = requests.post(
        RESEND_API_URL,
        headers=_build_resend_headers(api_key, idempotency_key),
        json=payload,
        timeout=timeout_seconds,
    )

    if response.status_code >= 400:
        raise RuntimeError(_resend_error_message(response))

    try:
        body: Mapping[str, Any] = response.json()
    except ValueError:
        LOGGER.warning("Resend returned non-JSON success response")
        return None
    message_id = body.get("id")
    return str(message_id) if message_id else None


def _build_resend_headers(api_key: str, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key[:256]
    return headers


def _resend_error_message(response: Any) -> str:
    try:
        body = response.json()
    except ValueError:
        body = response.text
    return f"Resend send failed with HTTP {response.status_code}: {body}"


def _config_value(config: Any | None, name: str, default: Any = None) -> Any:
    if config is None:
        return default
    if isinstance(config, Mapping):
        value = config.get(name, default)
        return default if value is None else value
    value = getattr(config, name, default)
    return default if value is None else value


if __name__ == "__main__":
    raise SystemExit("Use send_weekly_email() from src.main after rendering the report.")
