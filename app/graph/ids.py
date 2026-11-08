"""M2 — Stable, deterministic IDs (guide §8.2, §8.7).

Same input -> same ID, every time. That is what makes re-running ingestion
idempotent: MERGE on a deterministic id finds the existing node instead of
creating a duplicate. Never use Neo4j's internal element IDs for this.
"""
import hashlib
import re


def normalize_name(name: str) -> str:
    """'  Tech   Nova ' -> 'tech nova'. Deterministic, no guessing."""
    return re.sub(r"\s+", " ", name.strip()).casefold()


def compact_key(name: str) -> str:
    """Matching key that ignores spacing/punctuation: 'Tech Nova', 'Tech-Nova', 'TECHNOVA' -> 'technova'.
    Keeps non-English letters (\\w is Unicode-aware); falls back if nothing is left."""
    return re.sub(r"[\W_]+", "", normalize_name(name)) or normalize_name(name)


def _short_hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def entity_id(entity_type: str, name: str) -> str:
    return f"{entity_type.lower()}-{_short_hash(entity_type, normalize_name(name))}"


def relation_id(source_id: str, rel_type: str, target_id: str) -> str:
    return f"rel-{_short_hash(source_id, rel_type, target_id)}"
