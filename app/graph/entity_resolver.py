"""M5 — Entity resolution: is this mention an entity we already know? (guide §8.1, §8.2)

Strategy (deterministic first, as the guide recommends):
  1. Look up an existing entity of the SAME TYPE with the same compact key
     ('Tech Nova' == 'TechNova' == 'TECH-NOVA').
       one match   -> reuse its id, remember the new spelling in e.aliases
       no match    -> create a new entity with a deterministic id
       many match  -> AMBIGUOUS: never guess, send to human review
  2. Type matters: 'VisionAI' the Project and 'VisionAI' the Company stay separate.

Not done here (later / advanced): pronoun coreference across chunks, fuzzy or
embedding similarity ("T. Nova Inc."). Those need confidence thresholds and
review, because a WRONG merge (two different people fused) is worse than a
missed one — it silently corrupts every query.
"""
from dataclasses import dataclass

from neo4j import ManagedTransaction

from app.graph.ids import compact_key
from app.graph.ontology import check_entity_type
from app.graph.repository import upsert_entity


class AmbiguousEntity(ValueError):
    pass


@dataclass(frozen=True)
class Resolved:
    id: str
    name: str            # the canonical (stored) name
    created: bool
    alias_added: bool


def resolve_entity(tx: ManagedTransaction, entity_type: str, name: str) -> Resolved:
    label = check_entity_type(entity_type)
    rows = tx.run(
        "MATCH (e:Entity {type: $type, compactName: $ck}) RETURN e.id AS id, e.name AS name LIMIT 2",
        type=label, ck=compact_key(name),
    ).data()
    if len(rows) > 1:
        raise AmbiguousEntity(f"{label} {name!r} matches several entities: {[r['name'] for r in rows]}")
    if not rows:
        return Resolved(upsert_entity(tx, label, name), name.strip(), created=True, alias_added=False)

    existing = rows[0]
    spelled = name.strip()
    alias_added = False
    if spelled != existing["name"]:
        alias_added = tx.run(
            """
            MATCH (e:Entity {id: $id})
            WITH e, coalesce(e.aliases, []) AS aliases
            WHERE NOT $alias IN aliases
            SET e.aliases = aliases + $alias
            RETURN true AS added
            """,
            id=existing["id"], alias=spelled,
        ).single() is not None
    return Resolved(existing["id"], existing["name"], created=False, alias_added=alias_added)
