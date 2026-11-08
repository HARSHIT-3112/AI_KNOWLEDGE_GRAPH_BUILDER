"""M4 — Don't trust the model: check every proposed fact (guide §7.5, §7.8).

Pydantic already guaranteed the SHAPE (allowed type names, field lengths).
This module checks the MEANING, against the source chunk:

  entity      : its name actually occurs in the chunk text (no invented entities)
                and it isn't given two different types (ambiguous -> flagged)
  relationship: both endpoints resolve to accepted entities
                direction/type combination is allowed by the ontology
                not a self-loop, not a duplicate
                the evidence quote really appears in the chunk  <- grounding
                the quote names BOTH endpoints and any role/year, else -> needs_review

Nothing is silently dropped: every rejection carries a reason, so you can
see whether the prompt, the model, or the ontology needs fixing.
"""
import re
from dataclasses import dataclass, field

from app.graph.ids import normalize_name
from app.graph.ontology import check_relation
from app.schemas.extraction import Extraction

_QUOTE_MAP = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                            "–": "-", "—": "-", " ": " "})


def normalize_text(text: str) -> str:
    text = text.translate(_QUOTE_MAP).casefold()
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" .,;:\"'")


@dataclass
class AcceptedEntity:
    name: str
    type: str


@dataclass
class AcceptedRelationship:
    source: str
    source_type: str
    type: str
    target: str
    target_type: str
    properties: dict
    evidence: str
    chunk_id: str


@dataclass
class Rejection:
    item: dict
    reason: str


@dataclass
class ValidationReport:
    chunk_id: str
    entities: list[AcceptedEntity] = field(default_factory=list)
    relationships: list[AcceptedRelationship] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
    needs_review: list[Rejection] = field(default_factory=list)   # plausible but not proven -> human


def validate_extraction(extraction: Extraction, chunk_text: str, chunk_id: str) -> ValidationReport:
    report = ValidationReport(chunk_id=chunk_id)
    chunk_norm = normalize_text(chunk_text)

    # ---- entities -------------------------------------------------------
    types_by_name: dict[str, set[str]] = {}
    for e in extraction.entities:
        types_by_name.setdefault(normalize_name(e.name), set()).add(e.type)

    entity_by_name: dict[str, AcceptedEntity] = {}
    for e in extraction.entities:
        key = normalize_name(e.name)
        item = e.model_dump()
        if key in entity_by_name:
            continue                                                   # exact duplicate
        if len(types_by_name[key]) > 1:
            if not any(r.item.get("name") == e.name and r.reason.startswith("ambiguous") for r in report.rejected):
                report.rejected.append(Rejection(item, f"ambiguous: given types {sorted(types_by_name[key])}"))
            continue
        if normalize_text(e.name) not in chunk_norm:
            report.rejected.append(Rejection(item, "entity name does not appear in the chunk"))
            continue
        entity_by_name[key] = AcceptedEntity(name=e.name.strip(), type=e.type)
    report.entities = list(entity_by_name.values())

    # ---- relationships --------------------------------------------------
    seen: set[tuple[str, str, str]] = set()
    for r in extraction.relationships:
        item = r.model_dump()
        src = entity_by_name.get(normalize_name(r.source))
        tgt = entity_by_name.get(normalize_name(r.target))
        if src is None or tgt is None:
            missing = r.source if src is None else r.target
            report.rejected.append(Rejection(item, f"endpoint {missing!r} is not an accepted entity"))
            continue
        if normalize_name(src.name) == normalize_name(tgt.name):
            report.rejected.append(Rejection(item, "self-loop"))
            continue
        try:
            check_relation(r.type, src.type, tgt.type)
        except ValueError as exc:
            report.rejected.append(Rejection(item, f"ontology: {exc}"))
            continue
        evidence = normalize_text(r.evidence)
        if len(evidence) < 5 or evidence not in chunk_norm:
            report.rejected.append(Rejection(item, "evidence quote not found in the chunk"))
            continue
        key = (normalize_name(src.name), r.type, normalize_name(tgt.name))
        if key in seen:
            continue
        seen.add(key)
        # The quote exists, but does it talk about THESE two entities?
        # Real failure seen with qwen2.5:7b: a quote about Meera attached to Ravi.
        # Pronouns ("she works on X") also land here -> review, not silent accept.
        unnamed = [n for n in (src.name, tgt.name) if normalize_text(n) not in evidence]
        if unnamed:
            report.needs_review.append(Rejection(item, f"evidence does not mention {unnamed}"))
            continue
        # Same idea for properties: role/since must be in the quote too.
        # Real failure: role 'CTO' attached with evidence "DataForge was founded by Ravi".
        props = r.properties.model_dump(exclude_none=True)
        unsupported = [f"{k}={v}" for k, v in props.items() if normalize_text(str(v)) not in evidence]
        if unsupported:
            report.needs_review.append(Rejection(item, f"evidence does not support {unsupported}"))
            continue
        report.relationships.append(AcceptedRelationship(
            source=src.name, source_type=src.type, type=r.type, target=tgt.name, target_type=tgt.type,
            properties=props, evidence=r.evidence.strip(), chunk_id=chunk_id,
        ))
    return report
