"""Unit tests — no database needed (guide §12.3, bottom of the pyramid)."""
import pytest

from app.graph.ids import entity_id, normalize_name, relation_id
from app.graph.ontology import check_entity_type, check_relation
from app.ingestion.chunker import chunk_pages, chunk_text
from app.ingestion.file_validator import InvalidDocument, validate_file


# ---- ids
def test_normalize_name():
    assert normalize_name("  Tech   Nova \n") == "tech nova"


def test_entity_id_is_deterministic_and_case_insensitive():
    assert entity_id("Company", "TechNova") == entity_id("Company", "  technova ")
    assert entity_id("Company", "TechNova") != entity_id("Project", "TechNova")


def test_relation_id_depends_on_direction():
    assert relation_id("a", "WORKS_AT", "b") != relation_id("b", "WORKS_AT", "a")


# ---- ontology
def test_ontology_rejects_unknown_label():
    with pytest.raises(ValueError):
        check_entity_type("Person) DETACH DELETE (n")      # injection attempt


def test_ontology_rejects_wrong_direction():
    check_relation("WORKS_AT", "Person", "Company")
    with pytest.raises(ValueError):
        check_relation("WORKS_AT", "Company", "Person")


# ---- chunker
TEXT = "\n\n".join(f"Paragraph {i}. " + "word " * 40 for i in range(10))


def test_chunks_respect_max_size():
    for c in chunk_text(TEXT, max_chars=300, overlap_chars=50):
        assert len(c) <= 300


def test_huge_paragraph_is_split():
    giant = "This is a sentence. " * 200          # one paragraph, ~4000 chars
    chunks = chunk_text(giant, max_chars=500, overlap_chars=50)
    assert len(chunks) > 1 and all(len(c) <= 500 for c in chunks)


def test_every_paragraph_is_covered():
    joined = " ".join(chunk_text(TEXT, max_chars=300, overlap_chars=50))
    for i in range(10):
        assert f"Paragraph {i}." in joined


def test_chunk_ids_are_stable():
    pages = [{"page": 1, "text": TEXT}]
    a = [c.id for c in chunk_pages("doc-x", pages, 300, 50)]
    b = [c.id for c in chunk_pages("doc-x", pages, 300, 50)]
    assert a == b and len(set(a)) == len(a)


def test_overlap_must_be_smaller_than_max():
    with pytest.raises(ValueError):
        chunk_text("abc", max_chars=100, overlap_chars=100)


# ---- validator
def test_same_bytes_same_document_id(tmp_path):
    (tmp_path / "a.txt").write_text("hello graph")
    (tmp_path / "b.txt").write_text("hello graph")
    assert validate_file(tmp_path / "a.txt").id == validate_file(tmp_path / "b.txt").id


@pytest.mark.parametrize("name,content", [("x.exe", b"MZ"), ("empty.txt", b""), ("fake.pdf", b"not a pdf")])
def test_invalid_files_rejected(tmp_path, name, content):
    (tmp_path / name).write_bytes(content)
    with pytest.raises(InvalidDocument):
        validate_file(tmp_path / name)
