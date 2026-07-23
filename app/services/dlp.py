"""Data-loss prevention: PII masking and prompt guardrails.

Conservative by design — patterns are chosen to avoid mangling business text
(amounts, client ids). Applied before text is indexed for retrieval, before
copilot queries reach generation, and before audit metadata is persisted.

Redaction engines:
- **regex** (default) — deterministic, offline, dependency-free.
- **presidio** — set ``DLP_ENGINE=presidio`` with Microsoft Presidio
  installed (``pip install -r requirements-ml.txt``) for ML-driven PII
  detection (names, locations, and locale-specific identifiers the regex
  patterns don't cover). Any Presidio failure falls back to the regex path
  so redaction never silently disappears.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Tuple

# --------------------------------------------------------------------- PII
_PII_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    # International (+44...) or separator-formatted phone numbers only —
    # a bare digit run like "500000" is an amount, not a phone number.
    ("PHONE", re.compile(r"(?:\+\d{1,3}[\s-]?)\d(?:[\s-]?\d){6,12}\b|\b\d{3}[\s-]\d{3}[\s-]\d{4}\b")),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("UK_NINO", re.compile(r"\b[A-CEGHJ-PR-TW-Z]{2}\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-D]\b")),
]


def _luhn_valid(number: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", number)]
    if not 13 <= len(digits) <= 19:
        return False
    checksum = 0
    for i, digit in enumerate(reversed(digits)):
        if i % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


# Presidio entity types -> the platform's placeholder labels.
_PRESIDIO_LABELS = {
    "EMAIL_ADDRESS": "EMAIL",
    "PHONE_NUMBER": "PHONE",
    "IBAN_CODE": "IBAN",
    "CREDIT_CARD": "CARD",
    "UK_NINO": "UK_NINO",
    "PERSON": "PERSON",
    "LOCATION": "LOCATION",
    "US_SSN": "SSN",
}

_presidio_analyzer: Any = None
_presidio_failed = False


def _get_presidio_analyzer() -> Any:
    """Presidio AnalyzerEngine when ``DLP_ENGINE=presidio``; else ``None``."""
    global _presidio_analyzer, _presidio_failed
    if os.getenv("DLP_ENGINE", "regex").lower() != "presidio" or _presidio_failed:
        return None
    if _presidio_analyzer is None:
        try:
            from presidio_analyzer import AnalyzerEngine

            _presidio_analyzer = AnalyzerEngine()
        except Exception:
            _presidio_failed = True
            return None
    return _presidio_analyzer


def _mask_pii_presidio(text: str, analyzer: Any) -> Tuple[str, List[str]]:
    results = analyzer.analyze(text=text, language="en", entities=list(_PRESIDIO_LABELS))
    found: List[str] = []
    masked = text
    # Replace right-to-left so earlier spans keep their offsets.
    for result in sorted(results, key=lambda r: r.start, reverse=True):
        label = _PRESIDIO_LABELS.get(result.entity_type, result.entity_type)
        if label not in found:
            found.append(label)
        masked = masked[: result.start] + f"[{label}]" + masked[result.end :]
    return masked, found


def mask_pii(text: str) -> Tuple[str, List[str]]:
    """Replace PII with typed placeholders; return (masked_text, types_found)."""
    analyzer = _get_presidio_analyzer()
    if analyzer is not None:
        try:
            return _mask_pii_presidio(text, analyzer)
        except Exception:
            pass  # regex fallback below — redaction must never be skipped

    found: List[str] = []
    masked = text
    for pii_type, pattern in _PII_PATTERNS:
        def _replace(match: re.Match[str], pii_type: str = pii_type) -> str:
            value = match.group(0)
            # Card numbers must pass Luhn — otherwise it's just a long number.
            if pii_type == "CARD" and not _luhn_valid(value):
                return value
            if pii_type not in found:
                found.append(pii_type)
            return f"[{pii_type}]"

        masked = pattern.sub(_replace, masked)
    return masked, found


# --------------------------------------------------------------- guardrails
_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+(instructions|prompts|rules)", re.I),
    re.compile(r"disregard\s+(your|the|all)\s+(instructions|rules|guidelines|system\s+prompt)", re.I),
    re.compile(r"(reveal|show|print|repeat)\s+(your|the)\s+(system\s+)?prompt", re.I),
    re.compile(r"you\s+are\s+now\s+(a|an|in)\b", re.I),
    re.compile(r"(jailbreak|dan\s+mode|developer\s+mode)", re.I),
    re.compile(r"pretend\s+(you\s+have\s+no|there\s+are\s+no)\s+(rules|restrictions|guidelines)", re.I),
]


def guard_prompt(query: str) -> Dict[str, Any]:
    """Screen a user query before it reaches retrieval/generation.

    Returns ``allowed`` (injection attempts are refused), the PII-``sanitised``
    query to use downstream, and the ``flags`` explaining any action taken.
    """
    flags: List[str] = []
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(query):
            flags.append("prompt_injection")
            break
    sanitised, pii_types = mask_pii(query)
    flags.extend(f"pii_masked:{t}" for t in pii_types)
    return {
        "allowed": "prompt_injection" not in flags,
        "sanitised": sanitised,
        "flags": flags,
    }
