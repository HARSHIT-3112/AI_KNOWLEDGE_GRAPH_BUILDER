"""M4 — Talk to the local LLM through Ollama's HTTP API (guide §7.2).

Key ideas:
  * `format=<JSON schema>` -> Ollama constrains generation to that schema.
  * temperature 0 -> as repeatable as possible (extraction is not creative work).
  * Bounded retries: malformed output is retried a fixed number of times, then
    we give up loudly (guide §2.2) instead of looping forever.
  * We keep the RAW text of every attempt so failures can be diagnosed.
"""
from dataclasses import dataclass, field

import httpx
from pydantic import BaseModel, ValidationError

from app.core.config import get_settings
from app.extraction.prompts import SYSTEM_PROMPT, build_user_prompt
from app.schemas.extraction import Extraction


class ExtractionFailed(RuntimeError):
    def __init__(self, message: str, raw_attempts: list[str]):
        super().__init__(message)
        self.raw_attempts = raw_attempts


@dataclass
class LLMResult:
    parsed: BaseModel                 # an Extraction (M4) or an Answer (M8) — whatever schema was asked for
    raw: str
    model: str
    attempts: int
    prompt_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    raw_attempts: list[str] = field(default_factory=list)

    @property
    def extraction(self) -> Extraction:
        return self.parsed  # type: ignore[return-value]


class OllamaClient:
    def __init__(self, model: str | None = None, url: str | None = None, timeout: float | None = None,
                 max_attempts: int | None = None, transport: httpx.BaseTransport | None = None):
        s = get_settings()
        self.model = model or s.ollama_model
        self.max_attempts = max_attempts or s.llm_max_attempts
        self.num_ctx = s.llm_num_ctx
        self._http = httpx.Client(base_url=url or s.ollama_url, timeout=timeout or s.llm_timeout_seconds,
                                  transport=transport)

    def ensure_model(self) -> None:
        names = {m["name"] for m in self._http.get("/api/tags").raise_for_status().json().get("models", [])}
        if self.model not in names and f"{self.model}:latest" not in names:
            raise RuntimeError(f"Ollama model {self.model!r} not found. Run: ollama pull {self.model}")

    def extract(self, chunk_text: str) -> LLMResult:
        return self.chat_json(SYSTEM_PROMPT, build_user_prompt(chunk_text), Extraction)

    def chat_json(self, system: str, user: str, schema: type[BaseModel]) -> LLMResult:
        """One system+user exchange whose reply must validate against `schema`."""
        payload = {
            "model": self.model,
            "stream": False,
            "format": schema.model_json_schema(),
            "options": {"temperature": 0, "num_ctx": self.num_ctx},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        raws: list[str] = []
        for attempt in range(1, self.max_attempts + 1):
            try:
                body = self._http.post("/api/chat", json=payload).raise_for_status().json()
            except httpx.TimeoutException:                       # transient: count it, retry
                raws.append(f"<timeout on attempt {attempt}>")
                continue
            raw = body.get("message", {}).get("content", "")
            raws.append(raw)
            try:
                parsed = schema.model_validate_json(raw)             # schema check #1
            except ValidationError:
                continue                                             # bounded retry
            return LLMResult(
                parsed=parsed, raw=raw, model=self.model, attempts=attempt,
                prompt_tokens=body.get("prompt_eval_count", 0), output_tokens=body.get("eval_count", 0),
                seconds=body.get("total_duration", 0) / 1e9, raw_attempts=raws,
            )
        raise ExtractionFailed(f"No schema-valid output after {self.max_attempts} attempts", raws)

    def close(self) -> None:
        self._http.close()
