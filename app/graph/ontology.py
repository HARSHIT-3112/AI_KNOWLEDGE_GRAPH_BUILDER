"""M2 — Ontology: the CONTRACT for what the graph may contain (guide §3.5).

Everything downstream (LLM prompt, Pydantic validation, Cypher templates)
reads from here. If it's not in these allow-lists, it never reaches Neo4j.
"""

# Entity labels allowed in the graph. Every entity ALSO gets the shared
# label :Entity so one uniqueness constraint covers all of them.
ENTITY_TYPES: frozenset[str] = frozenset({"Person", "Company", "Project", "Technology", "Team"})

# Relationship type -> (allowed source labels, allowed target labels)
RELATION_TYPES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "WORKS_AT":          (frozenset({"Person"}), frozenset({"Company"})),
    "FOUNDED":           (frozenset({"Person"}), frozenset({"Company"})),
    "MEMBER_OF":         (frozenset({"Person"}), frozenset({"Team"})),
    "WORKS_ON":          (frozenset({"Person", "Team"}), frozenset({"Project"})),
    "USES":              (frozenset({"Person", "Project", "Team", "Company"}), frozenset({"Technology"})),
    "COLLABORATES_WITH": (frozenset({"Person"}), frozenset({"Person"})),
    "OWNS":              (frozenset({"Company", "Team"}), frozenset({"Project"})),
}

# Relationship properties we accept (anything else is dropped).
ALLOWED_REL_PROPERTIES: frozenset[str] = frozenset({"role", "since"})


def check_entity_type(label: str) -> str:
    if label not in ENTITY_TYPES:
        raise ValueError(f"Entity type {label!r} is not in the ontology")
    return label


def check_relation(rel_type: str, source_type: str, target_type: str) -> str:
    if rel_type not in RELATION_TYPES:
        raise ValueError(f"Relation type {rel_type!r} is not in the ontology")
    sources, targets = RELATION_TYPES[rel_type]
    if source_type not in sources or target_type not in targets:
        raise ValueError(f"{source_type}-[:{rel_type}]->{target_type} is not allowed by the ontology")
    return rel_type
