"""M3 — Validate a file and fingerprint it (guide §6.2).

The SHA-256 of the file BYTES becomes the document id. Upload the same file
twice (even under a different name) -> same id -> no duplicate Document.
"""
import hashlib
from dataclasses import dataclass
from pathlib import Path

ALLOWED_SUFFIXES = {".pdf", ".txt", ".md"}
MAX_BYTES = 15 * 1024 * 1024


class InvalidDocument(ValueError):
    pass


@dataclass(frozen=True)
class DocumentInfo:
    id: str
    path: Path
    filename: str
    suffix: str
    sha256: str
    size_bytes: int


def validate_file(path: str | Path) -> DocumentInfo:
    p = Path(path)
    if not p.is_file():
        raise InvalidDocument(f"Not a file: {p}")
    suffix = p.suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise InvalidDocument(f"Unsupported file type {suffix!r}; allowed: {sorted(ALLOWED_SUFFIXES)}")
    data = p.read_bytes()
    if not data:
        raise InvalidDocument("File is empty")
    if len(data) > MAX_BYTES:
        raise InvalidDocument(f"File too large ({len(data)} bytes > {MAX_BYTES})")
    if suffix == ".pdf" and not data.startswith(b"%PDF"):
        raise InvalidDocument("File has .pdf extension but is not a PDF")
    digest = hashlib.sha256(data).hexdigest()
    return DocumentInfo(
        id=f"doc-{digest[:16]}", path=p, filename=p.name, suffix=suffix,
        sha256=digest, size_bytes=len(data),
    )
