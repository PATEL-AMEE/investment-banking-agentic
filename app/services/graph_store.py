from __future__ import annotations

import ast
import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

# Country-risk framework: jurisdictions classified as high risk for ownership
# and onboarding purposes (offshore secrecy or sanctions-adjacent). Shared by
# both graph backends so path explanations stay consistent.
HIGH_RISK_COUNTRIES = {"KY", "VG", "PA", "IR", "SY", "KP", "RU", "BY"}


@dataclass
class GraphStore:
    data_dir: Path
    nodes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    relationships: List[Dict[str, Any]] = field(default_factory=list)
    reviews: List[Dict[str, Any]] = field(default_factory=list)

    def load_demo_data(self, data_dir: Path | None = None) -> None:
        base_dir = data_dir or self.data_dir
        client_path = base_dir / "clients.csv"
        document_path = base_dir / "documents.csv"
        evidence_path = base_dir / "evidence.csv"
        policy_path = base_dir / "policies.csv"
        regulation_path = base_dir / "regulations.csv"
        ownership_path = base_dir / "ownership.csv"

        self._load_csv_rows(client_path, "ClientProfile", "client_id")
        self._load_csv_rows(document_path, "Document", "doc_id")
        self._load_csv_rows(evidence_path, "Evidence", "evidence_id")
        self._load_csv_rows(policy_path, "Policy", "policy_id")
        self._load_csv_rows(regulation_path, "Regulation", "regulation_id")
        self._load_csv_rows(ownership_path, "Entity", "entity_id")

        self._build_relationships()

    def _build_relationships(self) -> None:
        """Derive relationships from the loaded node data.

        Kept in sync with Neo4jStore.load_demo_data so both backends behave
        identically:
          - ClientProfile -[:CREATED_FROM]-> Document   (from client pII_refs)
          - Evidence      -[:SUPPORTS]->     Document    (from evidence.doc_id)
          - Policy        -[:CITES]->        Regulation  (from policy.regulation_id)
          - Regulation    -[:APPLIES_TO]->   ClientProfile (jurisdiction match / EU)
          - ClientProfile -[:OWNED_BY]->     Entity      (from ownership.client_id)
        """
        clients = [n for n in self.nodes.values() if n.get("label") == "ClientProfile"]
        regulations = [n for n in self.nodes.values() if n.get("label") == "Regulation"]

        for client in clients:
            for doc_id in self._parse_refs(client.get("pII_refs")):
                if doc_id in self.nodes:
                    self.relationships.append(
                        {"from": client["client_id"], "to": doc_id, "type": "CREATED_FROM"}
                    )

        for node in self.nodes.values():
            if node.get("label") == "Evidence" and node.get("doc_id") in self.nodes:
                self.relationships.append(
                    {"from": node["evidence_id"], "to": node["doc_id"], "type": "SUPPORTS"}
                )

        for node in self.nodes.values():
            if node.get("label") == "Policy":
                reg_id = node.get("regulation_id")
                if reg_id and reg_id in self.nodes:
                    self.relationships.append(
                        {"from": node["policy_id"], "to": reg_id, "type": "CITES"}
                    )

        for regulation in regulations:
            jurisdiction = regulation.get("jurisdiction", "")
            for client in clients:
                if jurisdiction == client.get("country") or jurisdiction == "EU":
                    self.relationships.append(
                        {"from": regulation["regulation_id"], "to": client["client_id"], "type": "APPLIES_TO"}
                    )

        for node in self.nodes.values():
            if node.get("label") == "Entity" and node.get("client_id") in self.nodes:
                self.relationships.append(
                    {"from": node["client_id"], "to": node["entity_id"], "type": "OWNED_BY"}
                )

    @staticmethod
    def _parse_refs(raw: Any) -> List[str]:
        """Parse a pII_refs cell like "['D-0001','D-0002']" into a list."""
        if not raw:
            return []
        if isinstance(raw, list):
            return [str(item) for item in raw]
        try:
            parsed = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return []
        if isinstance(parsed, (list, tuple)):
            return [str(item) for item in parsed]
        return [str(parsed)]

    def _load_csv_rows(self, path: Path, label: str, key_field: str) -> None:
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:
            # Key each node by its own id column so that, e.g., Evidence rows
            # (which also carry a doc_id) don't overwrite Document nodes.
            key = row.get(key_field)
            if not key:
                # skip rows with no identifier (e.g. blank or malformed lines)
                continue
            self.nodes[key] = {"label": label, **row}

    def get_client(self, client_id: str) -> Dict[str, Any] | None:
        return self.nodes.get(client_id)

    def get_related_documents(self, client_id: str) -> List[Dict[str, Any]]:
        related_ids = [
            rel["to"]
            for rel in self.relationships
            if rel["from"] == client_id and rel["type"] == "CREATED_FROM"
        ]
        return [self.nodes[doc_id] for doc_id in related_ids if doc_id in self.nodes]

    def get_evidence(self, doc_id: str) -> List[Dict[str, Any]]:
        return [
            node
            for node in self.nodes.values()
            if node.get("label") == "Evidence" and node.get("doc_id") == doc_id
        ]

    def get_related_policies(self, client_id: str) -> List[Dict[str, Any]]:
        # Client <-[:APPLIES_TO]- Regulation <-[:CITES]- Policy
        regulation_ids = {
            rel["from"]
            for rel in self.relationships
            if rel["to"] == client_id and rel["type"] == "APPLIES_TO"
        }
        policy_ids = {
            rel["from"]
            for rel in self.relationships
            if rel["type"] == "CITES" and rel["to"] in regulation_ids
        }
        return [self.nodes[pid] for pid in policy_ids if pid in self.nodes]

    def get_ownership(self, client_id: str) -> List[Dict[str, Any]]:
        """Beneficial-ownership entities for a client (via OWNED_BY edges)."""
        entity_ids = [
            rel["to"]
            for rel in self.relationships
            if rel["from"] == client_id and rel["type"] == "OWNED_BY"
        ]
        return [self.nodes[eid] for eid in entity_ids if eid in self.nodes]

    def get_regulations_by_jurisdiction(self, jurisdiction: str) -> List[Dict[str, Any]]:
        """Return regulations that apply to a jurisdiction (exact match or EU-wide)."""
        jurisdiction = (jurisdiction or "").upper()
        return [
            node
            for node in self.nodes.values()
            if node.get("label") == "Regulation"
            and node.get("jurisdiction", "").upper() in {jurisdiction, "EU"}
        ]

    def add_review(
        self,
        review_id: str,
        client_id: str,
        reason: str,
        severity: str,
        user_id: str,
        details: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Upsert a review task (idempotent on review_id — no duplicates).

        ``details`` is the approval package shown to the human reviewer:
        evidence, policy references, agent recommendation, approver role,
        and the available actions.
        """
        review = {
            "review_id": review_id,
            "client_id": client_id,
            "reason": reason,
            "severity": severity,
            "user_id": user_id,
            "status": "pending",
            "details": details or {},
        }
        for index, existing in enumerate(self.reviews):
            if existing["review_id"] == review_id:
                self.reviews[index] = review
                return review
        self.reviews.append(review)
        return review

    def list_pending_reviews(self) -> List[Dict[str, Any]]:
        return [review for review in self.reviews if review["status"] == "pending"]

    def list_all_reviews(self) -> List[Dict[str, Any]]:
        return list(self.reviews)

    def resolve_review(self, review_id: str, decision: str, reviewer: str, notes: str | None = None) -> Dict[str, Any]:
        for review in self.reviews:
            if review["review_id"] == review_id:
                review["status"] = "resolved"
                review["decision"] = decision
                review["reviewer"] = reviewer
                review["notes"] = notes or ""
                return review
        raise ValueError(f"Review {review_id} not found")

    def add_document(
        self,
        doc_id: str,
        source: str,
        upload_date: str,
        sha256: str,
        metadata: Dict[str, Any] | None = None,
        client_id: str | None = None,
        evidence_ids: list[str] | None = None,
    ) -> Dict[str, Any]:
        document = {
            "label": "Document",
            "doc_id": doc_id,
            "source": source,
            "upload_date": upload_date,
            "sha256": sha256,
            "metadata": metadata or {},
        }
        self.nodes[doc_id] = document
        if client_id:
            self.relationships.append({"from": client_id, "to": doc_id, "type": "CREATED_FROM"})
        if evidence_ids:
            for evidence_id in evidence_ids:
                self.relationships.append({"from": evidence_id, "to": doc_id, "type": "SUPPORTS"})
        return document

    def snapshot(self) -> Dict[str, Any]:
        return {"nodes": self.nodes, "relationships": self.relationships, "reviews": self.reviews}
