"""Client notifications — status-change alerts for the external portal.

Phase 6b.5: "notify the client by email when their status changes." Clients
are not watching a dashboard, so each transition (received → documents needed
→ under review → decision) pushes a plain-language message to the client's
contact email. As everywhere on the client side, the message carries only the
client-safe status — never risk scores, screening results, or reasoning.

Delivery is pluggable behind ``NOTIFY_CHANNEL``:

- ``outbox`` (default) — dev sink: appended to an in-memory outbox and written
  to the signed audit trail, so the flow is fully testable without a mailer.
- ``email``  — production: hand off to a real transport (Azure Communication
  Services / SMTP). Wired as a clearly-marked stub so prod swaps in one place.

Every send is audited as a ``client_notification`` event regardless of channel.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Dev sink. A real deployment keeps no such buffer — the transport owns delivery.
_outbox: List[Dict[str, Any]] = []


def _channel() -> str:
    return os.getenv("NOTIFY_CHANNEL", "outbox").lower()


def _deliver_email(message: Dict[str, Any]) -> None:
    """Production email transport (Azure Communication Services / SMTP).

    Intentionally a stub: prod wires the real client here. Kept isolated so the
    rest of the platform depends only on ``send`` and never on a mail library.
    """
    raise NotImplementedError(
        "Real email delivery is not configured. Set NOTIFY_CHANNEL=outbox for "
        "dev, or implement _deliver_email against Azure Communication Services."
    )


def send(
    recipient: str,
    subject: str,
    body: str,
    *,
    application_id: Optional[str] = None,
    status: Optional[str] = None,
    audit_log: Any = None,
) -> Dict[str, Any]:
    """Send a client-safe notification and audit it. Never raises to the caller.

    A delivery failure must not break the onboarding pipeline that triggered
    it, so transport errors are swallowed and recorded as a failed send.
    """
    message = {
        "to": recipient,
        "subject": subject,
        "body": body,
        "application_id": application_id,
        "status": status,
        "channel": _channel(),
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    result = "sent"
    if _channel() == "email":
        try:
            _deliver_email(message)
        except Exception:
            result = "failed"
    else:
        _outbox.append(message)

    message["result"] = result
    if audit_log is not None:
        audit_log.record(
            event_type="client_notification",
            actor_id="client-portal:notifier",
            action="notify_status_change",
            result=result,
            resource_id=application_id,
            metadata={"status": status or "", "channel": message["channel"]},
        )
    return message


def outbox() -> List[Dict[str, Any]]:
    """Dev/test view of everything the outbox channel has captured."""
    return list(_outbox)


def clear_outbox() -> None:
    _outbox.clear()
