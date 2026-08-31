#!/usr/bin/env bash
# Offline tests by default:      ./tests/run_all.sh
# Include live CRM read tests:   EF_LIVE=1 ./tests/run_all.sh
# Include live CRM writes too:   EF_LIVE_WRITE=1 ./tests/run_all.sh   (creates then deletes records)
set -e
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}

OFFLINE="tests/test_store.py tests/test_agent.py tests/test_webhooks.py"
LIVE_READ="tests/test_crm_read.py"
LIVE_WRITE="tests/test_crm_write.py tests/test_service_visit.py tests/test_end_to_end.py"

TESTS="$OFFLINE"
[ -n "$EF_LIVE" ] || [ -n "$EF_LIVE_WRITE" ] && TESTS="$TESTS $LIVE_READ"
[ -n "$EF_LIVE_WRITE" ] && TESTS="$TESTS $LIVE_WRITE"

for t in $TESTS; do
  echo "── $t"
  "$PY" "$t" 2>&1 | grep -Ev '^[0-9]{4}-[0-9]{2}-[0-9]{2}|^(INFO|WARNING) ' || true
done
