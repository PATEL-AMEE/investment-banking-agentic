from __future__ import annotations

import itertools
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class AuditLog:
    """In-memory, append-only audit trail.

    Mirrors the ``audit_logger`` tool from the design docs: every decision or
    significant action is recorded as an immutable event with a timestamp,
    actor, action, result and free-form metadata. The free-tier prototype keeps
    events in memory; a production deployment would forward them to an immutable
    sink (Azure Event Hub / Blob object-lock).
    """

    def __init__(self) -> None:
        self._events: List[Dict[str, Any]] = []
        self._counter = itertools.count(1)

    def record(
        self,
        event_type: str,
        actor_id: str,
        action: str,
        result: str = "success",
        resource_id: str | None = None,
        request_id: str | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        event = {
            "log_id": f"AUDIT-{next(self._counter):06d}",
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "actor_id": actor_id,
            "resource_id": resource_id,
            "request_id": request_id,
            "action": action,
            "result": result,
            "metadata": metadata or {},
            "immutable": True,
        }
        self._events.append(event)
        return event

    def list(self, request_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if request_id is None:
            return list(self._events)
        return [event for event in self._events if event.get("request_id") == request_id]


# Shared process-wide audit trail used by the API layer.
audit_log = AuditLog()
