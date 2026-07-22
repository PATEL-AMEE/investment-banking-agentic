"""Phase 2 tests: vector store, GraphRAG retrieval, chunking, OFAC sanctions."""
from __future__ import annotations

import csv
from pathlib import Path

from app.services.graph_store import GraphStore
from app.services.ingestion import chunk_text, extract_text, ingest_document, seed_demo_data
from app.services.retrieval import get_retriever
from app.services.sanctions import SDN_PATH, sanctions_check
from app.services.vector_store import InMemoryVectorStore


def _store() -> GraphStore:
    store = GraphStore(data_dir=Path("data"))
    seed_demo_data(store, data_dir=Path("data"))
    return store


# ---------------------------------------------------------------- vector store
def test_vector_store_ranks_relevant_text_first():
    index = InMemoryVectorStore()
    index.add("A", "Enhanced due diligence requirements for high risk clients")
    index.add("B", "Cafeteria menu for the London office")
    results = index.search("due diligence high risk", k=2)
    assert results
    assert results[0]["id"] == "A"


# ------------------------------------------------------------------- retrieval
def test_graphrag_retriever_returns_scored_citations():
    retriever = get_retriever(_store())
    citations = retriever.retrieve("politically exposed person screening", k=3)
    assert citations
    assert all("source_id" in c and "excerpt" in c for c in citations)
    # Retrieval is semantic, not the fallback constant.
    assert any(c.get("score") is not None for c in citations)


# -------------------------------------------------------------------- chunking
def test_chunk_text_overlaps_and_covers_all_content():
    text = " ".join(f"word{i}" for i in range(600))
    chunks = chunk_text(text, chunk_size=400, overlap=80)
    assert len(chunks) > 1
    assert all(len(chunk) <= 400 for chunk in chunks)
    assert "word0" in chunks[0] and "word599" in chunks[-1]


def test_ingest_document_extracts_chunks_and_indexes(tmp_path: Path):
    doc = tmp_path / "kyc_policy.txt"
    doc.write_text("Enhanced due diligence is mandatory for politically exposed persons. " * 30)
    store = _store()
    result = ingest_document(str(doc), metadata={"client_id": "C123"}, store=store)
    assert result["chunk_count"] > 0
    hits = get_retriever(store).retrieve("politically exposed due diligence", k=3)
    assert any(hit["source_id"].startswith(result["doc_id"]) for hit in hits)


def test_extract_text_reads_plain_text(tmp_path: Path):
    doc = tmp_path / "note.txt"
    doc.write_text("sanctions screening note")
    assert "sanctions screening" in extract_text(doc)


# ------------------------------------------------------------------- sanctions
def test_ofac_sdn_list_is_loaded_and_matches_real_entry():
    assert SDN_PATH.exists(), "run scripts/update_sanctions.py to download the OFAC SDN list"
    with SDN_PATH.open(newline="", encoding="utf-8", errors="ignore") as handle:
        first_name = next(row[1] for row in csv.reader(handle) if len(row) > 1 and row[1] and row[1] != "-0-")
    result = sanctions_check(first_name, "GB", [])
    assert result["is_sanctioned"] is True
    assert result["matches"][0]["list"] == "OFAC-SDN"
    assert result["screened_against"] == "OFAC-SDN+local"


def test_fuzzy_match_catches_spelling_variant():
    with SDN_PATH.open(newline="", encoding="utf-8", errors="ignore") as handle:
        long_name = next(
            row[1] for row in csv.reader(handle)
            if len(row) > 1 and row[1] and row[1] != "-0-" and len(row[1]) > 15
        )
    variant = long_name[:-1]  # drop last character
    result = sanctions_check(variant, "GB", [])
    assert result["is_sanctioned"] is True


def test_clean_name_remains_clean_against_full_list():
    result = sanctions_check("Totally Clean Innovations", "GB", [])
    assert result["is_sanctioned"] is False
