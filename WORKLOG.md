# Finworld Worklog

Session-by-session record. A future session with no memory should read the newest entry's **Next** line, verify with `git log --oneline -3` and the test suite, and proceed.

Environment quirks (persistent):
- Test discovery appears to hang when piped (`| tail`) — run tests directly or via the faulthandler wrapper; they actually take ~15s.
- External APIs (GLEIF, SEC) intermittently drop TLS handshakes; one later retry usually succeeds. Never declare a source dead on a single failure.
- SEC Archives (`www.sec.gov`) 403s a bare UA but returns 200 with a contact-bearing UA; `data.sec.gov` JSON APIs work with the plain finworld UA. The pipeline never fetches Archives documents.
- This runtime exposes `unittest` (not pytest). venv tools: `/tmp/opencode/finworld-venv/bin/{python,ruff,mypy,finworld}` (venv is ephemeral — recreate with `pip install -e '.[dev]'` if missing).
- macOS/Android-style runtimes may lack `os.link`; export no-overwrite then fails safely (tested).

---

## 2026-09-17 — Session 1: MVP foundation (OSFI + hardening + consolidation)

Shipped:
- Full package: registry (SQLite schema v3, migrations, evidenced edges), importer, analysis engine, CLI + menu, bounded HTTPS fetcher.
- Connectors: GLEIF, FDIC, World Bank, OSFI Canada (full 343-record feed verified live; 328 evidenced `regulated_by` edges; rep offices get no supervision edge).
- Combined-source DB verified: OSFI + GLEIF + FDIC; 369-row CA export JSON/CSV parity.
- Init git repo; committed validated state (`ceca1c6`).

Failures/corrections:
- Test suite hung twice when piped; faulthandler-invocation avoids it (see quirks).
- First repo staging included `__pycache__`; added `.gitignore`, unstaged, recommitted.

## 2026-09-17 — Session 2: GLEIF funds + SEC EDGAR fundamentals

Shipped:
- GLEIF `--category FUND` → `kind=fund`, website sourcing (scheme-validated, stripped for column), subCategory + associatedEntity mapping (3b40a63 followed).
- SEC source: submissions JSON (profile, tickers, website, recent filings with archive URLs), optional `--financials` (20 MiB cap) mapping six canonical metrics with the tag-precedence rule (a652ed6).
- Two live-data bugs found and fixed with regression tests: `primaryDocument` subpaths (`xslF345X06/form4.xml`); stale `Revenues` tag precedence (2018 revenue picked over FY2026 current tag).
- Live AAPL end-to-end: registrant + 50 filings + FY2026 facts; `analyze` produced ratios (net_margin 0.278474). Commit `a652ed6`.

Failures/lessons:
- My own float assertions misfired 3x (wrong engine key names, a miscomputed constant, 1e-9 vs 6-dp rounding). Lesson: assert on real field names and computed expectations, not guessed values.
- `git stash` in a compound command was killed by timeout mid-pipeline; stash was left applied-reverted state. Lesson: never compound stash/test/pop in one command.
- SEC Archives URLs are provenance-only; fetching them 403s without a contact UA (probe-confirmed).

## 2026-09-17 — Session 3 (current): continuity docs + GLEIF relationship probe

Shipped this entry:
- `DEVELOPMENT_PLAN.md` (this session's main deliverable) and this `WORKLOG.md`.
- Probes: GB FUND 100-record sample has zero populated `associatedEntity.lei`; guessed GLEIF relationship endpoints (`fund-relationships`, `parent-relationships`) return 404 — both documented in the plan's "known facts".

Next (exact steps):
1. Check official GLEIF API docs page for the real relationship endpoints; if verified, implement fund→manager/child→parent edges (queue item 1) with offline tests + one live sample; if unresolvable, mark queue item blocked with findings.
2. Then pick queue item 2 (next jurisdiction pack) or 5 (finmap v1) per acceptance gates.
3. End-of-session: update WORKLOG "Next" line, annotate DEVELOPMENT_PLAN, commit.

Session-continuity state at commit time: DEVELOPMENT_PLAN.md, WORKLOG.md (this file), README.md documents section — all committed together with no pending code changes. Tests verified green before the commit. A new session should run `python3 -m unittest discover -s tests` (expect 120 tests), `git log --oneline -5`, then read this Next section.
