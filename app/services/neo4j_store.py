from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable


class Neo4jStore:
    def __init__(self, uri: str, user: str, password: str):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        try:
            self.driver.verify_connectivity()
        except ServiceUnavailable as exc:
            raise ConnectionError(
                f"Unable to connect to Neo4j at {uri}. "
                "Make sure Neo4j is running on that address and the credentials are correct."
            ) from exc

    def close(self) -> None:
        self.driver.close()

    def load_demo_data(self, data_dir: Path | None = None) -> None:
        base_dir = data_dir or Path('data')
        with self.driver.session() as s:
            # Hygiene: collapse Review duplicates left by the old CREATE-based
            # add_review, then enforce uniqueness so they cannot recur.
            s.run(
                "MATCH (r:Review) WITH r.review_id AS id, collect(r) AS nodes "
                "WHERE size(nodes) > 1 FOREACH (n IN tail(nodes) | DETACH DELETE n)"
            )
            s.run(
                "CREATE CONSTRAINT review_id_unique IF NOT EXISTS "
                "FOR (r:Review) REQUIRE r.review_id IS UNIQUE"
            )
            client_path = base_dir / 'clients.csv'
            if client_path.exists():
                with client_path.open('r', encoding='utf-8') as fh:
                    for row in csv.DictReader(fh):
                        s.run(
                            "MERGE (c:ClientProfile {client_id: $client_id}) SET c += $props",
                            client_id=row.get('client_id'),
                            props=row,
                        )

            doc_path = base_dir / 'documents.csv'
            if doc_path.exists():
                with doc_path.open('r', encoding='utf-8') as fh:
                    for row in csv.DictReader(fh):
                        s.run(
                            "MERGE (d:Document {doc_id: $doc_id}) SET d += $props",
                            doc_id=row.get('doc_id'),
                            props=row,
                        )

            evidence_path = base_dir / 'evidence.csv'
            if evidence_path.exists():
                with evidence_path.open('r', encoding='utf-8') as fh:
                    for row in csv.DictReader(fh):
                        s.run(
                            "MERGE (e:Evidence {evidence_id: $evidence_id}) SET e += $props",
                            evidence_id=row.get('evidence_id'),
                            props=row,
                        )

            policy_path = base_dir / 'policies.csv'
            if policy_path.exists():
                with policy_path.open('r', encoding='utf-8') as fh:
                    for row in csv.DictReader(fh):
                        s.run(
                            "MERGE (p:Policy {policy_id: $policy_id}) SET p += $props",
                            policy_id=row.get('policy_id'),
                            props=row,
                        )

            regulation_path = base_dir / 'regulations.csv'
            if regulation_path.exists():
                with regulation_path.open('r', encoding='utf-8') as fh:
                    for row in csv.DictReader(fh):
                        s.run(
                            "MERGE (r:Regulation {regulation_id: $regulation_id}) SET r += $props",
                            regulation_id=row.get('regulation_id'),
                            props=row,
                        )

            # Create relationships based on CSV references and jurisdiction matching.
            if client_path.exists():
                with client_path.open('r', encoding='utf-8') as fh:
                    for row in csv.DictReader(fh):
                        refs = row.get('pII_refs', '')
                        if refs:
                            try:
                                doc_ids = eval(refs)
                            except Exception:
                                doc_ids = []
                            for doc_id in doc_ids:
                                s.run(
                                    "MATCH (c:ClientProfile {client_id:$client_id}), (d:Document {doc_id:$doc_id}) MERGE (c)-[:CREATED_FROM]->(d)",
                                    client_id=row.get('client_id'),
                                    doc_id=doc_id,
                                )

            if evidence_path.exists():
                with evidence_path.open('r', encoding='utf-8') as fh:
                    for row in csv.DictReader(fh):
                        s.run(
                            "MATCH (e:Evidence {evidence_id:$evidence_id}), (d:Document {doc_id:$doc_id}) MERGE (e)-[:SUPPORTS]->(d)",
                            evidence_id=row.get('evidence_id'),
                            doc_id=row.get('doc_id'),
                        )

            if policy_path.exists():
                with policy_path.open('r', encoding='utf-8') as fh:
                    for row in csv.DictReader(fh):
                        regulation_id = row.get('regulation_id')
                        if not regulation_id:
                            continue
                        s.run(
                            "MATCH (p:Policy {policy_id:$policy_id}), (r:Regulation {regulation_id:$regulation_id}) MERGE (p)-[:CITES]->(r)",
                            policy_id=row.get('policy_id'),
                            regulation_id=regulation_id,
                        )

            ownership_path = base_dir / 'ownership.csv'
            if ownership_path.exists():
                with ownership_path.open('r', encoding='utf-8') as fh:
                    for row in csv.DictReader(fh):
                        s.run(
                            "MERGE (e:Entity {entity_id: $entity_id}) SET e += $props",
                            entity_id=row.get('entity_id'),
                            props=row,
                        )
                        if row.get('client_id'):
                            s.run(
                                "MATCH (c:ClientProfile {client_id:$client_id}), (e:Entity {entity_id:$entity_id}) MERGE (c)-[:OWNED_BY]->(e)",
                                client_id=row.get('client_id'),
                                entity_id=row.get('entity_id'),
                            )

            if regulation_path.exists() and client_path.exists():
                with regulation_path.open('r', encoding='utf-8') as regs, client_path.open('r', encoding='utf-8') as clients:
                    reg_rows = list(csv.DictReader(regs))
                    for client_row in csv.DictReader(clients):
                        client_country = client_row.get('country', '')
                        for reg_row in reg_rows:
                            jurisdiction = reg_row.get('jurisdiction', '')
                            if jurisdiction == client_country or jurisdiction == 'EU':
                                s.run(
                                    "MATCH (r:Regulation {regulation_id:$regulation_id}), (c:ClientProfile {client_id:$client_id}) MERGE (r)-[:APPLIES_TO]->(c)",
                                    regulation_id=reg_row.get('regulation_id'),
                                    client_id=client_row.get('client_id'),
                                )

    def retrieval_corpus(self) -> List[Dict[str, Any]]:
        """Policy/Regulation/Evidence corpus for the GraphRAG vector index.

        Mirrors the shape the retriever builds from the in-memory store's
        ``nodes``: one entry per indexable node with its source id and text.
        """
        items: List[Dict[str, Any]] = []
        with self.driver.session() as s:
            for record in s.run("MATCH (p:Policy) RETURN p.policy_id AS id, p.summary AS text"):
                if record["id"] and record["text"]:
                    items.append({"source_id": record["id"], "text": record["text"], "label": "Policy", "node": {"policy_id": record["id"]}})
            for record in s.run("MATCH (r:Regulation) RETURN r.regulation_id AS id, r.title AS text"):
                if record["id"] and record["text"]:
                    items.append({"source_id": record["id"], "text": record["text"], "label": "Regulation", "node": {"regulation_id": record["id"]}})
            for record in s.run("MATCH (e:Evidence) RETURN e.evidence_id AS id, e.excerpt AS text"):
                if record["id"] and record["text"]:
                    items.append({"source_id": record["id"], "text": record["text"], "label": "Evidence", "node": {"evidence_id": record["id"]}})
        return items

    def graph_summary(self) -> Dict[str, Any]:
        """Return node counts by label and relationship counts by type.

        Useful as a post-seed sanity check against a live database.
        """
        with self.driver.session() as s:
            node_rows = s.run(
                "MATCH (n) UNWIND labels(n) AS label "
                "RETURN label AS label, count(*) AS c ORDER BY label"
            )
            node_counts = {r["label"]: r["c"] for r in node_rows}
            rel_rows = s.run(
                "MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c ORDER BY t"
            )
            rel_counts = {r["t"]: r["c"] for r in rel_rows}
        return {"nodes": node_counts, "relationships": rel_counts}

    def get_client(self, client_id: str) -> Optional[Dict[str, Any]]:
        with self.driver.session() as s:
            res = s.run("MATCH (c:ClientProfile {client_id:$client_id}) RETURN c LIMIT 1", client_id=client_id)
            rec = res.single()
            if not rec:
                return None
            return dict(rec['c'])

    def get_related_documents(self, client_id: str) -> List[Dict[str, Any]]:
        with self.driver.session() as s:
            res = s.run(
                "MATCH (:ClientProfile {client_id:$client_id})-[:CREATED_FROM]->(d:Document) RETURN d",
                client_id=client_id,
            )
            return [dict(r['d']) for r in res]

    def get_evidence(self, doc_id: str) -> List[Dict[str, Any]]:
        with self.driver.session() as s:
            res = s.run("MATCH (e:Evidence {doc_id:$doc_id}) RETURN e", doc_id=doc_id)
            return [dict(r['e']) for r in res]

    def get_related_policies(self, client_id: str) -> List[Dict[str, Any]]:
        with self.driver.session() as s:
            res = s.run(
                "MATCH (:ClientProfile {client_id:$client_id})<-[:APPLIES_TO]-(r:Regulation)<-[:CITES]-(p:Policy) RETURN p",
                client_id=client_id,
            )
            return [dict(r['p']) for r in res]

    def get_ownership(self, client_id: str) -> List[Dict[str, Any]]:
        """Beneficial-ownership entities for a client (via OWNED_BY edges)."""
        with self.driver.session() as s:
            res = s.run(
                "MATCH (:ClientProfile {client_id:$client_id})-[:OWNED_BY]->(e:Entity) RETURN e",
                client_id=client_id,
            )
            return [dict(r['e']) for r in res]

    def get_regulations_by_jurisdiction(self, jurisdiction: str) -> List[Dict[str, Any]]:
        """Regulations applying to a jurisdiction (exact match or EU-wide).

        Mirrors GraphStore.get_regulations_by_jurisdiction so the
        ``graph_retriever`` tool works identically on both backends.
        """
        jurisdiction = (jurisdiction or "").upper()
        with self.driver.session() as s:
            res = s.run(
                "MATCH (r:Regulation) WHERE toUpper(r.jurisdiction) IN [$jurisdiction, 'EU'] RETURN r",
                jurisdiction=jurisdiction,
            )
            return [dict(r['r']) for r in res]

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
        props = {
            "doc_id": doc_id,
            "source": source,
            "upload_date": upload_date,
            "sha256": sha256,
            # Neo4j property values must be primitives (or arrays of primitives),
            # so serialize the metadata map to a JSON string.
            "metadata": json.dumps(metadata or {}),
        }
        with self.driver.session() as s:
            s.run(
                "MERGE (d:Document {doc_id: $doc_id}) SET d += $props",
                doc_id=doc_id,
                props=props,
            )
            if client_id:
                s.run(
                    "MATCH (c:ClientProfile {client_id:$client_id}), (d:Document {doc_id:$doc_id}) MERGE (c)-[:CREATED_FROM]->(d)",
                    client_id=client_id,
                    doc_id=doc_id,
                )
            if evidence_ids:
                for evidence_id in evidence_ids:
                    s.run(
                        "MATCH (e:Evidence {evidence_id:$evidence_id}), (d:Document {doc_id:$doc_id}) MERGE (e)-[:SUPPORTS]->(d)",
                        evidence_id=evidence_id,
                        doc_id=doc_id,
                    )
        return props

    def add_review(
        self,
        review_id: str,
        client_id: str,
        reason: str,
        severity: str,
        user_id: str,
        details: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Upsert a review task (MERGE on review_id — idempotent, no duplicates).

        ``details`` (the reviewer approval package) is serialized to JSON
        because Neo4j properties must be primitives.
        """
        with self.driver.session() as s:
            s.run(
                "MERGE (r:Review {review_id:$review_id}) "
                "SET r.client_id=$client_id, r.reason=$reason, r.severity=$severity, "
                "r.user_id=$user_id, r.status='pending', r.details=$details",
                review_id=review_id,
                client_id=client_id,
                reason=reason,
                severity=severity,
                user_id=user_id,
                details=json.dumps(details or {}),
            )
        return {"review_id": review_id, "client_id": client_id, "status": "pending", "details": details or {}}

    @staticmethod
    def _parse_review(props: Dict[str, Any]) -> Dict[str, Any]:
        """Deserialize the JSON ``details`` property back into a dict."""
        raw = props.get("details")
        if isinstance(raw, str):
            try:
                props["details"] = json.loads(raw)
            except (ValueError, TypeError):
                props["details"] = {}
        return props

    def list_pending_reviews(self) -> List[Dict[str, Any]]:
        with self.driver.session() as s:
            res = s.run("MATCH (r:Review {status:'pending'}) RETURN r")
            return [self._parse_review(dict(r['r'])) for r in res]

    def list_all_reviews(self) -> List[Dict[str, Any]]:
        with self.driver.session() as s:
            res = s.run("MATCH (r:Review) RETURN r")
            return [self._parse_review(dict(r['r'])) for r in res]

    def resolve_review(self, review_id: str, decision: str, reviewer: str, notes: str | None = None) -> Optional[Dict[str, Any]]:
        with self.driver.session() as s:
            res = s.run(
                "MATCH (r:Review {review_id:$review_id}) SET r.status='resolved', r.decision=$decision, r.reviewer=$reviewer, r.notes=$notes RETURN r",
                review_id=review_id,
                decision=decision,
                reviewer=reviewer,
                notes=notes or '',
            )
            record = res.single()
            return dict(record['r']) if record else None
