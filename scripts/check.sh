#!/usr/bin/env bash
# M10 — Quality gate. Run before every change you keep (guide §12.3 "regression tests").
#
#   bash scripts/check.sh
#
# 1. the full test suite (unit + Neo4j integration + API/worker + hardening)
# 2. retrieval regression: hybrid search must stay at or above the M7 baseline
#    (Recall@5 1.00, MRR 0.87 when first measured; thresholds leave a little room)
# Needs Neo4j (docker compose up -d neo4j) and Ollama for step 2.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python

echo "== 1/2 tests"
$PY -m pytest -q -p no:warnings

echo
echo "== 2/2 retrieval regression gate"
$PY -m scripts.m7_eval --gate hybrid --min-recall 0.93 --min-mrr 0.80

echo
echo "ALL CHECKS PASSED"
