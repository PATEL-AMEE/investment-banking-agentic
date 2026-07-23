"""NLP pipeline: NER, contract clause extraction, document classification.

Feeds the RAG layer with structured signals extracted from regulatory and
contractual documents. Engine resolution mirrors the LLM adapter pattern:

1. **spaCy** — used when ``NLP_ENGINE=spacy`` and ``en_core_web_sm`` is
   installed (``pip install -r requirements-ml.txt`` + model download).
2. **Hugging Face zero-shot** — used for classification when
   ``NLP_ENGINE=transformers`` and the ``transformers`` package is installed.
3. **Rule-based** — deterministic gazetteer/regex fallback, always available,
   keeps the pipeline (and the test suite) fully offline.

Failures never propagate: any engine error falls back to the rule-based path
so ingestion keeps functioning without the heavyweight ML dependencies.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List

# --------------------------------------------------------------- NER (rules)
_ORG_SUFFIXES = r"(?:Ltd|PLC|LLP|LLC|Inc|GmbH|SA|AG|Bank|Capital|Holdings|Partners|Group|Ventures|Finance|Trading|Corp)"
_ENTITY_PATTERNS: List[tuple[str, re.Pattern[str]]] = [
    ("ORG", re.compile(rf"\b(?:[A-Z][\w&.-]+\s)+{_ORG_SUFFIXES}\.?\b")),
    ("MONEY", re.compile(r"(?:[£$€]\s?\d[\d,.]*(?:\s?(?:million|billion|m|bn|k))?|\b\d[\d,.]*\s?(?:GBP|USD|EUR|CHF|SGD|HKD)\b)")),
    ("DATE", re.compile(r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}\s(?:January|February|March|April|May|June|July|August|September|October|November|December)\s\d{4})\b")),
    ("PERSON", re.compile(r"\b(?:Mr|Mrs|Ms|Dr|Prof)\.?\s[A-Z][a-z]+(?:\s[A-Z][a-z]+)?\b")),
    ("REG_REF", re.compile(r"\b(?:POL|REG|SAN|AUDIT)-[A-Z]{2,5}?-?\d{2,6}\b|\b(?:MiFID\s?I?I|GDPR|FCA|SEC|FinCEN|FATF|Basel\s?I{1,3})\b")),
]

# Jurisdiction gazetteer — aligned with the countries used in the demo corpus.
_GPE_GAZETTEER = {
    "united kingdom": "GB", "uk": "GB", "great britain": "GB",
    "united states": "US", "usa": "US",
    "ireland": "IE", "germany": "DE", "france": "FR", "netherlands": "NL",
    "spain": "ES", "italy": "IT", "singapore": "SG", "hong kong": "HK",
    "switzerland": "CH", "luxembourg": "LU",
}
_GPE_PATTERN = re.compile(r"\b(" + "|".join(re.escape(name) for name in _GPE_GAZETTEER) + r")\b", re.I)

# ------------------------------------------------------------------- clauses
# Contract clause taxonomy: clause type -> trigger keywords. A sentence
# containing a trigger is extracted as that clause's excerpt.
_CLAUSE_KEYWORDS: Dict[str, List[str]] = {
    "governing_law": ["governing law", "governed by the laws", "jurisdiction of the courts"],
    "sanctions_compliance": ["sanctions", "embargo", "restricted party", "ofac", "hm treasury"],
    "confidentiality": ["confidential", "non-disclosure", "shall not disclose"],
    "termination": ["terminate", "termination", "notice period"],
    "indemnification": ["indemnify", "indemnification", "hold harmless"],
    "data_protection": ["personal data", "data protection", "gdpr", "data subject"],
    "audit_rights": ["right to audit", "audit rights", "books and records"],
    "payment_terms": ["payment terms", "payable within", "settlement date"],
    "liability": ["limitation of liability", "liable for", "aggregate liability"],
    "anti_bribery": ["anti-bribery", "corruption", "facilitation payment"],
}

# -------------------------------------------------------- classification
# Document categories with weighted keyword evidence.
_CATEGORY_KEYWORDS: Dict[str, Dict[str, float]] = {
    "aml_policy": {"anti-money laundering": 2, "aml": 2, "money laundering": 2, "suspicious activity": 1.5, "due diligence": 1},
    "kyc_procedure": {"know your customer": 2, "kyc": 2, "customer identification": 1.5, "onboarding": 1, "identity verification": 1.5},
    "sanctions_notice": {"sanctions": 2, "ofac": 2, "embargo": 1.5, "designated person": 1.5, "restricted party": 1.5},
    "trading_agreement": {"agreement": 1, "counterparty": 1.5, "settlement": 1, "isda": 2, "collateral": 1.5, "governing law": 1},
    "regulatory_filing": {"regulator": 1.5, "filing": 1.5, "mifid": 2, "disclosure": 1, "supervisory": 1.5},
    "pep_screening": {"politically exposed": 2, "pep": 2, "public official": 1.5, "enhanced review": 1},
}


def engine() -> str:
    """Resolve the active NLP engine (``rule-based`` unless ML extras are on)."""
    requested = os.getenv("NLP_ENGINE", "rule-based").lower()
    if requested == "spacy" and _spacy_nlp() is not None:
        return "spacy"
    if requested == "transformers" and _zero_shot() is not None:
        return "transformers"
    return "rule-based"


# Cached optional heavyweight engines (None = unavailable).
_SPACY_NLP: Any = False  # False = not resolved yet
_ZERO_SHOT: Any = False


def _spacy_nlp() -> Any:
    global _SPACY_NLP
    if _SPACY_NLP is False:
        try:
            import spacy

            _SPACY_NLP = spacy.load(os.getenv("SPACY_MODEL", "en_core_web_sm"))
        except Exception:
            _SPACY_NLP = None
    return _SPACY_NLP


def _zero_shot() -> Any:
    global _ZERO_SHOT
    if _ZERO_SHOT is False:
        try:
            from transformers import pipeline

            _ZERO_SHOT = pipeline("zero-shot-classification", model=os.getenv("HF_ZERO_SHOT_MODEL", "facebook/bart-large-mnli"))
        except Exception:
            _ZERO_SHOT = None
    return _ZERO_SHOT


# ---------------------------------------------------------------------- NER
def extract_entities(text: str) -> List[Dict[str, Any]]:
    """Named entities as ``{text, label, start, end}`` (deduplicated)."""
    nlp = _spacy_nlp() if os.getenv("NLP_ENGINE", "").lower() == "spacy" else None
    if nlp is not None:
        try:
            doc = nlp(text[:20000])
            return [
                {"text": ent.text, "label": ent.label_, "start": ent.start_char, "end": ent.end_char}
                for ent in doc.ents
            ]
        except Exception:
            pass  # fall through to rules

    entities: List[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for label, pattern in _ENTITY_PATTERNS:
        for match in pattern.finditer(text):
            key = (label, match.group(0))
            if key not in seen:
                seen.add(key)
                entities.append({"text": match.group(0), "label": label, "start": match.start(), "end": match.end()})
    for match in _GPE_PATTERN.finditer(text):
        key = ("GPE", match.group(0))
        if key not in seen:
            seen.add(key)
            entities.append(
                {
                    "text": match.group(0),
                    "label": "GPE",
                    "start": match.start(),
                    "end": match.end(),
                    "country_code": _GPE_GAZETTEER[match.group(1).lower()],
                }
            )
    return sorted(entities, key=lambda e: e["start"])


# -------------------------------------------------------------------- clauses
_SENTENCE_SPLIT = re.compile(r"(?<=[.;])\s+|\n+")


def extract_clauses(text: str) -> List[Dict[str, Any]]:
    """Contract clauses as ``{clause_type, excerpt, trigger}`` (first hit per type)."""
    clauses: List[Dict[str, Any]] = []
    found: set[str] = set()
    for sentence in _SENTENCE_SPLIT.split(text):
        lowered = sentence.lower()
        for clause_type, keywords in _CLAUSE_KEYWORDS.items():
            if clause_type in found:
                continue
            for keyword in keywords:
                if keyword in lowered:
                    clauses.append(
                        {"clause_type": clause_type, "excerpt": sentence.strip()[:300], "trigger": keyword}
                    )
                    found.add(clause_type)
                    break
    return clauses


# ------------------------------------------------------------ classification
def classify_document(text: str) -> Dict[str, Any]:
    """Regulatory document category with confidence and per-category scores."""
    zero_shot = _zero_shot() if os.getenv("NLP_ENGINE", "").lower() == "transformers" else None
    if zero_shot is not None:
        try:
            labels = list(_CATEGORY_KEYWORDS)
            output = zero_shot(text[:2000], candidate_labels=labels)
            scores = dict(zip(output["labels"], [round(s, 4) for s in output["scores"]]))
            return {"category": output["labels"][0], "confidence": round(output["scores"][0], 4), "scores": scores, "engine": "transformers"}
        except Exception:
            pass  # fall through to rules

    lowered = text.lower()
    scores = {
        category: round(sum(weight for keyword, weight in keywords.items() if keyword in lowered), 2)
        for category, keywords in _CATEGORY_KEYWORDS.items()
    }
    best = max(scores, key=lambda c: scores[c])
    total = sum(scores.values())
    if scores[best] == 0:
        return {"category": "general_correspondence", "confidence": 0.0, "scores": scores, "engine": "rule-based"}
    return {"category": best, "confidence": round(scores[best] / total, 4), "scores": scores, "engine": "rule-based"}


# ------------------------------------------------------------------ pipeline
def analyze(text: str) -> Dict[str, Any]:
    """Full NLP pass over a document: entities + clauses + classification."""
    from app.services.telemetry import span

    with span("nlp.analyze", engine=engine()):
        return {
            "engine": engine(),
            "entities": extract_entities(text),
            "clauses": extract_clauses(text),
            "classification": classify_document(text),
        }
