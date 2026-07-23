# Enterprise Agentic AI Platform — Barclays Investment Banking

At Barclays, I lead an enterprise Agentic AI platform for the investment banking division, orchestrating specialised LLM agents via LangGraph and MCP to automate regulatory compliance, client onboarding, and knowledge retrieval. The platform uses GraphRAG on Neo4j, Azure AI with Kubernetes, Docker, and Apache Kafka, with end-to-end Security controls across all LLM endpoints.

- Designed the end-to-end Architecture of a multi-agent Agentic AI platform using LangGraph to orchestrate compliance, onboarding, and knowledge-retrieval agents across the investment banking division.

- Implemented MCP as the inter-agent communication protocol, enabling structured context sharing and tool invocation across compliance-checking, document-analysis, and client-profiling LLM agents.

- Built a GraphRAG pipeline using Neo4j knowledge graphs and Azure AI Search to enable LLM agents to traverse complex regulatory relationships and surface contextually grounded compliance guidance.

- Developed a Copilot experience for compliance analysts using Azure OpenAI Service and LangGraph, providing natural-language NLP-driven regulatory Q&A grounded in the bank's internal policy corpus via RAG retrieval.

- Deployed the platform on Azure Kubernetes Service with Docker containerisation, Apache Kafka for event streaming, and Azure Monitor for real-time observability across all Agentic AI agent interactions.

- Engineered NLP pipelines using Python, spaCy, and Hugging Face Transformers for named-entity recognition, contract clause extraction, and regulatory document classification feeding the RAG layer.

- Implemented comprehensive Security controls including prompt injection defence, PII redaction via Microsoft Presidio, role-based access control for LLM endpoints, and cryptographic audit trails for all agent decisions.

- Integrated Vertex AI for fine-tuning domain-specific LLM adapters on proprietary banking compliance corpora, improving retrieval accuracy on niche regulatory topics by 34%.

- Built automated evaluation harnesses using RAGAS and custom Python benchmarks to measure faithfulness, answer relevancy, and hallucination rates across the RAG and GraphRAG retrieval chains.

- Led a team of six engineers, establishing Architecture review boards, code-review standards, and Security sign-off gates for every LLM-integrated feature release.

- Presented quarterly AI roadmap updates to the CTO and Chief Risk Officer, translating Agentic AI and Copilot capabilities into strategic risk-reduction outcomes for the bank's regulatory posture.

- Partnered with the bank's information Security team to complete penetration testing and threat modelling for the Agentic AI platform, addressing OWASP Top 10 for LLM Applications.
