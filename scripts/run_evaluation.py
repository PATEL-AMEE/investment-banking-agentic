"""Run the RAGAS-style evaluation harness against the copilot RAG chain.

Usage (from the repo root):

    python scripts/run_evaluation.py [--dataset data/eval/golden_qa.json]

Runs offline against the in-memory GraphStore + configured LLM provider
(mock by default) and writes the report to ``data/eval/results/``.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.eval.harness import load_golden_dataset, run_evaluation  # noqa: E402
from app.services.graph_store import GraphStore  # noqa: E402
from app.services.ingestion import seed_demo_data  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the RAG/GraphRAG retrieval chain")
    parser.add_argument("--dataset", default=None, help="Path to a golden Q&A JSON dataset")
    args = parser.parse_args()

    store = GraphStore(data_dir=Path("data"))
    seed_demo_data(store, data_dir=Path("data"))
    dataset = load_golden_dataset(args.dataset)
    report = run_evaluation(dataset, store)

    out_dir = Path("data") / "eval" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"eval_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Evaluated {report['case_count']} cases -> {out_path}")
    print("Aggregate metrics:")
    for name, value in report["aggregate"].items():
        print(f"  {name:20s} {value:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
