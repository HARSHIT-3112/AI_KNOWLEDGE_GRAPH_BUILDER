"""M0 — Configuration.

Settings are read from environment variables / the .env file, never hard-coded.
Concept: secrets live outside code so the code can be shared safely.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    neo4j_uri: str = "bolt://localhost:7688"
    neo4j_username: str = "neo4j"
    neo4j_password: str
    neo4j_database: str = "neo4j"

    # M4 — local LLM via Ollama (no API key needed)
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"
    llm_timeout_seconds: float = 600.0   # 8 GB Macs: a 7B model can take minutes per chunk
    llm_max_attempts: int = 3
    llm_num_ctx: int = 4096              # context window; bigger = more RAM

    # M7 — embeddings (also via Ollama). Dimensions MUST match the model's output.
    embedding_model: str = "mxbai-embed-large"
    embedding_dimensions: int = 1024

    # M8 — below this top cosine score (and no linked entities) the question is
    # treated as off-topic and answered "not in the knowledge base" WITHOUT calling the LLM.
    # Calibrated on this corpus: on-topic 0.80–0.87, off-topic 0.65–0.70.
    min_vector_score: float = 0.76

    # M9 — API + worker
    api_key: str                          # required: clients send it as the X-API-Key header
    upload_dir: str = "documents/uploads"
    max_upload_bytes: int = 15 * 1024 * 1024
    # M10 — rate limits per API key, requests per minute
    rate_ask_per_minute: int = 10         # each /ask is an LLM call
    rate_upload_per_minute: int = 30
    rate_read_per_minute: int = 300
    job_queue: str = "default"           # workers only claim jobs from their own queue (tests use a private one)
    job_max_attempts: int = 3            # transient failures (Neo4j/Ollama down) are retried this often
    job_retry_base_seconds: int = 30      # backoff: 30 s, 60 s, 120 s ...
    job_stale_after_minutes: int = 30     # a "processing" job older than this is assumed crashed

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
