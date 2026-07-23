"""RAGAS-style evaluation harness for the RAG/GraphRAG retrieval chains.

Measures, per golden Q&A case and in aggregate:

- **faithfulness** — fraction of answer sentences grounded in the retrieved
  contexts (token-overlap entailment proxy);
- **hallucination_rate** — ``1 - faithfulness``;
- **answer_relevancy** — cosine similarity between question and answer
  token-count vectors;
- **context_precision / context_recall** — retrieved citation ids vs the
  case's expected sources.

The metrics are deterministic and offline (custom Python benchmarks), so
they run in CI against the mock LLM. When the ``ragas`` package is installed
and ``EVAL_ENGINE=ragas`` is set, the same dataset shape can be handed to
RAGAS's LLM-judged metrics for a deeper (paid) evaluation pass — see the
AKS implementation guide, §12.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

_WORD = re.compile(r"[a-z0-9']+")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
# Grounding threshold: an answer sentence counts as faithful when at least
# this fraction of its content tokens appears in some retrieved context.
_FAITHFUL_OVERLAP = 0.5
_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "be", "been", "for", "of", "to", "in",
    "on", "and", "or", "with", "by", "at", "as", "that", "this", "it", "its",
    "must", "shall", "should", "may", "not", "no", "all", "any", "per",
}


def _tokens(text: str) -> List[str]:
    return [t for t in _WORD.findall(text.lower()) if t not in _STOPWORDS]


# ----------------------------------------------------------------- metrics
def faithfulness(answer: str, contexts: List[str]) -> float:
    """Fraction of answer sentences supported by at least one context."""
    # Citation markers like [POL-AML-01] are pointers, not claims — strip
    # them so they are neither scored as sentences nor counted as tokens.
    answer = re.sub(r"\[[A-Za-z0-9_-]+\]", " ", answer)
    sentences = [s for s in _SENTENCE_SPLIT.split(answer) if _tokens(s)]
    if not sentences:
        return 0.0
    context_tokens = set()
    for context in contexts:
        context_tokens.update(_tokens(context))
    if not context_tokens:
        return 0.0
    supported = 0
    for sentence in sentences:
        tokens = _tokens(sentence)
        overlap = sum(1 for t in tokens if t in context_tokens) / len(tokens)
        if overlap >= _FAITHFUL_OVERLAP:
            supported += 1
    return round(supported / len(sentences), 4)


def answer_relevancy(question: str, answer: str) -> float:
    """Cosine similarity between question and answer token-count vectors."""
    q_counts, a_counts = Counter(_tokens(question)), Counter(_tokens(answer))
    if not q_counts or not a_counts:
        return 0.0
    dot = sum(q_counts[t] * a_counts[t] for t in q_counts)
    norm = math.sqrt(sum(v * v for v in q_counts.values())) * math.sqrt(sum(v * v for v in a_counts.values()))
    return round(dot / norm, 4) if norm else 0.0


def context_precision(retrieved_ids: List[str], expected_ids: List[str]) -> float:
    """Fraction of retrieved citations that were expected."""
    if not retrieved_ids:
        return 0.0
    expected = set(expected_ids)
    return round(sum(1 for r in retrieved_ids if r in expected) / len(retrieved_ids), 4)


def context_recall(retrieved_ids: List[str], expected_ids: List[str]) -> float:
    """Fraction of expected sources that were actually retrieved."""
    if not expected_ids:
        return 1.0
    retrieved = set(retrieved_ids)
    return round(sum(1 for e in expected_ids if e in retrieved) / len(expected_ids), 4)


# ------------------------------------------------------------------- runner
def evaluate_case(case: Dict[str, Any], store: Any) -> Dict[str, Any]:
    """Run one golden Q&A case through the copilot chain and score it."""
    from app.agents.copilot import run_copilot

    result = run_copilot(case["question"], store)
    answer = result.get("answer", "")
    citations = result.get("citations", [])
    retrieved_ids = [c.get("source_id", "") for c in citations]
    contexts = [c.get("excerpt", "") for c in citations]

    faith = faithfulness(answer, contexts)
    return {
        "case_id": case.get("case_id"),
        "question": case["question"],
        "answer": answer,
        "retrieved_ids": retrieved_ids,
        "metrics": {
            "faithfulness": faith,
            "hallucination_rate": round(1 - faith, 4),
            "answer_relevancy": answer_relevancy(case["question"], answer),
            "context_precision": context_precision(retrieved_ids, case.get("expected_sources", [])),
            "context_recall": context_recall(retrieved_ids, case.get("expected_sources", [])),
        },
    }


def run_evaluation(dataset: List[Dict[str, Any]], store: Any) -> Dict[str, Any]:
    """Evaluate every case; return per-case results plus aggregate means."""
    from app.services.telemetry import span

    with span("eval.run", cases=len(dataset)):
        cases = [evaluate_case(case, store) for case in dataset]
    metric_names = ["faithfulness", "hallucination_rate", "answer_relevancy", "context_precision", "context_recall"]
    aggregate = {
        name: round(sum(c["metrics"][name] for c in cases) / len(cases), 4) if cases else 0.0
        for name in metric_names
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "case_count": len(cases),
        "aggregate": aggregate,
        "cases": cases,
    }


def load_golden_dataset(path: str | Path | None = None) -> List[Dict[str, Any]]:
    dataset_path = Path(path) if path else Path("data") / "eval" / "golden_qa.json"
    return json.loads(dataset_path.read_text(encoding="utf-8"))
