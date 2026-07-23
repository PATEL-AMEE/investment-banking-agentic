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


# --------------------------------------------------- Kafka connection config
def test_kafka_config_plain_local_broker():
    from app.services.event_bus import kafka_connection_config

    config = kafka_connection_config(servers="localhost:9092", connection_string="")
    assert config["bootstrap_servers"] == ["localhost:9092"]
    assert "security_protocol" not in config  # plaintext dev broker


def test_kafka_config_event_hubs_connection_string():
    from app.services.event_bus import kafka_connection_config

    conn = (
        "Endpoint=sb://ehns-invbank.servicebus.windows.net/;"
        "SharedAccessKeyName=RootManageSharedAccessKey;SharedAccessKey=abc123"
    )
    config = kafka_connection_config(servers="", connection_string=conn)
    # Bootstrap derived from the namespace endpoint, Kafka port 9093.
    assert config["bootstrap_servers"] == ["ehns-invbank.servicebus.windows.net:9093"]
    assert config["security_protocol"] == "SASL_SSL"
    assert config["sasl_mechanism"] == "PLAIN"
    assert config["sasl_plain_username"] == "$ConnectionString"
    assert config["sasl_plain_password"] == conn


def test_kafka_config_explicit_servers_win_over_derived():
    from app.services.event_bus import kafka_connection_config

    conn = "Endpoint=sb://ehns-invbank.servicebus.windows.net/;SharedAccessKey=abc"
    config = kafka_connection_config(servers="broker-a:9093,broker-b:9093", connection_string=conn)
    assert config["bootstrap_servers"] == ["broker-a:9093", "broker-b:9093"]
    assert config["security_protocol"] == "SASL_SSL"


def test_kafka_config_generic_sasl_env(monkeypatch):
    from app.services.event_bus import kafka_connection_config

    monkeypatch.setenv("KAFKA_SECURITY_PROTOCOL", "SASL_SSL")
    monkeypatch.setenv("KAFKA_SASL_MECHANISM", "SCRAM-SHA-256")
    monkeypatch.setenv("KAFKA_SASL_USERNAME", "svc-agentic")
    monkeypatch.setenv("KAFKA_SASL_PASSWORD", "s3cret")
    config = kafka_connection_config(servers="kafka.internal:9093", connection_string="")
    assert config["security_protocol"] == "SASL_SSL"
    assert config["sasl_mechanism"] == "SCRAM-SHA-256"
    assert config["sasl_plain_username"] == "svc-agentic"
    assert config["sasl_plain_password"] == "s3cret"
