"""M4 — The shape the LLM must return (guide §7.4, §7.7).

The allowed types are generated FROM ontology.py, so the prompt, the JSON
schema sent to the model, and the validator can never drift apart.
This schema is also handed to Ollama as `format`, which constrains the
model's decoding to valid JSON of exactly this shape ("structured output").
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.graph.ontology import ENTITY_TYPES, RELATION_TYPES

EntityType = Literal[tuple(sorted(ENTITY_TYPES))]          # type: ignore[valid-type]
RelationType = Literal[tuple(sorted(RELATION_TYPES))]      # type: ignore[valid-type]

SCHEMA_VERSION = "extraction-v1"


class Entity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    type: EntityType


class RelProperties(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str | None = Field(default=None, max_length=100)
    since: int | None = Field(default=None, ge=1800, le=2100)


class Relationship(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=200)
    type: RelationType
    target: str = Field(min_length=1, max_length=200)
    properties: RelProperties = Field(default_factory=RelProperties)
    evidence: str = Field(min_length=5, max_length=500)


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entities: list[Entity] = Field(default_factory=list, max_length=100)
    relationships: list[Relationship] = Field(default_factory=list, max_length=100)
