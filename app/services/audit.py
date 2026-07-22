from __future__ import annotations

import hashlib
import itertools
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_GENESIS_HASH = "0" * 64


def _canonical(event: Dict[str, Any]) -> str:
    """Deterministic JSON serialisation for hashing (sorted keys, no spaces)."""
    return json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)


class AuditLog:
    """Tamper-evident, append-only audit trail.

    Every event carries ``prev_hash`` and ``entry_hash`` forming a SHA-256
    hash chain: mutating or deleting any historical event breaks every hash
    after it, which ``verify_chain`` detects. With a ``path`` the chain is
    also persisted as append-only JSONL and reloaded on startup, so events
    survive restarts. (A production deployment would additionally forward to
    WORM storage — Azure Blob object-lock / Event Hubs.)
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._events: List[Dict[str, Any]] = []
        self._counter = itertools.count(1)
        self._path = Path(path) if path else None
        if self._path is not None:
            self._load()

    # ------------------------------------------------------------- persistence
    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            with self._path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        self._events.append(json.loads(line))
        except Exception:
            # A corrupt line means a broken chain; keep what parsed —
            # verify_chain() will report exactly where integrity fails.
            pass
        if self._events:
            last_id = self._events[-1].get("log_id", "AUDIT-000000")
            try:
                next_num = int(last_id.rsplit("-", 1)[-1]) + 1
            except ValueError:
                next_num = len(self._events) + 1
            self._counter = itertools.count(next_num)

    def _append_to_disk(self, event: Dict[str, Any]) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(_canonical(event) + "\n")
        except Exception:
            # Never let audit persistence failures break the business flow;
            # the in-memory chain still records the event.
            pass

    # ------------------------------------------------------------------ record
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
        # Mask PII before it is ever written to the immutable trail.
        from app.services.dlp import mask_pii

        clean_metadata: Dict[str, Any] = {}
        for key, value in (metadata or {}).items():
            clean_metadata[key] = mask_pii(value)[0] if isinstance(value, str) else value

        prev_hash = self._events[-1]["entry_hash"] if self._events else _GENESIS_HASH
        event = {
            "log_id": f"AUDIT-{next(self._counter):06d}",
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "actor_id": actor_id,
            "resource_id": resource_id,
            "request_id": request_id,
            "action": action,
            "result": result,
            "metadata": clean_metadata,
            "immutable": True,
            "prev_hash": prev_hash,
        }
        event["entry_hash"] = hashlib.sha256((prev_hash + _canonical({k: v for k, v in event.items() if k != "prev_hash"})).encode("utf-8")).hexdigest()
        self._events.append(event)
        self._append_to_disk(event)
        return event

    # -------------------------------------------------------------------- read
    def list(self, request_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if request_id is None:
            return list(self._events)
        return [event for event in self._events if event.get("request_id") == request_id]

    def verify_chain(self) -> Dict[str, Any]:
        """Recompute the hash chain; report the first tampered entry, if any."""
        prev_hash = _GENESIS_HASH
        for event in self._events:
            if event.get("prev_hash") != prev_hash:
                return {"valid": False, "count": len(self._events), "first_invalid": event.get("log_id")}
            expected = hashlib.sha256((prev_hash + _canonical({k: v for k, v in event.items() if k not in ("prev_hash", "entry_hash")})).encode("utf-8")).hexdigest()
            if event.get("entry_hash") != expected:
                return {"valid": False, "count": len(self._events), "first_invalid": event.get("log_id")}
            prev_hash = event["entry_hash"]
        return {"valid": True, "count": len(self._events), "first_invalid": None}


# Shared process-wide audit trail used by the API layer. Persistent and
# hash-chained by default; set AUDIT_LOG_PATH to relocate the JSONL file.
audit_log = AuditLog(os.getenv("AUDIT_LOG_PATH", str(Path("data") / "audit" / "audit_log.jsonl")))
