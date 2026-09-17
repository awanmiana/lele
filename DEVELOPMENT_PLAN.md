# Finworld Development Plan

Single source of truth for scope, delivered state, and the build queue. Companion documents: `README.md` (user-facing usage and boundaries) and `WORKLOG.md` (session-by-session record). Update all three at the end of any work session, then commit, so a future session can resume without context loss.

## Mission

A local-first research registry of the world's financial institutions and adjacent entities (regulators, issuers, funds), with transparent, evidence-linked analysis. Success is measured by reproducible provenance and explicit coverage boundaries — not by claims of global completeness.

## Non-goals (standing)

No trading or brokerage automation. No price forecasts, targets, or buy/sell outputs. No scraping or access-control circumvention. No news crawling without licensed feeds. No inference of misconduct from relationships. No auto-merging of cross-source identities without review tooling.

## Architecture (current)

```
finworld/
  core/constants.py    endpoints, limits, env overrides (FINWORLD_*)
  core/registry.py     SQLite schema v3, entities/attributes/edges/metrics/signals/filings,
                       migrations, RELATIONSHIPS allowlist
  core/importer.py     offline JSON import: validation-before-write, savepoints, provenance rules
  fetchers/http.py     HTTPS allowlist client: TLS, no redirects, 2 MiB cap (20 MiB for SEC facts),
                       per-host pacing, bounded 429/5xx retries, TTL disk cache (128 slots)
  fetchers/sources.py  connectors: gleif, fdic, worldbank, osfi, sec (catalog + mappers + fetch_source)
  analysis/engine.py   deterministic fundamentals (same period/source groups, 8 ratios),
                       lexicon sentiment with evidence recording, full caveat reporting
  cli/main.py          argparse CLI + interactive menu, bounded exports, JSON mode, exit codes 0/1/2
tests/                 132 offline tests (unittest, stdlib only; no runtime dependencies)
```

## Delivered and live-verified

| Capability | Command | Verified evidence (2026-09-17) |
| --- | --- | --- |
| Registry, imports, analysis, CLI | all commands | 132 tests; ruff + mypy clean |
| GLEIF manager edges (queue item 1 delivered scope) | `edges --kind fund --source gleif --limit 25` | User-provided live verification: `/lei-records/254900MSKVGH4SG63N77/fund-manager` returns manager `5493001JY2KC4SJGF862` (M & G SECURITIES LIMITED); companion `/fund-manager-relationship` publishes `IS_FUND-MANAGED_BY`, ACTIVE, registration status and lastUpdateDate. Offline provenance/fallback/skip/limit tests pass; endpoints not re-probed this session. |
| GLEIF entities | `fetch gleif` | live queries; category total 249,236 FUND LEIs |
| GLEIF funds | `fetch gleif --category FUND --country XX` | GB: 7,233 funds, 25 stored in one call |
| GLEIF websites/subCategory/associatedEntity | mapped when source populates them | GB sample: fields present but null; unit test covers populated path |
| FDIC US banks | `fetch fdic` | live: 4,233 active institutions available; 2-record + 50-record pulls |
| OSFI Canada | `fetch osfi --country CA` | live: full 343-record feed, 4 pages, 328 evidenced `regulated_by` links |
| SEC EDGAR registrants | `fetch sec --query CIK|ticker` | live: Apple Inc. profile + 50 filings with archive URLs |
| SEC XBRL fundamentals | `fetch sec --financials` | live: AAPL FY2026 assets 383.27B, liabilities 275.75B, equity 107.52B |
| Analysis on live data | `analyze` | AAPL 2026-06-27 group: net_margin 0.278474, liabilities/assets 0.719464 |
| World Bank macro | `fetch worldbank` | live smoke during build |
| Exports | `export --format json|csv` | 369-row CA export, JSON/CSV parity asserted |

Commits: `ceca1c6` base registry+connectors, `a652ed6` GLEIF funds + SEC EDGAR, `3b40a63` GLEIF association fields. Git history is the durable record of code state.

## Known verified facts that constrain design

- SEC `primaryDocument` can be a relative subpath (`xslF345X06/form4.xml`); mapper accepts subpaths, rejects traversal.
- Revenue tag precedence rule (freshest `(end,filed)` across `Revenues` and `RevenueFromContractWithCustomerExcludingAssessedTax`, then longest reported duration, then tag priority, then deterministic JSON identity) — legacy `Revenues` tags can be years stale.
- `www.sec.gov` Archives requires a contact-bearing User-Agent (bare UA gets 403); `data.sec.gov` JSON works with plain UA. The pipeline stores archive URLs but never fetches documents.
- OSFI publishes no stable institution ID; identity is sha256 of normalized (name, type, group, industry). CKAN `_id` is row provenance only.
- GLEIF `parentRelationship`/`associatedEntity` were null for a 100-record GB FUND sample. Fund→manager edges use the dedicated fund-manager sub-resources (see Delivered table); umbrella-fund links may exist similarly, and a direct-parent link may be a reporting exception with no parent — neither is traversed today.
- External APIs intermittently drop TLS handshakes from this environment; one retry later usually succeeds. Never infer source death from a single failure.

## Build queue (ordered, each with acceptance gate)

1. **GLEIF relationship edges** — delivered for fund→manager: evidenced `managed_by` edges via the `edges` command. *Gate passed 2026-09-17: user-verified live fund-manager and fund-manager-relationship sub-resources (IS_FUND-MANAGED_BY, ACTIVE, registration status/lastUpdateDate corroboration metadata); offline tests; remaining child/parent and umbrella traversal stays deferred in Phase 3 item 043.*
2. **Jurisdiction packs** — UK FCA, ECB/SSM, or other official registers, following the OSFI recipe (verify endpoint, license, provenance, no invented classifications). *Gate: full-feed exhaustion test like OSFI's.*
3. **Sanctions/enforcement signals** — OFAC SDN (official XML download) stored as signals with list/date provenance; never as relationships implying guilt. *Gate: parse-only from official file; documented update cadence.*
4. **Contact surfaces** — sourced websites already exist; add per-entity `attributes` capture of official registry contact fields only (no social scraping). *Gate: field-level provenance.*
5. **Money-flow map v1 (next)** — `finmap` command: for companies with SEC facts, emit per-period balance-sheet snapshots (assets/liabilities/equity/income/revenue/cash) as a directed "money position" view; peer comparison by SIC when stored. *Gate: Apple + one more issuer analyzed side by side; no cross-period mixing.*
6. **Merge tooling** — candidate matching by LEI/local IDs with review queue (no silent merges).
7. **Research validation** — point-in-time discipline, out-of-sample evaluation harness.

## Session continuity protocol (mandatory)

- Before ending a session: update `WORKLOG.md` (what shipped, what failed, exact next step), tick/annotate `DEVELOPMENT_PLAN.md`, commit everything.
- Never leave uncommitted working code.
- Record environment quirks in `WORKLOG.md` (see its header for the current list).
- When resuming: read `WORKLOG.md` last-entry "Next" section first, then `git log --oneline -5` and `python -m unittest discover -s tests` to confirm the stated state.
