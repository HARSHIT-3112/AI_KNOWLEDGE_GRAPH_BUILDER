"""M1/M2: build a small hand-written graph from Python (no LLM yet).

Guide rule: "First make the graph correct without an LLM."
Run it twice — the counts must NOT change the second time (idempotency).

Run:  .venv/bin/python -m scripts.m1_seed_sample_graph
"""
from app.core.config import get_settings
from app.db.neo4j import close_driver, get_driver
from app.graph.repository import count_graph, create_schema, neighbours, upsert_relationship

# (source_type, source, REL, target_type, target, properties)
FACTS = [
    ("Person", "Harshit", "WORKS_AT", "Company", "TechNova", {"role": "ML Engineer"}),
    ("Person", "Priya", "WORKS_AT", "Company", "TechNova", {"role": "Data Scientist"}),
    ("Person", "Karan", "WORKS_AT", "Company", "TechNova", {"role": "Backend Engineer"}),
    ("Person", "Ravi", "WORKS_AT", "Company", "DataForge", {"role": "CTO"}),
    ("Person", "Meera", "WORKS_AT", "Company", "DataForge", {"role": "Frontend Engineer"}),
    ("Person", "Ravi", "FOUNDED", "Company", "DataForge", {"since": 2019}),
    ("Person", "Harshit", "MEMBER_OF", "Team", "Vision Team", {}),
    ("Person", "Priya", "MEMBER_OF", "Team", "Vision Team", {}),
    ("Company", "TechNova", "OWNS", "Project", "VisionAI", {}),
    ("Company", "TechNova", "OWNS", "Project", "DocuMind", {}),
    ("Company", "DataForge", "OWNS", "Project", "PulseAnalytics", {}),
    ("Person", "Harshit", "WORKS_ON", "Project", "VisionAI", {}),
    ("Person", "Priya", "WORKS_ON", "Project", "VisionAI", {}),
    ("Person", "Karan", "WORKS_ON", "Project", "DocuMind", {}),
    ("Person", "Meera", "WORKS_ON", "Project", "PulseAnalytics", {}),
    ("Project", "VisionAI", "USES", "Technology", "Python", {}),
    ("Project", "VisionAI", "USES", "Technology", "PyTorch", {}),
    ("Project", "DocuMind", "USES", "Technology", "Neo4j", {}),
    ("Project", "DocuMind", "USES", "Technology", "FastAPI", {}),
    ("Project", "PulseAnalytics", "USES", "Technology", "React", {}),
    ("Person", "Harshit", "COLLABORATES_WITH", "Person", "Ravi", {}),
]


def seed(tx) -> None:
    for st, s, rel, tt, t, props in FACTS:
        upsert_relationship(tx, st, s, rel, tt, t, props)


if __name__ == "__main__":
    db = get_settings().neo4j_database
    driver = get_driver()
    try:
        create_schema(driver, db)
        before = count_graph(driver, db)
        with driver.session(database=db) as session:
            session.execute_write(seed)          # ONE transaction: all facts or none
        after = count_graph(driver, db)
        print(f"before: {before}\nafter:  {after}")
        print("\nHarshit's neighbourhood:")
        for row in neighbours(driver, db, "harshit"):
            arrow = "->" if row["outgoing"] else "<-"
            props = {k: v for k, v in row["props"].items() if k != "id"}
            print(f"  Harshit {arrow}[{row['rel']}] {row['other']} ({row['otherType']}) {props or ''}")
    finally:
        close_driver()
