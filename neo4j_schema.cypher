// Neo4j schema: constraints and sample node/relationship templates
CREATE CONSTRAINT IF NOT EXISTS FOR (r:Regulation) REQUIRE r.id IS UNIQUE;
CREATE CONSTRAINT IF NOT EXISTS FOR (p:Policy) REQUIRE p.id IS UNIQUE;
CREATE CONSTRAINT IF NOT EXISTS FOR (c:ClientProfile) REQUIRE c.client_id IS UNIQUE;
CREATE CONSTRAINT IF NOT EXISTS FOR (d:Document) REQUIRE d.doc_id IS UNIQUE;
CREATE CONSTRAINT IF NOT EXISTS FOR (e:Evidence) REQUIRE e.evidence_id IS UNIQUE;
CREATE CONSTRAINT IF NOT EXISTS FOR (a:AuditEvent) REQUIRE a.event_id IS UNIQUE;

// Sample node creation
CREATE (reg:Regulation {id: 'REG-001', title: 'AML Regulation 2023', effective_date: '2023-01-01', jurisdiction: 'GB'})
CREATE (pol:Policy {id: 'POL-AML-01', department: 'Compliance', summary: 'KYC requirements for corporate clients', version: '2024-01'})
CREATE (cli:ClientProfile {client_id: 'C123', name: 'Acme Corp', risk_score: 0.42, country: 'GB', onboarding_status: 'pending'})
CREATE (doc:Document {doc_id: 'D-0001', source: 'upload', upload_date: date(), sha256: 'sha256-sample', classification: 'internal'})
CREATE (e:Evidence {evidence_id: 'E-1', doc_id: 'D-0001', page: 3, excerpt: 'Beneficial owner is listed as...', confidence: 0.91})
CREATE (a:AuditEvent {event_id: 'AUD-001', event_type: 'decision', timestamp: datetime(), request_id: 'REQ-001', user_id: 'U-001'})

// Sample relationships
CREATE (reg)-[:APPLIES_TO]->(cli)
CREATE (e)-[:CITED_IN]->(pol)
CREATE (cli)-[:CREATED_FROM]->(doc)
CREATE (a)-[:RELATED_TO]->(cli)
CREATE (a)-[:PROVENANCE_FOR]->(e)
