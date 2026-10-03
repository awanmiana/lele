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
| Local OHLC export | Price history for a listed equity or exchange-traded commodity, via `import-history` | User supplies an export from a source they may use; no network call, no field verified, `rights_verified` always false, `adjustment` basis required. Not wired to a provider on purpose — see below |

Use `lele --json sources` to see actual endpoints and coverage. Requests are bounded to 1–1000 input records and at most ten pages. Results report pages, counts, warnings and truncation. Results are bounded and not a snapshot guarantee; small feeds can be exhausted when `truncated` is false and counts match the reported total. This does not establish complete jurisdiction coverage. `stored` counts processed institution upserts, excluding auxiliary regulator entities. Cross-source duplicates are deliberately not auto-merged. OSFI has no stable institution ID: its keys hash normalized name/type/group/industry, not CKAN row IDs; renamed or reclassified records require reconciliation.

No bundled speculative bank list, automatic global regulator discovery, news crawling, comprehensive market-price coverage, sanctions matching, trading or brokerage integration is implemented. Source availability and terms can change. **Market-price coverage outside Binance spot is not implemented as a fetch**: no free, keyless daily OHLC source with terms that permit automated use was found, Yahoo is recorded as unsupported, Stooq is behind a browser-verification challenge that is not ours to defeat, and the keyed providers are blocked until a real key exists. `import-history` takes the export from you instead; that is a deliberate exclusion, not an oversight.
## Commands

Global options **precede** the command: `lele --db PATH --json COMMAND ...`.

Two ways in. `lele` with no command on a terminal, or `lele menu`, opens an
interactive menu that lists every command by number and name and runs whichever
you choose; arguments are typed in with shell quoting, no shell is executed, and
`back` or `quit` returns at any prompt. `lele` with no command and no terminal
prints the help, and `lele --json` with no command prints the command table as
data.

**`lele summary` writes and prints a generated inventory of everything above.** It
is the fastest way to see the whole surface area, and it is regenerated from the
program rather than maintained by hand:

```bash
lele summary                          # prints the document, writes lele-summary.md
lele summary --output report.md       # a chosen destination
lele summary --format json            # the same report as data
lele summary --quiet                  # write the file, print only the receipt
lele summary --force                  # replace an existing file atomically
```

The file carries every command with its one-line purpose, its full argument
syntax and whether it reads or writes the registry; the five official datasets
behind `fetch` with each one's stated coverage limit; the endpoint allowlist,
which is the real boundary of what the program can reach; the observation
taxonomy by family; the volatility estimators; the frozen indicator and forecast
methods; the cited allocation frameworks and the claims this project declines to
assert; the standing objective at its frozen 0.9 with status `not_achieved`; and
what the program does not do. It works with no registry at all and says so, rather
than printing zeros that would be indistinguishable from an empty one. An
existing file is refused without `--force`, and the target may not be the registry
or its journal files.

What the document is **not**: evidence that any of it works, and not a coverage
claim. It says what the program can *attempt*. Every count in it is a count of
this program's own vocabulary, and its completeness is reported as `unknown`.

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
lele import-history 42 aapl-daily.json --interval 1d
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
lele context series stablecoin_supply
lele context derive stablecoin_supply --measure change
lele context derive market_activity --instrument binance:BTCUSDT
lele context show stablecoin_supply --measure zscore --limit 20
lele prune plan  --before 2024-01-01T00:00:00+00:00 --keep-bars 400
lele prune apply --before 2024-01-01T00:00:00+00:00 --reason "storage pressure"
lele prune runs
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

## User-supplied price history for a non-Binance instrument

`fetch-history` stores years of Binance spot klines and nothing else, so a listed
equity or an exchange-traded commodity has no bars and therefore **no measured
window**: `explain` could report an issuer's SEC filings, insider trades and
holdings while having no price to measure any of them against. That was the
largest remaining gap in the tool.

It is a data gap, and it cannot be closed by picking a provider here:

| Candidate | Why it is not wired |
| --- | --- |
| Yahoo chart API | Already recorded in this project's own source inventory as unsupported and license-unclear. Not cleared for stored history. |
| Stooq | Free and keyless, but observed on 2026-09-29 answering **every** request, including the plain CSV download, with a JavaScript proof-of-work browser check whose `/__verify` step exists to keep non-browser clients out. Passing it is defeating an access control, so it is recorded as blocked rather than integrated. |
| Alpha Vantage, FMP, Polygon, Twelve Data, EODHD, Nasdaq Data Link | Terms are clear, but each requires an API key. This project treats a key as blocked until a real one exists (the same rule that leaves FEC and FRED unwired). Never invent a contact address to obtain one. |
| Exchange and vendor feeds (CME, ICE, LBMA, licensed vendors) | Correct data, licensed and paid. Not something to scrape around. |

So the provider is you. `import-history` stores a bounded OHLC export you took
from a source you have the right to use:

```bash
lele import-history 42 aapl-daily.json --interval 1d
lele moves 42 --interval 1d --move-hours 24 --thresholds 3 5 7 11
lele explain 42 --from 2026-09-01T00:00:00Z --to 2026-09-08T00:00:00Z
```

The format, with `Date`-style fields you would map from a CSV or a broker export:

```json
{
  "instrument": {
    "symbol": "AAPL",
    "venue": "NASDAQ",
    "currency": "USD",
    "asset_class": "equity",
    "adjustment": "unadjusted",
    "rights_basis": "my subscription, research use"
  },
  "source": "my-broker",
  "source_url": "local:broker-export-2026-09-28",
  "retrieved_at": "2026-09-28T12:00:00+00:00",
  "bars": [
    {"open_time": "2026-09-25T13:30:00Z", "close_time": "2026-09-26T20:00:00Z",
     "open": "224.50", "high": "226.10", "low": "223.90", "close": "225.80",
     "volume": "41200000"}
  ]
}
```

What it guarantees, each asserted by `tests/test_price_import.py`:

- **Validated before anything is written.** One bad row means the file stores
  nothing, rather than storing a silently short series. The bad-row tests put the
  defect in the *last* bar, so a validator that only checked the opening rows
  would still fail them.
- **Idempotent, and unambiguous.** Rows are keyed on `(instrument, interval, open
  time)` as `fetch-history` does, and every timestamp is canonicalized to UTC
  first — so one session written as `09:30-05:00` and as `14:30Z` is one bar, not
  two. Re-running refines; it does not duplicate.
- **Adjustment basis is required, not guessed.** A split repairs nothing and
  silently corrupts every return that crosses it, so `adjustment` must be stated
  as `adjusted`, `unadjusted` or `unknown`, and `unknown` is reported as a known
  unknown rather than assumed away.
- **Absence is reported as absence.** A `venue`, `asset_class` or `rights_basis`
  the file omits appears in `unknowns` and is stored as `unknown`. A symbol is
  never used to guess a venue, a currency or an asset class.
- **Cadence is measured, not assumed.** An equity's daily bars are not one day
  apart throughout: weekends and holidays make the spacing alternate between one
  day and three. The report gives the measured dominant spacing and the number of
  holes, so `moves` can exclude a window that spans one instead of silently
  calling it a 24-hour move.
- **No network call is made, and nothing is verified.** The module cannot reach
  the network, and a test asserts that it holds no client. `rights_verified` is
  `false` whatever the file claims: the report records the export's own claims.
- **A bar that has not closed is refused.** `close_time` in the future is not a
  measurement. If `close_time` is omitted it is derived from `open_time` plus the
  interval, and each stored row records that it was derived.
- **No accuracy claim.** Imported bars are stored with an `ingest_runs` row
  (source, counts and a records hash), so provider health monitoring can see them
  later like any other source.

Two limits are inherent rather than fixed. `price_bars` has no unknown-volume
representation, so a provider that reports no volume must be given one
explicitly. And a `1d` interval is a label, not a session: `moves --move-hours 24`
on equity daily bars measures a one-session return, and any window spanning a
weekend or holiday is excluded and counted, not reported as a 24-hour move.

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

## Instrument identity, realized volatility, and unusual moves (v17)

Four commands and two tables answer the question "how much does this asset
usually move, and is this move unusual for it?" That question is answerable from
stored bars. "Where will it go next" is a different question and is not answered
here.

```bash
lele instruments add ID --symbol SYMBOL --venue VENUE --asset-class CLASS [options]
lele instruments list [--asset-class CLASS] [--symbol SYM]
lele instruments show ID
lele volatility ID [--interval 1d|4h|1h] [--window-bars N]
                  [--estimator NAME]... [--asset-class CLASS] [--store]
lele rag detect-anomalies --instrument-key KEY [--mode percentile|z_score|absolute]
                          [--level LEVEL] [--window-bars N] [--baseline-bars N]
lele framework list|show|excluded|all [--grade GRADE] [--topic TOPIC]
```

**A symbol is not an identity.** `instruments` records symbol, venue, asset class,
quote currency, contract multiplier, expiry, adjustment basis and the recorded
basis on which the series may be used. The same symbol on two venues is two
instruments; a continuous futures series is not a fixed-expiry contract; a
back-adjusted series is not the unadjusted one. That distinction previously lived
only inside an `evidence` blob, which nothing could join or filter on.
`rights_verified` is always false: nothing in this project verifies a
redistribution right, and no contact address was invented to obtain one.

**`volatility` implements nine estimators** from stored OHLC, all in `Decimal`:

| Estimator | Note |
|---|---|
| `close_to_close`, `close_to_close_demeaned` | zero drift is biased upward by μ²; demeaning spends a degree of freedom |
| `parkinson` | high-low only; cannot see an opening gap |
| `garman_klass` | signed per-bar terms; summed before rooting |
| `rogers_satchell` | allows drift; still assumes no opening gap |
| `yang_zhang` | the only one carrying an explicit overnight-gap term |
| `lpv` | **the default**, the equal-weighted mean of Parkinson, Garman-Klass and Rogers-Satchell |
| `atr`, `natr` | Wilder smoothing, seed pinned |

Each estimate reports the **convention that produced it** — drift handling, ddof,
the Yang–Zhang weight formula, the ATR seed and period — because the estimator name
alone is not reproducible. Two reputable references give two formulas for the
Yang–Zhang weight `k` (~0.2% apart); this project pins the TTR form and records the
choice in every stored row.

Why `lpv` is the default rather than the most accurate-sounding estimator: across
replicated studies the R² of all five range estimators against next-period realized
volatility is nearly identical (roughly 43–46%). The high/low information buys
*calibration*, not predictive power. Averaging is the choice that is hard to get
badly wrong when no prior information favours one estimator.

**What is refused rather than approximated.** A window whose bars are not
contiguous is refused with `window_spans_gap`, because a 30-bar window across a
missing bar covers more elapsed time than it names. A signed Garman-Klass or
Rogers-Satchell total that is not positive is `not_computable`, never clamped to a
small number. An all-identical-price window is `not_computable`, because a zero
variance is not a volatility of zero to be divided by later. Realized kernel and
bipower jump detection need an intraday sampling grid and are **absent** rather than
approximated from daily bars. An unregistered instrument has no asset class, so its
annualized figure is `unknown` rather than scaled by an assumed 252. Annualization
multiplies by a conventional bars-per-year count (252 session-based, 365 crypto) and
is recorded as a comparability convention, not a scaling law.

`natr` is explicitly flagged as **not** invariant to an additive splice, because it
divides a price range by a price. On a spliced futures or crypto series the usable
figures are absolute `atr` and the log-return estimators.

**`detect-anomalies` judges a window against that instrument's own trailing
distribution.** Three modes, in order of preference:

- `percentile` (default) — the empirical CDF, so no distributional assumption.
  Primary, because returns are fat-tailed and a Gaussian table would overstate how
  exceptional a move is.
- `z_score` — reported beside it, because a percentile cannot distinguish the 94th
  from the 96th at n=20.
- `absolute` — comparable across instruments, and the only mode where a fixed
  percentage is meaningful. Kept because sometimes that is genuinely the question.

The baseline **never contains the window being judged**: including it inflates the
mean and deflates the spread, compressing the score toward zero until the threshold
quietly stops firing. Minimum baselines are 100 observations for a percentile, 60
for a z-score (the sample standard deviation's relative error is ~15% at n=20 and
~9% at n=60), 20 for an absolute rule. Below the minimum the verdict is `null` with
the reason, never a confident answer.

Observed live on 267 daily bars of a synthetic index series, 258 windows examined:
the 95th-percentile rule flagged 8 moves, the 99.5th flagged 1, and an absolute 3%
rule flagged **36** — the same data, the same windows, and a fixed percentage firing
four times as often. That gap is the argument for the percentile mode.

Severity is derived from the percentile **observed**, not from the level requested,
so a permissive 95th-percentile rule cannot manufacture `critical` alarms.

**`framework` is a cited record, not advice.** It holds documented positions on how
money is allocated, each with its primary source, an evidence grade
(`primary`/`secondary`/`vendor`), and the documented criticism of it. It also holds
an explicit list of widely circulated claims this project **declines to assert**
because no primary source was reached — including the Paul Tudor Jones 1/5/6 rule,
"volatility-managed portfolios doubled the Sharpe ratio", and the specific
Bogle/Cooke/Buffett figures that circulate with citations attached. It produces no
signal, no score, no ranking and no position, and nothing in it is consumed by any
detector or estimator. `tests/test_framework_notes.py` enforces this mechanically: a
directive or a quoted Sharpe/return claim anywhere in the asserted content fails
the suite.

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

**`import-history ID PATH [--interval 1d|4h|1h]`** stores a **user-supplied** OHLC export in the same `price_bars` table, keyed idempotently on `(instrument_key, interval_seconds, open_time)`, and makes no network call. It exists because `fetch-history` reaches only Binance spot pairs, so a listed equity or an exchange-traded commodity had no stored bars and therefore no measured window at all. The JSON format, the required `adjustment` basis, the UTC canonicalization that keeps one session from becoming two rows, the measured-cadence and gap reporting, and the exclusions for providers whose terms do not permit automated use are all documented under **User-supplied price history for a non-Binance instrument**. Every run is recorded in `ingest_runs` with a records hash, so provider health monitoring can see imported series like any other source. Nothing in the file is verified and `rights_verified` is always false.

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
- **The size of the control group is reported, not just its existence.** `control_sampling` publishes `requested`, `eligible` (non-move timestamps that existed before thinning), `used`, `dropped_without_coverage` and the move-to-control ratio. A permutation test can only resolve a difference the smaller group can express, so a small `used` against a large `eligible` means the power was set by the requested sample rather than by the stored data. `--controls` defaults to 10, which is a quick look and not a powered comparison; a full run needs `--controls 500`.
- **A stored move row is re-checked against the stored bars before it is used.** A row records a start and an end but nothing about the bars between them, so a row written by a detector that predates the contiguity check could anchor a pre-window to the wrong date. `moves.verify_stored` re-derives the cadence, the gap set and the horizon from the stored bars and refuses a row whose window spans a missing bar, whose start or end bar is gone, that runs backwards, whose length is not the recorded move length, or that cannot be checked because no bars are stored. `profile_context` and `profile` report the refusals as `unverified_move_windows`, `attribute` as `moves_unverified`, and `explain` as `unverified_rows`.

A group of one window produces no p-value, because it has no permutation variance.

**Measured result, and it is a negative one.** On the stored 4-hour series, every available condition reads `move_share 1.0` against `control_share 1.0` and every p-value is 1.0. At a 24-hour pre-window the daily-cadence context series are **saturated**: a daily observation falls inside every window, so presence carries no information. At a 6- or 12-hour pre-window the same series leave most windows empty, so no usable sample exists. In other words the current context sources have **no discriminative power for this question at any window length**, and the tool now proves that with a proper test and a multiple-testing correction rather than asserting it; the 2026-10-02 re-establishment on the daily ladder with a powered control group reaches the same conclusion by the measured-mean route (`AUDIT.md` §12). The fix is not more statistics but higher-cadence context: the free sentiment and supply series are daily-only, so detecting a pre-move condition requires an hourly or finer source that no free provider here offers.

### Measured conditions, and why presence alone was not enough (M05)

`causes context` compares a condition's **measured level**, not merely whether it appears. The earlier presence-only version saturated: a daily-cadence series falls inside every 24-hour window, so presence was 1.0 in both groups and every p-value was 1.0. Keeping the recorded magnitude restores the information the series actually carries, and a two-sided permutation test runs on the difference in means.

Three further guards, each test-asserted:

- **A level that drifts with calendar time is confounded, not a finding.** Aggregate stablecoin supply rises steadily, so a window from 2021 and a window from 2026 are not comparable levels. The earliest and latest thirds of the pooled windows are compared, and a drift of at least one standard deviation marks the comparison `confounded_by_time_trend`, which withholds `significant_after_correction` no matter how small the p-value. This is what stops a large, highly significant-looking difference from being reported as a result.
- **A group of one window produces no p-value**, for either the presence or the magnitude test.
- **The verdict and the per-row flag cannot disagree**: `inference_status` counts only conditions that cleared the correction and are free of the drift confound.

**Measured result on the daily ladder, 24-hour pre-window, re-established 2026-10-02** on 3332 Binance daily bars (2017-08-17 to 2026-10-01) with 499 controls used of 1948–2940 eligible:

| tier | move windows | controls | strongest condition | difference | p | adjusted p | drift-confounded |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `p3` | 526 | 499 | `stablecoin_supply` | −$15.8bn (−7.0%) | 0.031 | **0.093** | yes |
| `p5` | 252 | 499 | `stablecoin_supply` | −$12.3bn (−5.5%) | 0.251 | 0.673 | yes |
| `p7` | 124 | 499 | `stablecoin_supply` | −$27.6bn (−12.5%) | 0.127 | 0.382 | yes |
| `p11` | 30 | 499 | `stablecoin_supply` | +$14.3bn (+6.5%) | 0.728 | 0.728 | yes |

**Nothing survives at any tier**, which is the negative finding, and it holds. `inference_status` reports that no category stands out against the control windows; the fear and greed index gives no significant difference at any tier (adjusted p 0.27 to 0.93) and no drift, and `market_activity` gives none over the one year its coverage reaches.

**An earlier reading of this table was wrong and has been withdrawn.** It reported a monotone pattern — −$31.5bn at `p3` growing to −$53.0bn at `p11`, adjusted p as low as 0.0015 — and called the monotone shape "exactly the shape a real effect would have". That table used 99 control windows; this one uses 499. With 9 controls the raw p on `stablecoin_supply` was 0.93 and with 499 it is 0.031, so the group size was not a detail. Under a powered control group the difference is negative at three tiers and **positive at the top one**, and no tier reaches adjusted p ≤ 0.05. The monotone shape was an artifact of a thin control group drawn from across a series that rises by an order of magnitude. What survives is the part the earlier reading also reached: the level drifts 2.33 standard deviations across the compared windows, so its difference is confounded with when the windows sit and no amount of sampling makes a trending level testable this way. Reading it as "capital was pulled out before big moves" would be reading a calendar trend as a mechanism. `AUDIT.md` §12 records the re-establishment and the two defects it found.

Two honest consequences. First, a trending level must be stored as a **change** rather than a level to be testable this way, which is what `market_activity` already does and why it is the better-shaped series — but it reaches 365 days, leaving 37 usable windows at `p3` and 1 at `p11`. Second, the conditions that would survive are ones whose value is stationary by construction: a daily change, a rate, a ratio, or a z-score against that asset's own trailing history. That is now built — see the next section — and on this data the stationary comparison is negative too.

### A drifting level, stored as a stationary quantity (v18)

`lele context derive <kind>` turns a stored context series into a **stationary** one, so the drift confound in the table above can be removed instead of merely reported.

```bash
lele context series stablecoin_supply                 # read-only: what is derivable, what is stored, what was refused
lele context derive stablecoin_supply                # writes context_measures rows
lele context show stablecoin_supply --measure change # read-only: the stored rows
lele causes context 1 --controls 500 --measure change --measure zscore
```

Two measures are derived, in `Decimal`, from stored rows only:

- **`change`** — the difference from the previous stored value, in a unit that names the interval it spans (`usd_change_per_86400s`). Stationary by construction and it needs no tuned parameter.
- **`zscore`** — the value in units of its own trailing baseline, through `volatility.z_score`, with the baseline excluding the point being scored and at least 60 observations, the minimum `MIN_BASELINE_Z` already requires of a published z-score.

`rate` and `ratio` are **not offered**, and `context series` says why for each rather than leaving it to look like an oversight: a rate is a change divided by a chosen period, which cannot change a conclusion the change does not already give; a ratio divides by a prior value that a change series crosses zero on, which is where the signal is largest.

Four rules govern every derivation, each the rule the move detector already follows. Cadence is **measured** from the stored timestamps, not read from a label. A difference spanning a hole is **refused, not computed**, so a derived series can be shorter than its parent. Every refusal is **counted by name** — `interrupted`, `no_prior_point`, `baseline_short`, `flat_baseline`, `unit_changed`, `not_increasing`, `repeated_timestamp`, `too_short_to_measure_cadence`, `unparsable_value`, `no_stored_level`, `value_not_representable` — and a test asserts every name in that list is reachable from some input. Re-deriving over unchanged rows **rewrites the same rows**: 8943 stored measures before and after a second full derivation on the live registry.

The derived rows go in a new **`context_measures` table (schema v18), not in `observations`**. A derived quantity is not a second reading of the world, and putting it in `observations` would let a difference and a level be listed side by side as if both had been observed, while every reader of that table — the window scan, the evidence projection, the flow attribution sums — would have no way to know one number is a difference of the other. The parameters are real columns for the reason `volatility_estimates` stores `ddof`: `baseline_observations` is part of the uniqueness key, so a score against 60 points cannot silently overwrite one against 120, and a reader shown two baselines is told so instead of being handed one.

**Measured on the same data, 499 controls, same windows:**

| tier | series | drift before | drift after | raw p | adjusted p |
| --- | --- | --- | --- | --- | --- |
| `p3` | `stablecoin_supply` level | 2.334 | — | 0.031 | 0.093 |
| `p3` | `stablecoin_supply` change | — | **0.039** | 0.161 | 0.193 |
| `p3` | `stablecoin_supply` zscore | — | 0.723 | 0.099 | 0.157 |
| `p5` | `stablecoin_supply` change | — | **0.026** | 0.589 | 0.848 |
| `p7` | `stablecoin_supply` change | — | **0.162** | 0.800 | 0.960 |
| `p11` | `stablecoin_supply` change | — | **0.048** | 0.797 | 0.967 |

**The drift is gone and the comparison it was blocking is negative.** The level's nearest approach, adjusted 0.093 at `p3`, becomes 0.193 on its change and 0.157 on its z-score; nothing in the measure family survives the correction at any tier. Three things about that number. The measure family corrects **six** tests against the level family's three, so adjusted values are not comparable across the two sections and quoting whichever clears the correction would be selecting on the outcome. `--measure` is **off by default**, and a default run on the same registry is byte-identical to the `--measure` run in `kinds` and `families`, because a feature that made older numbers look cleaner by default would have restated them. And `zscore` only partly removes the drift where `change` removes it, while costing 60 points at the start of the series and its own coverage: 1141 days against the change's 1200.

**A plan item turned out to be false, and is now recorded as measured.** It said extending `market_activity` over years "needs no new provider", because the endpoint accepts any `days`. It does not: keyless, `days=365` returns 366 daily points and `days=400` returns **HTTP 401**. The 365-day cap is the free tier's edge, so `market_activity` still leaves one measured move window at `p11` and no code change makes it otherwise.

Stationarity removes one confound. It does not make any context series a cause of a move, and it does not touch the saturated 24-hour pre-window: every wired context source is daily. `AUDIT.md` §14 records the built layer and the two defects found while building it.

## A failed fetch leaves a row (v19)

A run row exists for every fetch that reached the request stage, including the ones that failed. Before this, a failed fetch rolled its transaction back and left nothing — so a source that stopped answering and one that was never asked looked identical, and `lele providers` could report silence but never failure.

```bash
lele runs                       # the recorded runs, including failed ones
lele providers                  # failed_runs and the recorded_failure flag per series
```

The row is written **outside** the transaction it rolls back, which sounds like a contradiction and is not: `get_conn` rolls back first, so every row the fetch wrote is discarded, and *then* it writes and commits the row saying the fetch failed. One connection, one place that commits. A recorder that fails is swallowed and the original error still propagates — a failure to record a failure never replaces the failure.

A failure is recorded as `status: failed` with the request identity and query scope it was attempting, a reason from a closed vocabulary (`source_request`, `database`, `file`, `data`, `cancelled`, `unexpected`) and the exception's **class name**. Never its message: an exception message can carry a token or a password, and the rule that a classified message replaces the raw one applies to a database row as much as to the console. A test plants a secret in six exception types and asserts it reaches no column.

**Every fetcher that records a success records a failure.** Form 4, 13F, N-PORT, USAspending, LDA, Treasury, EIA, BLS, OpenSky, sanctions, Comtrade, Census, Federal Register, Form ADV, price history and `import-history` all register the same recorder their success row sits beside, so a failure lands in the same series as that source's successes. The coverage claim is a test over the source rather than a list of adopting commands: every module that calls `record_ingest_run` must also call `record_failures` **and** `clear_failure_recorder`, checked with `ast`.

**Every command that writes rows to the registry records a completed run and a failed one** — including `fetch-sentiment`, `fetch-stablecoins`, `fetch-market-activity` and `store-evidence`, which until now recorded nothing at all, so a *success* of theirs was invisible too. The three crypto-context sources also store the response hash they already computed and never persisted, so for those three a content change is detectable from the run table where it is not for the others.

**Four commands are absent from the run history, and the reason is a property of the code rather than an omission:** `fetch-prices`, `fetch-evidence`, `fetch-cot` and `fetch-short` read the registry through `read_connect` and write a document to a file. There is no transaction to roll back and no row to record. They are named in `provider_health.EXPORT_ONLY_COMMANDS`, printed in the report and quoted by `lele summary`, and **machine-checked against the routing**: a test parses the dispatch branches in `_dispatch` and fails if one of them ever opens a write session.

`AUDIT.md` §17 has the design and the two that could not work; §18 has the family adoption, two bugs a script wrote on the way, and the fact that the previous handoff's reason for deferring sanctions was simply wrong.

## Provider health: what the recorded runs say (v19)

`lele providers` reads the durable ingest runs and says which sources changed shape. `lele doctor` carries a reduced block, so the answer is where you look when something is already wrong.

```bash
lele providers                                   # every source-query series
lele providers --source sec-form4 --since-hours 168
lele providers --stale-after-hours 0             # switch the staleness flag off
lele doctor                                      # includes a reduced providers block
```

Runs are grouped by source, query, country, indicator and category, and each series publishes its run count, first and last start, age in hours, whether the recorded request identity was stable, and the **first/last/min/max of `fetched`, `stored`, `skipped`, `missing` and `pages`** — so a different threshold can be applied by hand rather than only accepting the one below.

Nine flags, each reachable from some input: `request_changed` (the recorded request identity differed, so an outcome difference is evidence about the request), `count_collapse` (fetched fell to at or below 0.5 of its peak across runs of the same request), `stored_nothing_once`, `stored_zero_while_fetched`, `returned_nothing`, `never_stored`, `always_truncated`, `no_record_hash`, `stale`.

**What it detects is change and silence, never wrongness**, and the report says so every time:

- **A failed run rolls back with its transaction and is never recorded**, so absence of a new run is not evidence that a source works. There is no `healthy` status and no `failed` flag, because the table cannot support either.
- **No run records a hash of the records themselves.** 17 of 25 recording sites pass no `records_sha256` at all; the 8 that do hash counts and page metadata. A provider returning the same number of different records is invisible here.
- **`request_sha256` is not uniformly a request hash** — `sanctions` stores the downloaded file's SHA-256 in it — so `request_changed` means the recorded request identity changed, which may be a query or a payload.
- `never_stored` cannot distinguish a source with nothing to report from one that stopped answering; both readings are in the flag detail. `stale` cannot distinguish a source that stopped producing from an operator who stopped asking. `always_truncated` is a coverage statement, not a defect: the stored history is a prefix of what the provider offered, so `completeness` stays `unknown`.

Measured on the research registry read-only: 15 runs, 11 sources, 12 series — `no_record_hash` on 10, `always_truncated` on 8, `never_stored` on 3, `stored_nothing_once` on 1. `AUDIT.md` §16 has the detail and the measured premise the item was built on.

## Retention cuts on stored price history (v19)

`price_bars` and `move_events` grow for as long as a fetcher runs, so there is now a way to cut them that says what it did.

```bash
lele prune plan  --before 2024-01-01T00:00:00+00:00     # read-only: counts, writes nothing
lele prune apply --before 2024-01-01T00:00:00+00:00 --reason "storage pressure"
lele prune runs                                          # what was cut, when, and why
lele prune plan  --before 2024-01-01T00:00:00+00:00 --series binance:BTCUSDT --interval 1d
```

A **plan is pure reads** and runs against a read-only mount, so the counts can be inspected before anything is deleted. `apply` counts through the same SQL predicate the plan counted with, checks each delete's row count against the plan, and writes a `prune_runs` row naming the instant, the reason, the per-table counts removed, the per-table counts straddling and what remains. `lele explain` and every stored-bar report then carry the recorded cut, so a window before it says the history was **removed** rather than being indistinguishable from a window that was never fetched.

Six rules, each one a rule some other part of this tool already follows:

- **A row is removed when the last instant it describes is strictly before the cut.** A move is dated by its end, an estimate by the end of its window, a bar by its close, an article by its own instant. A row landing exactly on the cut is kept, so every surviving bar lies wholly at or after it.
- **A row that straddles the cut is kept and counted.** Deleting it would remove a measurement still mostly inside the retained history; keeping it silently would leave a value that can no longer be reproduced from the remaining bars. `moves.verify_stored` refuses such a row by name when a reader asks.
- **A cut that would leave a series too short to measure is refused, with the series named.** The floor is `moves.MIN_BASELINE_BARS + 2` — the smallest number of closes the move detector accepts — and a series whose own longest stored window is longer gets a longer floor. Nothing is clamped silently.
- **The reason is required.** `apply` without `--reason` is refused before a connection is opened.
- **A deletion is counted, and the count is checked.** `move_causes` hangs off `move_events` by a cascading foreign key, so an article dated after the cut can be removed with its move; the delete set is the union of the aged and attached rows and the breakdown is published.
- **What is not pruned is named, with the reason.** `observations` and `event_store` are filed evidence, `ingest_runs` is the provenance a bar is attached to, `context_measures` is bounded by its parent series rather than by price history, and `semantic_embeddings` is rebuilt by nothing. All five are quoted in the report and in `lele summary`.

Named refusals, each reported rather than raised: `cut_not_in_the_past`, `unstated_reason`, `too_many_series`, `timestamps_not_canonical`, `cut_precision_not_stored`, `series_below_floor`, `scope_has_no_stored_bars`. A series whose timestamps are stored in two UTC conventions is refused rather than counted with a comparison that would put the boundary in the wrong place, and a test asserts every refusal name is reachable from some input.

Removed: `move_causes`, `move_events`, `volatility_estimates`, `cause_scans`, `price_anomalies`, `price_bars`. A cut is irreversible from inside this tool — the record states how many rows went, not what they contained, so take a `lele backup` first. Measured on a copy of the §12 registry: a cut at 2024-01-01 keeping 400 bars removed 2326 bars and 966 move rows and left 1006. `AUDIT.md` §15 has the detail and the four defects found while building it.

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

The offline suite covers 916 tests, including the stored-move-window verifier and the control-sampling report (`AUDIT.md` §12), SEC observation round-trip, quarter/YTD mismatch, accession/form conflicts and legacy-row safeguards, plus the R02 decision/flow observation bridge (import/validate/project into world-state, 8 tests), the P06 SEC Form 4 extractor (parse/fetch/store, 6 tests), the SEC Form 13F holdings extractor (parse/index/fetch/store, 7 tests), the SEC material-event extractor (metadata/filter/project, 6 tests), the SEC Form D extractor (parse/filter/fetch/store/project, 6 tests), the SEC N-PORT holdings extractor (parse/filter/fetch/store, 6 tests), the P07 USAspending award extractor (exact-UEI matching/fetch/store, 4 tests), the P07 Senate LDA lobbying extractor (party binding/fetch/store, 4 tests) and the P07 Treasury Fiscal Data extractor (macro_release/fetch/store, 6 tests), including GLEIF manager-edge provenance, optional corroboration fallback, local batching and CLI validation, plus OSFI mapping, pagination, evidence links, estimated totals, malformed envelopes and atomic rollback, plus GLEIF category/website and SEC submissions/filings/facts coverage, plus bounded price-export, projection, comparison, event-study and P03 prospective-ledger coverage (29 prospective tests), the evidence-first world-state layer (18 world-state tests), the Binance USD-M futures, CFTC COT and FINRA short interest evidence extractors (25 evidence tests), the GDELT news extractor (9 news tests) and the pump/dump episode engine with non-episode controls (10 episode tests). Live acceptance on 2026-09-17: GLEIF FUND selection (7,233 GB funds, 25 stored), SEC AAPL with `--financials` (FY2026 balance-sheet facts), and the analyze command consuming them (net margin 0.278474 from live data). Live acceptance exercised FDIC fetch → list → show → analyze → export against a temporary database. An empty analysis correctly reported missing fundamentals rather than inventing ratios. GLEIF and World Bank also received small live connector smoke checks during implementation. These checks do not establish worldwide completeness or future API availability.
