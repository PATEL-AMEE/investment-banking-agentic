from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AuditEvent:
    event_id: str
    event_type: str
    request_id: str
    user_id: str
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentState:
    request_id: str
    user_id: str
    session_id: Optional[str] = None
    client_id: Optional[str] = None
    tx_data: Optional[Dict[str, Any]] = None
    query: Optional[str] = None
    retrieved_context: List[Dict[str, Any]] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    decision: Optional[str] = None
    confidence: Optional[float] = None
    rationale: Optional[str] = None
    provenance: List[Dict[str, Any]] = field(default_factory=list)
    review_required: bool = False
    audit_events: List[AuditEvent] = field(default_factory=list)
