#!/usr/bin/env bash
# The one command that decides whether this tree is shippable.
#
#   .tools/check.sh          # lint, types, bytecode, tests
#   .tools/check.sh --fast   # skip the test suite
#
# Every step must pass. Anything less is not a warning, because a check that
# can be ignored is not a check.
set -uo pipefail

cd "$(dirname "$0")/.."
PY=.venv/bin/python
RUFF=.venv/bin/ruff
LOG=.check
mkdir -p "$LOG"

FAST=0
[ "${1:-}" = "--fast" ] && FAST=1

status=0
run() {
    local name="$1"; shift
    echo "== $name"
    if "$@" > "$LOG/$name.log" 2>&1; then
        echo "   ok"
    else
        echo "   FAILED (see $LOG/$name.log)"
        tail -n 25 "$LOG/$name.log" | sed 's/^/   /'
        status=1
    fi
}

# The package must be importable with the standard library alone.
run import "$PY" -c "import lele, lele.cli.main, lele.core.db; print(lele.__name__)"
# `-q` on a path that does not exist prints "Can't list" and still exits 0, so a
# stale target turns this step into a no-op that reports success. tests/test_gate.py
# fails if the package named here is not importable.
run compileall "$PY" -m compileall -q lele tests
run lint "$RUFF" check
run lint-tests "$RUFF" check tests
run types "$PY" -m mypy
# A leaked sqlite3 connection is silent memory growth in a long cron run, so
# the storage guarantees are checked with ResourceWarning promoted to an error.
run connections "$PY" -W error::ResourceWarning -m unittest discover -s tests -p 'test_db_*.py'

if [ "$FAST" = "0" ]; then
    # The whole suite under the same warning rule, not just the storage tests.
    run tests "$PY" -W error::ResourceWarning -m unittest discover -s tests
fi

echo
if [ "$status" = "0" ]; then
    echo "all checks passed"
else
    echo "CHECKS FAILED"
fi
exit "$status"
