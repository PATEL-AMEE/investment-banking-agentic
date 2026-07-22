"""Document-analysis agent — a LangGraph ``StateGraph``.

classify_document → extract_metadata → persist_document

Wraps the ingestion service in an agent workflow: classifies the file,
enriches its metadata with detected compliance tags, then persists the
document node (with content hash) into the knowledge graph.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, TypedDict

from langgraph.graph import END, START, StateGraph

from app.services.event_bus import TOPIC_DOCUMENT_INGESTED, event_bus
from app.services.ingestion import ingest_document

_DOC_TYPES = {
    ".pdf": "pdf_document",
    ".doc": "word_document",
    ".docx": "word_document",
    ".txt": "text_document",
    ".csv": "tabular_data",
    ".json": "structured_data",
}

# Compliance topics detected from the document's name/content sample.
_TAG_KEYWORDS = {
    "aml": "anti-money-laundering",
    "kyc": "know-your-customer",
    "sanction": "sanctions",
    "pep": "politically-exposed-person",
    "audit": "audit",
    "policy": "policy",
    "regulation": "regulatory",
}


class DocumentState(TypedDict, total=False):
    # inputs
    file_path: str
    metadata: Dict[str, Any]
    store: Any
    # intermediate
    doc_type: str
    tags: List[str]
    # output
    result: Dict[str, Any]


def _classify_document(state: DocumentState) -> Dict[str, Any]:
    suffix = Path(state["file_path"]).suffix.lower()
    return {"doc_type": _DOC_TYPES.get(suffix, "binary_document")}


def _extract_metadata(state: DocumentState) -> Dict[str, Any]:
    path = Path(state["file_path"])
    sample = path.name.lower()
    if path.exists() and state["doc_type"] in {"text_document", "tabular_data", "structured_data"}:
        try:
            sample += " " + path.read_text(errors="ignore")[:4000].lower()
        except Exception:
            pass
    tags = sorted({tag for keyword, tag in _TAG_KEYWORDS.items() if keyword in sample})
    return {"tags": tags}


def _persist_document(state: DocumentState) -> Dict[str, Any]:
    metadata = dict(state.get("metadata") or {})
    metadata.setdefault("doc_type", state["doc_type"])
    if state["tags"]:
        metadata.setdefault("tags", state["tags"])
    document = ingest_document(state["file_path"], metadata=metadata, store=state["store"])
    event_bus.publish(
        TOPIC_DOCUMENT_INGESTED,
        {
            "doc_id": document["doc_id"],
            "doc_type": state["doc_type"],
            "tags": state["tags"],
            "chunk_count": document.get("chunk_count", 0),
            "client_id": metadata.get("client_id"),
            "actor": "AGENT_DOCANALYSIS_001",
        },
    )
    return {"result": {**document, "doc_type": state["doc_type"], "tags": state["tags"]}}


def build_document_agent():
    graph = StateGraph(DocumentState)
    graph.add_node("classify_document", _classify_document)
    graph.add_node("extract_metadata", _extract_metadata)
    graph.add_node("persist_document", _persist_document)

    graph.add_edge(START, "classify_document")
    graph.add_edge("classify_document", "extract_metadata")
    graph.add_edge("extract_metadata", "persist_document")
    graph.add_edge("persist_document", END)
    return graph.compile()


_document_agent = None


def get_document_agent():
    global _document_agent
    if _document_agent is None:
        _document_agent = build_document_agent()
    return _document_agent


def run_document_analysis(file_path: str, metadata: Dict[str, Any] | None = None, store: Any = None) -> Dict[str, Any]:
    from app.services.telemetry import span

    with span("agent.document_analysis"):
        final_state = get_document_agent().invoke(
            {"file_path": file_path, "metadata": metadata or {}, "store": store}
        )
    return final_state["result"]
