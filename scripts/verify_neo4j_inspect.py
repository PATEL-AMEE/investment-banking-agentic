from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.neo4j_store import Neo4jStore
from app.services.workflow import run_inspection_workflow


def main() -> None:
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "test")

    print(f"Connecting to Neo4j at {uri}")
    try:
        store = Neo4jStore(uri, user, password)
    except ConnectionError as exc:
        print("Neo4j connection failed:", exc)
        return

    try:
        print("Loading demo data into Neo4j...")
        store.load_demo_data(Path("data"))

        print("Running inspect workflow for client C123...")
        result = run_inspection_workflow(
            client_id="C123",
            tx_data={"amount": 250000, "currency": "GBP"},
            request_id="REQ-VERIFY-001",
            user_id="verify-user",
            session_id="verify-session",
            workflow_step="compliance-review",
            store=store,
        )
        print("Inspect result:")
        for key, value in result.items():
            if key in {"rule_hits", "provenance"}:
                print(f"{key}: {len(value)} items")
            else:
                print(f"{key}: {value}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
