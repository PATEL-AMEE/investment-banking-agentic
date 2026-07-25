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

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

load_dotenv()  # evaluate against the configured LLM provider, as the API does

from app.eval.harness import load_golden_dataset, run_ablation, run_evaluation  # noqa: E402
from app.services.graph_store import GraphStore  # noqa: E402
from app.services.ingestion import seed_demo_data  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the RAG/GraphRAG retrieval chain")
    parser.add_argument("--dataset", default=None, help="Path to a golden Q&A JSON dataset")
    parser.add_argument(
        "--ablation",
        action="store_true",
        help="A/B the vector RAG vs graph-augmented GraphRAG chains (deterministic, no LLM)",
    )
    args = parser.parse_args()

    store = GraphStore(data_dir=Path("data"))
    seed_demo_data(store, data_dir=Path("data"))
    dataset = load_golden_dataset(args.dataset)

    out_dir = Path("data") / "eval" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    if args.ablation:
        report = run_ablation(dataset, store)
        out_path = out_dir / f"ablation_{stamp}.json"
        out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Ablation over {report['case_count']} cases -> {out_path}")
        for name, metrics in report["aggregate"].items():
            print(f"  {name:12s} precision={metrics['context_precision']:.4f}  recall={metrics['context_recall']:.4f}")
        delta = report["graph_rag_delta"]
        print(f"  graph hop -> precision {delta['context_precision']:+.4f}  recall {delta['context_recall']:+.4f}")
        return 0

    report = run_evaluation(dataset, store)
    out_path = out_dir / f"eval_{stamp}.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Evaluated {report['case_count']} cases -> {out_path}")
    print("Aggregate metrics:")
    for name, value in report["aggregate"].items():
        print(f"  {name:20s} {value:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
