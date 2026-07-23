"""Event bus for asynchronous agent-to-agent messaging.

Agents publish domain events (``document.ingested``, ``client.onboarded``,
``review.escalated`` …) to named topics; subscribers react without the
publisher knowing about them. Two backends behind one interface:

- ``InProcessEventBus`` — synchronous fan-out inside the API process
  (default; zero infrastructure).
- ``KafkaEventBus`` — publishes to a Kafka-compatible broker (Redpanda
  locally via docker-compose, Azure Event Hubs in the cloud). Enabled by
  setting ``KAFKA_BOOTSTRAP_SERVERS``.

Every event carries a topic, an ISO timestamp, and a JSON payload, so the
in-process and Kafka wire formats are identical.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Protocol

logger = logging.getLogger("event_bus")

Subscriber = Callable[[Dict[str, Any]], None]

# Canonical topic names (Kafka topics map 1:1).
TOPIC_DOCUMENT_INGESTED = "documents.ingested"
TOPIC_CLIENT_ONBOARDED = "clients.onboarded"
TOPIC_COMPLIANCE_DECISION = "compliance.decisions"
TOPIC_REVIEW_ESCALATED = "reviews.escalated"
TOPIC_REVIEW_RESOLVED = "reviews.resolved"
TOPIC_SUPERVISOR_ROUTED = "agents.supervisor.routed"


def _make_event(topic: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "topic": topic,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }


class EventBus(Protocol):
    def publish(self, topic: str, payload: Dict[str, Any]) -> Dict[str, Any]: ...

    def subscribe(self, topic: str, handler: Subscriber) -> None: ...


class InProcessEventBus:
    """Synchronous pub/sub with the same contract as the Kafka backend.

    Handlers run on the publisher's thread; failures are logged and never
    propagate into the publishing agent. Keeps a bounded in-memory history
    so the API can expose recent events for observability.
    """

    def __init__(self, history_limit: int = 200) -> None:
        self._subscribers: Dict[str, List[Subscriber]] = {}
        self._history: List[Dict[str, Any]] = []
        self._history_limit = history_limit
        self._lock = threading.Lock()

    def publish(self, topic: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        event = _make_event(topic, payload)
        with self._lock:
            self._history.append(event)
            if len(self._history) > self._history_limit:
                self._history = self._history[-self._history_limit :]
            handlers = list(self._subscribers.get(topic, []))
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                logger.exception("subscriber failed for topic %s", topic)
        return event

    def subscribe(self, topic: str, handler: Subscriber) -> None:
        with self._lock:
            self._subscribers.setdefault(topic, []).append(handler)

    def recent(self, topic: str | None = None, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            events = [e for e in self._history if topic is None or e["topic"] == topic]
        return events[-limit:]


class KafkaEventBus:
    """Kafka-backed bus (Redpanda / Apache Kafka / Azure Event Hubs).

    Publishes every event to its topic; also fans out to in-process
    subscribers so local reactions don't require a consumer group. Requires
    ``kafka-python`` and ``KAFKA_BOOTSTRAP_SERVERS``.
    """

    def __init__(self, bootstrap_servers: str, history_limit: int = 200) -> None:
        from kafka import KafkaProducer  # imported lazily; optional dependency

        self._producer = KafkaProducer(
            bootstrap_servers=bootstrap_servers.split(","),
            value_serializer=lambda value: json.dumps(value).encode("utf-8"),
            retries=3,
        )
        self._local = InProcessEventBus(history_limit=history_limit)

    def publish(self, topic: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        event = self._local.publish(topic, payload)  # local fan-out + history
        try:
            self._producer.send(topic, event)
        except Exception:
            logger.exception("kafka publish failed for topic %s", topic)
        return event

    def subscribe(self, topic: str, handler: Subscriber) -> None:
        self._local.subscribe(topic, handler)

    def recent(self, topic: str | None = None, limit: int = 50) -> List[Dict[str, Any]]:
        return self._local.recent(topic, limit)

    def flush(self) -> None:
        self._producer.flush(timeout=5)


def _build_default_bus() -> InProcessEventBus | KafkaEventBus:
    servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "")
    if servers:
        try:
            bus = KafkaEventBus(servers)
            logger.info("event bus: Kafka backend (%s)", servers)
            return bus
        except Exception:
            logger.exception("Kafka unavailable; falling back to in-process bus")
    return InProcessEventBus()


# Shared process-wide bus used by the API layer and agents.
event_bus = _build_default_bus()
