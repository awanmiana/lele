# Finworld

A local-first financial-institution research CLI. **Working MVP, not a complete global database or investment recommendation system.** Python 3.11+, SQLite 3.35+, no runtime third-party dependencies.

## Run

From this directory, without installing:

```bash
python3 -m finworld --help
python3 -m finworld menu
```

Or install in your own virtual environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/finworld menu
```

The menu exposes every operation, command-specific `--help`, back and quit. It prompts for CLI-style arguments; it never executes shell input. No command opens the menu automatically unless stdin is a terminal. Commands below assume `finworld` is on PATH; alternatively use `python3 -m finworld`.

## What works now

| Source | Live ingestion | Important boundary |
| --- | --- | --- |
| FDIC BankFind | Active insured US banks and savings institutions, certificate IDs, regulator codes and reported websites | Not every US financial institution; regulator codes are attributes, not a populated global authority graph |
| GLEIF | Legal entities, LEIs, legal-address country, registration status; `--category FUND` selects fund entities (worldwide, country-filterable) | An LEI record does **not** establish that an entity is a bank, licensed or solvent; FUND does not distinguish VC from other funds |
| SEC EDGAR | Single-issuer registrant profile, recent filings with archive URLs, optional latest us-gaap USD facts (assets, liabilities, equity, income, revenue, cash) | US filers only, one issuer per fetch; only recent submissions are stored and ticker resolution requires a previously stored ticker; documents are never downloaded |
| World Bank | Indicator observations by economy and period | Macroeconomic context, including aggregates; not company fundamentals |
| OSFI Canada | Monthly federal institution list, bank branches, insurers, trust/loan companies and representative offices; evidenced OSFI regulator links | Federal scope only, not all Canadian institutions; representative offices receive no inferred supervision edge |
| Local JSON | Institutions, authorities, auditors, owners, relationships, accounting metrics and filing references | User supplies evidence and data; ingestion does not independently verify claims |

Use `finworld --json sources` to see actual endpoints and coverage. Requests are bounded to 1–1000 input records and at most ten pages. Results report pages, counts, warnings and truncation. Results are bounded and not a snapshot guarantee; small feeds can be exhausted when `truncated` is false and counts match the reported total. This does not establish complete jurisdiction coverage. `stored` counts processed institution upserts, excluding auxiliary regulator entities. Cross-source duplicates are deliberately not auto-merged. OSFI has no stable institution ID: its keys hash normalized name/type/group/industry, not CKAN row IDs; renamed or reclassified records require reconciliation.

No bundled speculative bank list, automatic global regulator discovery, news crawling, market prices, sanctions matching, trading or brokerage integration is implemented. Source availability and terms can change.

## Commands

Global options **precede** the command: `finworld --db PATH --json COMMAND ...`.

```bash
finworld init
finworld sources
finworld fetch fdic --limit 25
finworld fetch gleif --category FUND --country GB --limit 1000
finworld fetch sec --query 0000320193 --financials --limit 100
finworld fetch osfi --country CA --limit 1000
finworld list --country CA --kind bank_branch --limit 1000
finworld fetch gleif --country GB --query bank --limit 25
finworld fetch worldbank --country US --indicator NY.GDP.MKTP.CD --limit 10
finworld list --kind bank --country US --limit 50
finworld list --query State
finworld show 1
finworld relationships 1
finworld countries
finworld stats
finworld import institutions.json
finworld sentiment 1 --file article.txt --source 'Licensed news input' --ref 'article-001'
finworld analyze 1
finworld --json analyze 1
finworld export --format csv --output banks.csv --kind bank --limit 1000
finworld export --format json --output entities.json --limit 1000
```

IDs are local; use `list` to find yours. `list` and `export` default to 50 rows and are bounded views, **not full-database exports**. Export files contain flat entity records, not nested evidence or restorable backups. `show` includes at most 100 recent signals and 50 filings; metrics and immediate relationships are included. JSON analysis can be redirected to a report file using your shell.

Exports require an existing parent directory, refuse overwrite without `--force`, prevent overwriting the active database/journals, and write atomically. CSV formula-like cells are prefixed with an apostrophe. Use a runtime with `os.link` for atomic no-overwrite exports. Exit codes: success `0`, data/network/storage failure `1`, command usage error `2`. JSON results go to stdout; errors and menu prompts go to stderr.

### Canadian coverage

OSFI uses the official Open Government CKAN datastore, not redirected CSV downloads. `--query` performs source full-text search, not bank-only filtering. `country=CA` means regulatory jurisdiction, not headquarters. Source categories distinguish `bank`, `bank_branch`, `representative_office`, `insurance_company`, `trust_company`, `loan_company` and `fraternal_benefit_society`; unknown categories remain `financial_institution`.

Institution records retain original classifications, retrieval times, dataset provenance and the `ca-ogl-lgo` license identifier. Representative contact names and street addresses are not imported. For federal institutions, `relationships ID` shows an evidenced `regulated_by` link to OSFI; representative offices receive no such inferred link. Records are marked listed, not assumed active or solvent. Missing rows on later runs are not deleted. Estimated totals and changed totals generate warnings; zero omissions cannot be inferred from estimated totals.

Live acceptance on 2026-09-17 exhausted 343 institution records in four pages: 49 banks, 28 bank branches, 190 insurers, 41 trust companies, 12 loan companies, 8 fraternal benefit societies and 15 representative offices. It added one regulator and 328 evidenced links. This completes ingestion of that published federal feed at that observation, **not all Canadian or worldwide financial institutions**; provincial registers, pension plans and history remain separate work.

## Offline data format

The following is **synthetic demonstration data**, not a real bank or regulator. Save it as `institutions.json` to exercise imports and analysis without networking:

```json
{
  "entities": [
    {
      "key": "demo:bank",
      "kind": "bank",
      "name": "Example Bank (synthetic)",
      "country": "US",
      "attributes": {
        "source_url": "local:synthetic-fixture",
        "currency": "USD",
        "unit": "currency",
        "scale": 1
      },
      "metrics": [
        {"k": "total_assets", "v": 1000, "period": "2025", "source": "local:synthetic-fixture"},
        {"k": "total_liabilities", "v": 900, "period": "2025", "source": "local:synthetic-fixture"},
        {"k": "total_equity", "v": 100, "period": "2025", "source": "local:synthetic-fixture"},
        {"k": "net_income", "v": 12, "period": "2025", "source": "local:synthetic-fixture"}
      ]
    },
    {
      "key": "demo:regulator",
      "kind": "regulator",
      "name": "Example Authority (synthetic)",
      "country": "US",
      "attributes": {"source_url": "local:synthetic-fixture"}
    }
  ],
  "relationships": [
    {
      "src": "demo:bank",
      "rel": "supervised_by",
      "dst": "demo:regulator",
      "source_url": "local:synthetic-fixture",
      "observed_at": "2025-12-31",
      "evidence": "Synthetic supervisory relationship for testing only."
    }
  ]
}
```

Entity keys, kinds and names must be nonempty. Every imported entity requires `attributes.source_url`. Relationships require both endpoints in the same import, a supported category, source reference and evidence; `observed_at` is optional. Supported categories include `regulated_by`, `supervised_by`, `owned_by`, `audited_by`, `subsidiary_of`, `member_of` and `enforcement_by`; the full allowlist is `RELATIONSHIPS` in `finworld/core/registry.py`.

Source references may be public HTTPS, `local:<reference>` or `file:///absolute/path`. References are stored, not fetched. Provenance validation checks syntax, not truth. Relationships show direction and retain evidence. Owners, counterparties, auditors and authorities are not assumed to exploit institutions; enforcement entries require documented legal/regulatory facts and should distinguish allegations from adjudicated findings in the evidence text.

Optional entity fields: `website`, `lei`, `notes`, `country`, `attributes`, `metrics`, `filings`. A filing accepts `form`, `date`, `title`, `url`, `source`; supply title or URL. Metrics accept `k`, `v`, `period`, `source`. Unknown fields, duplicate entity keys, invalid references and nonfinite values are rejected. Limits: 10 MiB, 10,000 entities, 50,000 relationships, 100,000 total records. Imports validate before writes and use transactional savepoints. Reimporting matching keys updates records; evidence updates are not a historical audit log.

## Analysis methodology

Fundamentals use stored accounting metrics only: `total_assets`, `total_liabilities`, `total_equity`, `net_income`, `revenue`, `cash_and_equivalents`, `total_debt`. Common aliases such as `assets`, `equity`, `cash`, `net_profit` are supported. Eight possible ratios compare liabilities/assets, equity/assets, debt/assets, debt/equity, income/revenue, income/assets, income/equity and cash/assets.

Inputs must share nonempty period and source. Currency, unit and scale metadata must match when supplied; partially supplied/conflicting metadata blocks affected ratios. When neither input has metadata, a common-basis assumption is prominently reported. Per-metric companions such as `total_assets_currency` override only when consistent with group metadata. There is no FX conversion or reconciliation between IFRS/GAAP or consolidated/standalone accounts. Nonpositive denominators and unavailable data yield unknowns, never a healthy default. Income/assets and income/equity use period-end balances, not average balances, and are not annualized.

Generic ratios do **not** determine bank solvency, CET1, liquidity coverage or NPL quality. World Bank metrics are stored as `macro:<indicator>` and do not enter institution ratios. The current institution connectors do not fetch accounting statements; import fundamentals from properly licensed or public filings.

Sentiment is an explainable **English token lexicon with simple negation**, not an LLM, forecasting model or reliable multilingual classifier. It records supplied-text evidence, positive/negative word counts and a bounded score. Empty/unrecognized inputs remain insufficient; lexical coverage is not statistical confidence. Repeated input is not deduplicated and can bias the mean of recent stored signals. Sarcasm, entity attribution and nuanced financial language remain limitations. No sentiment score or ratio generates buy/sell advice or price targets.

## Storage and networking

Default database: `~/.finworld/finworld.db`. Override with `--db` or `FINWORLD_DB`. SQLite uses foreign keys, transactions, WAL and schema migrations; future schema versions are rejected. Stop writers before backing up, or use SQLite's backup API; copying only an active WAL database file can omit committed data. Protect the database/cache with local account permissions; they are not encrypted.

| Environment variable | Default / purpose |
| --- | --- |
| `FINWORLD_DB` | Database path |
| `FINWORLD_CACHE_DIR` | `~/.finworld/cache` |
| `FINWORLD_CACHE_TTL` | 21600 seconds; `0` disables caching |
| `FINWORLD_RATE_LIMIT` | 1.5 seconds between same-host requests in one process |
| `FINWORLD_USER_AGENT` | Honest application identifier; optional real contact identifier |

HTTP is restricted to configured HTTPS official API paths, validates TLS, refuses redirects, caps responses at 2 MiB, uses a 20-second request deadline and retries only 429/5xx up to three attempts with a capped 30-second Retry-After. Cache storage uses 128 bounded slots; cached retrieval timestamps preserve original fetch time. No credentials are required for these connectors. API access is not a blanket redistribution license: review source terms before redistributing or adding connectors. No scraping or access-control bypass is implemented. Rate limits are process-local, not shared across concurrent CLI processes.

## Engineering plan and acceptance gates

Scope: build a worldwide research registry in stages, not assert that one API contains every institution. Completion means reproducible evidence, coverage boundaries, licensed access, stable identifiers and passing tests. Tasks below are ordered roughly by dependency. The implemented foundation is distinct from the unimplemented expansion backlog; this plan is not an executable job scheduler.

### Phase 1 — delivered foundation

- [x] 001 Package a stdlib-only Python CLI with isolated installation.
- [x] 002 Provide module and console entry points.
- [x] 003 Provide argument validation, help, structured JSON and exit codes.
- [x] 004 Provide an interactive command menu with safe cancellation.
- [x] 005 Persist source-scoped institutions in SQLite.
- [x] 006 Validate schema versions and migrate legacy child tables.
- [x] 007 Add foreign keys, indexes and rollback-safe operations.
- [x] 008 Support evidenced, directed institutional relationships.
- [x] 009 Import bounded validated offline JSON atomically.
- [x] 010 Preserve metric periods, sources and optional metadata.
- [x] 011 Publish a live connector coverage catalog.
- [x] 012 Implement TLS-validated, bounded HTTP requests and caching.
- [x] 013 Implement bounded retry and per-host pacing.
- [x] 014 Ingest GLEIF identities without assuming financial licenses.
- [x] 015 Ingest active FDIC-insured US institutions.
- [x] 016 Ingest World Bank macroeconomic observations separately.
- [x] 017 Report partial ingestion and source errors.
- [x] 018 List, filter, inspect and count stored entities.
- [x] 019 Export bounded flat JSON/CSV lists atomically.
- [x] 020 Calculate transparent same-basis accounting ratios.
- [x] 021 Record explainable supplied-English-text sentiment.
- [x] 022 Report missing evidence and analytical limitations.
- [x] 023 Exercise adapters offline with mocked API fixtures.
- [x] 024 Test CLI workflows, migrations, rollback and export safety.
- [x] 025 Configure and run lint, typecheck and installation smoke tests.

### Phase 2 — evidence and operational reliability

- [ ] 026 Define versioned schemas for source snapshots and run manifests.
- [ ] 027 Record source licenses, attribution obligations and retention policies.
- [ ] 028 Store content hashes and immutable raw-evidence snapshots.
- [ ] 029 Add ingest-run history, freshness checks and coverage reports.
- [ ] 030 Add resumable checkpoints and paginated local list/export commands.
- [ ] 031 Add explicit full-evidence backup/restore, with recovery tests.
- [ ] 032 Add time-bounded relationship validity and immutable change history.
- [ ] 033 Introduce candidate entity matching by LEI, local IDs and jurisdictions.
- [ ] 034 Require reviewed decisions for ambiguous cross-source merges.
- [ ] 035 Implement merge provenance, reversible merges and conflict queues.
- [ ] 036 Add shared-process rate budgets and resumable job queues.
- [ ] 037 Add optional scheduled refresh with per-source budgets and cancellation.
- [ ] 038 Add contract fixtures, drift detection and opted-in live integration tests.
- [ ] 039 Test supported Python/SQLite versions on Linux, macOS and Windows.
- [ ] 040 Add dependency locking, release automation and package integrity checks.

### Phase 3 — jurisdiction and authority coverage

For each new jurisdiction: verify the official licensing register, establish access/terms, preserve original identifiers and reporting dates, test pagination and inactive entities, and report known exclusions before declaring coverage.

- [ ] 041 Model regulators, central banks, deposit insurers and supervisory mandates separately.
- [ ] 042 Normalize ISO jurisdictions without confusing headquarters and licenses.
- [ ] 043 Ingest GLEIF direct/ultimate parent disclosures and reporting exceptions (GLEIF `--category FUND` selection delivered; parent facts pending).
- [ ] 044 Resolve FDIC regulator codes to documented authority identities.
- [ ] 045 Extend US coverage to credit unions and other relevant registers.
- [ ] 046 Add UK authorized-firm and prudential registers after access review.
- [ ] 047 Add euro-area supervised institutions and national competent authorities.
- [ ] 048 Add EEA nonbank investment firms, insurers and funds using official registers.
- [ ] 049 Add Canadian federal/provincial financial licensing sources (OSFI federal institution connector delivered; provincial registers and pension plans pending).
- [ ] 050 Add Australian and New Zealand bank/insurer registers.
- [ ] 051 Add Japanese licensed institutions and supervisory authorities.
- [ ] 052 Add Indian banks, NBFCs and securities intermediaries.
- [ ] 053 Add Singapore and Hong Kong institution/license records.
- [ ] 054 Add mainland China and South Korea sources with original-language names.
- [ ] 055 Add Brazil, Mexico and other Latin American jurisdiction packs.
- [ ] 056 Add South Africa and remaining African jurisdiction packs.
- [ ] 057 Add Gulf and other Middle Eastern jurisdiction packs.
- [ ] 058 Add remaining Asian, European and Pacific jurisdiction packs.
- [ ] 059 Track offshore jurisdictions, cross-border branches and passported permissions.
- [ ] 060 Publish per-country coverage denominators, unknowns and source freshness.

### Phase 4 — financial and relationship intelligence

- [x] 061 Add properly identified SEC submissions/company facts access with required contact policy (registrants, recent filings, latest us-gaap USD facts; historical files and ticker directory remain open items).
- [ ] 062 Add FDIC financial observations with units, dates and institution scope.
- [ ] 063 Normalize IFRS/GAAP taxonomy mappings without silently conflating concepts.
- [ ] 064 Distinguish duration/instant facts, restatements and consolidated accounts.
- [ ] 065 Add explicit FX basis and unit normalization with reproducible inputs.
- [ ] 066 Add bank-specific CET1, liquidity and NPL metrics where officially reported.
- [ ] 067 Build as-of peer comparisons and historical trend reports.
- [ ] 068 Add licensed price/volume data with corporate-action adjustments.
- [ ] 069 Ingest evidenced ownership and beneficial-ownership disclosures with legal/privacy review.
- [ ] 070 Add auditor appointments, deposit insurers and rating-agency relationships.
- [ ] 071 Add official enforcement events with allegation/finding/appeal status.
- [ ] 072 Add official sanctions lists with identity review, expiry and delisting handling.
- [ ] 073 Model disclosed exposures and counterparties; never infer misconduct from adjacency.
- [ ] 074 Add graph traversal, path evidence and bounded graph exports.
- [ ] 075 Add sourced event timelines and corrected-record notifications.

### Phase 5 — sentiment and research validation

- [ ] 076 Add opt-in, licensed news/feed ingestion with publication/observation timestamps.
- [ ] 077 Deduplicate syndicated articles and repeated supplied sentiment inputs.
- [ ] 078 Resolve entity mentions, quoted speakers and topic relevance before scoring.
- [ ] 079 Add language detection and evaluated per-language sentiment pipelines.
- [ ] 080 Separate reported facts, opinions, uncertainty and source reliability.
- [ ] 081 Build manually labeled financial text benchmarks, including negation and sarcasm.
- [ ] 082 Evaluate calibration and bias; never treat lexical coverage as predictive confidence.
- [ ] 083 Add explainable event-window and source-weighted sentiment reports.
- [ ] 084 Design point-in-time datasets preventing look-ahead/survivorship leakage.
- [ ] 085 Evaluate research hypotheses out of sample with costs and uncertainty.
- [ ] 086 Add user-defined watchlists and evidence-linked alerts, without trading automation.
- [ ] 087 Add portable research report bundles containing methods, sources and missing-data warnings.
- [ ] 088 Conduct security, privacy, licensing and financial-methodology reviews before broader release.

## Verification

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/python -m compileall -q finworld
```

The offline suite covers 120 tests, including OSFI mapping, pagination, evidence links, estimated totals, malformed envelopes and atomic rollback, plus GLEIF category/website and SEC submissions/filings/facts coverage. Live acceptance on 2026-09-17: GLEIF FUND selection (7,233 GB funds, 25 stored), SEC AAPL with `--financials` (FY2026 balance-sheet facts), and the analyze command consuming them (net margin 0.278474 from live data). Live acceptance exercised FDIC fetch → list → show → analyze → export against a temporary database. An empty analysis correctly reported missing fundamentals rather than inventing ratios. GLEIF and World Bank also received small live connector smoke checks during implementation. These checks do not establish worldwide completeness or future API availability.
