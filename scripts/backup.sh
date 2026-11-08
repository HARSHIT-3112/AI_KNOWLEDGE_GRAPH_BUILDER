#!/usr/bin/env bash
# M10 — Back up the Neo4j database to backups/<timestamp>/neo4j.dump, then PROVE it restores.
#
#   bash scripts/backup.sh
#
# Neo4j Community can only dump an OFFLINE database, so this:
#   1. records node/relationship counts            (what "correct" looks like)
#   2. stops api + worker + neo4j                    (~30 s downtime)
#   3. dumps with neo4j-admin into backups/<ts>/
#   4. starts everything again
#   5. restore test: loads the dump into a THROWAWAY volume, starts a temporary Neo4j on it,
#      compares counts with step 1, then deletes the temporary container + volume.
#      Your live database is never touched by the restore test.
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE="neo4j:2026.10.1@sha256:7dbd307cc8f35d6a061cf71659d3d8f783c3db9e8c2f5bb9d54f77921931952a"
VOLUME="ai_knowledge_graph_kg_neo4j_data"
PASSWORD="$(grep '^NEO4J_PASSWORD=' .env | cut -d= -f2-)"
TS="$(date +%Y%m%d-%H%M%S)"
OUT="backups/$TS"
mkdir -p "$OUT"

count() {  # $1 = container name
  docker exec "$1" cypher-shell -u neo4j -p "$PASSWORD" --format plain \
    "MATCH (n) WITH count(n) AS n MATCH ()-[r]->() RETURN n, count(r) AS r" | tail -1
}

echo "1/5 counting live data"
EXPECTED="$(count kg-learning-neo4j)"
echo "    nodes, relationships = $EXPECTED"

restart_services() { docker compose start neo4j api worker >/dev/null 2>&1 || true; }
# Whatever happens from here on (even a failed dump), bring the services back up.
trap restart_services EXIT

echo "2/5 stopping services (writers first)"
docker compose stop worker api >/dev/null
# Lesson learned: the default 10 s grace period is too short — Docker force-killed Neo4j before
# it finished flushing, and the dump then refused ("Active logical log detected"). Give it 2 min.
docker compose stop -t 120 neo4j >/dev/null

echo "3/5 dumping"
docker run --rm -v "$VOLUME:/data" -v "$PWD/$OUT:/backups" "$IMAGE" \
  neo4j-admin database dump neo4j --to-path=/backups >"$OUT/dump.log" 2>&1 \
  || { echo "dump failed:" >&2; tail -8 "$OUT/dump.log" >&2; exit 1; }
echo "$EXPECTED" > "$OUT/expected_counts.txt"

echo "4/5 starting services again"
restart_services

echo "5/5 restore test in a throwaway volume"
TMPV="kg_restore_test_$TS"
TMPC="kg-restore-test"
trap 'docker rm -f "$TMPC" >/dev/null 2>&1 || true; docker volume rm "$TMPV" >/dev/null 2>&1 || true; restart_services' EXIT
docker volume create "$TMPV" >/dev/null
docker run --rm -v "$TMPV:/data" -v "$PWD/$OUT:/backups" "$IMAGE" \
  neo4j-admin database load neo4j --from-path=/backups --overwrite-destination=true >"$OUT/restore-test.log" 2>&1 \
  || { echo "restore failed:" >&2; tail -8 "$OUT/restore-test.log" >&2; exit 1; }
docker run -d --name "$TMPC" -v "$TMPV:/data" -e NEO4J_AUTH="neo4j/$PASSWORD" \
  -e NEO4J_server_memory_heap_max__size=256m -e NEO4J_server_memory_pagecache_size=64m "$IMAGE" >/dev/null
for _ in $(seq 1 30); do
  docker exec "$TMPC" cypher-shell -u neo4j -p "$PASSWORD" "RETURN 1" >/dev/null 2>&1 && break
  sleep 3
done
RESTORED="$(count "$TMPC")"

echo
echo "backup:   $OUT/neo4j.dump ($(du -h "$OUT/neo4j.dump" | cut -f1))"
echo "expected: $EXPECTED"
echo "restored: $RESTORED"
if [ "$EXPECTED" = "$RESTORED" ]; then
  echo "RESTORE TEST PASSED"
else
  echo "RESTORE TEST FAILED — do not trust this backup" >&2
  exit 1
fi
