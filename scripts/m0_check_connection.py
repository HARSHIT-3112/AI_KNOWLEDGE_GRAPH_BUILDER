"""M0 acceptance test: Python can reach Neo4j with credentials from .env.

Run:  .venv/bin/python -m scripts.m0_check_connection
"""
from app.db.neo4j import close_driver, health_check

if __name__ == "__main__":
    try:
        print("Connected to Neo4j:", health_check())
    finally:
        close_driver()
