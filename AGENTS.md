# lele — session-resume guide

## First actions after session/context loss

1. Work in the project root, whatever directory it was cloned into.
2. Read `AUDIT.md` — it states what this tool can and cannot establish, and
   every defect found. Then `WORKLOG.md` **Current handoff**, then
   `DEVELOPMENT_PLAN.md` priority queue, then relevant README sections.
3. Run `git status --short`, `git diff --stat`, and `git log --oneline -5`. Preserve uncommitted changes; do not reset, stash or commit automatically.
4. Inspect the affected implementation/tests before editing. The docs are a handoff, not proof that a feature works; confirm against code and test output.

## Development and checks

Python >=3.11, SQLite >=3.35. Runtime code is standard-library only. Tests use unittest, not pytest. Dev tools are declared in `pyproject.toml`.

One command runs everything and every step must pass:

```bash
.tools/check.sh              # import, compileall, ruff, mypy, resource warnings, tests
.tools/check.sh --fast       # skip the test suite
```

That is `unittest discover`, `ruff check`, `mypy` and `compileall`, plus two
things the individual commands did not do: the suite runs with
`ResourceWarning` promoted to an error, so a leaked database handle fails, and
`mypy` requires full annotations on `core/db.py`, `core/clock.py`,
`core/schema.py` and `fetchers/http.py`.

Run tests directly. Prior `| tail` pipelines hung in this environment; the cause
was never established. Do not use repeated pipelines and do not claim a test
failure without examining direct output — a piped command in this environment
can hang after the work has already finished. Network tests are opt-in:
isolate live data in temporary databases, respect rate limits, and never
fabricate a contact email or bypass blocks.

## Time is an input, not an accident

`lele/core/clock.py` is the only source of wall-clock time. Nothing in the
package may call `datetime.now()` directly. Use `clock.now()` in library code,
`clock.freeze(instant)` in a test, and an explicit `now=` parameter where a
result must be reproducible. `LELE_NOW` pins the process for a script that
must be byte-reproducible.

This is not a style rule. The suite had already lost a test to it: a fixture
dated 2026-09-19 aged out of its 48-hour window and began failing on a day
nobody changed anything. A test that depends on today's date reports failures it
did not find, and teaches the reader to ignore red.

## Invariants

- Never claim all institutions/countries/categories are covered. Completeness
  is not discoverable from a bounded public fetch, so it is reported as
  `unknown`, not as `False` and not as `True`.
- A read command must not write. `registry.get_read_conn` opens `mode=ro`; if a
  read-only open is impossible the flag records why. Add a new read-only
  command to `READ_ONLY_COMMANDS` and to the exhaustive matrix in
  `tests/test_db_guarantees.py`, which fails if a command is unclassified.
- `get_conn` is the only place that commits or rolls back. Analysis and
  fetchers write rows and return.
- Preserve evidence, source IDs, retrieval time and unknowns. Do not guess
  organization websites or social handles.
- GLEIF association fields are not automatically managers/parents. A managing LOU is an LEI service provider, not necessarily the entity's investment manager.
- An explicit contradictory relationship must not fall back to a current edge. Management is not ownership or money transfer.
- SEC period-end alone is not duration alignment; strengthen observation provenance before the planned `finmap` feature.
- Every XML path refuses a DTD or entity declaration before parsing.
- The pre-registered objective is 0.9 across bitcoin, gold, oil and stock, in
  one constant. It is never lowered to make a number pass.
- Use bounded requests, fixture-based offline tests, transactions and existing conventions. Do not add code comments unless requested.

## Closing a session

Update the WORKLOG handoff with work actually done, tests actually run,
limitations, dirty files and the precise next task. Update the development plan
status, `AUDIT.md` and README only where behaviour changed. Do not mark planned
work completed. Keep secrets and research databases out of git. Commit, amend
or push only on explicit user request; past automatic commits are not
authorization for future ones.

Docs preserve context locally, not model memory or protection against workspace
deletion. Research databases/cache under user paths and `/tmp` need separate
backup; git alone does not retain them.
