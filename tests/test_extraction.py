"""M4 tests — no real LLM needed (the model is faked), so they're fast and deterministic.

These are the "LLM contract tests" from guide §12.3: whatever the model says,
only schema-valid, ontology-valid, evidence-grounded facts get through.
"""
import json

import httpx
import pytest
from pydantic import ValidationError

from app.extraction.llm_client import ExtractionFailed, OllamaClient
from app.extraction.validator import validate_extraction
from app.schemas.extraction import Extraction

CHUNK = ("Harshit works at TechNova as an ML Engineer. He uses Python and works on VisionAI. "
         "DataForge was founded by Ravi in 2019.")


def ext(entities, relationships):
    return Extraction.model_validate({"entities": entities, "relationships": relationships})


E = lambda n, t: {"name": n, "type": t}
R = lambda s, t, o, ev, **p: {"source": s, "type": t, "target": o, "evidence": ev, "properties": p}

BASE_ENTITIES = [E("Harshit", "Person"), E("TechNova", "Company"), E("VisionAI", "Project"),
                 E("Python", "Technology"), E("Ravi", "Person"), E("DataForge", "Company")]


# ------------------------------------------------------------ schema layer
def test_schema_rejects_type_outside_ontology():
    with pytest.raises(ValidationError):
        ext([E("Harshit", "Alien")], [])


def test_schema_rejects_unknown_relation_and_extra_fields():
    with pytest.raises(ValidationError):
        ext([], [R("a", "HATES", "b", "a hates b")])
    with pytest.raises(ValidationError):
        Extraction.model_validate({"entities": [], "relationships": [], "cypher": "MATCH (n) DELETE n"})


# --------------------------------------------------------- validator layer
def test_good_facts_are_accepted_with_chunk_id():
    rep = validate_extraction(ext(BASE_ENTITIES, [
        R("Harshit", "WORKS_AT", "TechNova", "Harshit works at TechNova as an ML Engineer.", role="ML Engineer"),
        R("Ravi", "FOUNDED", "DataForge", "DataForge was founded by Ravi in 2019", since=2019),
    ]), CHUNK, "chunk-1")
    assert [(r.source, r.type, r.target) for r in rep.relationships] == [
        ("Harshit", "WORKS_AT", "TechNova"), ("Ravi", "FOUNDED", "DataForge")]
    assert rep.relationships[0].properties == {"role": "ML Engineer"}
    assert all(r.chunk_id == "chunk-1" for r in rep.relationships)
    assert rep.rejected == []


def test_fabricated_evidence_is_rejected():
    rep = validate_extraction(ext(BASE_ENTITIES, [
        R("Harshit", "WORKS_ON", "VisionAI", "Harshit leads the VisionAI project"),   # not in text
    ]), CHUNK, "c")
    assert rep.relationships == [] and "evidence" in rep.rejected[0].reason


def test_evidence_matching_tolerates_case_whitespace_and_smart_quotes():
    rep = validate_extraction(ext(BASE_ENTITIES, [
        R("Harshit", "WORKS_AT", "TechNova", "  HARSHIT works at   “TechNova”. ".replace("“", "").replace("”", "")),
    ]), CHUNK, "c")
    assert len(rep.relationships) == 1


def test_hallucinated_entity_is_rejected_and_its_relations_too():
    rep = validate_extraction(ext(BASE_ENTITIES + [E("Google", "Company")], [
        R("Harshit", "WORKS_AT", "Google", "Harshit works at TechNova"),
    ]), CHUNK, "c")
    reasons = [x.reason for x in rep.rejected]
    assert "entity name does not appear in the chunk" in reasons
    assert any("not an accepted entity" in r for r in reasons)


def test_wrong_direction_is_rejected_by_ontology():
    rep = validate_extraction(ext(BASE_ENTITIES, [
        R("TechNova", "WORKS_AT", "Harshit", "Harshit works at TechNova"),
    ]), CHUNK, "c")
    assert rep.relationships == [] and rep.rejected[0].reason.startswith("ontology")


def test_real_quote_about_someone_else_goes_to_review():
    # Seen live with qwen2.5:7b: a genuine quote about Harshit attached to Ravi.
    rep = validate_extraction(ext(BASE_ENTITIES, [
        R("Ravi", "WORKS_AT", "TechNova", "Harshit works at TechNova as an ML Engineer."),
    ]), CHUNK, "c")
    assert rep.relationships == [] and "Ravi" in rep.needs_review[0].reason


def test_property_not_in_quote_goes_to_review():
    # Seen live: role 'CTO' with evidence "DataForge was founded by Ravi".
    rep = validate_extraction(ext(BASE_ENTITIES, [
        R("Ravi", "FOUNDED", "DataForge", "DataForge was founded by Ravi", since=2020),
    ]), CHUNK, "c")
    assert rep.relationships == [] and "since=2020" in rep.needs_review[0].reason


def test_pronoun_evidence_goes_to_review_not_accepted():
    rep = validate_extraction(ext(BASE_ENTITIES, [R("Harshit", "USES", "Python", "He uses Python")]), CHUNK, "c")
    assert rep.relationships == [] and len(rep.needs_review) == 1


def test_ambiguous_entity_type_is_flagged_not_guessed():
    rep = validate_extraction(ext([E("VisionAI", "Project"), E("VisionAI", "Company")], []), CHUNK, "c")
    assert rep.entities == [] and rep.rejected[0].reason.startswith("ambiguous")


def test_duplicates_and_self_loops():
    rep = validate_extraction(ext(BASE_ENTITIES + [E("harshit", "Person")], [
        R("Harshit", "WORKS_AT", "TechNova", "Harshit works at TechNova"),
        R("Harshit", "WORKS_AT", "TechNova", "Harshit works at TechNova"),
        R("Harshit", "COLLABORATES_WITH", "harshit", "Harshit works at TechNova"),
    ]), CHUNK, "c")
    assert len(rep.relationships) == 1
    assert [x.reason for x in rep.rejected] == ["self-loop"]


# ------------------------------------------------------------ client layer
def fake_ollama(replies):
    """An httpx transport that pretends to be Ollama, returning `replies` in order."""
    calls = iter(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        assert sent["options"]["temperature"] == 0 and "properties" in sent["format"]
        return httpx.Response(200, json={"message": {"content": next(calls)}, "eval_count": 7})
    return httpx.MockTransport(handler)


def test_client_retries_malformed_output_then_succeeds():
    good = json.dumps({"entities": [E("Harshit", "Person")], "relationships": []})
    client = OllamaClient(model="fake", url="http://x", max_attempts=3,
                          transport=fake_ollama(["Sure! Here is the JSON:", '{"entities": [{"name": "X", "type": "Alien"}]}', good]))
    result = client.extract("Harshit")
    assert result.attempts == 3 and result.extraction.entities[0].name == "Harshit"
    assert len(result.raw_attempts) == 3


def test_client_retries_after_timeout():
    good = json.dumps({"entities": [], "relationships": []})
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ReadTimeout("slow model", request=request)
        return httpx.Response(200, json={"message": {"content": good}})

    client = OllamaClient(model="fake", url="http://x", max_attempts=3, transport=httpx.MockTransport(handler))
    result = client.extract("text")
    assert result.attempts == 2 and result.raw_attempts[0].startswith("<timeout")


def test_client_gives_up_after_bounded_attempts():
    client = OllamaClient(model="fake", url="http://x", max_attempts=2, transport=fake_ollama(["nope", "still nope"]))
    with pytest.raises(ExtractionFailed) as info:
        client.extract("text")
    assert info.value.raw_attempts == ["nope", "still nope"]
