"""Phase 5 tests: event bus and agent event publishing."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.api import app
from app.services.event_bus import (
    TOPIC_CLIENT_ONBOARDED,
    TOPIC_REVIEW_ESCALATED,
    InProcessEventBus,
    event_bus,
)

client = TestClient(app)


def test_publish_reaches_subscriber_and_history():
    bus = InProcessEventBus()
    received = []
    bus.subscribe("test.topic", received.append)
    event = bus.publish("test.topic", {"hello": "world"})
    assert received == [event]
    assert bus.recent("test.topic")[-1]["payload"] == {"hello": "world"}


def test_subscriber_failure_does_not_break_publisher():
    bus = InProcessEventBus()

    def bad_handler(event):
        raise RuntimeError("boom")

    bus.subscribe("test.topic", bad_handler)
    event = bus.publish("test.topic", {"ok": True})
    assert event["payload"] == {"ok": True}


def test_onboarding_publishes_events_end_to_end():
    before = len(event_bus.recent(TOPIC_CLIENT_ONBOARDED, limit=200))
    response = client.post(
        "/api/agents/onboarding/kyc",
        json={"clientName": "Red Star Trading", "jurisdiction": "IR", "requestId": "REQ-EVT-1"},
    )
    assert response.status_code == 200
    onboarded = event_bus.recent(TOPIC_CLIENT_ONBOARDED, limit=200)
    assert len(onboarded) == before + 1
    assert onboarded[-1]["payload"]["request_id"] == "REQ-EVT-1"
    escalations = event_bus.recent(TOPIC_REVIEW_ESCALATED, limit=200)
    assert any(e["payload"]["request_id"] == "REQ-EVT-1" for e in escalations)


def test_escalation_event_lands_on_audit_trail():
    client.post(
        "/api/agents/onboarding/kyc",
        json={"clientName": "Oleg Volkov", "jurisdiction": "GB", "requestId": "REQ-EVT-2"},
    )
    audit = client.get("/api/audit/logs", params={"request_id": "REQ-EVT-2"}).json()
    assert any(e["event_type"] == "review_escalated" for e in audit["audit_events"])


def test_events_endpoint_exposes_recent():
    body = client.get("/api/events/recent", params={"topic": TOPIC_CLIENT_ONBOARDED}).json()
    assert body["count"] >= 1
    assert all(e["topic"] == TOPIC_CLIENT_ONBOARDED for e in body["events"])
