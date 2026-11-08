"""M10 — Rate limiting (guide §12.4: limit processing time and LLM budget).

Token bucket per (client, route group): `capacity` requests at once, refilled at
`per_minute`/60 tokens per second. /ask is the expensive one (an LLM call), so
it gets the tightest limit; exceeding it returns 429 with Retry-After.

Limitation (be honest about it): state lives in THIS process's memory. With
several API processes, each has its own buckets — use Redis or the gateway
(nginx, cloud API gateway) for a shared limit.
"""
import threading
import time

from fastapi import HTTPException, Request, status


class TokenBucket:
    def __init__(self, per_minute: int, capacity: int | None = None):
        self.rate = per_minute / 60.0
        self.capacity = capacity or per_minute
        self._state: dict[str, tuple[float, float]] = {}       # key -> (tokens, last_time)
        self._lock = threading.Lock()

    def take(self, key: str) -> float:
        """Consume one token. Returns 0 if allowed, else seconds until the next token."""
        now = time.monotonic()
        with self._lock:
            tokens, last = self._state.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens >= 1:
                self._state[key] = (tokens - 1, now)
                return 0.0
            self._state[key] = (tokens, now)
            return (1 - tokens) / self.rate


def limit(bucket: TokenBucket, group: str):
    def dependency(request: Request) -> None:
        client = request.headers.get("X-API-Key", "")[-8:] or (request.client.host if request.client else "?")
        wait = bucket.take(f"{group}:{client}")
        if wait:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"rate limit for {group}; retry later",
                                headers={"Retry-After": str(int(wait) + 1)})
    return dependency
