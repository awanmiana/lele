<pre>
 _      ___    _      ___  
| |    | __|  | |    | __| 
| |    | _|   | |    | _|  
| |___ | __|  | |___ | __| 
|____| |___|  |____| |___| 
</pre>

> a public-source financial research registry
>
> provenance-first ingestion, time-window queries, reproducible analysis

# lele

A public-source financial research registry with a command-line interface.
**Working MVP, not a complete global database and not an investment
recommendation system.** Python 3.11+, SQLite 3.35+, no runtime third-party
dependencies.

Project documents:
- `README.md` (this file): usage, capabilities, boundaries.
- `AUDIT.md`: a full review of this tree — what the tool can and cannot do, every defect found, what was fixed with a test, and what is still open.
- `DEVELOPMENT_PLAN.md`: architecture, delivered state, and the ordered build queue with acceptance gates.
- `WORKLOG.md`: current handoff, verification results, historical work and exact next task.
- `AGENTS.md`: agent startup instructions, test commands, invariants and handoff protocol.

**Read `AUDIT.md` before trusting a number this tool prints.** Two findings change
how results must be read: a "24-hour move" whose stored bars span a gap used to be
reported as a 24-hour move, and the pre-registered target in the cron runner was
`0.5` against a documented objective of `0.9`. Both are fixed. What is *not*
achievable is the 90% five-minute direction objective itself — see `AUDIT.md` §2.

These files preserve context while the workspace exists; they do not preserve model memory or back up databases. Start with WORKLOG's **Current handoff** after a break. SEC observation provenance/duration alignment, `finmap` financial-position views, and F03 inventory/debt-component/net-cash-flow facts are implemented and verified offline. P01 bounded price adapters are implemented and verified offline; the next gate is live source/access verification in the approved order bitcoin, gold, oil/fuel, then stocks. P03 prospective ledger and the evidence-first world-state layer (`worldstate`, `prospective --evidence`, `fetch-evidence`) are implemented; the first live Binance USD-M futures evidence extraction and `worldstate` study ran 2026-09-18. F04 remains pending; see the development plan.

## Renamed from finworld

The package, the console script, the home directory and the environment
variables were renamed from `finworld` to `lele`. Nothing was lost:

| Was | Is now |
| --- | --- |
| `finworld <command>` | `lele <command>` |
| `python3 -m finworld` | `python3 -m lele` |
| `~/.finworld/finworld.db` | `~/.lele/lele.db` |
| `FINWORLD_DB` | `LELE_DB` |
| `FINWORLD_CACHE_DIR`, `FINWORLD_CACHE_TTL` | `LELE_CACHE_DIR`, `LELE_CACHE_TTL` |
| `FINWORLD_RATE_LIMIT` | `LELE_RATE_LIMIT` |
| `FINWORLD_USER_AGENT` | `LELE_USER_AGENT` |
| `FINWORLD_NOW`, `FINWORLD_DEBUG` | `LELE_NOW`, `LELE_DEBUG` |

The previous names are still read, and the new ones win, so an existing
configuration keeps working. A registry at the old default location is still
found rather than reported as missing, and nothing is copied or moved: to move
it deliberately,

```bash
mkdir -p ~/.lele && mv ~/.finworld/lele.db ~/.lele/lele.db
```

`lele doctor` reports which path is in use and why.

## Run

From this directory, without installing:

```bash
python3 -m lele --help
python3 -m lele menu
```

Or install in your own virtual environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/lele menu
```

Runtime requirements are Python **3.11+** and SQLite **3.35+** (the registry uses `RETURNING`); `lele version` prints the application, Python, SQLite and registry-schema versions, and the runtime check fails with a clear message rather than a subtle error. `lele kinds` lists the documented entity-kind catalog — each kind's definition and which source produces it — so the vocabulary is explicit: for example `fund` is a GLEIF `FUND` category and is **not** proof of being a fundraiser/VC/adviser, `legal_entity` may still be a manager or parent, `economy` and `instrument` are not legal entities, and OSFI `financial_institution` means an unrecognized classification rather than a bank. `lele doctor` is a read-only self-check: it reports runtime support, whether the registry exists (never creating it), `PRAGMA integrity_check`, foreign-key violations, the journal mode, the stored vs expected schema version, any promised table or index that is actually missing (`incomplete`), and entity/edge counts, for use before/after upgrades or backups.

**Read commands do not write.** A command that only reads opens the registry
through SQLite's read-only mode, so `lele list` on a read-only mount
succeeds, cannot be made to write by a bug, and leaves the file byte-identical.
An up-to-date database is recognised from its committed schema version and
opened with no transaction at all, so a read takes no write lock. A command
that needs a registry which does not exist says so and exits without creating
one. `pyproject.toml` declares no runtime dependencies (standard library only) and optional `dev` tools (`ruff`, `mypy`); a packaging test keeps the metadata, package discovery and `lele.cli.main:main` console script consistent with the code.

The menu exposes every operation, command-specific `--help`, back and quit. It prompts for CLI-style arguments; it never executes shell input. No command opens the menu automatically unless stdin is a terminal. Commands below assume `lele` is on PATH; alternatively use `python3 -m lele`.

## Checks

One command decides whether this tree is shippable. Every step must pass; a
check that can be ignored is not a check.

```bash
.tools/check.sh            # import, bytecode, lint, types, resource warnings, tests
.tools/check.sh --fast     # skip the test suite
```

It is stricter than the previous four commands in one important way: the test
suite runs with `ResourceWarning` promoted to an error, so a leaked database
handle fails the run instead of quietly consuming a file descriptor in a
long-lived cron process. `ruff` runs a defect-oriented rule set and `mypy`
requires full annotations on the foundation modules; the historical modules are
still partly unannotated, which `AUDIT.md` §4 states rather than hides.

`LELE_DEBUG=1` prints a full traceback for a failure. The default output
never echoes the exception text, because an exception can carry a token, a
password or a request body into a shell history or a CI log.

## Asking about a window

`explain` answers "what is recorded for this instrument between these two
instants, and what is not". It is read-only, takes an explicit aware ISO-8601
window (a bare date is refused as ambiguous), and returns:

- the window's price behaviour **measured from stored bars**, including the
  largest single-bar move and when it happened;
- threshold episodes (ZigZag legs) of the stored series that touch the window;
- stored moves by tier, which are cumulative and nested, not independent counts;
- every stored record inside the window, and every record in the preceding
  `--pre-hours`, each with its source, its URL and the time it became available;
- a **coverage block** naming every channel that is empty and the command that
  would fill it.

Nothing is ever marked as a cause. Every record carries `is_cause: false` and a
`relation` of `coincident` or `precursor`, because a timestamp can establish
ordering and not a mechanism. The output says this in a `what_this_is` block.

`capital ID --from --to` is the capital and positioning view over the same
window: every measured series, the change across it, and a label for each saying
what the number actually is. A stablecoin supply total is labelled "a supply
total, not a net flow into this asset"; a percent change is suppressed for a
bounded unit, because 74 to 75 on a 0–100 index is not "1.4% more of anything".
It also names what this build cannot produce at all: net exchange flow for a
crypto asset, investor-level flow for a listed equity, and order-level
attribution.

`store-evidence` persists the futures evidence `fetch-evidence` writes to a
file, so open interest, funding, long/short ratios, taker flow and book depth
become queryable by window later. It copies each measurement unchanged and
invents no counterparty, direction or motive.

Both commands run read-only and work on a read-only file. The window is an
input, not a side effect: the same call returns the same answer tomorrow.

## What works now

| Source | Live ingestion | Important boundary |
| --- | --- | --- |
| FDIC BankFind | Active insured US banks and savings institutions, certificate IDs, regulator codes and reported websites | Not every US financial institution; regulator codes are attributes, not a populated global authority graph |
| GLEIF | Legal entities, LEIs, legal-address country, registration status; `--category FUND` selects fund entities (worldwide, country-filterable) | An LEI record does **not** establish that an entity is a bank, licensed or solvent; FUND does not distinguish VC from other funds |
| SEC EDGAR | Single-issuer registrant profile, recent filings with archive URLs, optional latest us-gaap USD facts (assets, liabilities, equity, income, revenue, cash, net inventory, separate debt components and three net cash flows) | US filers only, one issuer per fetch; only recent submissions are stored and ticker resolution requires a previously stored ticker; documents are never downloaded |
| SEC Form 4 | Insider transactions for one existing SEC issuer, stored as signed-share `insider_trade` observations via `fetch-form4` | Recent submissions only, Form 4/A amendments ignored, reporting owners are `sec-owner:<CIK>` keys (no person entities), direction is the filed acquisition/disposition code, no counterparty or motive inference |
| SEC Form 13F | Manager-reported quarterly holdings for one existing SEC investment manager, stored as `holding` observations via `fetch-13f` | Recent original 13F-HR filings only, 13F-HR/A amendments not applied; holdings are bound to the manager as actor and the issuer is name/CUSIP only (no issuer instrument inferred); filer USD value is a position snapshot, not a money flow |
| SEC material events | `8-K`/`S-1`/`424B*`/`DEF 14A` filing metadata for one existing SEC issuer, stored as measurement-optional `filing_event` observations via `fetch-material` | Recent submissions metadata only; filing contents/exhibits are never downloaded or interpreted, amendments stay separate events, and no money amount or motive is inferred |
| SEC Form D | Private-fundraising cover data for one existing SEC issuer, stored as `filing_event` observations via `fetch-formd` | Recent original `D`/`D/A` cover XML only; exhibits not parsed, `D/A` amendments are separate and must not be summed, investors/counterparties/motives never inferred, reported amount is the filer's figure not a settled flow |
| SEC N-PORT | Public `NPORT-P` portfolio holdings for one existing SEC fund, stored as `holding` observations via `fetch-nport` | Recent public filings only; confidential `NPORT-NP` data and `NPORT-P/A` amendments not applied; holdings bound to the fund as actor with the security name/LEI/CUSIP/ISIN only, no issuer instrument inferred and no positions summed |
| USAspending awards | Federal contract and assistance award obligations for one or more explicit recipient UEIs, stored as routed `fund_flow` observations via `fetch-awards` | Exact recipient-id filters are ignored by the API, so a name search only narrows candidates and only awards whose per-award `Recipient UEI` exactly matches an explicit UEI are kept; one bounded page per award group; amounts are obligations, not settled transfers; the agency is a textual origin key, and recipients are never matched by name |
| Senate LDA lobbying | Quarterly lobbying income for one explicit client or registrant id, stored as routed `fund_flow` observations via `fetch-lobbying` | Explicit LDA party id required, never matched by name; only positive registrant-reported income is stored (a filed figure, not a settled transfer); the other party is a textual key; amounts are not summed across periods |
| Treasury Fiscal Data | Daily Treasury Statement operating cash balance (Treasury General Account) stored as `macro_release` observations via `fetch-treasury` | Public API, no key required; no counterparty or recipient identity is needed since the data is a macro aggregate; amounts are reported balances, not settled cash transfers; zero/negative balances are skipped |
| World Bank | Indicator observations by economy and period | Macroeconomic context, including aggregates; not company fundamentals |
| OSFI Canada | Monthly federal institution list, bank branches, insurers, trust/loan companies and representative offices; evidenced OSFI regulator links | Federal scope only, not all Canadian institutions; representative offices receive no inferred supervision edge |
| Binance USD-M futures | Bounded public five-minute net taker flow, open-interest notional change, funding, global/top long-short ratios and one retrieval-time order-book depth snapshot as point-in-time world-state evidence for `worldstate`/`prospective` | USDT is not USD; one symbol (`BTCUSDT`); depth is a single transient snapshot and long/short ratios are measured-only; liquidations have no public REST endpoint; publication lag unverified; no counterparty identity and no license or redistribution claim |
| CFTC Commitments of Traders | Weekly legacy futures-only total open interest and net non-commercial positioning for one pinned contract market (`BITCOIN-CME`, `GOLD-COMEX`, `WTI-NYMEX`), via `fetch-cot` | Weekly and measured-only; `available_at` is the conservative local retrieval time (exact publication time not exposed); one contract market per fetch, not the whole asset market; public Socrata data, no license claim |
| FINRA short interest | Twice-monthly consolidated short interest (shares) for one equity symbol, via `fetch-short` | Switched to a `POST` filter because GET filters are ignored; `available_at` is the conservative local retrieval time (publication time not exposed); one symbol; position stock, not flow; measured-only |
| GDELT news | Article-list headlines, source domain and country for one keyword phrase, via `fetch-news` | Keyword relevance and see-date availability unverified; pacing is one request per ~5 s and the plain-text throttle reply is retried; headline/domain/country only; public research index, terms not asserted |
| UN Comtrade | Monthly/annual trade flows by reporter/partner/HS code, via `fetch-comtrade` | API v1 preview endpoint changed; check https://comtrade.un.org/data/ for current access; free tier may require registration; amounts in USD or quantity |
| US Census Trade | Monthly US imports/exports by commodity/country, via `fetch-census-trade` | Endpoint may have changed; check https://api.census.gov/data.html; may require API key; both imports/exports fetched; amounts in USD |
| EIA Energy | Energy data series (petroleum, natgas, electricity, coal, renewables), via `fetch-eia` | **REQUIRES free API key** from https://www.eia.gov/opendata/register.php; pass via --api-key; common series: PET.RWTC.D, NG.RNGWHHD.D; frequency varies |
| BLS Labor | US labor statistics (employment, unemployment, JOLTS, wages), via `fetch-bls` | Optional free registration key at https://data.bls.gov/registrationEngine/ for 500 req/day (25 without); common series: LNS14000000, JTS00000000JOL; uses POST |
| OpenSky Flights | Real-time ADS-B flight positions/intervals, via `fetch-opensky` | Anonymous: 10 req/min, states only; free registration at https://openskynetwork.org/ for higher limits and flights endpoint; no key needed for basic states |
| Local JSON | Institutions, authorities, auditors, owners, relationships, accounting metrics and filing references | User supplies evidence and data; ingestion does not independently verify claims |

Use `lele --json sources` to see actual endpoints and coverage. Requests are bounded to 1–1000 input records and at most ten pages. Results report pages, counts, warnings and truncation. Results are bounded and not a snapshot guarantee; small feeds can be exhausted when `truncated` is false and counts match the reported total. This does not establish complete jurisdiction coverage. `stored` counts processed institution upserts, excluding auxiliary regulator entities. Cross-source duplicates are deliberately not auto-merged. OSFI has no stable institution ID: its keys hash normalized name/type/group/industry, not CKAN row IDs; renamed or reclassified records require reconciliation.

No bundled speculative bank list, automatic global regulator discovery, news crawling, comprehensive market-price coverage, sanctions matching, trading or brokerage integration is implemented. Source availability and terms can change.
## Commands

Global options **precede** the command: `lele --db PATH --json COMMAND ...`.

```bash
lele init
lele version
lele doctor
lele sources
lele kinds
lele fetch fdic --limit 25
lele fetch gleif --category FUND --country GB --limit 1000
lele edges --kind fund --source gleif --limit 25
lele fetch sec --query 0000320193 --financials --limit 100
lele fetch osfi --country CA --limit 1000
lele list --country CA --kind bank_branch --limit 1000
lele fetch gleif --country GB --query bank --limit 25
lele fetch worldbank --country US --indicator NY.GDP.MKTP.CD --limit 10
lele list --kind bank --country US --limit 50
lele list --query State
lele show 1
lele relationships 1
lele tree 1 --depth 3 --direction both
lele countries
lele stats
lele import institutions.json
lele runs --limit 50
lele backup backups/registry.db
lele resolve candidates
lele sanctions fetch ofac-sdn
lele sanctions fetch un-consolidated
lele sanctions fetch uk-ofsi
lele sanctions fetch eu-consolidated
lele sanctions candidates
lele flows list --limit 50
lele flows summary 1
lele observations import observations.json
lele observations list --limit 50
lele observations evidence 1 --output evidence.json
lele fetch-form4 1 --limit 25
lele fetch-13f 1 --limit 4
lele fetch-material 1 --limit 25
lele fetch-formd 1 --limit 10
lele fetch-nport 1 --limit 1
lele fetch-awards 1 --recipient-uei G4KDGE4JFFK7 --search LOCKHEED --limit 25
lele fetch-lobbying 1 --client-id 58116 --filing-year 2024 --limit 25
lele fetch-treasury 1 --limit 25 --start 2026-09-01 --end 2026-09-30
lele fetch-opensky 1 states --limit 100
lele fetch-bls 1 LNS14000000 --limit 25
lele fetch-eia 1 PET.RWTC.D NG.RNGWHHD.D --api-key YOUR_EIA_KEY --limit 25
lele fetch-comtrade 1 --reporter 842 --partner 0 --limit 25
lele fetch-census-trade 1 --limit 25
lele links set 1 https://example.com --type website --source-url https://register.example/1
lele sentiment 1 --file article.txt --source 'Licensed news input' --ref 'article-001'
lele analyze 1
lele --json analyze 1
lele --json prospective preregister --ledger ledger.jsonl --config config.json
lele --json prospective forecast --ledger ledger.jsonl --id 1 --path prices.json
lele --json prospective settle --ledger ledger.jsonl --id 1 --path prices.json
lele --json prospective score --ledger ledger.jsonl
lele --json worldstate ID prices.json --evidence evidence.json
lele --json fetch-evidence ID binance-futures BTCUSDT --limit 288 --output evidence.json
lele --json store-evidence ID binance-futures BTCUSDT --limit 288
lele --json explain ID --from 2026-09-20T00:00:00+00:00 --to 2026-09-27T00:00:00+00:00 --pre-hours 72
lele explain ID --from 2026-09-20T00:00:00+00:00 --to 2026-09-27T00:00:00+00:00 --summary
lele --json capital ID --from 2026-09-20T00:00:00+00:00 --to 2026-09-27T00:00:00+00:00
lele --json episodes ID prices.json --evidence evidence.json --threshold-percent 1 --horizon-hours 72
lele --json prospective forecast --ledger ledger.jsonl --id 1 --path prices.json --evidence evidence.json
lele export --format csv --output banks.csv --kind bank --limit 1000
lele export --format json --output entities.json --limit 1000
```

`links ACTION` records **verified** organization-controlled or registry-published links with field-level provenance and never guesses: `links set ID URL --type website|social|registry|other --source-url URL [--label --observed-at --evidence]` requires a public HTTPS URL and the `--source-url` page that publishes or verifies it; `links list [ID]` shows recorded links; `links remove ID URL` deletes one. Unknown or unverified links remain empty rather than being inferred from names.

`flows ACTION` is the documented cross-entity money-flow view. `flows import PATH` ingests a **user-supplied** JSON of documented directed flows (`src`, `dst`, `type`, positive `amount`, `currency`, optional `occurred_at`, required `source_url`, optional `evidence`) where both counterparties must already exist as entities and each flow carries provenance; `flows list [ID] [--limit]` shows directed flows, optionally for one entity, and `flows summary ID [--limit]` reports that entity's per-currency `inflow`/`outflow`/`net` and its counterparties, summed only within the same currency (no FX conversion or cross-currency netting; `net` is a sum of documented flows, not a balance, position or exposure). Supported `type` values are a closed documented set (loan, repayment, investment, divestment, dividend, grant, purchase, sale, fee, transfer, guarantee, other). **Balances never create a flow**, no flow is inferred from ownership, management or a filing, and a missing flow is unknown coverage rather than evidence of no transfer.

`observations ACTION` is the normalized decision/flow bridge (R02). `observations import PATH` ingests a **user-supplied** JSON of observations (`source`, `external_id`, `kind`, `description`, `actor`, `counterparty`, `instrument`, `action`, `reason`, `reason_basis`, `amount`, `unit`, `currency`, `basis`, `occurred_at`, `observed_at`, `available_at`, required `source_url`, `evidence`); unknown actor, counterparty, amount and motive stay empty and are never inferred from balances, ownership or management. Only a `decision` observation may carry an `action` and reason (with `reason_basis` ∈ unknown/stated_reason/documented_mandate/analyst_hypothesis), and only a routed flow kind (`fund_flow`, `exchange_transfer`, `onchain_transfer`, `etf_flow`, `stablecoin_mint_burn`, `liquidation`) requires an actor (origin) and counterparty (destination) with a positive amount and a currency. A nonempty `instrument` must already exist as an entity, since `observations evidence ID --output PATH` projects only the observations explicitly bound to that instrument into the validated world-state evidence file consumed by `worldstate` and `prospective` (the projection carries actor/action/reason for decisions and origin/destination for routed flows; other kinds keep their actor in the registry but not in the evidence view). `observations list [ID] [--limit]` shows stored observations. The bridge is deliberately lossy by design and nothing is auto-linked.

`fetch-form4 ID [--limit N]` (P06, first SEC decision-layer extractor) reads one existing SEC issuer's recent submissions, fetches up to `N` original Form 4 documents from the allowlisted `www.sec.gov/Archives/edgar/data` prefix, rejects DTDs, and stores one `insider_trade` observation per transaction with a signed share count (acquired positive, disposed negative), reporting owner `sec-owner:<CIK>`, transaction date, acceptance-time `observed_at`/`available_at`, source URL and JSON evidence (code, price, ownership, officer/director flags). Unknowns stay empty: no person entity is created, no counterparty or motive is inferred, only recent submissions are read, and Form 4/A amendments are ignored. The checkable facts are the filed shares, code and ownership; the direction is the acquisition/disposition code, not a forecast. No net buy/sell indicator or predictive claim is made.

`fetch-13f ID [--limit N]` (P06, second SEC decision-layer extractor) reads one existing SEC investment manager's recent submissions and stores one `holding` observation per reported position from each original `13F-HR` information table (`informationTable` XML under the allowlisted `www.sec.gov/Archives/edgar/data` prefix, selected from the filing `index.json`). Each observation binds the manager as actor, records the issuer only by name/CUSIP/title, the filer's reported USD `<value>`, shares and share type, investment discretion, voting authority and put/call flag, with the report period as `occurred_at` and the filing acceptance time as `observed_at`/`available_at`. No issuer instrument is inferred from a CUSIP, no amendment is applied, and the reported value is a filer-reported position snapshot, not a money flow; no net-buy/sell, ownership-delta or motive is computed here.

`fetch-material ID [--limit N]` (P06, third SEC decision-layer extractor) reads one existing SEC issuer's recent submissions metadata and stores one measurement-optional `filing_event` observation per `8-K`, `8-K/A`, `S-1`, `S-1/A`, `424B*`, `DEF 14A` or `DEFA14A` filing, bound to the issuer instrument. Each event records the form, filing and report dates, acceptance-time `observed_at`/`available_at`, the primary-document URL, the primary-document description and any 8-K item numbers; report date is `occurred_at` when present, otherwise the filing date. Filing contents, exhibits and amendment text are never downloaded or interpreted, amendments are separate events, malformed rows are skipped and counted, and no money amount, counterparty or motive is inferred. The event is a filing fact, not a signal.

`fetch-formd ID [--limit N]` (P06, fourth SEC decision-layer extractor) reads one existing SEC issuer's recent submissions and parses each original `D`/`D/A` Form D cover XML (`primary_doc.xml` under the allowlisted `www.sec.gov/Archives/edgar/data` prefix). It stores one measurement-optional `filing_event` observation per filing, bound to the issuer, whose measurement is the filer-reported `totalAmountSold` in USD when present; JSON evidence records the total offering/remaining amounts, industry group, revenue range, federal exemptions, date of first sale, investor count, sales commissions, finders fees, gross proceeds used and up to 50 related persons with their relationships. `occurred_at` is the first-sale date when present (else the signature date) and `observed_at`/`available_at` are the filing acceptance time. Exhibits are not parsed, `D/A` amendments are separate events that must not be summed with the original, and investors, counterparties and motives are never inferred; the reported amount is the filer's offering figure, not a settled money flow.

`fetch-nport ID [--limit N]` (P06, fifth SEC extractor) reads one existing SEC fund's recent submissions, selects public `NPORT-P` filings and parses each namespaced cover XML (`primary_doc.xml` under the allowlisted `www.sec.gov/Archives/edgar/data` prefix). It stores one `holding` observation per `invstOrSec` security, bound to the fund as actor, recording the security name/title/LEI/CUSIP/ISIN, balance and units, currency, reported `valUSD` as the measurement, percentage of net assets, payoff profile, asset/issuer category, investment country and fair-value level, with the report period and fund totals (total assets/liabilities/net assets) in evidence. `occurred_at` is the report period date and `observed_at`/`available_at` is the filing acceptance time; holdings are capped per filing. Confidential `NPORT-NP` data and `NPORT-P/A` amendments are not applied, no issuer instrument is inferred from the CUSIP, and positions are never summed across funds or periods. This is a position snapshot, not a flow or a signal.

`fetch-awards ID --recipient-uei UEI [UEI ...] [--search TEXT] [--limit N] [--start --end]` (P07, first government-money extractor) queries the public USAspending `spending_by_award` API for federal contract and assistance awards. Because USAspending silently ignores exact recipient-id/UEI filters on that endpoint (verified live), the name `--search` (default: the entity name) only narrows candidates and the command keeps strictly the awards whose per-award `Recipient UEI` exactly matches an explicitly supplied UEI — identity is never inferred from a name. Contracts (`A`–`D`) and assistance (`02`–`05`) cannot be combined in one request, so one bounded page is fetched per group, deduplicated by award identifier. Each positive award becomes a routed `fund_flow` observation bound to the selected registry entity: origin `usaspending:agency:<slug>`, reported USD obligation amount, award start as `occurred_at`, retrieval time as `observed_at`/`available_at`, and evidence with award id, kind (contract/grant), agency, recipient name/UEI and matched UEIs. Negative/zero/missing amounts are skipped. Amounts are obligations, not settled cash transfers, and no motive is implied.

`fetch-lobbying ID (--client-id N | --registrant-id N) [--filing-year YYYY] [--limit N]` reads public Senate Lobbying Disclosure Act quarterly activity filings (`Q1`–`Q4`) for one explicit LDA client or registrant id — exactly one is required and identity is never inferred from a name. Each positive registrant-reported `income` becomes a routed `fund_flow` observation bound to the selected registry entity: the bound party is the entity endpoint (client → registrant direction), the other party is a textual key (`lda:client:<id>` / `lda:registrant:<id>`), the quarter start is `occurred_at`, the LDA posting time is `observed_at`/`available_at`, and evidence carries both parties' ids/names, filing type/period/year, income, expenses and issue codes. The canonical host is `https://lda.gov/api/v1/filings` (`lda.senate.gov` 301-redirects there). Income is a filed lobbying figure, not a settled cash transfer, and amounts are never summed across quarters, clients or registrants.

`fetch-treasury` reads the public Treasury Fiscal Data Daily Treasury Statement operating cash balance (`api.fiscaldata.treasury.gov`, no API key) and stores each positive balance as a `macro_release` observation — a non-routed event kind that requires no counterparty identity, since the data is a macro aggregate (Treasury General Account). Bounded by `--limit`, `--start`, and `--end`; zero and negative balances are skipped. Amounts are reported balances, not settled cash transfers. The record date is `occurred_at` and the retrieval time is `observed_at`/`available_at`.

`fetch-opensky ID {states,flights} [--limit N] [--time UNIX_TS] [--icao24 CODE] [--bbox LAMIN,LOMIN,LAMAX,LOMAX] [--begin ISO|UNIX_TS] [--end ISO|UNIX_TS] [--username USER] [--password PASS]` (P08, physical/real-economy flow) queries the OpenSky Network API for ADS-B flight data. `states` returns current aircraft positions (anonymous: 10 req/min, limited history); `flights` returns interval summaries (requires free registration at https://openskynetwork.org/). Each observation is a `flight_event` with aircraft ICAO24, callsign, origin country, position/altitude/velocity, and timestamps. No API key needed for basic `states` access. Register for higher limits and `flights` endpoint. occurred_at is position/flight time; available_at is retrieval time.

`fetch-bls ID SERIES_ID [SERIES_ID ...] [--limit N] [--start YYYY] [--end YYYY] [--registration-key KEY]` (P08, physical/real-economy flow) queries the BLS Public API v2 for US labor statistics. Works without a key (25 req/day); get free registration key at https://data.bls.gov/registrationEngine/ for 500 req/day. Common series: LNS14000000 (unemployment rate), JTS00000000JOL (job openings), CES0000000001 (total nonfarm payrolls). Each observation is a `labor_metric` with the series value and period. Data is monthly/quarterly/annual. Uses POST as required by API. occurred_at is period date; available_at is retrieval time.

`fetch-eia ID SERIES_ID [SERIES_ID ...] --api-key KEY [--limit N] [--start YYYY-MM-DD] [--end YYYY-MM-DD]` (P08, physical/real-economy flow) queries the EIA API v2 for energy data series. **REQUIRES free API key** from https://www.eia.gov/opendata/register.php — sign up, get key, pass via `--api-key`. Common series: PET.RWTC.D (WTI crude daily), NG.RNGWHHD.D (Henry Hub natgas daily), ELEC.PRICE.ALL-US.A (US electricity annual). Each observation is an `energy_flow` with the series value, unit and period. Frequency varies by series (daily/weekly/monthly). occurred_at is period date; available_at is retrieval time.

`fetch-comtrade ID [--reporter CODE] [--partner CODE] [--hs-code CODE] [--limit N] [--start YYYY-MM] [--end YYYY-MM] [--freq M|A]` (P08, physical/real-economy flow) queries the UN Comtrade API for international trade flows. **NOTE**: The v1 preview endpoint (comtradeapi.un.org/public/v1/preview) appears to have changed; check https://comtrade.un.org/data/ for current API access. Free tier may require registration. Amounts in USD (primaryValue) or quantity. Each observation is a `trade_flow` with reporter, partner, HS code, flow direction (import/export/re-export/re-import) and value. occurred_at is period start; available_at is retrieval time. If you get "source request failed", the endpoint may have moved or require a subscription.

`fetch-census-trade ID [--limit N] [--start YYYY-MM] [--end YYYY-MM] [--commodity CODE] [--country CODE]` (P08, physical/real-economy flow) queries the US Census Bureau trade API for monthly US imports and exports. **NOTE**: The timeseries/intltrade endpoint may have changed; check https://api.census.gov/data.html for current endpoints. May require API key. Both imports and exports are fetched; amounts in USD. Each observation is a `trade_flow` with commodity, country, direction (import/export) and value. occurred_at is period start; available_at is retrieval time. If you get "source request failed", the endpoint path may have changed or require a key.

`sanctions ACTION` handles sanctions/enforcement listings without ever asserting guilt from a name. `sanctions fetch ofac-sdn` downloads the official OFAC SDN CSV (about 5.4 MiB, ~19k rows) and stores each row as a designation listing with basis `designation`, status `active` and a recorded file SHA-256 plus an ingest-run record. `sanctions fetch un-consolidated` downloads the official UN Security Council consolidated list XML (about 2.1 MiB, ~1,000 individuals and entities) and stores each as a designation listing with its reference number, list type, nationality/address country, `LISTED_ON` date, aliases/comments evidence and file hash; the document is rejected if it contains a DTD. `sanctions fetch uk-ofsi` downloads the UK OFSI Consolidated List CSV (about 16 MiB, ~5,000 designations keyed by `Group ID`) directly from the official OFSI Azure blob and stores one listing per designation using the primary name (family name `Name 6` with given names for people, the name for entities/ships), regime, country, `Listed On` date and file hash; alias rows are not stored as separate listings. `sanctions fetch eu-consolidated` downloads the EU consolidated list XML (about 25 MiB, ~6,200 entities) directly from the public FSF extract and stores one listing per `euReferenceNumber`, preferring a strong English `nameAlias`, mapping `subjectType` person/enterprise, and recording the programme, citizenship/address country and regulation date. The OFAC and UN endpoints answer `302` to pinned allowlisted redirect targets (US-government S3 under `/Published/`, UN Azure blob under `/publiclegacyxmlfiles/`), re-validated on every hop; all three stream to a temporary file, are capped and never auto-link to any entity. `sanctions import SOURCE PATH --source-url URL [--retrieved-at ISO]` ingests a **user-supplied** JSON file of official listings (fields `key`, `name`, `type`, `program`, `country`, `basis` ∈ designation/allegation/finding/unknown, `status` ∈ active/delisted, `published_at`, `effective_at`, `evidence`); it records the file SHA-256, the required official `source-url` provenance and the retrieval time, and fetches nothing itself. `sanctions candidates [ID] [--fuzzy]` lists exact-normalized-name matches (and, with `--fuzzy` plus one entity ID, shared-token matches after stripping legal suffixes) as `unreviewed_candidate`; a name or token overlap is never a match on its own. `sanctions link LISTING_ID ENTITY_ID [--reason]` records a **human-approved, reversible** association (with the listing's basis, status, source and URL); it also adds a reversible `sanctioned_by` edge from the entity to a per-source `authority:sanctions:<source>` node, so the relationship graph and `tree` reflect reviewed links. `sanctions unlink` removes the association and retracts that edge only once no reviewed link from the same source remains; `sanctions listings [--active-only]` and `sanctions links` show stored rows. Allegations, findings and designations are kept distinct by the `basis` field; publication/effective dates are preserved; `active` vs `delisted` is preserved rather than deleted. A repeat `sanctions fetch` marks any previously active listing that is absent from the current official file as `delisted` (row, provenance and dates kept; returned as `delisted` in the report), and a listing that reappears is set back to `active` — so delisting history is tracked instead of erased.

`resolve ACTION` is the reviewed entity-resolution workflow. `resolve candidates [ID] [--limit] [--fuzzy]` lists **unreviewed** possible duplicate pairs with a reason and strength (`same_lei`, `same_name_and_country`, `same_name`) and never merges by itself; each item is marked `review=unreviewed_candidate`. `--fuzzy` (requires exactly one entity ID) additionally matches on shared name tokens after stripping legal suffixes, with `reason=fuzzy_name`, and is deliberately conservative: a token overlap is a candidate, never a match. `resolve merge ALIAS_ID CANONICAL_ID [--reason --source-url]` records a human-approved alias, `resolve unmerge ALIAS_ID` removes it (reversible), and `resolve list` shows recorded aliases. Merges are stored in an `entity_aliases` table and are **aliases only**: they do not rewrite existing edges, metrics or filings, and merging a canonical that still owns aliases, or merging an existing alias, is refused. Name similarity is not identity, so a candidate is never treated as a match without review.

`backup PATH [--force]` writes a consistent copy of the existing registry using SQLite's online backup API (a read-only source connection, so it never creates or migrates the source; the copy keeps the source's schema version). It runs `PRAGMA integrity_check`, refuses an absent source, refuses a target that is the registry or one of its journal files, requires the target parent to exist, and refuses an existing target without `--force`. The result reports the entity count, byte size and SHA-256. To restore, stop any writer and use the backup file directly as `--db` (or copy it into place). A backup is a normal registry database, not a separate archive format.

`runs` lists durable **ingest-run records** for completed `fetch` calls: source and query parameters, started/finished timestamps, fetched/stored/skipped/missing counts, pages, reported total, truncation, warnings, coverage text, two SHA-256 hashes (a canonical hash of the request parameters and a hash of the stored record keys), a per-page detail list (`page`, `offset`, `rows`, `selected`), and a resumable checkpoint (`next_offset`, `next_page`). A run is marked resumable only when it stopped at a full page boundary after hitting the limit or the page budget; a partial-page stop is not resumable. `fetch ... --resume` continues the latest matching resumable truncated run by starting at its saved page/offset instead of refetching, and errors if there is no matching checkpoint (SEC submissions do not support resume). A run that fails rolls back with its transaction and is not recorded.

IDs are local; use `list` to find yours. `list` and `export` accept a broad `--country` search plus exact `--legal-country` (the entity's legal-address country), `--jurisdiction` (GLEIF `iso_jurisdiction`), `--hq-country` (GLEIF `headquartersAddress.country`) and `--source` (stored ingestion source such as `gleif`) filters, so regulatory jurisdiction, legal address, headquarters and source are not conflated; matching is exact and case-sensitive for the exact flags. `list` and `export` default to 50 rows and are bounded views, **not full-database exports**. Export files contain flat entity records, not nested evidence or restorable backups. `show` includes at most 100 recent signals and 50 filings; metrics and immediate relationships are included. JSON analysis can be redirected to a report file using your shell.

Exports require an existing parent directory, refuse overwrite without `--force`, prevent overwriting the active database/journals, and write atomically. CSV formula-like cells are prefixed with an apostrophe. Use a runtime with `os.link` for atomic no-overwrite exports. Exit codes: success `0`, data/network/storage failure `1`, command usage error `2`. JSON results go to stdout; errors and menu prompts go to stderr.

### GLEIF relationship edges (fund managers and direct parents)

`lele edges --kind fund --source gleif --limit 25 [--refresh]` enriches the local registry, not a remote fund search. By default it selects at most 1–1000 stored `kind=fund` entities with `lei:` keys (default 25), excluding any fund with an existing **active** outgoing `managed_by` edge, and orders candidates by retry state then local ID. `skipped` counts all such already-linked local LEI funds, not unselected rows beyond the limit. Repeats advance past linked funds; missing or failed records stay eligible for retry but are deprioritized by an `edge_retry` queue (ordered after never-attempted funds by failure count and last attempt), so repeatedly failing early funds cannot occupy every batch. With `--refresh`, already-linked funds are re-selected and re-checked: a manager edge that is no longer published, conflicts, or is replaced by a different manager is marked `retracted` (status and timestamp recorded) instead of being deleted, so its provenance and history survive. A successful re-check updates `last_seen_at` and increments `seen_count` while preserving `first_seen_at`.

Only funds whose GLEIF record publishes a manager receive an edge. The `/lei-records/{fund-LEI}/fund-manager` single-record sub-resource supplies the target LEI, legal name, legal-address country and primary evidence URL/time. New managers are `legal_entity` unless their record category is `FUND`; existing entity kinds and the source fund's kind are preserved. No separate manager-side record request is made. Each valid manager record gets one additional, optional `/fund-manager-relationship` request through the existing paced HTTPS client.

Corroboration must match the fund and manager LEIs, `IS_FUND-MANAGED_BY` and `ACTIVE`. When valid, relationship and registration metadata are preserved as JSON in the fund's `gleif.fund_manager_relationship` attribute, with `.source_url` and `.retrieved_at` companion attributes, and appended to edge evidence. Unavailable or structurally incomplete corroboration can fall back to lei-record-only evidence. Explicit non-ACTIVE status, mismatched type/endpoints, or a relationship start after a trustworthy retrieval timestamp blocks the new edge and is reported as missing with a warning. Already-linked funds are skipped unless `--refresh` is given, in which case they are revalidated and stale edges are retracted rather than erased. Manager record failures count as `missing` with warnings and make no writes for that fund; successful fund writes use a savepoint. Management implies no ownership, parentage, licensing or solvency claims.

`lele edges --kind parent --source gleif --limit 25 [--level direct|ultimate] [--refresh]` follows each stored `lei:` entity's published parent link. `--level direct` requests `/lei-records/{child-LEI}/direct-parent-relationship` and requires `IS_DIRECTLY_CONSOLIDATED_BY`; `--level ultimate` requests `/lei-records/{child-LEI}/ultimate-parent-relationship` and requires `IS_ULTIMATELY_CONSOLIDATED_BY`. Both require `ACTIVE` status and an `endNode` of type `LEI`, then fetch the parent `/lei-records/{parent-LEI}` record and store `subsidiary_of` (direct) or `ultimate_subsidiary_of` (ultimate) from child to parent, with the relationship JSON in `gleif.direct_parent_relationship` or `gleif.ultimate_parent_relationship`. A level mismatch (for example a direct relationship returned for an ultimate request) is treated as an exception, not an edge. Three outcomes are distinguished and preserved: **no parent** (HTTP 404 → `no_parent`, no edge), a **reported but unusable relationship** (wrong type, non-ACTIVE status, or a parent without an LEI → `exceptions`, stored as a level-specific `gleif.{level}_parent_exception` attribute, no edge), and unavailable/invalid data (`missing`). Refresh, retraction, retry ordering and `first_seen_at`/`last_seen_at`/`seen_count` behave as for managers. Parentage is evidence of GLEIF-reported consolidation, not proof of control, ownership or beneficial ownership.

`tree ID [--depth 1..20] [--direction both|parents|subsidiaries] [--max-nodes 1..10000]` is a read-only report over the stored `subsidiary_of`/`ultimate_subsidiary_of` edges: it walks up (ancestors), down (descendants), or both from one entity and lists each node with its level, direction, reaching relation, edge provenance and a `parent_status` of `recorded` (an active edge exists), `exception` (a level-specific `gleif.*_parent_exception` attribute is stored) or `unknown` (neither). It does not treat `unknown` as "no parent", skips and counts cycle edges, and reports `coverage.truncated` when the depth or node cap stops the walk. A recorded consolidation link is not proof of control or ownership.

CLI JSON keys are `source`, `kind`, `processed`, `linked`, `missing`, `retracted`, `skipped`, `warnings` (a list; its length is the warning count); `--kind parent` additionally reports `no_parent` and `exceptions`. The sources API `edge_fund_managers(conn, limit=25, progress=None, refresh=False)` and `edge_parents(conn, limit=25, progress=None, refresh=False)` return the counts, warning list and `edges` containing the stored edge dictionaries; an optional callback receives the four attempt counts after each attempted entity. Human output prints a counts table and warning lines. Inspect stored provenance with `show ID` or `relationships ID`.

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

Entity keys, kinds and names must be nonempty. Every imported entity requires `attributes.source_url`. Relationships require both endpoints in the same import, a supported category, source reference and evidence; `observed_at` is optional. Supported categories include `regulated_by`, `supervised_by`, `owned_by`, `audited_by`, `subsidiary_of`, `member_of` and `enforcement_by`; the full allowlist is `RELATIONSHIPS` in `lele/core/registry.py`.

Source references may be public HTTPS, `local:<reference>` or `file:///absolute/path`. References are stored, not fetched. Provenance validation checks syntax, not truth. Relationships show direction and retain evidence. Owners, counterparties, auditors and authorities are not assumed to exploit institutions; enforcement entries require documented legal/regulatory facts and should distinguish allegations from adjudicated findings in the evidence text.

Optional entity fields: `website`, `lei`, `notes`, `country`, `attributes`, `metrics`, `filings`. A filing accepts `form`, `date`, `title`, `url`, `source`; supply title or URL. Metrics accept `k`, `v`, `period`, `source`. Unknown fields, duplicate entity keys, invalid references and nonfinite values are rejected. Limits: 10 MiB, 10,000 entities, 50,000 relationships, 100,000 total records. Imports validate before writes and use transactional savepoints. Reimporting matching keys updates records; evidence updates are not a historical audit log.

## SEC observation evidence (F01)

`fetch sec --query 0000320193 --financials` now stores each selected metric as a `sec_fact_v1` JSON envelope in the existing SQLite metric value column. `show` returns that serialized value; `analyze` exposes its parsed `observation` in `input_evidence` and each indicator's inputs. No schema migration is needed.

Each envelope includes numeric `value`, `currency=USD`, `unit=currency`, `scale=1`, plus taxonomy/tag, accession, form, filed date, start/end dates, instant/duration type, source reference, retrieval timestamp and selection rule. Missing accession/form/start remain null, not fabricated. Contradictory values for the same selected observation metadata are marked ambiguous and blocked from ratios.

Example metric within the existing import format (synthetic data):

```json
{
  "k": "net_income",
  "period": "2025-12-31",
  "source": "sec",
  "v": {
    "value": 12,
    "currency": "USD",
    "unit": "currency",
    "scale": 1,
    "observation": {
      "schema": "sec_fact_v1",
      "taxonomy": "us-gaap",
      "tag": "NetIncomeLoss",
      "accession": "0000000001-26-000001",
      "form": "10-K",
      "filed": "2026-02-01",
      "start": "2025-01-01",
      "end": "2025-12-31",
      "period_type": "duration",
      "source_url": "local:synthetic-filing",
      "retrieved_at": "2026-02-02T00:00:00Z",
      "selection": "synthetic example"
    }
  }
}
```

Structured `v` objects are validated before import writes and serialized deterministically. Legacy scalar values remain accepted. SEC scalar rows without observation evidence no longer produce ratios: refetch the issuer with `--financials`. Refetch replaces matching metric/end/source rows, retains older rows, and does not invent historical provenance. Generic non-SEC scalar analysis remains available with an explicit unverified scope/duration warning.

SEC ratios require matching end dates, filing accessions and forms, plus consistent units and source. Duration-to-duration ratios require identical starts as well; quarter and year-to-date income are not interchangeable. Duration-to-instant returns use the matching filing's period-ending stock balance and are explicitly nonannualized. Missing, malformed or ambiguous evidence yields unknown ratios.

Selection remains bounded to one latest observation per canonical metric, considering both supported revenue tags, latest `(end,filed)`, earliest valid duration start, tag priority and deterministic tie-breaking. This does not retain every historical observation or find an alternative common filing when independently selected facts disagree. There is still no IFRS mapping, full reporting-scope reconciliation or money-transfer graph.

## Bounded five-minute price export (P01)

`fetch-prices ID SOURCE SYMBOL --output PATH [--limit 288] [--end ISO8601] [--force]` fetches one instrument for an **existing local entity ID**. Live observation, 2026-09-17: Binance BTCUSDT exported 1000 five-minute closes and passed `project`/`events` round-trips; Yahoo AAPL exported 313; Yahoo futures GC=F/CL=F/RB=F initially failed a strict grid check whose live cause is documented below and is now handled explicitly. Its exact stored `key` is copied into `instrument.entity_key`; no ticker lookup, entity creation or guessed identity mapping occurs. Selecting the appropriate entity is the user's responsibility. `sources` remains the institution-source catalog; price choices are listed by `fetch-prices --help`.

Examples in the approved order (replace each uppercase ID placeholder with the appropriate existing positive integer ID; these commands perform network requests when run):

```bash
lele --db research.db --json fetch-prices BTC_ID binance BTCUSDT --limit 288 --output btc-prices.json
lele --db research.db --json fetch-prices GOLD_ID yahoo 'GC=F' --limit 1000 --output gold-futures-prices.json
lele --db research.db --json fetch-prices OIL_ID yahoo 'CL=F' --limit 1000 --output oil-futures-prices.json
lele --db research.db --json fetch-prices FUEL_ID yahoo 'RB=F' --limit 1000 --output gasoline-futures-prices.json
lele --db research.db --json fetch-prices STOCK_ID yahoo AAPL --limit 1000 --output aapl-prices.json
lele --db research.db --json project BTC_ID btc-prices.json
lele --db research.db --json events BTC_ID btc-prices.json --move-hours 1
```

| Provider/symbol | Instrument and quotation | Important boundary |
| --- | --- | --- |
| Binance `BTCUSDT` | Binance spot five-minute close, USDT per BTC | USDT is not USD; access depends on jurisdiction |
| Binance `PAXGUSDT` | Binance spot five-minute close, USDT per PAX Gold token (1 token ≈ 1 fine troy ounce) | Tokenized-gold **proxy**, not COMEX `GC=F` or spot gold; USDT is not USD; access depends on jurisdiction |
| Yahoo `GC=F` | Gold futures-series close, USD per troy ounce | Not spot gold or a verified fixed-expiry contract |
| Yahoo `CL=F` | Crude oil futures-series close, USD per barrel | Not physical spot oil or a verified fixed-expiry contract |
| Yahoo `RB=F` | RBOB gasoline futures-series close, USD per US gallon | Not retail pump fuel prices |
| Yahoo `AAPL` | Equity close, USD per share | Provider `quote.close`, not `adjclose`; no local corporate-action adjustment |

Only these source/symbol pairs are supported. The HTTP allowlist adds the exact Binance `/api/v3/klines` path, four exact encoded Yahoo chart paths on `query1.finance.yahoo.com`, a `https://www.sec.gov/Archives/edgar/data` prefix used only by the SEC Form extractors to read raw filing XML (the `data.sec.gov` submissions API remains separately prefix-allowlisted), the `https://api.usaspending.gov/api/v2/recipient` / `.../search/spending_by_award` prefixes used only by `fetch-awards`, the `https://lda.gov/api/v1/filings` prefix used only by `fetch-lobbying`, and the `https://api.fiscaldata.treasury.gov/services/api/fiscal_service` prefix used only by `fetch-treasury`, not whole hosts or arbitrary URLs. No authentication, alternate-host fallback, cookies or access-block bypass. Existing TLS verification, redirect refusal, pacing, 20-second request timeout/deadline, 2 MiB response limit and bounded retry policy apply (one logical request, at most three attempts on 429/5xx). The one deliberate exception is `sanctions fetch`: the pinned OFAC and UN publications answer `302` to signed object-storage URLs, so a **separate** download path validates each redirect target against a two-entry host+path prefix allowlist (`...s3.us-gov-west-1.amazonaws.com/Published/` and `unsolprodfiles.blob.core.windows.net/publiclegacyxmlfiles/`), limits hops to three, sends no credentials and streams the body under a larger cap (32 MiB OFAC / 16 MiB UN); every other request still refuses redirects. Price caching is disabled even when cache environment variables are set, avoiding cache-directory writes and stale cached partial bars.

`--limit` is **2–1000 elapsed five-minute slots**, not a promise of that many observations (288 slots = 24 hours; 1000 = 83 hours 20 minutes). The window ends at `--end`, an aware ISO8601 UTC-aligned five-minute boundary, or the latest elapsed boundary on the local clock. Future ends and pre-epoch starts are rejected. Yahoo windows must start within the last 60 days. No pagination, backfill or automatic widening across weekends/holidays. Responses are capped at `limit + 1` rows to inspect a possible extra provider candle; exported points never exceed `limit`. Fewer than two usable closes fails without writing output.

Candles are stamped at their **END boundary: opening + 300 seconds**, never opening time. Binance's close-time field must equal end boundary minus one millisecond. Yahoo timestamps must be integer Unix seconds on five-minute boundaries; metadata must match symbol, currency, instrument type and `5m` granularity. Closure is checked against request-start time, retrieval time and requested end. Partial/future and out-of-window rows are omitted and counted; malformed close prices/timestamps and duplicate openings (even identical values) fail the entire export. Rows are sorted; null closes are omitted and counted; gaps stay gaps. A final provider row whose timestamp is off the five-minute grid is accepted **only** as a trailing provider snapshot (for example Yahoo's live quote at the last trade time) when the timestamp is an exact integer equal to `meta.regularMarketTime` and the close is a positive valid number: it is then excluded from the export, counted as `skipped.trailing_snapshots` and never stamped to a synthetic boundary; the meta price itself is not compared because providers round it. Negative or out-of-range timestamps and any other off-grid row fail. Prices use the existing positive bounded Decimal-string schema, so genuine nonpositive futures prices are unsupported. Unused OHLC/volume fields are not interpreted or certified.

The file contains **exactly `instrument` and `prices`**, unchanged from the `project`/`events` schema. The returned JSON report separately includes the existing entity key, source/request URL, retrieval and request-start UTC timestamps, requested window, received/exported/skipped/missing-slot counts, Yahoo provider metadata, limitations, exact output-file SHA-256/bytes, and a SHA-256 of canonical parsed-response JSON (sorted keys, ASCII, compact separators; **not original wire bytes**). Save this returned report separately if durable retrieval provenance is needed; no sidecar is automatically written. Shell redirection is outside the CLI's output protections, so never redirect reports over the database or prices file. `truth_verified` remains false.

The existing atomic export convention is reused: parent directory must exist; no overwrite by default (atomic hard-link publication); `--force` atomically replaces output. Registry, journal/WAL/SHM paths, symlinks and hard-link aliases to those files are protected. No price rows or metrics are written to SQLite; normal registry connection/schema checks still apply.

**Access and research limits:** The user approved free public APIs, but this is not evidence of a permissive redistribution license. Binance's [REST documentation](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md) documents public klines. Yahoo chart is **unsupported**, not an official stable developer contract or guaranteed free licensed feed; review [Yahoo terms](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html) for the intended use. The [yfinance project](https://github.com/ranaroussi/yfinance) documents the public chart convention and warns about personal-use restrictions; it is not a dependency. Do not bypass a denial. Yahoo numeric closes pass through Python float before Decimal strings, so they are not exact exchange tick evidence. Futures rolls, stock splits/distributions, session calendars and source delay/revisions are not repaired or independently checked. A locally elapsed candle is not proof of publication availability at that end boundary. These adapters have **offline fixture verification only; no live price requests were run for P01 implementation**, and no prospective accuracy claim is supported.

## Standing objective: five-minute price projection (P-track)

The project's long-horizon goal is five-minute-ahead price projection for gold, oil/fuel, bitcoin and listed stocks/shares, with **90% accuracy across all predictions**, per the user's standing objective. The user later directed the accuracy criterion to be **direction plus tolerance** (with exact Decimal equality kept as a diagnostic) and the target to cover the four asset classes together; the literal exact-equality target is retained and reported but is near-unattainable on liquid markets. Bounded live price exports and one live prospective forecast now exist, but there is still no independent price-truth or usage-rights verification, so accuracy is not yet measurable at the required scale.

`lele --json project ID prices.json` runs the first milestone: a **historical as-of persistence baseline** over a supplied recorded-price file (2 MiB / 10,000 points; strictly increasing UTC five-minute timestamps; exact decimal prices; instrument identity must match the local entity's key). It evaluates the naive rule "next price equals previous price" across every consecutive five-minute pair, reporting eligible predictions, skipped gaps, exact hits, exact-match rate, MAE, whether the historical rate met 0.9 and an `objective.status` that stays `not_achieved` until independent, out-of-sample, multi-asset live evidence exists. Research context (entity + finmap) is attached for provenance but explicitly excluded from forecasting to avoid look-ahead. `project` itself does not fetch prices, fit models, guarantee accuracy or execute trades; `fetch-prices` exports compatible input separately. File provenance is hashed but syntax-only. See the development plan's P01–P03 queue for the path to a verifiable objective.

```bash
lele --json compare ID PRICES_JSON [--train-percent 70]
```

`compare` is the P02 **exploratory historical** model comparison over the same bounded price file. It splits the observations chronologically — the first `floor(observation_count * train_percent / 100)` rows (default 70, `--train-percent` 1–99) become training and the remainder test; choose the split before inspecting scores, because repeatedly re-running with different percentages to pick a flattering one defeats the holdout. Three fixed small methods are compared with no fitting or tuning to recent samples: `five_minute_persistence_v1` (last price), `five_minute_linear_extrapolation_v1` (last minus previous, extrapolated one step) and `five_minute_trailing_mean_3_v1` (mean of the three most recent prices). A method is **selected only on training targets** — most exact hits, then smallest exact total absolute error, then fixed method order — and frozen; test scores are diagnostic and never reselect. Every target needs three consecutive history points plus a label exactly 300 seconds later, identical across methods; gaps are never bridged. Test walk-forward uses observed prior test prices (the first eligible test target may look back into training observations) and never the current or future label. Nonpositive forecasts (possible for linear extrapolation) are kept unclamped, counted per method and scored on the same denominator as every other method — they are not valid positive-price quotes and are never silently excluded. Insufficient data (fewer than four observations) and no-eligible-target sections are explicit statuses, not silent zeros; empty training prevents selection. All arithmetic runs in an isolated 128-digit half-even Decimal context; forecast prices, rates and MAE are decimal strings. The report includes training/test metrics (eligible counts, warmup/gap skips, exact hits, rate, MAE, nonpositive counts) plus every scored prediction with its timestamps and per-method price, the selection and its frozen test score, split times, the raw file SHA-256, instrument identity and `objective.status = not_achieved` — historical held-out numbers are exploratory, not the prospective P03 evidence, and no >90% assertion is made. The command opens the existing registry **read-only** (no schema creation or migration), performs no writes and no network requests; an absent database file fails.

## Prospective forecast ledger (P03)

```bash
lele --json prospective preregister --ledger ledger.jsonl --config config.json
lele --json prospective forecast --ledger ledger.jsonl --id ID --path prices.json
lele --json prospective settle --ledger ledger.jsonl --id ID --path prices.json
lele --json prospective score --ledger ledger.jsonl [--run RUN_ID]
```

P03 records forecasts **before their outcomes exist** in an append-only, hash-chained JSONL ledger, so the evaluation cannot be backfilled silently. `preregister` freezes a run: one of the three fixed methods, the required asset classes, the target metric and a direction/tolerance criterion, the target rate and a minimum settled sample per class. It refuses to overwrite an existing ledger without `--force`. `forecast` computes the frozen method from the last three consecutive five-minute closes of a supplied recorded-price file and appends one record per instrument, stamped with the issuance wall-clock time; an identical forecast for the same run/entity/as-of/method is returned, not duplicated. A forecast counts as `prospective` only when its target time is later than issuance; otherwise it is stored as `backfilled` and excluded from scoring. `settle` appends outcomes only for forecasts with an exact observation at their target boundary; gaps stay `pending` and are never bridged. `score` reports direction, tolerance, exact and combined hits, MAE within a single unit, coverage and per-class metrics, and an `objective.status` that stays `not_achieved` until every required class meets the pre-registered sample and rate; even then it is `prospective_target_met_unverified`, never `achieved`, because independence, provider truth, wall-clock integrity and data rights are not established by a self-recorded ledger.

The user directed the accuracy criterion to be **direction plus tolerance** (up/down/flat and a basis-point band), with exact Decimal equality retained as a diagnostic, and the target to cover **bitcoin, gold, oil and stocks together**. Because exact equality of a future price is near-impossible on liquid markets, the combined criterion is the pre-registerable target; the frozen methods and threshold are recorded in the run config before any outcome is seen. `forecast`/`settle` open the existing registry read-only (entity-key binding only) and never write to SQLite; `preregister`/`score` need no database. Ledgers are bounded to 16 MiB / 200,000 records, and any sequence or hash mismatch aborts the read.

The first pre-registered prospective evaluation of the weighted evidence method ran on 2026-09-18 against live Binance data: six BTCUSDT 5-minute points (direction 3/6, tolerance 6/6, combined 3/6) plus one tokenized-gold `PAXGUSDT` point (direction miss, tolerance hit), all issued before their targets and settled from exact target observations; `objective.status=not_achieved`. The harness behaved as designed — a single direction hit briefly produced `prospective_target_met_unverified`, and every later direction miss returned the status to `not_achieved`. The weighted direction changed sign across the session (the first four `down`, the last two `up`) but the direction rate stayed at chance. These self-recorded points establish no skill. Two of the four required classes (bitcoin and tokenized gold) now have directional evidence; oil and stock have no verified free directional source, so independent multi-asset prospective coverage is still required.

The first live prospective run (2026-09-18) produced one genuinely prospective BTCUSDT record: persistence predicted no change, the price fell about 3.09 bps, so direction and combined missed but tolerance hit; the Yahoo gold/oil/fuel records were backfilled because the provider's last closed candle lagged the boundary, and stocks were unavailable while the market was closed. `objective.status` remained `not_achieved`. This is one self-recorded data point, not evidence of predictive skill, and it shows why a non-flat frozen method and candle-aware issuance timing are needed for a meaningful combined-criterion test.

The first real evidence extractor is `fetch-evidence ID binance-futures SYMBOL --output evidence.json` for `BTCUSDT` or `PAXGUSDT` (tokenized gold), which exports bounded Binance USD-M futures public data: net taker quote flow per closed five-minute candle (`order_flow`), five-minute open-interest notional change (`open_interest`), settled funding (`funding_rate`), global retail long/short account ratio (`long_short_account_ratio`) and top-trader long/short position ratio (`top_long_short_position_ratio`) over up to the most recent `min(limit, 120)` five-minute bars, plus one `order_book_imbalance` observation. The depth imbalance is a **single retrieval-time snapshot** of the top 500 levels per side (`(bidNotional - askNotional) / (bidNotional + askNotional)`, quantized to 12 places); it is not a historical series and its exchange transaction time may fall after the requested window end, which the report's `snapshot` section states. `order_book_imbalance` is a directional kind, while the two long/short ratios are **measured-only** features because crowded-positioning direction is ambiguous. Every record carries `observed_at` and `available_at`; because provider publication lag is unverified, `available_at` is set equal to `observed_at` and the report says so. The extractor makes no SQLite writes and asserts no license or counterparty identity. **Liquidations are not collected:** the public `fapi/v1/allForceOrders` REST endpoint returns 404 and `forceOrder` liquidations are WebSocket-only, so no liquidation kind is fabricated. On a live 2026-09-18 run with `--limit 12` it produced 47 observations (12 order flow, 12 open interest, 11 global and 11 top ratios, one depth snapshot, no funding in that hour); an earlier 24-observation run's `worldstate` study agreed with the next five-minute direction in only 1 of 11 boundaries — an honest early measurement that signed net taker flow alone is not predictive at this horizon, not a success claim.

A second, slower extractor covers weekly positioning: `fetch-cot ID cftc SYMBOL --output cot.json [--limit 12] [--end YYYY-MM-DD] [--force]` queries the CFTC's public Commitments of Traders Socrata dataset (legacy futures-only) for one **pinned** market name — `BITCOIN-CME`, `GOLD-COMEX` or `WTI-NYMEX` — emitting `cot_open_interest` (total open interest, contracts) and `cot_positioning` (net non-commercial long minus short, contracts) per report date. It is measured-only because crowded-positioning direction is ambiguous, and it rejects a response whose market does not match the request. `observed_at` is the CFTC report date and `available_at` is the conservative local retrieval time, since the endpoint does not expose the exact publication time; a weekly report therefore becomes usable only from retrieval. The command opens the registry read-only and writes nothing to it. The endpoint was verified live on 2026-09-18 (a `--limit 4` smoke run returned three reports for the CME bitcoin contract market).

A third extractor covers equity positioning: `fetch-short ID finra AAPL --output short.json [--months 12] [--end YYYY-MM-DD] [--force]` posts a bounded filter to FINRA's consolidated short interest dataset and emits the measured-only `short_interest` kind (current short position, shares) per twice-monthly settlement date. GET query filters are ignored by that API, so the client gained a bounded `post_json` that validates the allowlist, paces per host, refuses redirects, enforces the 2 MiB limit and does not cache. As with COT, `available_at` is the conservative local retrieval time because the endpoint exposes no publication date. The endpoint and POST filter were verified live on 2026-09-18 (a `--months 6` smoke run returned 12 AAPL settlements).

FRED is **not** ingested: its graph CSV endpoint failed to connect from this environment and its JSON API requires an API key that is not available, so no FRED series is fetched or invented (an explicit exclusion, not a hidden gap).

A fourth extractor adds event data: `fetch-news ID bitcoin --output news.json [--hours 24] [--limit 100] [--end ISO] [--force]` queries the public GDELT DOC 2.0 `artlist` API for one **plain keyword phrase** and emits `news_event` observations carrying the headline, source domain and source country (no measurement, since the kind is measurement-optional). A record is skipped, not fabricated, when its URL is not HTTPS, its see date falls outside the window, or it duplicates an earlier URL. The endpoint was verified live (200 with articles) but GDELT throttles aggressively: it paces this host to one request per ~5.5 s, and when GDELT replies `429` or with its non-JSON "Please limit requests…" text the client treats that as retryable and with a floor above the host minimum rather than parsing it as data. In this environment later requests were throttled, so `fetch-news` returned an honest source error instead of inventing articles; offline fixture tests cover parsing, skipping, empty results and the CLI path. `observed_at` and `available_at` are the article see date, which may be optimistic relative to indexing lag.

### Pump/dump and bull/bear episode study

```bash
lele --json episodes ID prices.json --evidence evidence.json --threshold-percent 3 --horizon-hours 72 [--control-stride 12]
```

`episodes` segments the price series with a threshold-defined ZigZag into alternating up (pump) and down (dump) legs, reporting each leg's start/end, magnitude percent, duration in hours and a 72-hour regime label (bull/bear/range/unknown from the lookback return). For every leg it profiles the preceding `--horizon-hours` of evidence using the same point-in-time rule as `worldstate` (observed inside the window, `available_at <= leg start`), then summarizes precursor means and evidence-kind totals separately for pumps and dumps. It also profiles **non-episode controls** — price points outside every leg span and outside each leg's preceding horizon, sampled every `--control-stride` points (default 12) — so a pump/dump precursor can be compared against ordinary windows; controls are not matched to episodes, can overlap each other, and no significance test is applied. The live 2026-09-18 BTC run on roughly 83 hours of prices and 25 hours of evidence found 13 legs at a 1% threshold (7 pumps, 6 dumps); with a 72-hour horizon only one control point was eligible, so that sample separated nothing (pump mean −13, dump −6, control −14). ZigZag legs depend on the chosen threshold, overlapping legs and controls are not independent samples, and this is association study, not a validated next-occurrence predictor.

## Evidence-first world state (P03 reframe)

The user's requirement is that the forecast be conditioned on the world, not on the asset's own price history. The price series is only the scored outcome and, at issuance, the anchor. `worldstate` extracts the evidence-only state of a 5-minute (or 1-second-to-24-hour) window ending at each boundary:

```bash
lele --json worldstate ID prices.json --evidence evidence.json [--window-seconds 300] [--limit 200]
```

A record counts only when `observed_at` is inside the window and `available_at <= as_of`; later publication is excluded. Features are evidence-only: counts by evidence kind, a signed `direction_score`/`net_direction`, measured totals by kind, routed amounts by currency with documented origins/destinations, decision reason bases and sentiment totals. The price file supplies the outcome label (the move to the next boundary) and is never a feature. Output reports coverage, direction agreement, a second `weighted_direction_agreement` (see below), both restricted to boundaries that actually carried evidence (`*_with_evidence`, since no-evidence boundaries predict flat and dilute the headline rate) and per-actual-direction pattern means, with `truth_verified=false` and an explicit directional-convention assumption (positive measurement means upward pressure). It is exploratory historical evidence, not validation.

Two fixed evidence-direction scores are published side by side so their exploratory agreement can be compared without pre-registering: the unweighted signed count (`net_direction`) and the weighted score `kind_weight_recency_buckets_v1`. The weighted score multiplies each directional observation's sign by a fixed **kind weight** — 3 for realized pressure (`order_flow`, `trade_print`, `block_trade`, `order_book_imbalance`, `liquidation`), 2 for money movement and positioning (`fund_flow`, `exchange_transfer`, `onchain_transfer`, `etf_flow`, `stablecoin_mint_burn`, `insider_trade`) and 1 for sentiment/context (`social_sentiment`, `market_context`, `capitulation_indicator`) — and by a **recency multiplier** that splits the observation window into three equal buckets (newest ×3, middle ×2, oldest ×1). The weights and bucket count are fixed, documented assumptions, not values estimated from the sample; non-directional kinds (including open interest, funding, short interest, holdings and event kinds) contribute zero.

The frozen prospective method `evidence_score_10bps_v1` reads that world state at the as-of boundary, turns the unweighted evidence direction into a forecast, and projects the last close by a fixed ±10 bps convention (magnitude is not fitted; direction is the signal). A second frozen method, `evidence_score_10bps_v2`, uses the weighted direction instead and records the exact weights, bucket count, scored kinds and weighted score alongside the world state. Both are pre-registered through the same ledger and neither changes the other:

```bash
lele --json prospective preregister --ledger ledger.jsonl --config config.json
lele --json prospective forecast --ledger ledger.jsonl --id 1 --path prices.json --evidence evidence.json
```

`config.json` selects `"method": "evidence_score_10bps_v1"` or `"evidence_score_10bps_v2"` and may set `"observation_window_seconds"` (1–86400, default 300). Price-based methods reject `--evidence`; the evidence methods require it. The user directed the accuracy target to be about 98–99% "close" rather than exact equality, so `target_rate` may be set to `"0.98"` or `"0.99"` and the direction/tolerance metrics are primary while exact equality stays a diagnostic. No free-source extractor is wired yet, so no real world-state dataset, accuracy or licensing is claimed; `objective.status` remains `not_achieved`.

## Historical moves and precursor evidence

```bash
lele --json events ID prices.json --move-hours 24 --thresholds 3 5 7 11 --evidence evidence.json --limit 100
```

Scans one supplied series for rolling endpoint returns **strictly greater** than the requested absolute percentages (a move of exactly 3% does not exceed 3%). `--move-hours` is 1–72, default 24; `--limit` is 1–100 newest matching events. Each event lists all exceeded thresholds once. Counts report total matches, truncation, warmup and missing starting endpoints. Exact endpoints are required; gaps are not interpolated. This is not an all-history/all-asset scan, intraperiod extreme detector or rally-onset detector.

Price input is the same bounded JSON used by `project`: exactly `instrument` and `prices`. `instrument` requires `entity_key`, `symbol`, `asset_class` (`gold`, `oil`, `fuel`, `bitcoin`, `stock`), `venue`, `currency`, `unit`, `price_type`, `source_url`. It must match the selected local entity key. `prices` contains 2–10,000 `{ "timestamp": "2024-01-04T00:00:00Z", "price": "100.00" }` observations, strictly increasing on UTC five-minute boundaries (sparse endpoints accepted). Decimal price strings must be positive and bounded; file limit 2 MiB. Review split/distribution adjustments and futures rolls before interpreting jumps. Instrument labels and file provenance are user supplied, not independently verified.

Optional evidence file: exactly `{ "observations": [...] }`, up to 1,000 records / 2 MiB. Example **synthetic** observation (not an actual flow):

```json
{
  "id": "synthetic-withdrawal",
  "entity_key": "fixture:asset",
  "observed_at": "2024-01-03T12:00:00Z",
  "available_at": "2024-01-03T13:00:00Z",
  "kind": "fund_flow",
  "source_url": "local:synthetic-evidence",
  "description": "Synthetic withdrawal, not verified market activity",
  "platform": "Fixture venue",
  "industry": null,
  "location": null,
  "measurement": {"value": "-10", "unit": "currency", "currency": "USD", "basis": "observed"}
}
```

All keys above are required; platform/industry/location and measurement currency may be null. IDs must be unique and entity keys must match the instrument. Evidence kinds cover money (`fund_flow`, `exchange_transfer`, `onchain_transfer`, `etf_flow`, `stablecoin_mint_burn`, `liquidation`, `liquidation_level`), liabilities/positioning (`open_interest`, `funding_rate`, `short_interest`, `margin_debt`, `options_positioning`, `order_book_imbalance`, `holding`), actions (`trade_print`, `block_trade`, `order_flow`, `insider_trade`, `decision`), events (`news_event`, `macro_release`, `calendar_event`, `filing_event`) and emotions (`market_context`, `social_sentiment`, `capitulation_indicator`). `liquidation_level` is a positive estimate only; `decision` carries no measurement. Documented `from`/`to` routes are allowed on flow, transfer and liquidation kinds. Numeric measurements are signed decimal strings; basis is `observed` or `estimated`. Availability must not precede observation. `news_event`, `macro_release`, `calendar_event`, `filing_event` and `market_context` may omit the measurement (description-only evidence); do not invent a numeric measurement for unquantified text.

Optional `mapping` object routes money and attributes decisions. For flows, transfers and liquidations, `from` and `to` (both required together) record the documented origin and destination of a positive amount in the record's currency; direction comes from your evidence, never from the sign of a net flow. `decision` records omit routes and instead carry `actor`, `action`, `reason` and `reason_basis` — one of `stated_reason` or `documented_mandate` (requires actor plus `source_locator`), `analyst_hypothesis` (interpreted, not stated) or `unknown` (reason must be null). `related_ids` links decisions to the flows/transfers they concern, cross-referencing IDs in the same file; dangling, duplicate or self-referencing IDs are rejected so every reported direction resolves. Example synthetic pair:

```json
{"id": "flow-1", "kind": "fund_flow", "measurement": {"value": "10", "unit": "currency", "currency": "USD", "basis": "observed"},
 "mapping": {"from": "Venue A", "to": "Desk B", "actor": null, "action": null, "reason": null,
             "reason_basis": "unknown", "related_ids": ["decision-1"], "source_locator": null}}
{"id": "decision-1", "kind": "decision", "measurement": null,
 "mapping": {"from": null, "to": null, "actor": "Reported manager", "action": "Rebalance inventory",
             "reason": "Published rationale", "reason_basis": "stated_reason",
             "related_ids": ["flow-1"], "source_locator": "p. 2"}}
```

Evidence may also be supplied as CSV with the same information: 22 columns — the observation fields (minus `measurement`/`mapping`), `value`, `unit`, `currency`, `basis`, then the mapping fields with `related_ids` as pipe-separated values; blank mapping columns produce no mapping. Rows must be complete and aligned.

The cumulative 24/48/72-hour precursor windows end **before the move START**, not its outcome timestamp. Only evidence observed within the window and available before the start is referenced. Late-publication counts deduplicate event/record pairs across nested windows and refer to reported events. Full included records — including mappings — are retained once in the report, with platform/industry/location and source intact. No observations means unknown coverage, not no activity. Routed `from`/`to` values record documented transfer legs only: no cross-platform sums, inferred money origins, guaranteed liquidation levels or confirmed capitulation/bottom calls are produced. `stated_reason`/`documented_mandate` quote the source's attribution; `analyst_hypothesis` is interpretation and is never presented as the actor's actual reason. Gross volume is not net inflow; timing is not causation. Effects on projection accuracy remain untested and `project` is unchanged.

## Tiered price moves and pre-move cause scan (M01)

Four commands answer "what happened before this move, and what is happening now with the same settings". They read stored history rather than a supplied file, which is what makes multi-year coverage and repeated current-time scans possible.

```bash
lele --json fetch-history ID SYMBOL --interval 1d --limit 4000 --pages 5
lele --json moves ID --interval 1d --move-hours 24 --thresholds 3 5 7 11 --baseline-bars 90
lele --json causes attribute ID --interval 4h --move-hours 24 --pre-hours 72 --moves 6 --controls 8
lele --json causes profile ID --interval 4h --move-hours 24 --pre-hours 72
lele --json scan ID --interval 4h --move-hours 24 --horizons 24 48 72
```

**`fetch-history`** stores Binance spot OHLC bars in `price_bars`, keyed idempotently on `(instrument_key, interval_seconds, open_time)`, so a repeated run refines rather than duplicates. `--interval` is `1d`, `4h` or `1h`; `--limit` 2–5000 bars total and `--pages` 1–10 bounded backward pages of 1000 bars. Unlike `fetch-prices`, this walks years back: 4000 requested daily bars returned 3326 bars for `BTCUSDT` spanning 2017-08-17 to 2026-09-25, one skipped malformed row and one unclosed candle. Spot is quoted in USDT, not USD; bars are unadjusted provider values; a short result means the provider history start, not a complete record. Every bar keeps its `source_url` and `retrieved_at`, and each run is recorded in `ingest_runs`.

**`moves`** turns stored bars into tiered move events over a ladder of absolute percentage thresholds, `--thresholds 3 5 7 11` by default. A candidate is a fixed-horizon return between bar closes; candidates are ranked by absolute change and retained only when their windows share no bar, so a fall is not counted once per bar. Each retained move carries a `z_score` and `baseline_percentile` computed against the `--baseline-bars` returns **strictly before** its own window, never including itself.

The ladder is **cumulative by design**: a 12% move is also a 3%, a 5%, a 7% and an 11% instance, and is recorded against every threshold it clears, as tier labels `p3`, `p5`, `p7`, `p11`. That matters because a single high threshold yields too few instances to test anything, while a 3% threshold yields many. On the 3326 daily bars above, 856 candidates reduced to 628 retained moves and 1169 tier rows:

| tier | up | down | total |
| --- | --- | --- | --- |
| `p3` | 354 | 274 | 628 |
| `p5` | 173 | 150 | 323 |
| `p7` | 102 | 71 | 173 |
| `p11` | 24 | 21 | 45 |

Each tier is therefore a cumulative sample of all moves at or above that threshold, not a market frequency, and a tier count is not the number of distinct events at that size. `--tier p3` selects one ladder rung on `causes`; `--thresholds` must be ascending and distinct, at most 8 values. The largest retained moves were the 2020-03 COVID crash (−39.5%), the 2017-12 peak (+22.5%), the 2021-02 rally, the 2021-05 China-crackdown selloff and the 2022-02 Ukraine rally. A move is a price outcome label, never a cause and never a forecast.

**`causes attribute`** reads, for each recent move, every headline published in the window strictly **before** the move start, classifies each headline against a fixed cause lexicon, scores its sentiment, and stores the per-move category set. `--pre-hours` 1–2160; `--moves` newest servable moves; `--controls` non-move windows. Windows older than the provider's reach are reported as unavailable rather than treated as quiet. The report separates four per-window states so a broken read is never presented as a silence: `attributed`, `attributed_truncated`, `no_articles` and `provider_unavailable`. Only `attributed` windows contribute to the reason counts, and the report gives reasons per move, the mean, and how many moves had no categorised reason.

**`causes profile`** compares how often each category appears before moves with how often it appears before the stored control windows, reporting move share, control share, enrichment and a **permutation p-value** under an explicit null (per-window category flags exchangeable between the two groups, seeded and reproducible). It states the null in the output and warns that 11 categories are tested, so p-values are descriptive until a multiple-testing correction is applied. A category never seen before a move yields a null enrichment rather than a fabricated ratio.

**`scan`** is the same detector applied to the newest stored bars: it reports the in-progress move, then every visible signal in each `--horizons` lookback window, bucketed by cause family. Horizons are nested and explicitly not additive. It reports current conditions only and forecasts nothing.

Signals come from four channels, each with its own reported status:

| Channel | Content | Coverage caveat |
| --- | --- | --- |
| `stored` | observations already recorded, in two scopes: rows bound to the instrument (`fund_flow`, `exchange_transfer`, `etf_flow`, `liquidation`, `margin_debt`, `cot_positioning`, `filing_event` and similar) and market-wide rows with no instrument key (`stablecoin_supply`, `social_sentiment`, `political_event`, `geopolitical_event`, `regulatory_action`, `macro_release`, `labor_metric`, `calendar_event`, `market_shock`, `market_context`) | market-wide rows are restricted to a fixed kind whitelist, so a routed flow between two named organisations is never presented as context for an unrelated instrument. Rows whose `available_at` is after the window end are **counted and named by kind, not shown**: a later retrieval must not enter an earlier window, and their absence is not evidence that nothing happened |
| `news` | bounded GDELT headlines, each classified into a reason category and scored for sentiment | the public index throttles, caps articles per request and lags its own live coverage; per-tile status and truncation are always reported |
| `structure` | Binance futures leverage state: funding rate, open interest, long/short account and top-position ratios, taker order flow | bounded to 25 hours of five-minute history, so a wider horizon excludes it rather than implying wider coverage |
| `market` | mean absolute bar move and a window-volume z-score against the trailing baseline | a zero-variance baseline reports `zero_variance_baseline` instead of a fabricated z-score |

Cause families: `money_movement` (funds entering or leaving, pulled for investment, transfers, liquidations), `policy_decision` (a government, regulator, court or corporate actor changing rules, rates or stated policy), `market_structure` (leverage, positioning, order book and funding conditions that can amplify or fake a move), `sentiment_emotion`, `security_incident`, `macro_environment`, `corporate_fundamental`.

Two documented limits on the cause layer. A public news index returns loosely related coverage for a single keyword phrase, so a category means "this headline matched these terms", not "this was the reason"; unrelated stories can match. And the sentiment scorer tries the general engine first, which declines financial headlines because its English vocabulary does not know the domain words, and only then applies a finance-word lexicon under its own method name, recording the engine's refusal. A headline with no sentiment word returns no score rather than zero, because an absence of words is not neutrality. Observed live: on one 72-hour window the scan returned 84 signals across all seven families, including 7 `money_movement`, 2 `policy_decision`, 6 `security_incident` and 59 `sentiment_emotion` headlines, while 55 of the 82 articles carried no sentiment term and 52 matched no category.

`observations ingest`-style storage is reachable from a fetcher payload through `observations.ingest_evidence`, which converts world-state evidence observations into normalized registry rows without inferring any actor, counterparty, amount or motive. Without that bridge a fetcher's observations could only reach the `observations` table through a hand-written file.

### Measured market context (M02)

Two provider series turn two of the families from keyword guesses into measurements. Both are market-wide, so they are stored with no instrument key and read by any instrument's scan.

```bash
lele --json fetch-sentiment --limit 900
lele --json fetch-stablecoins --limit 2500
```

`fetch-sentiment` reads the public daily crypto fear and greed index and stores one `social_sentiment` observation per day: the 0–100 value, its classification, `basis=estimated` because it is a composite, and `available_at` set to the following UTC day because a same-day read is not established. Live: 900 days stored with zero skipped, recent readings around 71 "Greed".

`fetch-stablecoins` reads daily aggregate stablecoin supply and stores one `stablecoin_supply` observation per day. The provider keys these fields by peg currency rather than returning a single figure, so the aggregate is summed across pegs here, the USD-pegged part is kept separately, and the peg count is recorded. Live: 2500 days stored, newest 2026-09-26, aggregate $313.2bn of which $311.6bn is USD-pegged across 28 peg currencies. A supply rise is consistent with capital entering the ecosystem and nothing more: it is not a net inflow, not a purchase of any asset, and it names no actor.

A freshly ingested record is newer than the last closed bar, so it first appears in a scan whose window end is at or after its `available_at`. The scan reports exactly which kinds were excluded for that reason rather than presenting the channel as empty.

### Federal Register evidence bounding (fix)

`fetch-political` could not store most real documents: the observation `evidence` field is bounded text and a Federal Register record carries a long abstract, nested agency objects and two URLs, so the whole fetch aborted on the first verbose document. Evidence is now built bounded, reduced in a fixed order (`abstract`, `agencies`, `effective_on`, `government_entity`, `citation`, `title`), and finally reduced to durable identity only; whatever was dropped is named in `bounded_fields.dropped` so a consumer sees the reduction instead of inferring completeness. The document URL is not repeated in evidence because the observation already carries it in `source_url`. Live: 60 documents stored with provenance intact, none skipped.

### Per-asset capital activity and a second headline source (M03)

```bash
lele --json fetch-market-activity ID --coin bitcoin --days 180
lele --json fetch-news-feed ID bitcoin --hours 48 --limit 100 --output headlines.json
lele --json scan ID --interval 4h --horizons 24 72 --news-source google_news
```

`fetch-market-activity` reads a second public provider's daily series for one coin and binds it to the selected entity. The stored measurement is the **day-on-day market capitalisation change**, which is the closest public proxy for capital entering or leaving this specific asset, with the level, close and volume kept in evidence. It is a proxy and the report says so: market capitalisation is price times supply, so the change rises with price as well as with demand, and it cannot separate a purchase from a sale, new capital from rotation out of another asset, or spot from derivative notional. It is also a second independent price source for cross-checking the exchange series. Live: 179 daily observations, and on 2026-09-24 the market capitalisation fell **$36.1bn, −2.09%**, in a single day.

`fetch-news-feed` reads a public multi-publisher news feed, so reason coverage no longer rests on one index. Headlines carry the publisher name and link, and the trailing publisher suffix a multi-publisher feed appends to each title is trimmed, which the keyword index does not give. A document type declaration or entity declaration is refused before parsing. Live: 75 observations across named publishers in a 48-hour window, including coverage at the current hour that the keyword index could not serve.

`--news-source {gdelt,google_news}` selects the provider for the `news` channel on `scan` and for `causes attribute`. One keyword phrase against one provider per run, so a reason count is a property of that provider rather than of the world. The two differ in reach: the keyword index serves a longer window but throttles and caps per request, while the feed serves a shorter window and returns far fewer items on a quiet topic. Live with `--news-source google_news`, all three 24-hour tiles succeeded where the keyword index previously refused two of three, taking the news channel from a partial read to `status: ok`.

Both new series follow the same conservative availability rule as the M02 series: a daily value is treated as available from the following UTC day, so today's value first appears in a later scan and the scan reports which kinds it excluded for that reason.

### Testing which conditions actually precede moves (M04)

```bash
lele --json causes context ID --interval 4h --move-hours 24 --pre-hours 24 --controls 60
```

`causes profile` needs a news provider and is limited by that provider's reach. `causes context` profiles **stored** conditions instead — money movement, policy actions, sentiment readings and macro releases a wired source already recorded — so it is not limited by provider reach and a missing condition is a missing record rather than a quiet news day. For each condition it reports how often it was present before a move against how often it was present before a control window, an enrichment ratio, a seeded permutation p-value, and a Benjamini-Hochberg adjusted value.

`adjusted_p` is the value to quote. Raw permutation p-values across many conditions are read as descriptive; `significant_after_correction` is the only flag that may be reported as a condition that stands out, and even then it is a difference against control windows, never a demonstrated cause. The report also carries `inference_status`, which withholds a verdict entirely below 8 move and 8 control windows.

Three design rules exist because ignoring any of them manufactures a finding, and each is asserted by a test:

- **A window is kept only when the stored channel has at least one observation inside it**, applied identically to moves and controls. Applying it to moves alone makes every condition look enriched, because an older control window that predates a series contributes a guaranteed absence.
- **Each condition is tested over its own coverage.** A series recorded for the last 180 days is not measured against controls sampled from a year of history where it could not have existed. Conditions are consequently tested on different samples, which the report states per condition.
- **Controls are spread across the whole span.** Stopping at the first *N* eligible timestamps would place every control in the oldest part of the series and silently remove the most recent period from the comparison.

A group of one window produces no p-value, because it has no permutation variance.

**Measured result, and it is a negative one.** On the stored 4-hour series, every available condition reads `move_share 1.0` against `control_share 1.0` and every p-value is 1.0. At a 24-hour pre-window the daily-cadence context series are **saturated**: a daily observation falls inside every window, so presence carries no information. At a 6- or 12-hour pre-window the same series leave most windows empty, so no usable sample exists. In other words the current context sources have **no discriminative power for this question at any window length**, and the tool now proves that with a proper test and a multiple-testing correction rather than asserting it. The fix is not more statistics but higher-cadence context: the free sentiment and supply series are daily-only, so detecting a pre-move condition requires an hourly or finer source that no free provider here offers.

### Measured conditions, and why presence alone was not enough (M05)

`causes context` compares a condition's **measured level**, not merely whether it appears. The earlier presence-only version saturated: a daily-cadence series falls inside every 24-hour window, so presence was 1.0 in both groups and every p-value was 1.0. Keeping the recorded magnitude restores the information the series actually carries, and a two-sided permutation test runs on the difference in means.

Three further guards, each test-asserted:

- **A level that drifts with calendar time is confounded, not a finding.** Aggregate stablecoin supply rises steadily, so a window from 2021 and a window from 2026 are not comparable levels. The earliest and latest thirds of the pooled windows are compared, and a drift of at least one standard deviation marks the comparison `confounded_by_time_trend`, which withholds `significant_after_correction` no matter how small the p-value. This is what stops a large, highly significant-looking difference from being reported as a result.
- **A group of one window produces no p-value**, for either the presence or the magnitude test.
- **The verdict and the per-row flag cannot disagree**: `inference_status` counts only conditions that cleared the correction and are free of the drift confound.

**Measured result on the daily ladder, 24-hour pre-window:**

| tier | move windows | controls | strongest condition | difference | p | adjusted p | drift-confounded |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `p3` | 433 | 99 | `stablecoin_supply` | −$31.5bn | 0.003 | 0.009 | yes |
| `p5` | 195 | 99 | `stablecoin_supply` | −$36.2bn | 0.002 | 0.006 | yes |
| `p7` | 96 | 99 | `stablecoin_supply` | −$52.4bn | 0.0005 | 0.0015 | yes |
| `p11` | 20 | 99 | `stablecoin_supply` | −$53.0bn | 0.021 | 0.042 | yes |

The 3% threshold you asked for is what made this testable at all: 433 move windows against 99 controls, comfortably past the 8-and-8 minimum, where the 11% threshold gave 20. But **nothing survives**. The one condition that comes close is aggregate stablecoin supply, lower before moves than in quiet periods, and its difference grows with move size — which is exactly the shape a real effect would have. It is also a level that rises by an order of magnitude over the sample, drifting 2.1 standard deviations across the compared windows, so the difference is confounded with when the windows sit and is reported as such. Reading it as "capital was pulled out before big moves" would be reading a calendar trend as a mechanism.

Two honest consequences. First, a trending level must be stored as a **change** rather than a level to be testable this way, which is what `market_activity` already does and why it is the better-shaped series — but it only covers 180 days, leaving 13 usable windows at `p3`. Second, the conditions that would survive are ones whose value is stationary by construction: a daily change, a rate, a ratio, or a z-score against that asset's own trailing history. None of the currently wired free sources is stored that way.

## Financial-position view (F02)

`lele --json finmap 1 2` displays stored financial positions for 1–10 unique local entity IDs, in request order. The menu also exposes `finmap`. Default output is indented JSON; `--json` is compact. No network requests or metric/relationship writes are made. Each issuer is limited to 1000 stored metric rows (including unsupported keys); exceeding the limit fails rather than silently truncating.

Each issuer has separate period/source groups containing fourteen canonical `positions`: assets, liabilities, equity, cash, total debt, net inventory and three debt components (stocks), revenue and net income (accounting flows), and three net cash flows. `kind` is stock/flow; `flow_type` distinguishes `accounting_flow` from `cash_flow` (null for stocks). Each position includes a scope description. Positions expose value, status/reason, currency/unit/scale and the F01 observation evidence, including dates, filing and retrieval provenance. `raw_metrics` preserves the stored inputs, including conflicting aliases. Missing, legacy, ambiguous or invalid inputs yield null values with reasons, never zeroes. Empty issuers have no invented periods and report a warning.

`alignment.stocks`, `alignment.flows` (income/revenue) and `alignment.cash_flows` check their respective required inputs and recorded end/accession/form/unit metadata; duration inputs also require matching start dates. Individually valid observations remain visible when alignment is unknown. Total debt is not mapped or derived from SEC components, so stock alignment remains unknown even when all other balances are available. Alignment is not independent verification of reporting scope or cross-issuer comparability, and separate category checks do not imply a common filing across them.

No totals, aggregation, market valuation or transfer edges are inferred. Cash is already part of assets. Different periods, filings and sources are not combined into an apparent common snapshot. Tests use two explicitly synthetic issuer fixtures, not verified real-company financial statements.

### Inventory, debt components and net cash flows (F03)

Seven exact-tag US-GAAP mappings extend the existing `sec_fact_v1` envelope; no migration or historical backfill:

| Metric | Exact tag | Type / reporting scope |
| --- | --- | --- |
| `inventory_net` | `InventoryNet` | Instant; net inventory expected to be sold/consumed within one year or the operating cycle, if longer |
| `long_term_debt_current` | `LongTermDebtCurrent` | Instant; current portion of long-term debt, excluding leases |
| `long_term_debt_noncurrent` | `LongTermDebtNoncurrent` | Instant; noncurrent portion of long-term debt, excluding leases |
| `short_term_borrowings` | `ShortTermBorrowings` | Instant; initial terms shorter than one year or operating cycle, not current maturities of long-term debt |
| `net_cash_from_operating_activities` | `NetCashProvidedByUsedInOperatingActivities` | Duration; net operating cash flow including discontinued operations |
| `net_cash_from_investing_activities` | `NetCashProvidedByUsedInInvestingActivities` | Duration; net investing cash flow including discontinued operations |
| `net_cash_from_financing_activities` | `NetCashProvidedByUsedInFinancingActivities` | Duration; net financing cash flow including discontinued operations |

Definitions and monetary/period types were checked against the [FASB 2025 schema](https://xbrl.fasb.org/us-gaap/2025/elts/us-gaap-2025.xsd), [documentation](https://xbrl.fasb.org/us-gaap/2025/elts/us-gaap-doc-2025.xml) and [labels](https://xbrl.fasb.org/us-gaap/2025/elts/us-gaap-lab-2025.xml). This does not verify every historical taxonomy release; companyfacts envelopes do not record a taxonomy year. Values remain USD/currency/scale 1; the taxonomy itself is not USD-only.

Debt components are never summed or substituted for `total_debt`; existing SEC debt ratios remain unknown. `LongTermDebt` and lease-combined tags have different scopes and are not fallback aliases. Net cash flow retains its reported sign, including negative and zero values; continuing-only/discontinued-only tags are not substitutes. Missing exact tags remain unknown (listed in `show` under `sec.facts_missing` after fetching). No derived quarters, free cash flow, cash reconciliation, or transfer counterparties are inferred. Inventory and cash are parts of assets, not additions to total assets. Refetch `--financials` to ingest these selected facts for an issuer.

## Analysis methodology

Fundamentals use stored accounting metrics only: `total_assets`, `total_liabilities`, `total_equity`, `net_income`, `revenue`, `cash_and_equivalents`, `total_debt`. Common aliases such as `assets`, `equity`, `cash`, `net_profit` are supported. Eight possible ratios compare liabilities/assets, equity/assets, debt/assets, debt/equity, income/revenue, income/assets, income/equity and cash/assets.

Inputs must share nonempty period and source. Currency, unit and scale metadata must match when supplied; partially supplied/conflicting metadata blocks affected ratios. When neither input has metadata, a common-basis assumption is prominently reported. Per-metric companions such as `total_assets_currency` override only when consistent with group metadata. There is no FX conversion or reconciliation between IFRS/GAAP or consolidated/standalone accounts. Nonpositive denominators and unavailable data yield unknowns, never a healthy default. Income/assets and income/equity use period-end balances, not average balances, and are not annualized.

Generic ratios do **not** determine bank solvency, CET1, liquidity coverage or NPL quality. World Bank metrics are stored as `macro:<indicator>` and do not enter institution ratios. SEC `fetch sec --financials` fetches selected US-GAAP USD facts, not complete statements or all-history data. Other connectors do not currently fetch company accounting statements.

Sentiment is an explainable **English token lexicon with simple negation**, not an LLM, forecasting model or reliable multilingual classifier. It records supplied-text evidence, positive/negative word counts and a bounded score. Empty/unrecognized inputs remain insufficient; lexical coverage is not statistical confidence. Repeated input is not deduplicated and can bias the mean of recent stored signals. Sarcasm, entity attribution and nuanced financial language remain limitations. No sentiment score or ratio generates buy/sell advice or price targets.

## Storage and networking

Default database: `~/.lele/lele.db`. Override with `--db` or `LELE_DB`. SQLite uses foreign keys, transactions, WAL and schema migrations; future schema versions are rejected. Stop writers before backing up, or use SQLite's backup API; copying only an active WAL database file can omit committed data. Protect the database/cache with local account permissions; they are not encrypted.

| Environment variable | Default / purpose |
| --- | --- |
| `LELE_DB` | Database path |
| `LELE_CACHE_DIR` | `~/.lele/cache` |
| `LELE_CACHE_TTL` | 21600 seconds; `0` disables caching |
| `LELE_RATE_LIMIT` | 1.5 seconds between same-host requests in one process |
| `LELE_USER_AGENT` | Honest application identifier; optional real contact identifier |

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

## Release checklist

Every change is prepared and verified offline before it is treated as releasable:

1. Record the work in `WORKLOG.md` (what was done, tests actually run, limitations, dirty files, exact next task) and update `DEVELOPMENT_PLAN.md`/README where behavior changed.
2. Run the full offline suite and linters directly (no truncating pipes): `unittest discover`, `ruff check .`, `mypy`, `compileall -q lele`, `git diff --check`.
3. Confirm `lele version`/`doctor` on the target platform: Python **3.11+**, SQLite **3.35+**, `integrity_check=ok`, no foreign-key violations, schema at the expected version.
4. Back up any real registry with `backup PATH` and confirm the copy opens and matches (`entities`, schema version); `doctor` reports a stale schema as `migration_pending`.
5. Re-check source rights/licensing and keep live source tests opt-in, isolated and bounded; never bypass blocks or guess endpoints.
6. Commit only on explicit request, with research databases/cache kept out of git; keep the objective status honest (`not_achieved` unless independently verified).
7. Tag/version bump: keep `APP_VERSION` and `pyproject.toml` in sync (the packaging test enforces this) and note that no claim of worldwide coverage or accuracy is implied.

## Verification

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/python -m compileall -q lele
```

The offline suite covers 485 tests, including SEC observation round-trip, quarter/YTD mismatch, accession/form conflicts and legacy-row safeguards, plus the R02 decision/flow observation bridge (import/validate/project into world-state, 8 tests), the P06 SEC Form 4 extractor (parse/fetch/store, 6 tests), the SEC Form 13F holdings extractor (parse/index/fetch/store, 7 tests), the SEC material-event extractor (metadata/filter/project, 6 tests), the SEC Form D extractor (parse/filter/fetch/store/project, 6 tests), the SEC N-PORT holdings extractor (parse/filter/fetch/store, 6 tests), the P07 USAspending award extractor (exact-UEI matching/fetch/store, 4 tests), the P07 Senate LDA lobbying extractor (party binding/fetch/store, 4 tests) and the P07 Treasury Fiscal Data extractor (macro_release/fetch/store, 6 tests), including GLEIF manager-edge provenance, optional corroboration fallback, local batching and CLI validation, plus OSFI mapping, pagination, evidence links, estimated totals, malformed envelopes and atomic rollback, plus GLEIF category/website and SEC submissions/filings/facts coverage, plus bounded price-export, projection, comparison, event-study and P03 prospective-ledger coverage (29 prospective tests), the evidence-first world-state layer (18 world-state tests), the Binance USD-M futures, CFTC COT and FINRA short interest evidence extractors (25 evidence tests), the GDELT news extractor (9 news tests) and the pump/dump episode engine with non-episode controls (10 episode tests). Live acceptance on 2026-09-17: GLEIF FUND selection (7,233 GB funds, 25 stored), SEC AAPL with `--financials` (FY2026 balance-sheet facts), and the analyze command consuming them (net margin 0.278474 from live data). Live acceptance exercised FDIC fetch → list → show → analyze → export against a temporary database. An empty analysis correctly reported missing fundamentals rather than inventing ratios. GLEIF and World Bank also received small live connector smoke checks during implementation. These checks do not establish worldwide completeness or future API availability.
