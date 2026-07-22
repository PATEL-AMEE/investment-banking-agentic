from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.neo4j_store import Neo4jStore


def main() -> None:
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "test")

    print(f"Connecting to Neo4j at {uri} as {user}")
    try:
        store = Neo4jStore(uri, user, password)
    except ConnectionError as exc:
        print("Neo4j connection failed:", exc)
        return

    data_dir = ROOT / "data"
    print(f"Seeding demo data from {data_dir}")
    try:
        store.load_demo_data(data_dir=data_dir)
        print("Seed completed.")
        summary = store.graph_summary()
        print("Node counts:")
        for label, count in summary["nodes"].items():
            print(f"  {label}: {count}")
        print("Relationship counts:")
        for rel_type, count in summary["relationships"].items():
            print(f"  {rel_type}: {count}")
    except Exception as exc:
        print("Seed failed:", exc)
    finally:
        store.close()


if __name__ == "__main__":
    main()
