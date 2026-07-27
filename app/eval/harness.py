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
import os
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


# ----------------------------------------------------------- LLM-judge engine
def eval_engine() -> str:
    """Which scoring engine to use for faithfulness/relevancy.

    - ``lexical`` (default): the deterministic token-overlap proxies above —
      free, offline, CI-safe.
    - ``llm``/``ragas``: LLM-as-judge (RAGAS-style entailment) using the
      configured Azure OpenAI via ``LLMAdapter``. Set ``EVAL_ENGINE=llm`` to
      enable; it makes one judge call per case and falls back to lexical on any
      error, so it's safe to leave wired. (The ``ragas`` package can be slotted
      in here later; the LLM judge is the same idea without the extra dependency.)
    """
    return os.getenv("EVAL_ENGINE", "lexical").strip().lower()


def _llm_judge(question: str, answer: str, contexts: List[str]) -> tuple[float, float]:
    """(faithfulness, answer_relevancy) scored 0-1 by an LLM judge, grounded
    strictly in the retrieved contexts. Raises on any failure so the caller can
    fall back to the deterministic proxies."""
    from app.services.llm_adapter import LLMAdapter

    context_block = "\n\n".join(f"[{i + 1}] {c}" for i, c in enumerate(contexts)) or "(no context retrieved)"
    system = "You are a strict RAG evaluation judge. Judge only against the provided context. Reply with JSON only."
    user = (
        f"Question:\n{question}\n\n"
        f"Retrieved context:\n{context_block}\n\n"
        f"Answer:\n{answer}\n\n"
        "Score two metrics from 0.0 to 1.0:\n"
        "- faithfulness: fraction of the answer's claims supported by the retrieved context "
        "(1.0 = fully grounded, 0.0 = unsupported / hallucinated).\n"
        "- answer_relevancy: how directly the answer addresses the question.\n"
        'Respond as JSON only: {"faithfulness": <float>, "answer_relevancy": <float>}'
    )
    raw = LLMAdapter().chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.0,
        max_tokens=120,
    )
    match = re.search(r"\{.*\}", raw, re.S)
    data = json.loads(match.group(0)) if match else {}
    faith = max(0.0, min(1.0, float(data["faithfulness"])))
    relevancy = max(0.0, min(1.0, float(data["answer_relevancy"])))
    return round(faith, 4), round(relevancy, 4)


def _score_answer(question: str, answer: str, contexts: List[str], engine: str | None = None) -> tuple[float, float]:
    """faithfulness, answer_relevancy under the chosen engine (LLM judge with a
    deterministic-proxy fallback). ``engine`` defaults to ``EVAL_ENGINE``."""
    if (engine or eval_engine()) in ("llm", "ragas"):
        try:
            return _llm_judge(question, answer, contexts)
        except Exception:  # provider down / unparseable — never fail the eval
            pass
    return faithfulness(answer, contexts), answer_relevancy(question, answer)


# ------------------------------------------------------------------- runner
def evaluate_case(case: Dict[str, Any], store: Any, engine: str | None = None) -> Dict[str, Any]:
    """Run one golden Q&A case through the copilot chain and score it."""
    from app.agents.copilot import run_copilot

    result = run_copilot(case["question"], store)
    answer = result.get("answer", "")
    citations = result.get("citations", [])
    retrieved_ids = [c.get("source_id", "") for c in citations]
    contexts = [c.get("excerpt", "") for c in citations]

    faith, relevancy = _score_answer(case["question"], answer, contexts, engine)
    return {
        "case_id": case.get("case_id"),
        "question": case["question"],
        "answer": answer,
        "retrieved_ids": retrieved_ids,
        "metrics": {
            "faithfulness": faith,
            "hallucination_rate": round(1 - faith, 4),
            "answer_relevancy": relevancy,
            "context_precision": context_precision(retrieved_ids, case.get("expected_sources", [])),
            "context_recall": context_recall(retrieved_ids, case.get("expected_sources", [])),
        },
    }


def run_evaluation(dataset: List[Dict[str, Any]], store: Any, engine: str | None = None) -> Dict[str, Any]:
    """Evaluate every case; return per-case results plus aggregate means.

    ``engine`` (``lexical`` | ``llm``) overrides ``EVAL_ENGINE`` for this run.
    """
    from app.services.telemetry import span

    engine = (engine or eval_engine()).strip().lower()
    with span("eval.run", cases=len(dataset)):
        cases = [evaluate_case(case, store, engine) for case in dataset]
    metric_names = ["faithfulness", "hallucination_rate", "answer_relevancy", "context_precision", "context_recall"]
    aggregate = {
        name: round(sum(c["metrics"][name] for c in cases) / len(cases), 4) if cases else 0.0
        for name in metric_names
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "engine": engine,
        "case_count": len(cases),
        "aggregate": aggregate,
        "cases": cases,
    }


def run_ablation(dataset: List[Dict[str, Any]], store: Any) -> Dict[str, Any]:
    """A/B the two retrieval chains: plain vector RAG vs graph-augmented GraphRAG.

    Deterministic (no LLM): each golden question is retrieved twice — once with
    the graph hop off (``graph_augment=False``), once on — and scored on context
    precision/recall against the case's expected sources. The delta shows what
    the knowledge graph actually buys: it recovers graph-connected sources
    (e.g. the Regulation a Policy cites) that vector search alone misses,
    typically lifting recall at some precision cost.
    """
    from app.services.retrieval import get_retriever
    from app.services.telemetry import span

    retriever = get_retriever(store)
    retriever.build_index()
    modes = {"vector_rag": False, "graph_rag": True}

    cases: List[Dict[str, Any]] = []
    with span("eval.ablation", cases=len(dataset)):
        for case in dataset:
            expected = case.get("expected_sources", [])
            row: Dict[str, Any] = {"case_id": case.get("case_id"), "question": case["question"], "expected_sources": expected, "modes": {}}
            for name, augment in modes.items():
                ids = [c["source_id"] for c in retriever.retrieve(case["question"], k=3, graph_augment=augment)]
                row["modes"][name] = {
                    "retrieved": ids,
                    "context_precision": context_precision(ids, expected),
                    "context_recall": context_recall(ids, expected),
                }
            cases.append(row)

    def _mean(mode: str, metric: str) -> float:
        return round(sum(c["modes"][mode][metric] for c in cases) / len(cases), 4) if cases else 0.0

    aggregate = {
        name: {"context_precision": _mean(name, "context_precision"), "context_recall": _mean(name, "context_recall")}
        for name in modes
    }
    delta = {
        "context_precision": round(aggregate["graph_rag"]["context_precision"] - aggregate["vector_rag"]["context_precision"], 4),
        "context_recall": round(aggregate["graph_rag"]["context_recall"] - aggregate["vector_rag"]["context_recall"], 4),
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "case_count": len(cases),
        "aggregate": aggregate,
        "graph_rag_delta": delta,
        "cases": cases,
    }


def load_golden_dataset(path: str | Path | None = None) -> List[Dict[str, Any]]:
    dataset_path = Path(path) if path else Path("data") / "eval" / "golden_qa.json"
    return json.loads(dataset_path.read_text(encoding="utf-8"))


def run_feedback_regression(store: Any) -> Dict[str, Any]:
    """Re-run every question staff flagged 'wrong' and score it now.

    Closes the Phase 6.6 → Phase 9 loop: human-spotted failures become an
    ongoing regression set. Each case is re-answered by the live copilot chain
    and scored for faithfulness/hallucination, so we can see whether a
    previously-wrong answer has since improved. No expected-source labels are
    assumed (feedback carries none), so precision/recall are omitted.
    """
    from app.services import feedback

    flagged = feedback.flagged_questions()
    cases: List[Dict[str, Any]] = []
    for entry in flagged:
        scored = evaluate_case({"question": entry["query"], "case_id": entry["feedback_id"]}, store)
        scored["flagged_by"] = entry.get("user_id")
        scored["comment"] = entry.get("comment", "")
        cases.append(scored)
    faith_vals = [c["metrics"]["faithfulness"] for c in cases]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "flagged_count": len(cases),
        "mean_faithfulness": round(sum(faith_vals) / len(faith_vals), 4) if faith_vals else 0.0,
        "cases": cases,
    }
