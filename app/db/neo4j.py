"""M0 — Driver lifecycle.

Concepts (guide §5.1):
  * Driver  = long-lived, shared, owns the connection pool. Create ONE per app.
  * Session = cheap, short-lived, one unit of work. Open, use, close.
  * Transaction = group of operations that commit or roll back together.
"""
from neo4j import Driver, GraphDatabase

from app.core.config import get_settings

_driver: Driver | None = None


def get_driver() -> Driver:
    global _driver
    if _driver is None:
        s = get_settings()
        _driver = GraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_username, s.neo4j_password))
    return _driver


def close_driver() -> None:
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None


def health_check() -> dict:
    """Return server info, or raise if Neo4j is unreachable / auth is wrong."""
    driver = get_driver()
    driver.verify_connectivity()
    info = driver.get_server_info()
    return {"address": str(info.address), "agent": info.agent, "protocol": str(info.protocol_version)}
