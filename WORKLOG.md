# Lele Worklog

## Current handoff — non-Binance price history, and the gate that was not gating (user-directed; resume here)

**Two defects in the gate itself, and the first queue item closed by not
fetching anything.** Everything below was done in one session on 2026-09-28 and
is recorded in `AUDIT.md` §7–§9.

**First, two gate defects, the same class as the one the audit already found
once.** `.tools/check.sh` ran `compileall -q finworld`, a directory the rename
had already deleted; `compileall -q` on a missing path prints `Can't list` and
**exits 0**, so the bytecode step had been a silent no-op reporting success since
the rename — and that step existing precisely so a syntax error is the *first*
thing noticed. Worse, `.gitignore` excluded `.tools/`, so the one command that
decides whether the tree is shippable existed only on the machine that wrote it.
Both fixed: the script compiles `lele tests`, the ignore is now `.tools/*` with
`!.tools/check.sh`, and `tests/test_gate.py` (6 tests) fails if either recurs.
Three of its tests were confirmed to fail against the pre-fix script.

**Second, the pipe hang now has a narrowed cause.** `AGENTS.md` said the cause
"was never established". `.tools/check.sh --fast | tail -20` blocks forever while
the unpiped command finishes in ~9 s; `time` shows the work completing (user time
matches) and then the read end waiting for an EOF that never comes, and `ps`
shows no stray child. This host runs the distribution under **PRoot** on Termux,
whose ptrace interception is the remaining suspect. AGENTS.md now says to
redirect to a file and read it. **Do not pipe the gate or a test run** — I did it
twice this session and lost ~12 minutes to it.

**Third, queue item 1 — non-Binance price history — closed as a rights problem,
not a code one.** TLS to every candidate verified from this host, so reachability
was never the issue:

- **Stooq** is free, keyless daily OHLC and was the intended pick. It answers
  *every* request, including the plain CSV download, with a JavaScript
  proof-of-work browser check whose `/__verify` step exists to keep non-browser
  clients out. Passing it is defeating an access control, so it is recorded as
  blocked. **The user had authorized wiring it; the authorization cannot be
  exercised, because the only way through is a bypass this project forbids. No
  bypass was attempted.**
- **Yahoo** is already recorded in this project's own inventory as unsupported.
- **Alpha Vantage, FMP, Polygon, Twelve Data, EODHD, Nasdaq Data Link** need an
  API key, which this project treats as blocked until a real one exists. No
  contact address was invented.
- **CME/ICE/LBMA** are licensed and paid.

So the provider is the user: `import-history ID PATH [--interval]` stores a
bounded, validated OHLC export into `price_bars` and makes no network call. 33
offline tests, including a CLI test that imports 30 daily equity bars and has
`moves` measure a −14% session. Verified load-bearing behaviour: timestamps are
canonicalized to UTC *before* the `(instrument, interval, open time)` idempotency
key is taken, so one session written as `09:30-05:00` and as `14:30Z` is one bar
rather than two — removing that canonicalization fails the test. `adjustment` is
required rather than inferred; omitted venue/asset class/rights basis are reported
in `unknowns` as `unknown`; cadence and holes are measured rather than assumed
from the interval label, which is what lets `moves` exclude a weekend-spanning
window instead of calling it a 24-hour move; a bar that has not closed is refused;
`rights_verified` is always false and a test asserts the module holds no HTTP
client. **Not verified live**, because it takes no network path and there was no
user export to run.

**Verified.** `.tools/check.sh` passes unpiped: 754 offline tests with
`ResourceWarning` as an error, `ruff` clean, `mypy` clean over 56 files,
`compileall` now genuinely compiling `lele` and `tests`, in 221 s.

**Precise next task.** Queue item 2, unchanged and now first: re-establish the
M04/M05 enrichment results on the corrected move detector, since those negative
results were produced by code that could mis-measure a window and should not be
cited until re-run. After that: prune `price_bars`/`move_events` (item 3),
provider health monitoring on the `ingest_runs` hashes (item 4, and
`import-history` now feeds it), then the type annotations (item 5).

**Dirty files, uncommitted, nothing staged or pushed:** `.gitignore`, `AGENTS.md`,
`AUDIT.md`, `DEVELOPMENT_PLAN.md`, `README.md`, `WORKLOG.md`, `lele/cli/main.py`,
`tests/test_cli.py`, `tests/test_db_guarantees.py`, and new untracked
`.tools/check.sh` (previously gitignored), `lele/analysis/price_import.py`,
`tests/test_gate.py`, `tests/test_price_import.py`.

---

**The audit summary that follows is the prior session's and is retained for
context.**

**The user asked for a full review of the project: every design, architecture and
code flaw, every bug, dead end and point of failure; whether the tool can
achieve its stated objective; and a professional rebuild of whatever is not
sound. That work is done and recorded in `AUDIT.md`. This is the summary.**

**What the tool is, stated plainly.** Two things share one codebase. A
public-source research registry — ingest official data, keep every claim
attached to a source, a retrieval time and a reason, never present a sample as
a population — which is achievable and largely built. And a market-analysis
harness — store history, detect moves, look for preceding conditions, record
prospective forecasts before the outcome — which is achievable as *apparatus*
and not as *answer*.

**The 90% five-minute direction objective cannot be met, and no engineering
changes that.** At that horizon the next bar's direction is close to a coin
flip; a 90% hit rate would need information that is not in public data. The
tool's own live results are honest negatives: stablecoin supply and the fear
and greed index carry no discriminative power at any window length, and the
promising result that preceded them was a coverage confound, correctly found and
removed. What was fixed is the *measurement*, so the question can be tested
honestly if anyone wants to test it.

**The most serious finding was in the measurement of the objective itself.**
`scripts/realtime_prospective.py` — the cron runner that accumulates the
prospective samples which would settle the question — pre-registered
`target_rate: 0.5` against a documented objective of `0.9`, and listed only
`bitcoin` among the required asset classes, and passed `force=True` so it
would overwrite a run in progress. That is the exact failure the harness exists
to prevent. It now carries `0.9`, all four required classes, refuses to
replace a run, and `tests/test_realtime_prospective.py` fails if any of that
drifts.

**Second: a "24-hour move" could be a move of any length.** Bars arrive from a
bounded, resumable fetch, so a series can contain a hole. Move detection took a
return as the difference between two closes a fixed number of bars apart with no
contiguity check, so a window spanning a missing bar silently covered eight
days while still being labelled, stored, counted and compared against a
baseline built on the same distorted series. Cadence is now measured from the
data, gaps are found, and any window that spans one — or whose real elapsed
time is not the requested horizon — is excluded and reported.
`tests/test_price_gaps.py` has nine tests, including a pair that differ only by
the presence of the hole.

**Third: read commands wrote to the database.** Every open replayed the whole
27-table schema in a `BEGIN IMMEDIATE`, so `lele list` created a file if
absent, took a write lock and grew the file by a page. A read cannot run on a
read-only mount and concurrent processes serialise. An up-to-date database is
now recognised from its committed version and opened with no transaction; read
commands go through `mode=ro`; a missing registry is reported without being
created. Cost per open fell from ~36 ms to ~15 ms, and the test suite from 285 s
to ~215 s.

**Fourth: the verification itself was decaying.** A test failed on arrival
because a fixture date had aged out of its window — the same class of failure
had already been hit and patched once. ~90 call sites read the system clock
directly. There is now one clock (`core/clock.py`), `freeze()` for tests, and
explicit `now=` parameters where a result must be reproducible. This is a
defect class, not a test bug: a suite that depends on today's date reports
failures it did not find and teaches the reader to ignore red.

**Fifth: the gates did not gate.** `ruff` selected four rule families and
reported "All checks passed" over 30 000 lines; `mypy` found nothing because
542 functions had no annotations; and `pyproject.toml` was not valid TOML
(`lint.per-file-ignores` as a bare dotted key with a hyphen — only ruff's
lenient parser accepted it, so `mypy` rejected the file). All three are fixed
and `.tools/check.sh` now runs everything as one gate. Turning on strict typing
immediately found two more real bugs: a fetcher reaching into the HTTP client's
private pacing state (broke when that state changed), and a `Path | None`
division in the cache path.

**Also fixed, each with a test:** a `billion laughs` XML bomb in the UN
sanctions parser, whose two sibling parsers already refused DTDs; row counts
computed from `conn.total_changes`, which is cumulative and so counted every
attempted insert after the first; twelve stray `conn.commit()` calls competing
with the connection's own; a hard-coded absolute registry path in the
cron runner; a cache that could never hold more than 128 series; host pacing
that held one process-wide lock while sleeping; a non-atomic sanctions
download; three blanket `except Exception: continue` blocks that turned a
`TypeError` into a silently short count; and a CLI that reported every failure
as one of five strings with no way to get a traceback. Error output still never
echoes the exception, because an exception can carry a token into a log.

**One incident, recorded in `AUDIT.md` §6.** While refactoring
`fetchers/opensky.py` I wrote the file back without newlines and destroyed it;
it was untracked, so git had no copy. It was recovered from the `.pyc` of the
last successful `compileall` and verified against that compiled original by
disassembly comparison — two functions byte-identical, two differing by exactly
the three instructions of a hoisted `import hashlib`, and the one function
replaced on purpose.

**Verified.** `.tools/check.sh` passes: 640 offline tests with
`ResourceWarning` as an error, `ruff` clean, `mypy` clean over 53 files,
`compileall` clean. Live end-to-end on 2026-09-28: `init` → `fetch fdic` (50
institutions) → `fetch-history 1 BTCUSDT` (799 real daily bars) → `moves` (124
candidates, 100 retained, every one exactly 24.0000 h, 0 gaps, tiers p3/p5/p7/p11
= 100/33/10/2) → `scan` and `causes context`, which report no stored context
rather than inventing one.

**Follow-up: asking about a window, and tracking capital.** The next question
was whether the tool can do the thing it exists for — find records that could
bear on a price move for any given window, and track money flow. It could not,
for three specific reasons: `events`, `episodes` and `volatility-analyze` all
demanded a hand-made `PRICES_JSON` file although bars had been stored for
months; nothing accepted a time range at all; and `fetch-evidence` read the
futures positioning data and wrote a file, so it could be fetched but never
asked about again.

Built: `explain ID --from --to` (the whole window answer, with a coverage block
naming what is missing and the command that fills it), `capital ID --from --to`
(every measured series, each labelled with what the number actually is), and
`store-evidence` (persists futures positioning so it is queryable by window),
on top of six new time-window reads in the registry.

The three properties that make the answer trustworthy, each with tests:
nothing is ever marked as a cause (`is_cause: false` and a relation of only
`coincident` or `precursor`, in all 35 window tests); absence is reported as
absence with a remedy, and a kind never stored appears in
`coverage.never_recorded`, which is different from a kind stored with nothing in
the window; and every number says what it is, with percent change suppressed for
bounded units. Live on 2026-09-28: 818 real futures positioning records stored,
and a real week answered in one call — 3.92% measured, one episode, 30 records,
3 of 7 channels with data, nothing marked as a cause.

What it still cannot do, and building will not fix: historical attribution
across years (the public headline indexes reach back days, not years, and no
licensed archive is wired — a 2021 window returns 0 records and says so rather
than inventing a reason); causation, which is not available from these sources;
and **non-crypto assets end to end**, because `fetch-history` is Binance-only,
so a listed equity has no stored bars and therefore no measured window even
though its SEC filings and holdings are stored and reported. That last one is
the obvious next build.

**Precise next task (user-directed choice, superseded by the handoff above).**
`AUDIT.md` §4 lists the seven open items. The highest-value next steps, in order:
(1) prune `price_bars` and `move_events`, which grow without bound; (2) build
provider health monitoring on the hashes `ingest_runs` already records, so a
source that starts returning garbage is noticed without a human reading a
report; (3) finish annotating the historical modules, which the gate now prevents
from growing but does not repay; (4) re-run the M04/M05 pre-move enrichment on
the repaired detector, because those negative results were produced by code that
mis-measured windows and should be re-established on the corrected one. Note
that §4 was where those seven items were supposed to live; they are now in §7,
which had been an empty heading.

## Current handoff — tiered moves and pre-move cause scan (user-directed; resume here)

**M01 implemented this session (user-directed).** The user asked for a tool that finds all big and medium price moves from history, reports how many candidate reasons each move had, and then detects the same pattern in current time. That mechanism now exists end to end and runs live. Nothing about it is committed; the working tree was already dirty before this session and was left that way.

**M02 completed this session (user-directed continuation).** The user asked what was happening in the world before a move, specifically whether money moved, whether an entity changed policy, and whether the move was sentiment or manipulation. Two of those are now backed by measurements rather than headline keywords, and the main policy source was unblocked.

- `fetchers/crypto_context.py` adds two market-wide series. `fetch-sentiment` stores the public daily crypto fear and greed index as `social_sentiment` (composite, so `basis=estimated`, available from the following UTC day). `fetch-stablecoins` stores daily aggregate stablecoin supply as `stablecoin_supply`, summing the provider's per-peg-currency objects and keeping the USD-pegged part and peg count separately. Live: 900 sentiment days, 2500 supply days, newest supply $313.2bn of which $311.6bn is USD-pegged across 28 pegs.
- `signals.stored` now reads two scopes: rows bound to the instrument, and market-wide rows with no instrument key restricted to a fixed kind whitelist (`GLOBAL_CONTEXT_KINDS`). A routed flow between two named organisations is therefore never presented as context for an unrelated instrument, which is asserted by a test. Rows inside the window by occurrence but recorded as available after it are now reported **by kind** with a note, instead of vanishing: a current reading shows `excluded_available_after_window_by_kind`, and that is why a freshly ingested record first appears in a later scan.
- Fixed a real pre-existing bug: `fetch-political` aborted on the first verbose Federal Register document because the observation evidence field is bounded text and those records carry long abstracts, nested agency objects and two URLs. Evidence is now built bounded, reduced in a fixed order, and finally reduced to durable identity, naming what it dropped in `bounded_fields.dropped`; the document URL is no longer duplicated because `source_url` already carries it. This had made the main `policy_decision` source unable to store real records. Live: 60 documents stored, none skipped.
- `cot_positioning` and `cot_open_interest` are now mapped to `market_structure`; they had no family, so instrument-bound CFTC rows were being dropped.
- A current reading over 72 hours now shows 2 `money_movement` (stablecoin supply), 60 `policy_decision` (Federal Register) and 2 `sentiment_emotion` (fear and greed) market-wide signals, alongside 7 `market_structure` leverage signals from Binance futures.

**Tests:** 564 pass. New `tests/test_crypto_context.py` holds 17 offline tests: index parsing and skipping, per-peg summation, absent mint/bridge objects, empty-series refusal, request validation, the market-wide whitelist rejecting a routed flow, instrument rows coexisting with market-wide rows, every global kind mapping to a family, and four Federal Register evidence-bounding tests. `ruff`, `mypy` and `compileall` are clean.

**M03 completed this session (user-directed continuation).** The two gaps named last session are now closed: a second headline source, and instrument-bound money movement.

- `fetch-market-activity` binds a second public provider's daily series to the selected entity and stores the day-on-day market capitalisation change as a new `market_activity` kind in the `money_movement` family, with level, close and volume in evidence. Live 179 daily observations; on 2026-09-24 the market capitalisation fell $36.1bn, -2.09%, in one day. This is the closest public proxy for capital entering or leaving this specific asset, and the report states plainly that it rises with price as well as demand and cannot separate buying from selling, new capital from rotation, or spot from derivative notional. It doubles as a second independent price source.
- `fetch-news-feed` reads a public multi-publisher news feed and plugs into the same signal detector as a drop-in alternative, keeping the publisher name and trimming the publisher suffix such a feed appends to each title, which the keyword index does not provide. A document type or entity declaration is refused before parsing. Live 75 observations across named publishers, including coverage at the current hour that the keyword index could not serve.
- `--news-source {gdelt,google_news}` selects the provider on `scan` and `causes attribute`. Live with the feed provider all three 24-hour tiles succeeded where the keyword index had refused two of three, so the news channel went from a partial read to `status: ok`. The two providers differ in reach: the index serves a longer window but throttles and caps per request, the feed serves less and returns fewer items when quiet.
- `HTTPClient.get_text` was added so a non-JSON document shares the existing host pacing and byte ceiling rather than opening a second, unpaced request path.
- A current 72-hour reading now returns 63 signals across all seven families with 8 `money_movement` (including the market capitalisation change and stablecoin supply), 1 `policy_decision`, 1 `security_incident`, 40 `sentiment_emotion` and 8 `market_structure`.

**Tests:** 575 pass. New `tests/test_market_activity.py` holds 11 offline tests: feed item parsing with publisher and trimmed title, window and malformed-item handling, document type declaration refusal, malformed XML refusal, request validation, exact capital-change arithmetic for a rise and a fall, series shortening when points lack a market cap, empty-series refusal, and kind/family registration. `ruff`, `mypy` and `compileall` clean.

**M04 completed this session (user-directed continuation).** The multiple-testing correction is now in, and the result is a negative finding rather than a headline number.

- `causes context` is a second profile that reads **stored** conditions instead of headlines, so it is not limited by provider reach: money movement, policy actions, sentiment readings and macro releases a wired source already recorded. It reports enrichment, a seeded permutation p-value and a **Benjamini-Hochberg adjusted p-value** at alpha 0.05. `adjusted_p` is the value to quote, `significant_after_correction` is the only reportable flag, and `inference_status` withholds a verdict below 8 move and 8 control windows. A one-window group yields no p-value because it has no permutation variance.
- Three anti-confound rules, each asserted by a test, because ignoring any one of them manufactures a finding: windows are kept only when the stored channel has coverage, applied identically to moves and controls; each condition is tested over its own coverage, so a 180-day series is never compared against a year of history where it could not have existed; and controls are spread across the whole span rather than clustering in the oldest part.
- **Two real bugs were found and fixed by this work.** Control windows were stride-sampled and stopped at the first N candidates, which placed every control in the oldest part of the series and removed the most recent period from the comparison; they are now thinned evenly after collecting every eligible candidate. And the headline attribution's servability check used the keyword index's 90-day reach regardless of the selected provider, so with the feed selected it requested windows the feed can never serve and reported them as quiet; reach now follows the provider.
- **Measured provider reach, recorded because it bounds everything downstream.** The multi-publisher feed serves roughly the last two to four days for a 24-hour window and returns nothing at seven days: 37 articles at 6 hours back, 13 at 24 hours, 6 at 48, 4 at 96, 0 at 168. The keyword index was unreachable for the whole of this session (429 and network timeouts on every probe). Consequence: of 113 stored move windows, exactly 1 is inside the feed's declared reach and it returned no articles, so **a historical headline sample is not obtainable today**.
- **The negative result.** On the stored 4-hour series every available condition reads move_share 1.0 against control_share 1.0 with p=1.0, adjusted p=1.0. At a 24-hour pre-window the daily-cadence context series are saturated: a daily observation falls inside every window, so presence carries no information. At 6 and 12 hours most windows are empty, so no usable sample exists. An earlier run of this work appeared to find market_activity enriched 4-of-11 moves against 0-of-40 controls at adjusted p=0.006; that was entirely the coverage confound and it disappeared once controls were matched to each condition's own coverage. The current free context sources have **no discriminative power** for this question at any window length, and the tool now proves it with a proper test rather than asserting it.

**Tests:** 589 pass. New `tests/test_enrichment.py` holds 14 offline tests: correction behaviour on a strong signal, a borderline value corrected away only in a family, monotonicity and the never-below-raw property over 200 random trials, missing p-values, permutation reproducibility, a real difference detected and equality not, a one-window group refused, control spread across the span, controls never overlapping a move window, zero controls, a saturated condition producing no signal, the short-series coverage confound, and both insufficient-window and below-minimum verdicts. `ruff`, `mypy` and `compileall` clean.

**M05 completed this session (user-directed).** The user made the decisive point: big moves are too rare to test, so the targets should be 3%, 5%, 7% and 11%, where small volatility instances are easy to find. They are right, and acting on it exposed a second flaw in my own test design.

- The detector now takes a **ladder** of absolute percentage thresholds, `--thresholds 3 5 7 11`, recorded **cumulatively**: a 12% move is stored at `p3`, `p5`, `p7` and `p11`, so every rung is a cumulative sample of all moves at or above it. Schema v16 replaces the `medium`/`big` vocabulary with percent labels under `CHECK(tier LIKE 'p%')`, validated by `registry.move_tier_percent`, and a lossless migration rebuilds `move_events` and `move_causes` in dependency order so existing rows and their causes survive. Live: 856 candidates, 628 retained moves, 1169 tier rows — p3 628, p5 323, p7 173, p11 45.
- **The presence-only test was the wrong test.** It saturated because a daily-cadence series falls inside every 24-hour window, so presence was 1.0 in both groups and every p-value was 1.0. The contrast now runs on the **difference in measured means** with a two-sided permutation test, which is where the information in these series actually is.
- **A time trend is a confound, and is now refused as one.** Aggregate stablecoin supply rises by an order of magnitude over the sample, so a 2021 window and a 2026 window are not comparable levels. The earliest and latest thirds of the pooled windows are compared, and a drift of at least one standard deviation marks the comparison confounded, withholding significance regardless of p-value. This is what stops a large significant-looking difference from being reported as a result.
- **The live result, using the 3% threshold the user asked for: 433 move windows against 99 controls, and still nothing survives.** Stablecoin supply reads $31.5bn lower before moves than in controls at p3 (p=0.003, adjusted 0.009), growing to $52.4bn lower at p7 (p=0.0005, adjusted 0.0015) — a monotone pattern in move size, which is the shape a real effect would have. It drifts 2.1 standard deviations across the compared windows, so it is confounded with when the windows sit and is reported as such. Reading it as "capital was pulled out before big moves" would be reading a calendar trend as a mechanism. The 11% rung gave only 20 move windows, confirming the user's diagnosis that high thresholds cannot support a test.

**Tests:** 598 pass. `tests/test_enrichment.py` grew to 23 tests including tier-label round-tripping, rejection of the old labels, a drifting level being refused as a finding while a flat level is not flagged, a measured difference where there is no drift, a one-window group yielding no test, a move recorded at exactly the thresholds it clears, and lower thresholds never yielding fewer instances. `ruff`, `mypy` and `compileall` clean.

**Precise next task (user-directed):** extend M01 through M05, in this order.
1. **Store context as stationary quantities, not levels.** The trend guard correctly blocks the one promising signal, and the fix is structural: a daily change, a rate, a ratio, or a value expressed as a z-score against that asset's own trailing history is stationary by construction and testable. `market_activity` already stores a change and is the better-shaped series, but it only covers 180 days, leaving 13 usable windows. Extending it over years, and re-expressing sentiment and supply as self-referenced z-scores, is the highest-value next step and needs no new provider.
1. **The bottleneck is still source cadence, not statistics.** The sentiment and supply series are daily-only, which saturates a 24-hour window. Before any further enrichment work, decide whether an hourly or finer context source is obtainable; none of the free providers reached so far offers one, so this may need a key, a paid feed, or an on-chain collector. Without it the enrichment question cannot be answered from these sources at all.
2. **True exchange netflow remains unavailable.** Both public routes need a key: CryptoCompare's exchange netflow answers 401, DefiLlama's CEX dataset paths answer 404. Until one is available "did someone pull their money out of an exchange" is approximated by market capitalisation change plus aggregate stablecoin supply, and the tool says so.
3. **Cross-source price reconciliation.** Two independent daily price series are stored and nothing compares them. A bounded agreement check would turn "two sources" into a verified cross-check and surface any provider that silently changes its sampling.
4. **Multi-asset.** Verified on `BTCUSDT` only. `PAXGUSDT` is the obvious second instrument and all three context series are crypto-wide so they carry over; equities and futures still have no multi-year path in this tool.

**Status (M01 — implemented, live-verified 2026-09-26):** schema v15 adds four tables (`price_bars`, `move_events`, `move_causes`, `cause_scans`) with accessors and an additive migration for the interim `move_causes` shape. `fetchers/history.py` walks Binance spot klines backward in bounded 1000-bar pages for `1d`/`4h`/`1h` and stores bars idempotently; live run stored 3326 daily bars spanning 2017-08-17 to 2026-09-25 (1 malformed row, 1 unclosed candle skipped), which removes the 35-day history ceiling that previously blocked any percentile claim. `analysis/moves.py` turns bars into tiered moves: candidates are ranked by absolute change and retained only when their windows share no bar, and each retained move is scored against the trailing baseline of returns strictly before its own window, never including itself. Live result: 392 candidates became 323 retained daily 24-hour moves (35 big up, 29 big down, 259 medium, largest 39.5%), and the largest retained moves are the 2020-03 COVID crash, the 2017-12 peak, the 2021-02 rally, the 2021-05 China-crackdown selloff and the 2022-02 Ukraine rally.

`analysis/signals.py` is the shared detector the user asked for first: one window definition, one schema, four channels (`stored` observations, `news` headlines, futures `structure`, `market` price and volume) mapped to seven cause families, bucketed into nested 24/48/72-hour horizons. `analysis/causes.py` holds the cause lexicon, the sentiment scorer, `attribute`, `profile` with a seeded permutation test, and `scan`. `scan` on the live 4h series returned 84 signals across all seven families in a 72-hour window (7 `money_movement`, 2 `policy_decision`, 6 `security_incident`, 59 `sentiment_emotion`, 11 `market_structure`), while GDELT refused two of three tiles after three attempts each; that refusal is reported per tile rather than smoothed over.

**Three pre-existing red tests fixed.** `prospective.forecast` passed `instrument["entity_key"]` into `volatility_anomaly_v1`, which requires a positive integer id, so the pre-registered `volatility_anomaly_v1` path could not complete a forecast; the two call sites and their tests now pass the id. `tests/test_rag.py` hardcoded a 2026-09-19 event date inside a 48-hour window, so it began failing as soon as that date aged out; it is anchored to today. `tests/test_prices.py` compared raw database bytes to prove read-only commands change nothing, but those commands reopen the registry read-write and replay the schema statements, so the file can grow by a page; the assertion now compares the content of every registry table, which is stricter about data and immune to WAL timing.

**Tests:** 545 pass (516 before, 7 failing). New file `tests/test_price_causes.py` holds 31 offline tests with no network: bar parsing and idempotency, non-overlapping selection and cooldown, baseline ordering, insufficient-history handling, classifier determinism and multi-label behaviour, point-in-time exclusion of not-yet-available observations, volume z-score including the zero-variance case, provider-failure reporting, horizon nesting, validation, and the permutation test's reproducibility. Checks run clean: `unittest discover`, `ruff check .`, `mypy`, `compileall`.

**What M01 does not do.** It establishes no cause. A category is a keyword match on a headline published before a move; a leverage reading is consistent with manipulation but identifies no manipulator; a funding rate or an ETF story is not proof of where money went. Nothing forecasts price, and the 90% standing objective is untouched by this work.

## Current handoff — decision/flow-driven indicators plan (user-directed; resume here)

**Next session (user-directed):** continue **P09 Phase 5** (run actual multi-session real-time evaluation over days/weeks to accumulate prospective samples). **R02** implemented (schema v14 `observations` table), **P06** has five live-verified SEC extractors, **P07** complete with three live-verified government-money extractors. **FEC blocked** on API key. **P09 Phase 4 completed**: Real-time prospective evaluation framework tested with 5 unit tests. **O01 Phase 1 completed**: SEC Form ADV fetchers live-verified. All 516 tests pass.

**P08 live verification results:**
- `fetch-opensky` ✅ WORKS — 5,855 records → 5 `flight_event` observations stored (no API key needed)
- `fetch-bls` ✅ WORKS — 5 `labor_metric` observations stored (no API key needed; registration key optional for higher limits)
- `fetch-comtrade` ❌ ENDPOINT CHANGED — UN Comtrade API v1 preview returns 404; new API requires subscription at `comtradeapi.un.org`
- `fetch-census-trade` ❌ ENDPOINT CHANGED — US Census `timeseries/intltrade` endpoint returns 404; may need new API path or key
- `fetch-eia` 🔑 REQUIRES API KEY — Free key from `https://www.eia.gov/opendata/register.php`; pass via `--api-key`

**To make P08 fetchers work:**
- `fetch-eia`: Get free key at https://www.eia.gov/opendata/register.php → `lele fetch-eia 1 PET.RWTC.D NG.RNGWHHD.D --api-key YOUR_KEY`
- `fetch-comtrade`: Check `https://comtrade.un.org/data/` for new API access; free tier may need registration
- `fetch-census-trade`: Check `https://api.census.gov/data.html` for current trade endpoints; may need API key

**P09 Indicator Construction (Phase 1 this session):**
- `lele indicators list` — shows all 6 frozen specs with inputs/window/normalization/direction/expected_sign
- `lele indicators spec --indicator insider_net_buy_v1` — shows one spec detail
- `lele indicators compute --entity-id 2 --indicator insider_net_buy_v1` — computes indicator for entity
- `lele indicators project --entity-id 2` — projects indicators into world-state evidence format

**P09 Phase 2 (new this session):** Pre-register and forecast with constructed indicators through prospective harness
- Added `constructed_indicator_v1` and `constructed_indicator_weighted_v1` methods to prospective module
- Both methods compute indicators on-the-fly from wired observations (P06-P08) at forecast time
- Pre-registered weights (not fitted): insider_net_buy=3, insider_cluster=2, filing_momentum=1, macro_release=2, labor_stress=1, flight_activity=1
- Demo: `constructed_indicator_v1` → direction "up" (macro_release + flight_activity); `constructed_indicator_weighted_v1` → direction "up" (weighted composite)
- Forecast projects ±10 bps from last close based on indicator direction
- Both methods work without evidence file (use registry observations directly)
- Batch test: 200 forecasts over 3.5 days of BTCUSDT 5-min data, all settled, hash chain valid

**P09 Phase 3 (completed this session):** Real-time prospective evaluation framework created
- Added `lele.scripts.realtime_prospective` module for periodic cron-based evaluation
- Supports 4 actions: `preregister`, `forecast`, `settle`, `score`
- Forecast action issues genuine prospective forecasts (target_time = now + 5min, status="prospective")
- Settle action resolves forecasts when target time has passed
- Score action evaluates ledger performance
- Designed for cron: `*/5 * * * * forecast`, `1,6,11,... * * * * settle`, `0 * * * * score`
- Demo: issued 1 prospective forecast for BTCUSDT (target 08:55, issued 08:52:40, direction="up")

**O01 Phase 1 (new this session):** SEC Form ADV (Investment Adviser Public Disclosure) fetchers
- Added `lele/fetchers/formadv.py` with two new CLI commands:
  - `fetch-formadv` — investment adviser firm registrations from Form ADV
  - `fetch-formadv-individual` — investment adviser individual (representative) registrations
- Both use SEC IAPD API (api.adviserinfo.sec.gov), free public API
- New observation kind: `investment_adviser` (added to events.KINDS)
- Added `SEC_IAPD` to SOURCES allowlist in constants.py
- Live verified: `fetch-formadv --query blackrock --limit 5` → 20 `investment_adviser` observations
- Live verified: `fetch-formadv-individual --query blackrock --limit 5` → 11 `investment_adviser` observations
- Both commands support `--query`, `--limit`, `--start` (pagination) parameters
- Added to CLI command catalog and test catalog

**P09 Phase 4 (completed this session):** Real-time prospective evaluation framework tested
- Added `lele.scripts.realtime_prospective` module for periodic cron-based evaluation
- Supports 4 actions: `preregister`, `forecast`, `settle`, `score`
- Forecast action issues genuine prospective forecasts (target_time = now + 5min, status="prospective")
- Settle action resolves forecasts when target time has passed
- Score action evaluates ledger performance
- Designed for cron: `*/5 * * * * forecast`, `1,6,11,... * * * * settle`, `0 * * * * score`
- Demo: issued 1 prospective forecast for BTCUSDT (target 10:35, issued 10:34:11, direction="up")
- Added `tests/test_realtime_prospective.py` with 5 unit tests covering preregister, forecast, settle, score
- All 516 tests pass (511 original + 5 new)

**S01 Enhancement (new this session):** OFAC SDN enforcement narratives
- Enhanced OFAC SDN parser to extract all 12 CSV columns (was only 4)
- New fields captured: title, call_sign, vess_type, tonnage, grt, vess_flag, vess_owner, remarks
- The `remarks` field often contains enforcement narrative details (legal basis, case numbers, court info)
- All fields preserved in sanctions listing evidence for provenance
- Updated evidence string to include all available fields
- This fulfills "enforcement narratives" part of S01 (enforcement-action narratives)

**P09 Phase 5 (next):** Run actual multi-session real-time evaluation over days/weeks to accumulate prospective samples. Need sequential issuance with wall-clock time separation for genuine prospective status. Current: 1 prospective sample for bitcoin; need min_settled_per_class=1 per required class (bitcoin, gold, oil, stock).
- `lele indicators project --entity-id 2` — projects all computed indicators into world-state evidence format for prospective validation
- Indicators defined BEFORE outcomes with frozen specs; each has defined inputs, window, normalization, direction convention, expected sign
- Demo: `macro_release_momentum_v1` → direction "positive" (TGA balance increasing); `flight_activity_v1` → direction "positive" (13 airborne flights)

**Status (R02 decision/flow bridge — implemented):** Schema v12 adds an `observations` table (source, external_id, kind, description, actor/counterparty/instrument keys, action, reason/reason_basis, amount/unit/currency/basis, occurred/observed/available times, source_url, evidence; unique `(source, external_id)` with idempotent upsert) and `registry.add_observation`/`list_observations`/`observations_for_instrument`. `analysis/observations.py` imports a user-supplied JSON (`observations import PATH`), validates every row against the frozen evidence taxonomy and decision/route rules, resolves a nonempty instrument key to an existing entity, and preserves unknown actor, counterparty, amount and motive as empty rather than inferring them. Only `decision` observations may carry an action/reason, and only the routed flow kinds require origin/destination plus a positive amount and currency; actor/counterparty keys are retained for other kinds but the evidence projection only carries actor/action/reason for decisions and origin/destination for routed flows. `observations evidence ID --output PATH` projects the explicitly instrument-bound rows into the exact world-state evidence format and re-validates it through `events._evidence`, so it feeds `worldstate`/`prospective` unchanged (offline test proves the projected file builds world-state features). `observations list [ID] [--limit]` reads back. 8 offline tests plus CLI coverage.

**Status (P06 SEC Form 4 — implemented, live-verified):** `fetch-form4 ID [--limit 1..200]` reads one existing SEC issuer's recent submissions (`data.sec.gov`), selects original Form 4 filings, and fetches each raw ownership XML from the newly allowlisted `https://www.sec.gov/Archives/edgar/data` prefix (the submissions `primaryDocument` is the XSL-rendered HTML, so the raw `form4.xml` basename is used instead). The parser rejects DTDs/entities and non-Form-4 roots, extracts issuer/reporting-owner identity, relationship flags and every non-derivative/derivative transaction, and stores one `insider_trade` observation per transaction: signed shares (acquired `A` positive, disposed `D` negative), `unit=shares`, reporting owner `sec-owner:<CIK>`, transaction date as `occurred_at`, filing acceptance time as `observed_at`/`available_at`, the XML URL and JSON evidence (transaction code, price per share, shares owned after, direct/indirect ownership, officer/director flags). It records an ingest run, infers no counterparty or motive, creates no person entity, reads only recent submissions and ignores Form 4/A amendments. Live 2026-09-19 (Apple `cik:0000320193`, limit 3): 3 filings → 6 observations, 0 skipped. 6 offline tests.

**Status (P06 SEC Form 13F — implemented, live-verified):** A new `holding` kind was added to the evidence taxonomy (non-directional position snapshot). `fetch-13f ID [--limit 1..40]` reads one existing SEC investment manager's recent submissions (`data.sec.gov`), selects original `13F-HR` filings, and for each reads the filing `index.json` under the allowlisted `https://www.sec.gov/Archives/edgar/data` prefix to pick the largest non-cover `.xml` information table. `parse_thirteenf` rejects DTDs/entities and non-`informationTable` roots, then emits one `holding` observation per position: manager bound as actor (`cik:<CIK>`), issuer name/title/CUSIP only (no issuer instrument inferred), filer-reported USD `<value>` as `unit=currency`, shares/share type, investment discretion, other manager, voting authority and put/call in JSON evidence, report period as `occurred_at` and filing acceptance time as `observed_at`/`available_at`. The cover `primary_doc.xml` supplies the report calendar quarter; a missing/unparseable cover degrades to an empty period with a warning. It records an ingest run, applies no 13F-HR/A amendment, sums nothing across managers or quarters, and asserts no ownership-delta or motive. Live 2026-09-19 (Berkshire Hathaway `cik:0001067983`, limit 1): 1 filing → 89 holdings, 0 skipped. 7 offline tests.

**Status (P06 SEC material events — implemented, live-verified):** `fetch-material ID [--limit 1..200]` reads one existing SEC issuer's recent submissions and filters original `8-K`, `8-K/A`, `S-1`, `S-1/A`, `DEF 14A`, `DEFA14A` and any `424B*` filing. It stores one measurement-optional `filing_event` observation per filing (instrument and actor bound to the issuer), with form, filing/report dates, acceptance-time `observed_at`/`available_at`, document URL, primary-document description and 8-K item numbers in JSON evidence; the report date is `occurred_at` when present, otherwise the filing date. Only recent submissions metadata is read; document contents/exhibits are never downloaded or interpreted, amendments stay separate events, malformed rows are skipped and counted, and no money amount or motive is inferred. Live 2026-09-19 (Apple `cik:0000320193`, limit 5): 5 material filings → 5 `filing_event` observations, 0 malformed. A projected `filing_event` file re-validates through `events._evidence` and builds world-state features. 6 offline tests.

**Status (P06 SEC Form D — implemented, live-verified):** `fetch-formd ID [--limit 1..200]` reads one existing SEC issuer's recent submissions, selects original `D`/`D/A` filings, and parses the raw Form D cover XML (`primary_doc.xml` basename from the allowlisted `www.sec.gov/Archives/edgar/data` prefix). `parse_formd` rejects DTDs/entities and non-Form-D roots, then stores one measurement-optional `filing_event` observation per filing bound to the issuer, carrying the reported `totalAmountSold` (USD) as the measurement when present, plus the total offering/remaining amounts, industry group, revenue range, federal exemptions, date of first sale, investor count, sales commissions, finders fees, gross proceeds used and up to 50 related persons with relationships in JSON evidence; `occurred_at` is the first-sale date when present (else the signature date), and `observed_at`/`available_at` are the filing acceptance time. Exhibits are not parsed, `D/A` amendments are separate events that must not be summed with the original, and investors, counterparties and motives are never inferred. Live 2026-09-19 (Space Exploration Technologies `cik:0001181412`, limit 2): 2 filings → 2 observations with amounts (D/A 1,724,965,480 USD; D 249,999,890 USD), 0 malformed. A projected Form D file re-validates through `events._evidence` and builds world-state features. 6 offline tests.

**Status (P06 SEC N-PORT — implemented, live-verified):** `fetch-nport ID [--limit 1..20]` reads one existing SEC fund's recent submissions, selects public `NPORT-P` filings, and parses the raw cover XML (`primary_doc.xml` basename from the allowlisted `www.sec.gov/Archives/edgar/data` prefix, namespaced). `parse_nport` rejects DTDs/entities and non-N-PORT roots, reads the registrant/series identity, report period and fund totals (total assets/liabilities/net assets), then stores one `holding` observation per `invstOrSec` security bound to the fund as actor: name/title/LEI/CUSIP/ISIN, balance and units, currency, reported `valUSD` as the measurement, percentage of net assets, payoff profile, asset/issuer category, investment country and fair-value level in the holding evidence. `occurred_at` is the report period date and `observed_at`/`available_at` is the filing acceptance time; holdings are capped at 5,000 per filing. Confidential `NPORT-NP` data and `NPORT-P/A` amendments are not applied, no issuer instrument is inferred and positions are never summed. Live 2026-09-19 (SPDR S&P 500 ETF Trust `cik:0000884394`, limit 1): 1 filing → 504 holdings, 0 warnings. 6 offline tests.

**Status (P07 USAspending federal awards — implemented, live-verified):** `fetch-awards ID --recipient-uei UEI [UEI ...] [--search TEXT] [--limit 1..100] [--start YYYY-MM-DD] [--end YYYY-MM-DD]` is the first government-money-layer extractor. USAspending ignores exact recipient-id/UEI filters on `POST /api/v2/search/spending_by_award/` (verified live: unknown filter keys are silently ignored), so the command uses a bounded recipient-name `--search` (default: the entity name) only to narrow candidates and keeps strictly the awards whose per-award `Recipient UEI` exactly matches the explicit UEI set. Contracts (`A`–`D`) and assistance (`02`–`05`) must be queried separately, so two bounded pages are requested and deduplicated by award identifier. Each positive award becomes a routed `fund_flow` observation: origin `usaspending:agency:<slug>` (a textual agency key), destination and instrument bound to the selected registry entity, amount as reported USD, `occurred_at` the award start date, `observed_at`/`available_at` the local retrieval time, and evidence carrying award id, kind (contract/grant), agency, recipient name/UEI, matched UEIs and search text. Negative/zero/missing amounts are skipped, no identity is inferred from a name, and amounts are obligations rather than settled transfers. Live 2026-09-19 (Lockheed Martin `cik:0000936468`, UEIs `G4KDGE4JFFK7` and `FYHNA5WC8XD7`, search `LOCKHEED MARTIN`, 2025): 6 candidates, 3 exact-UEI awards stored, 3 non-matching excluded, 0 skipped. 4 offline tests.

**Status (P07 Senate LDA lobbying — implemented, live-verified):** `fetch-lobbying ID (--client-id N | --registrant-id N) [--filing-year YYYY] [--limit 1..100]` reads public Senate Lobbying Disclosure Act quarterly activity filings (`Q1`–`Q4`, 2008..current+1) for one explicit LDA client or registrant id (exactly one required; identity is never inferred from a name) and stores each positive registrant-reported `income` as a routed `fund_flow` observation bound to the selected registry entity. The bound side becomes the entity endpoint and the other party a textual key (`lda:client:<id>` or `lda:registrant:<id>`); the flow direction is client → registrant, `occurred_at` is the quarter start, `observed_at`/`available_at` is the LDA posting time (normalized to UTC whole seconds), and evidence carries both parties' ids/names, filing type/period/year, income, expenses and issue codes. Endpoint: the canonical `https://lda.gov/api/v1/filings` (the `lda.senate.gov` host 301-redirects there; using the destination directly avoids the client's redirect refusal). Amounts are filed lobbying income, not settled transfers, and are never summed. Live 2026-09-19 (client id 58116, 2024): 3 quarterly filings, 3 routed `fund_flow` observations. 4 offline tests.

**Status (P07 Treasury Fiscal Data — implemented, live-verified):** `fetch-treasury` reads the public Treasury Fiscal Data Daily Treasury Statement operating cash balance (`api.fiscaldata.treasury.gov`, no API key required) and stores each positive balance as a `macro_release` observation (a non-routed event kind that requires no counterparty identity). Bounded by date range and limit; zero/negative balances are skipped. Amounts are reported Treasury General Account balances, not settled cash transfers. Live 2026-09-19: verified endpoint returns daily TGA opening balances; storage creates `macro_release` observations with full provenance. 6 offline tests.

**Status (P07 FEC — blocked):** The Federal Election Commission API requires a free API key that has not been obtained. Recorded as blocked rather than guessing a contact email or bypassing the access requirement.

**Status (P09 RAG CLI — implemented):** Added `lele rag` command with five subcommands exposing the `analysis/rag.py` pipeline:
- `store-events PATH [--source STR]` imports a user-supplied JSON list of events into `event_store` with full provenance (source, event_type, actor, occurrence/observation times, source URL, evidence). Validates required fields (event_type, title, occurred_at) and skips malformed entries. Idempotent on `(source, event_type, external_id)`.
- `build-graph [--window-hours 1..168] [--min-strength 0.0..1.0]` creates `event_relationships` between events within the time window. Relationship strength combines actor match (0.9), type match (0.5), or default (0.2). Inferred relationships: `triggers` for market_shock, `precedes` for regulatory/political, `related_to` otherwise.
- `attribute-flows [--window-hours 1..168]` links `money_flows` to causal events by actor key and temporal proximity (attribution score 0.9 within 1 hour, 0.6 within 6 hours, 0.3 within 24 hours, 0.1 within 48 hours). Stores `direct` or `indirect` attribution in `money_flow_attribution`.
- `detect-anomalies --instrument-key STR [--anomaly-type STR] [--lookback-days 1..365] [--threshold-sigma 0.1..10.0]` finds price volatility anomalies in `observations` (source=price) using z-score against rolling mean/std. Stores anomalies in `price_anomalies` with severity (critical ≥ 3σ, high ≥ 2.5σ, moderate ≥ 2σ).
- `indicator [--instrument-key STR] [--limit 1..100]` computes `event_indicator_v1`: weighted composite of event scores (by severity × confidence), attributed flow scores (by amount × flow_type weight), and anomaly scores (by z-score × severity weight). Returns score 0–10 with breakdown.

All commands use bounded parameters, preserve unknowns, enforce point-in-time discipline (`available_at <= as_of`), and record provenance. 7 offline tests in `tests/test_rag.py`; CLI validation and dispatch coverage added to `tests/test_cli.py`.

**Previous direction:** continue; run the real objective — a pre-registered prospective evaluation of the weighted evidence method — and broaden P04 coverage.

**Status (prospective):** Genuine prospective `evidence_score_10bps_v2` evaluation against live Binance data. Artifacts are disposable in `/tmp/opencode/p06-live/` (registry, prices1-5, evidence1-4, reports, ledger, scores); nothing is committed.
- Pre-registered run `17bacade…`: method `evidence_score_10bps_v2`, required class bitcoin, `target_metric=direction`, `tolerance_bps=100`, `target_rate=0.98`, `min_settled_per_class=1`, `observation_window_seconds=300`.
- Six settled prospective points (as_of → target, prediction → actual, bps): 10:40→10:45 down→down **hit** 6.51; 10:45→10:50 down→up **miss** 25.34; 11:10→11:15 down→down **hit** 0.79; 11:15→11:20 down→up **miss** 11.15; 11:50→11:55 up→up **hit** 2.81; 11:55→12:00 up→down **miss** 14.73.
- Final score: settled prospective 6, pending 0; direction 3/6 (rate 0.5), tolerance 6/6, exact 0/6, combined 3/6; `objective.status=not_achieved`, `target_met=false`. The append-only hash chain validated on read. After point 1 alone the status was `prospective_target_met_unverified`; every later direction miss returned it to `not_achieved`.
- Note: the weighted direction changed sign across the session (first four `down`, last two `up`) but the hit rate stayed at chance level.
- Exploratory historical cross-check on the 11 evidence-bearing boundaries of the point-1 window (`worldstate`): unweighted agreement 4/11, weighted `kind_weight_recency_buckets_v1` 7/11 — overlapping and in-sample, not prospective.

**Status (P04):** Expanded `COT_MARKETS` to gold (`GOLD-COMEX` → "GOLD - COMMODITY EXCHANGE INC.") and oil (`WTI-NYMEX` → "CRUDE OIL, LIGHT SWEET - NEW YORK MERCANTILE EXCHANGE"), all exact CFTC names verified live; `_cot_observations` now verifies the returned market matches the request (previously unchecked); +1 offline test. These remain measured-only and weekly, so they add context but not a 5-minute direction signal. **FRED is explicitly excluded** (graph CSV unreachable from this environment; JSON API needs a key), recorded in README and the plan rather than left as a silent gap.

**Status (second directional class — tokenized gold):** To advance beyond a bitcoin-only signal, added `PAXGUSDT` (Pax Gold, a gold-backed token) as a second Binance instrument: `prices.INSTRUMENTS`/`PRICE_ENDPOINTS` (asset_class `gold`, unit `PAXG token`, spot), `evidence.SOURCES`/`request_window` now symbol-aware, and CLI epilogs updated. `PAXGUSDT` futures order flow, open interest, funding and ratios were verified live. This is a **tokenized-gold proxy, not COMEX GC=F**; the instrument's symbol/venue/unit are recorded in every price file and ledger record so it is auditable. New pre-registered run for class `gold` (`/tmp/opencode/p07-gold/`, disposable): one prospective point 12:25→12:30Z, weighted `down` vs actual `up` → direction miss, 11.79 bps, tolerance hit; `objective.status=not_achieved`. Two of four required classes (bitcoin, tokenized gold) now have a directional signal; **oil and stock still have no verified free directional source** and remain blocked.

**Status (P05 non-episode controls):** `episodes` now profiles non-episode control points — price points outside every leg span and outside each leg's preceding horizon, sampled every `--control-stride` points (default 12) — and reports `summary.control_comparison` plus a bounded `controls` list using the same point-in-time `worldstate` rule. New `_aggregate_precursors` helper; CLI `--control-stride 1..10000`. Three new tests. Live 2026-09-18 BTC run (83 h prices, 24 h evidence, 1% threshold, 72 h horizon): 13 legs; with a 72-hour horizon only **1 control was eligible**, so pump (−13), dump (−6) and control (−14) means do not separate anything — an honest null on a tiny sample. Controls are unmatched, can overlap, and no significance test is applied.

**Status (E04 edge validity and refresh):** Bumped the registry schema to v4 with an additive migration; `edges` rows now carry `first_seen_at`, `last_seen_at`, `seen_count`, `status` and `retracted_at`. `registry.add_edge` maintains the lifecycle (preserves first sighting, updates last sighting, increments the count, reactivates on re-observation) and the new `registry.retract_edge` marks a link retracted without deleting its provenance. `sources.edge_fund_managers` gained `refresh=False`; `edges --refresh` re-selects linked funds, retracts manager edges that are missing/conflicting/replaced, and preserves history. CLI output and human table gained a `retracted` column. A new `edge_retry` table (schema v5) records failures/last-attempt per `(fund, managed_by)`; candidate selection orders never-attempted funds first, then by failure count and last attempt, and a success clears the row, so repeatedly failing early funds no longer occupy every batch. `record_edge_failure`/`clear_edge_failure` back this. Offline tests cover lifecycle, retraction idempotence, refresh-retract, manager replacement, retry deprioritization, and schema-constant agreement. The one deliberate invariant change: a failed manager fetch now writes retry bookkeeping (but still no entity, edge or attribute writes), and the affected tests assert exactly that.

**Status (E03 parent traversal):** `edges` gained `--kind parent` with `--level direct|ultimate`; `sources.edge_parents` follows GLEIF `/lei-records/{LEI}/{level}-parent-relationship`, requires the matching `IS_DIRECTLY_CONSOLIDATED_BY`/`IS_ULTIMATELY_CONSOLIDATED_BY` type with `ACTIVE` status and an `LEI` end node, fetches the parent `/lei-records/{parent-LEI}` record, and stores `subsidiary_of`/`ultimate_subsidiary_of` edges with the relationship JSON in `gleif.{level}_parent_relationship`. It distinguishes **no-parent** (HTTP 404), **reported-but-unusable or level-mismatched** (wrong type, non-ACTIVE, or no published parent LEI → level-specific `gleif.{level}_parent_exception` attribute, no edge), and **missing** (unavailable/invalid); refresh/retract/retry and the E04 lifecycle are reused. CLI JSON adds `no_parent`/`exceptions` for this kind. Offline tests cover direct and ultimate link/provenance, no-parent, three exception shapes, level/type mismatch, refresh/retract/replace, validation and invalid local LEI. Live 2026-09-18: Google LLC (`7ZW8QJWVPR4P1J1KQY45`) → ALPHABET INC. at both `direct` and `ultimate`. A new **`tree ID [--depth] [--direction both|parents|subsidiaries] [--max-nodes]`** report walks the stored consolidation edges and lists nodes with level, direction, reaching relation, edge provenance and `parent_status` (`recorded`/`exception`/`unknown`, where unknown is explicitly not "no parent"); it counts skipped cycle edges and reports truncation. The control-vs-consolidation distinction is stated as out of scope rather than claimed.

**Status (D01 durable ingest runs — complete):** `ingest_runs` (schema v7) records each completed `fetch` with source/query/country/indicator/category, start/finish timestamps, fetched/stored/skipped/missing counts, pages, reported total, truncation, canonical request SHA-256, stored-record SHA-256, warnings, coverage, **per-page detail** and a **resumable checkpoint** (`next_offset`/`next_page`). `fetch --resume` continues the latest matching resumable truncated run from its saved page/offset; a partial-page stop is not resumable and no-checkpoint resume errors (SEC unsupported). `runs [--limit]` lists newest first; failed fetches write no run row. Offline tests cover records/bounds, successful/failed fetch, offset- and page-based resume, non-resumable partial pages, and missing/invalid resume.

**Status (D02 filter dimensions):** `_gleif` now stores `gleif.headquarters_country` from `headquartersAddress.country`; `find_entities` gained exact `legal_country` (= `entities.country`), `jurisdiction` (= `iso_jurisdiction`), `headquarters_country` and `source` predicates, and `list`/`export` expose `--legal-country`, `--jurisdiction`, `--hq-country`, `--source` alongside the existing broad `--country`. Regulatory jurisdiction, legal address, headquarters and source are no longer conflated; the exact flags are case-sensitive. Category remains a `fetch`-time GLEIF option. Offline tests at the registry and CLI layers.

**Status (Q01 QA/releases, partial):** `backup PATH [--force]` + `registry.backup_database` write a consistent SQLite online-backup copy (read-only source, `integrity_check`, journal/parent/overwrite protections, entity count + byte size + SHA-256); restore means using the copy as `--db`. Added `version` (app/Python/SQLite/schema), `registry._require_runtime` (Python 3.11+/SQLite 3.35+ with a clear failure), and `tests/test_packaging.py` (pyproject metadata vs constants, package discovery, console script, stdlib-only dependencies); the later `doctor`/platform/release-checklist entries below extend this.

**Status (M01 reviewed entity resolution, partial):** New `entity_aliases` table (schema v8) and `resolve candidates|merge|unmerge|list`. `matching_candidates` emits **unreviewed** pairs by exact LEI (strength 3), normalized name + country (2), or name (1), excluding existing aliases; `merge_entity` records a human-approved reversible alias with self/alias/canonical-with-children protections; `unmerge_entity` removes it; `list_aliases` shows them. Aliases only: existing edges, metrics and filings are not rewritten, and no fuzzy scoring is added. Offline tests at the registry and CLI layers.

**Status (S01 sanctions/enforcement, partial):** New `sanctions_listings`/`sanctions_links` tables (schema v9), `analysis/sanctions.py` and `sanctions import|candidates|link|unlink|listings|links`. Import ingests a **user-supplied** JSON of official listings with the file SHA-256, a required official `source-url` and retrieval time, preserving `basis` (designation/allegation/finding/unknown), `status` (active/delisted) and publication/effective dates; it fetches nothing. `sanctions candidates [ID]` returns exact-normalized-name matches marked `unreviewed_candidate` (name is never identity); `link`/`unlink` record a reversible human-approved association. Offline tests at the registry, parser and CLI layers.

**Status (R01 cross-entity money flow, partial):** New `money_flows` table (schema v10), `analysis/flows.py` and `flows import|list`. Import accepts a **user-supplied** JSON of documented directed flows (`src`, `dst`, closed-set `type`, positive `amount`, `currency`, optional `occurred_at`, required `source_url`, optional `evidence`), requires both counterparties to already exist, records file SHA-256, and never sums across currencies; `flows list [ID]` shows directed flows and `flows summary ID` reports per-currency inflow/outflow/net with a bounded counterparty breakdown, summing only within one currency (no FX conversion or cross-currency netting; `net` is a flow sum, not a position). Balances, ownership, management and filings never create a flow. Offline tests at the registry, parser and CLI layers.

**Status (fuzzy candidate matching, opt-in):** Added `registry.name_tokens`/`fuzzy_name_match` and a `fuzzy` flag to `matching_candidates` and `sanctions_candidates` (and CLI `--fuzzy` on `resolve candidates`/`sanctions candidates`). Fuzzy matching requires one entity id, strips a small legal-suffix set, and marks results `reason=fuzzy_name`, `review=unreviewed_candidate`; it is conservative and never auto-links.

**Status (W01 verified links, partial):** `entity_links` table (schema v11) + `links set|list|remove`: records verified organization-controlled or registry-published links (website/social/registry/other) with a required public HTTPS URL, the verifying `--source-url`, optional label/observed-at/evidence and field-level provenance; nothing is guessed and unknowns stay empty. Offline tests at the registry and CLI layers.

**Status (S01 live OFAC fetch):** `sanctions fetch ofac-sdn` now downloads the official OFAC SDN CSV and stores the listings. OFAC answers `302` to a presigned URL on `wc2h-sls-prod-public-published.s3.us-gov-west-1.amazonaws.com/Published/`, so a **separate** download path (`HTTPClient.download` + `_ValidatedRedirect`) was added that re-validates every redirect target against a single host+path prefix allowlist, limits hops to three, sends no credentials, streams to a temp file and enforces a 32 MiB cap. All other requests still use `_NoRedirect`. `analysis/sanctions.py` parses the 12-column OFAC CSV (`ent_num, SDN_Name, SDN_Type, Program, ...`, `-0-` placeholders cleaned), stores `basis=designation`/`status=active` with the official publication URL and retrieval time, and records an ingest run whose `request_sha256` is the raw-file SHA-256. Live 2026-09-18: 5,695,725 bytes, 19,393 parsed/imported, 1 skipped; no automatic entity matching. A repeat fetch now calls `registry.mark_delisted`: any active listing of the same source absent from the current file becomes `delisted` (row and provenance kept, counted in the report's `delisted`), and a reappearing listing is set back to `active`. A second live fetch reported `delisted=0` with all 19,393 listings still active. `sanctions fetch un-consolidated` was added on the same allowlisted-download path: the UN publication `302`s to its own Azure blob (`unsolprodfiles.blob.core.windows.net/publiclegacyxmlfiles/`, now the second redirect-allowlist entry, 16 MiB cap). `parse_un_consolidated` reads the `CONSOLIDATED_LIST` XML (`INDIVIDUALS`/`ENTITIES`, `FIRST_NAME..FOURTH_NAME`, `REFERENCE_NUMBER`, `UN_LIST_TYPE`, `LISTED_ON`, nationality/address country, `COMMENTS1`), rejects any DTD, and stores designation listings under source `un-consolidated`. Live 2026-09-18: 2,176,957 bytes, 1,011 parsed/imported, 0 skipped, 0 delisted. `sanctions fetch uk-ofsi` was added on the same bounded download path but the UK endpoint serves the file directly (no redirect), so it only needed a `SANCTIONS_ENDPOINTS` allowlist entry (`ofsistorage.blob.core.windows.net/publishlive/2022format/ConList.csv`, 32 MiB cap). `parse_uk_ofsi` skips the leading `Last Updated` line, finds the `Name 6` header, keys one listing per `Group ID`, prefers the `Primary name` row over `AKA`/`FKA`, composes individuals as given names + family name `Name 6`, handles `Entity`/`Ship`, converts `dd/mm/yyyy` `Listed On` to ISO and stores regime/country/evidence. Live 2026-09-18: 16,641,139 bytes, 5,135 parsed/imported, 0 skipped, 0 delisted. Reviewed links now integrate with the graph: `link_sanctions` creates a per-source `authority:sanctions:<source>` entity and a reversible `sanctioned_by` edge (evidence states that a listing match is not guilt and is human-approved); `unlink_sanctions` retracts that edge only when no reviewed link from the same source remains, so `relationships`/`tree` and the reviewed set stay consistent. A fourth live source, `sanctions fetch eu-consolidated`, downloads the EU consolidated XML (~25 MiB) directly from the public FSF extract (no redirect). `parse_eu_consolidated` streams with `ElementTree.iterparse` (rejects DTD), keys one listing per `euReferenceNumber`, prefers a strong English `nameAlias`, maps `subjectType` person/enterprise to Individual/Entity, and stores programme, citizenship/address country and regulation date. Live 2026-09-18: 25,766,640 bytes, 6,234 parsed/imported, 0 skipped. The shared `HTTPClient.download` deadline is now `max(timeout, 120)` seconds so large official files are not cut off at the 20 s request timeout.

**Status (Q01 doctor, partial):** Added `registry.health_check` + CLI `doctor`: a read-only self-check reporting Python/SQLite support, registry presence (never creating the file), `PRAGMA integrity_check`, foreign-key violation count, stored vs expected schema version (`migration_pending` when behind, `issues` when corrupt/ahead) and entity/edge counts. Offline tests at the registry and CLI layers.

**Status (O01 kind vocabulary, partial):** Added `registry.KIND_DEFINITIONS` + `list_kinds` and CLI `kinds`: 16 documented entity kinds, each with a definition and the source that produces it, stating what the kind does not imply (`fund` = GLEIF category, not a fundraiser/VC; `legal_entity` may be a manager/parent; `economy`/`instrument` are not legal entities; OSFI `financial_institution` = unknown classification). No mapping behavior changed. Offline tests at the registry and CLI layers.

**Status (Q01 platform matrix and release process):** `version` and `doctor` now also report `implementation`/`platform`/`machine`; README gained a 7-step release checklist (worklog + plans, direct full-suite/lint/type/compile checks, version/doctor on target Python/SQLite, backup verification, rights re-check with opt-in bounded live tests, explicit-request commits and honest objective status, version-bump sync). Offline tests assert the new fields. Automated cross-OS CI remains.

**Verification:** Full suite **485 tests passed in 92.8s** (P07 Treasury Fiscal Data tests added; mypy 32 source files); ruff, mypy, `compileall -q lele` and `git diff --check` passed. Live 2026-09-19: `fetch-form4` on Apple (`cik:0000320193`, limit 3) → 6 `insider_trade`; `fetch-13f` on Berkshire Hathaway (`cik:0001067983`, limit 1) → 89 `holding`; `fetch-material` on Apple (limit 5) → 5 `filing_event`; `fetch-formd` on SpaceX (`cik:0001181412`, limit 2) → 2 `filing_event` with amounts; `fetch-nport` on SPDR S&P 500 ETF Trust (`cik:0000884394`, limit 1) → 504 `holding`; `fetch-awards` on Lockheed Martin (`cik:0000936468`, explicit UEIs, 2025) → 3 routed `fund_flow` awards; `fetch-lobbying` on LDA client 58116 (2024) → 3 routed `fund_flow` filings; `fetch-treasury` → `macro_release` observations from the Daily Treasury Statement operating cash balance; all 0 skipped. Projected evidence files re-validate through `events._evidence`. Prior live sanctions counts unchanged (EU 6,234; UK 5,135; OFAC 19,393; UN 1,011). `objective.status` stays `not_achieved`.

**Limits:** Self-recorded points far below the pre-registered sample (required classes bitcoin, gold, oil, stock). Directional evidence exists for Binance `BTCUSDT` and tokenized-gold `PAXGUSDT`; for gold the instrument is a **gold-backed token, not COMEX GC=F**. COT, FINRA, Binance long/short ratios and GDELT news are measured-only or event kinds, so `v2` forecasts flat for oil and stock — those two classes have no verified free directional source and the four-class target cannot yet be met. Adjacent windows are not independent; the ledger proves internal consistency, not honest wall-clock issuance or provider truth; Binance/USDT venue. No licensing, accuracy or trading claim.

**Precise next task:** **P09** — construct and pre-register indicators from wired observations. P08 fetchers implemented with live verification: OpenSky ✅, BLS ✅, EIA 🔑 (needs API key), Comtrade ❌ (endpoint changed), Census Trade ❌ (endpoint changed). P07 complete (Treasury Fiscal Data live-verified; FEC blocked on API key). P06 complete except 13D/13G (deferred as unstructured HTML). Every new extractor needs bounded fixtures, `observed_at`/`available_at` provenance and no inferred counterparties or motives. Also still open: F04 (licensed valuation — blocked), J01 (another jurisdiction pack), O01 (fundraiser/VC/adviser source packs), S01 (enforcement narratives and other national lists), R01 (automatic extraction), Q01 (automated cross-OS CI). Keep direction primary, `target_rate` 0.98–0.99, never promote self-recorded points to independent evidence, and never infer counterparties or motives. No commits authorized.

**How to make P08 fetchers work:**
- **OpenSky** (works now): `lele fetch-opensky 1 states --limit 100` — no key needed for states
- **BLS** (works now): `lele fetch-bls 1 LNS14000000 --limit 25` — optional registration key for 500 req/day at https://data.bls.gov/registrationEngine/
- **EIA** (needs key): Get free key at https://www.eia.gov/opendata/register.php → `lele fetch-eia 1 PET.RWTC.D NG.RNGWHHD.D --api-key YOUR_KEY`
- **Comtrade** (endpoint changed): Check https://comtrade.un.org/data/ for current API; free tier may need registration
- **Census Trade** (endpoint changed): Check https://api.census.gov/data.html for current trade endpoints; may need API key

**Dirty files:** prior set plus new `lele/analysis/observations.py`, `lele/fetchers/form4.py`, `lele/fetchers/thirteenf.py`, `lele/fetchers/material.py`, `lele/fetchers/formd.py`, `lele/fetchers/nport.py`, `lele/fetchers/awards.py`, `lele/fetchers/lobbying.py`, `tests/test_observations.py`, `tests/test_form4.py`, `tests/test_thirteenf.py`, `tests/test_material.py`, `tests/test_formd.py`, `tests/test_nport.py`, `tests/test_awards.py`, `tests/test_lobbying.py`; new `lele/analysis/event_correlation.py`, `lele/analysis/money_flow_attribution.py`, `lele/analysis/volatility_anomaly.py`, `tests/test_phase_c_d.py`, `tests/test_pipeline_integration.py`; new `lele/fetchers/comtrade.py`, `lele/fetchers/eia.py`, `lele/fetchers/opensky.py`; modified `lele/analysis/events.py`, `lele/core/constants.py`, `lele/core/registry.py`, `lele/analysis/prospective.py`, `lele/cli/main.py`, `tests/test_cli.py`, `tests/test_registry.py`, `tests/test_packaging.py`, `README.md`, `DEVELOPMENT_PLAN.md`, `WORKLOG.md`; disposable `/tmp/opencode/p06-live/` artifacts are out of git.

## Previous handoff — GDELT news extractor (P04 continued)

**User direction:** continue P04; GDELT news events chosen (maps to the existing, measurement-optional `news_event` kind) after FRED stayed unreachable.

**Endpoint verification (live, 2026-09-18):** `api.gdeltproject.org/api/v2/doc/doc` first probe returned HTTP 200 with `{"articles":[...]}`; later probes returned HTTP **429** and a **plain-text** body ("Please limit requests to one every 5 seconds…"). GDELT enforces roughly one request per five seconds. No throttle bypass was attempted; the extractor surfaces a source error when throttled.

**Status:**
- `lele/core/constants.py`: added `EVIDENCE_ENDPOINTS["GDELT"]` and `HOST_MIN_RATES` (`data.sec.gov` 1.5 s, `api.gdeltproject.org` 5.5 s).
- `lele/fetchers/http.py`: per-host minimum rate now comes from `HOST_MIN_RATES`; a `429` retry delay is floored above the host minimum; a non-JSON throttle body (`"limit requests"`) raises a retryable rate-limit error instead of `SourceError`; ordinary invalid-JSON handling is preserved.
- New `lele/fetchers/news.py`: `request_news_window` (1..2160 hours, optional aware end), `_seendate` (`YYYYMMDDTHHMMSSZ`), `_observations` (skips non-HTTPS URLs, out-of-window see dates and duplicate URLs; records headline/domain/country as `news_event` with no measurement), `_validate` (empty article lists allowed) and `fetch_news`.
- `lele/cli/main.py`: new `fetch-news ID TOPIC [--hours 1..2160] [--limit 1..250] [--end ISO] [--output PATH] [--force]`, read-only registry and atomic no-overwrite export; command catalog updated.
- README and DEVELOPMENT_PLAN document the source, pacing and throttle semantics.

**Verification this session:** new `tests/test_news.py` 9 tests (observation build, round-trip through `events._evidence`, skip non-HTTPS/outside/duplicate, empty results as valid, malformed response, window bounds, topic/limit/entity validation, CLI read-only, invalid-args-before-database) and `tests/test_fetchers.py` +2 HTTP tests (GDELT minimum pacing, throttle text retried). Full suite **377 tests passed in 45.3s**. Ruff, mypy (22 source files), `compileall -q lele` and `git diff --check` passed. Live `fetch-news 1 bitcoin --hours 24 --limit 10` was throttled (429) and correctly returned a source error with no fabricated output. No commits; latest commit remains `24a29f3`.

**Limits:** Keyword relevance, deduplication and see-date availability are unverified; only headline, domain and country are captured; no tone or article body. GDELT is a public research index and its terms are not asserted. It throttles this environment, so live news coverage is unreliable here. FRED remains absent. `objective.status` stays `not_achieved`.

**Precise next task:** Obtain a FRED API key or record an explicit FRED exclusion, and/or verify GDELT from a non-throttled network. Then run the real objective — a pre-registered prospective evaluation of `evidence_score_10bps_v2` (direction primary, `target_rate` 0.98–0.99) over non-overlapping windows using these evidence files. Do not bypass source blocks. No commits authorized.

**Dirty files:** prior set plus new `lele/fetchers/news.py`, `tests/test_news.py`; modified `lele/core/constants.py`, `lele/core/registry.py`, `lele/fetchers/http.py`, `lele/fetchers/evidence.py`, `lele/fetchers/sources.py`, `lele/fetchers/prices.py`, `lele/analysis/episodes.py`, `lele/cli/main.py`, `tests/test_fetchers.py`, `tests/test_cli.py`, `tests/test_registry.py`, `tests/test_episodes.py`, `tests/test_evidence.py`, `tests/test_prices.py`, `README.md`, `DEVELOPMENT_PLAN.md`, `WORKLOG.md`; disposable `/tmp/opencode/` artifacts are out of git.

## Previous handoff — FINRA short interest extractor (P04 continued)

**User direction:** resume P04; FINRA consolidated short interest chosen next because it maps to the already-defined, measured-only `short_interest` kind.

**Endpoint verification (live, 2026-09-18):** `api.finra.org/data/group/otcMarket/name/consolidatedShortInterest` returns 200. GET query filters (`?symbolCode=`) are **ignored**, and sorting is refused unless the partition key `settlementDate` is constrained with an EQUAL filter. **POST** with a JSON body `{"limit":N,"compareFilters":[{"fieldName":"symbolCode",...}],"dateRangeFilters":[{"fieldName":"settlementDate",...}]}` works without auth and returns JSON when `Accept: application/json`; a no-data filter returns HTTP **204**.

**Status:**
- `lele/fetchers/http.py`: new `post_json(url, payload)` — allowlist validation, per-host pacing, bounded retries, 2 MiB limit, no caching — and `_request(..., method, body)`; HTTP 204 now returns `[]`.
- `lele/core/constants.py`: added `EVIDENCE_ENDPOINTS["SHORT_INTEREST"]`.
- `lele/fetchers/evidence.py`: `request_short_window(months, end, now)` (1..36 months, optional ISO date), `_short_observations` (verifies the returned `symbolCode` matches the request, parses the settlement date, requires a nonnegative share quantity) and `fetch_short` (one bounded POST; `available_at` = retrieval truncated to whole seconds because no publication date is exposed).
- `lele/cli/main.py`: new `fetch-short ID finra SYMBOL [--months 1..36] [--end YYYY-MM-DD] [--output PATH] [--force]`, read-only registry and atomic no-overwrite export via the shared evidence path; command catalog updated.
- No taxonomy change: `short_interest` already existed and is measured-only. README and DEVELOPMENT_PLAN document the source, the POST requirement and the conservative availability time.

**Verification this session:** `tests/test_evidence.py` +9 short tests (observation build, request payload filters, round-trip through `events._evidence`, window bounds, malformed/symbol mismatch, empty/unsupported, CLI read-only, invalid-args-before-database) and `tests/test_fetchers.py` +3 `post_json` tests (method/body/no-cache, 204 empty, unlisted-host rejection). Full suite **366 tests passed in 42.2s**. Ruff, mypy (21 source files), `compileall -q lele` and `git diff --check` passed. Live smoke `fetch-short 1 finra AAPL --months 6` against `/tmp/opencode/p04-smoke/registry.db`: 12 settlements (2026-03-13..2026-09-15), all `available_at` = retrieval. No commits; latest commit remains `24a29f3`.

**Limits:** Twice-monthly settlement dates and one equity symbol at a time; `short_interest` is measured-only (position stock, not directional flow). `available_at` is conservative retrieval; FINRA may revise reported positions. FRED and GDELT remain absent. `objective.status` stays `not_achieved`.

**Precise next task:** Add GDELT news events (`news_event`) — first add a >=5 s per-host minimum rate and detect GDELT's plain-text throttle response before JSON parsing; or record a written exclusion for FRED (graph CSV was unreachable here and the JSON API needs a key). Then pre-register a prospective `evidence_score_10bps_v2` evaluation on non-overlapping windows. Do not fabricate sources. No commits authorized.

**Dirty files:** prior set plus modified `lele/core/constants.py`, `lele/fetchers/http.py`, `lele/fetchers/evidence.py`, `lele/cli/main.py`, `tests/test_evidence.py`, `tests/test_fetchers.py`, `tests/test_cli.py`, `README.md`, `DEVELOPMENT_PLAN.md`, `WORKLOG.md`.

## Previous handoff — CFTC COT extractor (P04 continued)

**User direction:** continue P04 after the Binance depth/ratio slice.

**Endpoint verification (live, 2026-09-18):** CFTC Socrata `publicreporting.cftc.gov/resource/6dca-aqww.json` 200; GDELT DOC API `api.gdeltproject.org/api/v2/doc/doc` 200 but rate-limited to one request per 5 s and it returns a **plain-text** throttle message (not JSON) when exceeded; FINRA `api.finra.org/data/group/otcMarket/name/consolidatedShortInterest` 200 (CSV); FRED `fredgraph.csv` connection failed (000) and the FRED JSON API needs a key. Chose CFTC COT: structured, key-free, first in the plan order.

**Status:**
- `lele/core/constants.py`: added `EVIDENCE_ENDPOINTS["COT"]` (the existing HTTP allowlist matches exact paths in `EVIDENCE_ENDPOINTS`).
- `lele/analysis/events.py`: measured-only kinds `cot_open_interest`, `cot_positioning` (not directional).
- `lele/fetchers/evidence.py`: `request_cot_window(reports, end, now)` accepts 1..52 weeks and an optional ISO date; `_cot_observations` parses whole-UTC-day report dates, requires nonnegative open interest, and emits net non-commercial positioning (long minus short, contracts); `fetch_cot` issues one bounded Socrata query for the pinned market `COT_MARKETS["BITCOIN-CME"]`, validates through `events._evidence`, and reports. `available_at` is the retrieval time truncated to whole seconds — conservative because the endpoint exposes no publication time.
- `lele/cli/main.py`: new `fetch-cot ID cftc BITCOIN-CME [--limit 1..52] [--end YYYY-MM-DD] [--output PATH] [--force]`, read-only registry and atomic no-overwrite export, sharing the `fetch-evidence` export path; command catalog updated.
- README and DEVELOPMENT_PLAN document the source and its weekly, measured-only semantics.

**Verification this session:** `tests/test_evidence.py` +7 COT tests (observation build, round-trip through `events._evidence`, window-bounds/validation, malformed rows, unsupported source/symbol/entity, CLI read-only round-trip, invalid-args-before-database) → **16 evidence tests**; full suite **355 tests passed in 41.0s**. Ruff, mypy (21 source files), `compileall -q lele` and `git diff --check` passed. Live smoke `fetch-cot 1 cftc BITCOIN-CME --limit 4` against `/tmp/opencode/p04-smoke/registry.db`: 3 reports (2 kinds × 3 for 2026-08-25, 09-01, 09-08). One bug found only by the live run and fixed: real retrieval timestamps carry microseconds, which `events._time` rejects, so `fetch_cot` truncates to whole seconds; the fake client now carries microseconds to pin this. No commits; latest commit remains `24a29f3`.

**Limits:** Weekly cadence and one pinned CME contract market, not the whole bitcoin market. `cot_positioning`/`cot_open_interest` are measured-only; no direction sign. `available_at` is conservative retrieval, so a report is usable only from when this project fetched it. FINRA, FRED and GDELT are still absent. `objective.status` stays `not_achieved`.

**Precise next task:** Add FINRA consolidated short interest (maps to the existing `short_interest` kind) and/or GDELT news events (`news_event`). GDELT needs a >=5 s per-host minimum rate and detection of its plain-text throttle response; FRED needs an API key and was unreachable here, so either obtain a key or drop it with a written exclusion. Then run a pre-registered prospective evaluation of `evidence_score_10bps_v2` on non-overlapping windows. No commits authorized.

**Dirty files:** prior set plus modified `lele/core/constants.py`, `lele/analysis/events.py`, `lele/fetchers/evidence.py`, `lele/cli/main.py`, `tests/test_evidence.py`, `tests/test_cli.py`, `README.md`, `DEVELOPMENT_PLAN.md`, `WORKLOG.md`.

## Previous handoff — evidence extractor: depth snapshot + long/short ratios (P04)

**User direction:** continue P04 after the weighted-method session; chose **Order-book snapshot + positioning ratios** after endpoint verification showed the plan's named liquidation source does not exist.

**Endpoint verification (live, 2026-09-18):** `/futures/data/globalLongShortAccountRatio` 200, `/futures/data/topLongShortPositionRatio` 200, `/futures/data/takerlongshortRatio` 200, `/fapi/v1/depth` 200, `/fapi/v1/allForceOrders` **404**. Binance has no public historical liquidation REST endpoint; `forceOrder` liquidations are WebSocket-only. No liquidation endpoint was added and none was guessed.

**Status:** Implemented offline:
- `lele/core/constants.py`: `EVIDENCE_ENDPOINTS` gains `DEPTH`, `GLOBAL_LONG_SHORT`, `TOP_LONG_SHORT` (exact paths; the HTTP allowlist already checks `EVIDENCE_ENDPOINTS` values).
- `lele/analysis/events.py`: taxonomy gains measured-only kinds `long_short_account_ratio` and `top_long_short_position_ratio` (not in `DIRECTIONAL_KINDS`, so weight 0).
- `lele/fetchers/evidence.py`: new `_order_book_imbalance` (rejects empty sides, non-positive prices and negative quantities; imbalance = `(bidNotional - askNotional)/(bidNotional + askNotional)` quantized to 12 places; `observed_at` from exchange `T`/`E`, else retrieval) and `_long_short_ratio` (bounds by `[start, finish)` and cutoff, positive ratio required). `fetch_evidence` now issues six bounded requests, keeps long/short history at `min(limit, 120)` bars so the worst case stays under the 1000-observation and 2 MiB caps (~941), and the report gains a `snapshot` section plus updated `timestamp_semantics` and limitations.
- `lele/cli/main.py`: `fetch-evidence` epilog documents the ratios, the retrieval-time depth snapshot and the liquidation gap.
- README and DEVELOPMENT_PLAN describe the new sources and their limits.

**Verification this session:** `tests/test_evidence.py` updated (depth/ratio fixtures, imbalance `0.191235059761` and ratio values, retrieval-time fallback, four malformed cases, round-trip now 8) → **9 evidence tests**; full suite **348 tests passed in 39.5s**. Ruff, mypy (21 source files), `compileall -q lele` and `git diff --check` passed. Live smoke `fetch-evidence 1 binance-futures BTCUSDT --limit 12` against a throwaway registry in `/tmp/opencode/p04-smoke/`: 47 observations (12 order_flow, 12 open_interest, 11 global ratio, 11 top ratio, 1 depth, 0 funding), output 35,430 bytes, no warnings. No commits; latest commit remains `24a29f3`.

**Limits:** The depth snapshot is transient, spoofable and not executed flow, and its observed_at can fall after the window end. Long/short ratios are measured-only because crowded-positioning direction is ambiguous, so they add features but no direction score. Ratio history is capped at 120 bars (10 h) to respect the observation budget; open interest still spans the requested window. `available_at` remains optimistic, one symbol/one provider only, no license. `objective.status` stays `not_achieved`.

**Precise next task:** Add the remaining P04 sources in order — CFTC COT, FINRA short interest/margin, FRED cross-asset, GDELT news — verifying official endpoints and licenses first and adding fixture tests with explicit exclusions. Then run a pre-registered prospective evaluation of `evidence_score_10bps_v2` (direction primary, `target_rate` 0.98–0.99) on non-overlapping windows. Do not fabricate liquidations or fit weights. No commits authorized.

**Dirty files:** prior set plus modified `lele/core/constants.py`, `lele/analysis/events.py`, `lele/fetchers/evidence.py`, `lele/cli/main.py`, `tests/test_evidence.py`, `README.md`, `DEVELOPMENT_PLAN.md`, `WORKLOG.md`.

## Previous handoff — weighted evidence method v2 (P03 method improvement)

**User direction:** improve `evidence_score_10bps_v1` beyond a single signed sum (per-kind weights or a pre-registered lag/structure) without fitting to the small live sample.

**Status:** Implemented offline:
- `lele/analysis/worldstate.py`: new fixed `DIRECTION_WEIGHTS` (integer tiers — 3 realized pressure: `order_flow`, `trade_print`, `block_trade`, `order_book_imbalance`, `liquidation`; 2 money movement/positioning: `fund_flow`, `exchange_transfer`, `onchain_transfer`, `etf_flow`, `stablecoin_mint_burn`, `insider_trade`; 1 sentiment/context: `social_sentiment`, `market_context`, `capitulation_indicator`) and `RECENCY_BUCKETS = 3`. New `weighted_direction(records, as_of, window_seconds)` computes an exact integer score = `sign × kind weight × recency multiplier`, splitting the window into three equal buckets (newest ×3, middle ×2, oldest ×1) with the same point-in-time rule (observed inside window and `available_at <= as_of`). It returns the method name, full weight table, bucket count, scored kinds/score and `net_direction`. v1's `build_features`, `direction_score` and `net_direction` are unchanged.
- `worldstate.study` also reports `weighted_direction_agreement`, `weighted_scoring` and both `*_with_evidence` sections (the all-boundary rate is diluted by no-evidence boundaries that predict flat); each row gains `weighted_direction`, `weighted_direction_agreement` and `weighted_score`.
- `lele/analysis/prospective.py`: added frozen `EVIDENCE_METHOD_V2 = "evidence_score_10bps_v2"` beside `evidence_score_10bps_v1`; both are in `METHODS`/`EVIDENCE_METHODS` and share `EVIDENCE_STEP_BPS = 10`. `forecast` branches by evidence method: v1 keeps using `world_state.net_direction` (behavior byte-identical); v2 uses `weighted_direction(...)["net_direction"]` and stores `evidence_scoring` (weights, buckets, scored kinds, score, net_direction) in the hash-chained record. Price methods still reject evidence; evidence methods still require it.
- README and DEVELOPMENT_PLAN describe both frozen methods and the fixed weights.

**Verification this session:** Updated `tests/test_worldstate.py` (+4: kind weights and recency, late/outside/non-directional exclusion, down/cancelling flat, window validation) and `tests/test_prospective.py` (+2: both method versions registered; v2 preregister/forecast record plus CLI forecast idempotency and unchanged DB bytes). Full suite **347 tests passed in 38.9s** (341 prior + 6). Ruff, mypy (21 source files), `compileall -q lele` and `git diff --check` passed. No commits; latest commit remains `24a29f3`.

**Exploratory live comparison (offline, read-only, 2026-09-18):** `worldstate 1 live-btc-83h.json --evidence live-evidence-24h.json` against the isolated `/tmp/opencode/lele-live-prices-9q_4g1zy/registry.db` — 999 eligible five-minute boundaries, 287 carrying evidence from the ~24 h Binance file. Unweighted agreement: 66/999 (6.6%) overall, 66/287 (23.0%) with evidence. Weighted `kind_weight_recency_buckets_v1`: 142/999 (14.2%) overall, 142/287 (49.5%) with evidence. The weighted direction changed predictions and agreed more often on this sample, but 287 overlapping windows are not independent, evidence covers only part of the price span, and this is in-sample exploratory scoring, not prospective validation. No accuracy is claimed.

**Limits:** The kind weights and recency buckets are documented assumptions, not estimated coefficients; they are frozen in the method and recorded per forecast for reproduction. The 49.5% with-evidence figure is in-sample, overlapping and provider-limited; `objective.status` stays `not_achieved`. v2 changes only the direction — the ±10 bps magnitude remains a fixed convention. No liquidation, order-book or news sources are wired yet (P04), so several weighted kinds receive no data.

**Precise next task:** Continue the P04 extractor (liquidations, order-book imbalance, then CFTC COT, FINRA short interest/margin, FRED cross-asset, GDELT news) so more weighted kinds carry data, then run a fresh pre-registered prospective evaluation of `evidence_score_10bps_v2` (direction primary, `target_rate` 0.98–0.99) against v1 on non-overlapping windows. Do not fit weights to the live sample. No commits authorized.

**Dirty files:** new `lele/analysis/worldstate.py`, `lele/analysis/episodes.py`, `lele/fetchers/evidence.py`, `tests/test_worldstate.py`, `tests/test_episodes.py`, `tests/test_evidence.py`; modified `lele/analysis/events.py`, `lele/analysis/prospective.py`, `lele/core/constants.py`, `lele/fetchers/http.py`, `lele/cli/main.py`, `tests/test_prospective.py`, `tests/test_cli.py`, `README.md`, `DEVELOPMENT_PLAN.md`, `WORKLOG.md`, plus the prior uncommitted set.

## Previous handoff — evidence-first world-state layer (P03 reframed)

**User clarification:** (1) "one second" meant the observation window may be as small as one second if that is easier or better for the analysis, not a change of horizon; (2) accuracy means the predicted price is close (about 98–99%), not exactly equal, so exact Decimal equality stays a diagnostic and the pre-registered direction/tolerance criterion drives the target. The user rejected price-history-driven prediction as "what every other application builds": the forecast must be conditioned on the world state (events, actions, emotions, money movement, liabilities, decisions) observed in a window, with the price used only as the scored outcome.

**Status:** Implemented the evidence-first foundation offline:
- `lele/analysis/events.py`: evidence taxonomy extended to money (`onchain_transfer`, `etf_flow`, `stablecoin_mint_burn`), liabilities/positioning (`open_interest`, `funding_rate`, `short_interest`, `margin_debt`, `options_positioning`, `order_book_imbalance`), actions (`trade_print`, `block_trade`, `order_flow`, `insider_trade`), events (`news_event`, `macro_release`, `calendar_event`, `filing_event`) and `social_sentiment`; `ROUTED_KINDS` now allows documented `from`/`to` routes on all flow/transfer/liquidation kinds; `MEASUREMENT_OPTIONAL_KINDS` lets description-only events (`news_event`, `macro_release`, `calendar_event`, `filing_event`, `market_context`) omit a measurement instead of failing. Existing kinds and validation unchanged.
- New `lele/analysis/worldstate.py`: point-in-time, evidence-only feature vectors for any boundary. A record enters a window only when observed inside it and `available_at <= as_of`; window is configurable 1 second to 24 hours (default 300 s). Features are counts by kind, signed `direction_score`/`net_direction`, measured totals by kind, routed amounts by currency with origins/destinations, reason bases and sentiment totals. `study` aligns boundaries to the 5-minute price grid, labels each with the future move only as the outcome, and reports direction agreement and per-actual-direction pattern means. No price lag is ever a feature.
- `lele/analysis/prospective.py`: new frozen method `evidence_score_10bps_v1` plus `METHODS`; `forecast(..., evidence_path=...)` builds features at the as-of boundary, derives direction from the evidence, and projects the last close by a fixed ±10 bps (the close is only an anchor). Optional config `observation_window_seconds` (1..86400). Price methods reject `--evidence`; the evidence method requires it. Ledger records carry `evidence_file_sha256`, `world_state` and `evidence_step_bps`.
- `lele/cli/main.py`: new `worldstate ID PRICES_JSON --evidence EVIDENCE_JSON [--window-seconds] [--limit]` command (read-only registry, no DB writes), and `prospective forecast --evidence`.
- New `lele/fetchers/evidence.py` and CLI `fetch-evidence ID SOURCE SYMBOL --output PATH [--limit] [--end] [--force]`: the first real extractor. Bounded public Binance USD-M futures calls to `/fapi/v1/klines` (net taker quote flow per closed candle → `order_flow`), `/futures/data/openInterestHist` (five-minute notional change → `open_interest`) and `/fapi/v1/fundingRate` (settled funding → `funding_rate`), all window-bounded. Exact endpoints added to `EVIDENCE_ENDPOINTS` and the HTTP allowlist. Output is an evidence file that round-trips through `events._evidence`; `available_at = observed_at` (provider lag unverified, stated in the report). Read-only registry, atomic no-overwrite export, no SQLite writes.
- New `lele/analysis/episodes.py` and CLI `episodes ID PRICES_JSON [--evidence] [--threshold-percent] [--horizon-hours] [--limit]`: threshold-defined ZigZag segments the 5-minute series into alternating up (pump) and down (dump) legs with magnitude, duration and a bull/bear/range/unknown 72-hour regime label, then profiles each leg's preceding horizon of point-in-time evidence (same rule as `worldstate`) and compares pump-versus-dump precursor means and kind totals. Read-only registry.

**Verification this session:** New `tests/test_worldstate.py` 14 tests, `tests/test_prospective.py` +2, new `tests/test_evidence.py` 8 tests, new `tests/test_episodes.py` 7 tests. Full suite **341 tests passed in 49.3s** (334 prior + 7). Ruff, mypy (21 source files) and `compileall -q lele` passed. Development failures: the worldstate and `fetch-evidence` read paths initially used the writing connection so DB bytes changed, both moved to read-only `mode=ro`; mypy required a `list[dict]` annotation on the ZigZag legs; the leg duration was computed from `timedelta.total_seconds()` as a float and is now exact Decimal seconds over 3600, with a test that pins it under a 128-digit context. No commits; latest commit remains `24a29f3`.

**First live evidence extraction and world-state study (2026-09-18T03:09Z):** `fetch-evidence 1 binance-futures BTCUSDT --limit 12` against the isolated registry produced 24 observations (12 `order_flow`, 12 `open_interest`, 0 `funding_rate`; an earlier un-bounded funding call returned 100 records over weeks, so funding and open interest are now window-bounded). A fresh 12-slot Binance spot price export plus that evidence gave 11 eligible boundaries, all with evidence, and **direction agreement of only 1 of 11 (rate 0.0909)**: signed net taker flow was frequently contrary to the next five-minute move on this short sample. A later 288-slot run produced 288 order-flow, 288 open-interest and 3 funding observations.

**Live episode run (2026-09-18):** On 1000 five-minute BTCUSDT closes (~83 h) plus the 24 h evidence file, `episodes --threshold-percent 1 --horizon-hours 72` found 13 legs (7 pumps, 6 dumps), including a −1.22% dump over 5.75 h and a +1.86% pump over 13.7 h; at 2% and 3% it found one −4.35% dump (26.7 h) and one +3.16% pump (56.5 h). Only two recent legs had a full 72 h precursor (evidence covers ~25 h): the dump precursor scored −6 and the pump precursor scored −13, both leaning negative before the move. Regimes were `unknown` because the 72 h lookback preceded the data start. Artifacts under `/tmp/opencode/` (`live-evidence*.json`, `live-btc-*.json`, `live-ep*.json`, `live-ws.json`) are disposable.

**Limits:** The directional convention (positive measurement means upward pressure) is an assumption of the supplied evidence, not verified causation; unambiguously directional kinds exclude open interest, funding, short interest, margin debt and options positioning, which remain measured-only features. Missing evidence is unknown coverage, not absence. The `evidence_score_10bps_v1` magnitude is a fixed ±10 bps convention, not fitted. The live extractor covers one Binance futures symbol, no liquidations/order-book depth, one provider, `available_at` may be optimistic, and no license or counterparty identity is established. Historical direction agreement is exploratory, overlapping and not independent; `objective.status` stays `not_achieved`.

**Precise next task:** Extend the extractor and method: add liquidations and order-book imbalance, then CFTC COT, FINRA short interest/margin, FRED cross-asset and GDELT news; improve the evidence method beyond a single signed sum (per-kind weights or a pre-registered lag/structure) without fitting to the small live sample; add non-episode controls and test whether the 72-hour precursor profiles actually separate pumps from dumps out of sample; then run a pre-registered `evidence_score_10bps_v1` prospective evaluation with direction primary and `target_rate` 0.98–0.99. Do not promote self-recorded results to independent evidence. No commits authorized.

**Dirty files:** new `lele/analysis/worldstate.py`, `lele/analysis/episodes.py`, `lele/fetchers/evidence.py`, `tests/test_worldstate.py`, `tests/test_episodes.py`, `tests/test_evidence.py`; modified `lele/analysis/events.py`, `lele/analysis/prospective.py`, `lele/core/constants.py`, `lele/fetchers/http.py`, `lele/cli/main.py`, `tests/test_prospective.py`, `tests/test_cli.py`, `README.md`, `DEVELOPMENT_PLAN.md`, `WORKLOG.md`, plus the prior uncommitted set.

## Previous handoff — P03 prospective harness implemented

**Status:** Implemented P03 offline: new `lele/analysis/prospective.py` and CLI `prospective ACTION --ledger PATH [--config ...] [--id ...] [--path ...] [--run ...] [--force]` with actions `preregister`, `forecast`, `settle`, `score`. `preregister` freezes a run config (one of the three fixed methods, required asset classes, target metric among direction/tolerance/exact/combined, `tolerance_bps`, `target_rate`, `min_settled_per_class`, optional notes), refuses an existing ledger without `--force`, and derives a deterministic run id. The ledger is append-only JSONL with a SHA-256 hash chain over canonical records; a sequence or hash mismatch aborts any read. `forecast` uses the frozen method over the last three consecutive five-minute closes of a supplied recorded-price file, binds the instrument to an existing local entity key read-only, appends the issuance wall-clock time, and marks status `prospective` only when the target time is later than issuance (otherwise `backfilled`, excluded from scoring); an identical run/entity/as-of/method returns the existing record. `settle` appends outcomes only when an exact observation exists at the target boundary; gaps stay `pending`, never bridged. `score` reports direction/tolerance/exact/combined hits, within-unit MAE, coverage (issued/prospective/backfilled/settled/pending, by class) and per-class metrics; `objective.status` stays `not_achieved` until every required class meets `min_settled_per_class` and the pre-registered rate, then `prospective_target_met_unverified` — never `achieved`, because the ledger is self-recorded. Per the user's direction the criterion is direction+tolerance with exact as a diagnostic, and the required classes are bitcoin, gold, oil and stock together. Ledger bounded to 16 MiB / 200,000 records; `forecast`/`settle` open the registry read-only and write no SQLite; `preregister`/`score` need no database.

**Verification this session:** New P03 suite **25 tests passed in 6.2s** (pre-registration and deterministic run id, overwrite/force, config-validation matrix, hash-chain tamper detection, prospective vs backfilled issuance, gap/insufficient-history rejection, entity/key checks, idempotent forecast/settle, exact-target settlement with pending gaps, direction/tolerance/exact/combined scoring, cross-class objective met/not-met, specific/unknown run, hostile Decimal context invariance, in-process and subprocess CLI round-trips, read-only database bytes, invalid args failing before database creation, ledger protecting the registry). Full suite **310 tests passed in 32.9s** (285 prior + 25). Ruff, mypy (18 source files) and `compileall -q lele` passed. No live price requests, no commits; prior dirty work preserved; latest commit remains `24a29f3`.

**First live prospective run (2026-09-18T00:23–00:26Z):** Reused the isolated P01 registry at `/tmp/opencode/lele-live-prices-9q_4g1zy/registry.db` (entities 1 BTCUSDT, 2 GC=F, 3 CL=F, 4 RB=F, 5 AAPL). New artifacts: `/tmp/opencode/p03-live-20260918T002342/` (`ledger.jsonl`, `config.json`, per-symbol phase-1/phase-2 exports and reports; disposable, needs separate backup). Pre-registered method `five_minute_persistence_v1`, required classes bitcoin/gold/oil/stock, `target_metric=combined`, `tolerance_bps=100`, `target_rate=0.9`, `min_settled_per_class=1`; run id `d57d04de…2b6c5b`. Phase 1 fetched 12-slot windows and issued forecasts: **BTCUSDT was the only prospective record** (target 00:25:00Z); GC=F and CL=F came back `backfilled` (latest Yahoo candle ended 00:15, target 00:20 < issuance) and RB=F likewise (target 00:15). AAPL `fetch-prices` failed because the US market was closed. Phase 2 re-fetched after the target and settled: BTC actual 76358.57 vs persistence forecast 76360.93 → direction miss (predicted flat, actual down), **tolerance hit at 3.09 bps ≤ 100**, exact miss, combined miss; the three backfilled Yahoo records also settled (small tolerance hits) but were excluded from scoring. Score: coverage issued 4 / prospective 1 / backfilled 3 / settled prospective 1; `objective.status=not_achieved`, `target_met=false`, combined rate 0/1. This is one genuine prospective data point, not evidence of skill.

**Finding:** With persistence, the predicted direction is always `flat`, so the `combined` metric (direction AND tolerance) is near-unreachable on moving markets even when the tolerance band is generous; the live BTC record hit tolerance but not direction. Yahoo's last closed candle lags the current boundary by 5–10 minutes, so immediate forecasts on GC=F/CL=F/RB=F are `backfilled`; prospective Yahoo runs need issuance timed to candle availability. Stocks are unavailable outside market hours. The harness reports these honestly rather than hiding them.

**Limits:** The one live prospective record is self-recorded and far below the pre-registered per-class sample; it does not establish accuracy. Hash chaining proves internal consistency, not honest wall-clock issuance. Forecast prices come from user-supplied recorded files and a frozen method, not a live feed. Settlements require exact target observations. MAE is reported only within a single unit. A met target stays unverified pending independent out-of-sample multi-asset evidence and data-rights review. `objective.status` never becomes `achieved` here.

**Precise next task:** Continue the prospective run across many boundaries and sessions to accumulate per-class samples, or pre-register a different frozen method (for example `five_minute_linear_extrapolation_v1`) so direction is non-flat and the combined metric is testable; either way the user should decide whether `combined` should remain direction AND tolerance or whether `target_metric` should be `direction`/`tolerance` alone. Time Yahoo issuance to candle availability, retry stocks during market hours, and keep `objective.status=not_achieved` until real prospective coverage and rate are met. Data rights, benchmark/contract identity and timestamp availability remain open. No commits authorized.

**Dirty files:** prior set plus new `lele/analysis/prospective.py` and `tests/test_prospective.py`; modified `lele/cli/main.py`, `tests/test_cli.py`, `README.md`, `DEVELOPMENT_PLAN.md`, `WORKLOG.md`.

## Previous handoff — P02 chronological held-out comparison implemented

**Status:** Implemented P02 offline: new `lele/analysis/comparison.py` plus CLI command `compare ID PRICES_JSON [--train-percent 1..99]` (default 70, argparse `choices=range(1,100)`, `--db` before the command). It reuses `projection._load` (exact `instrument`/`prices` schema, 2 MiB, 2–10,000 points, entity-key match, positive bounded Decimal prices) and splits chronologically: first `floor(n * percent / 100)` observations train, rest test; targets partition by index so no target is in both. Three fixed small methods, no fitting: `five_minute_persistence_v1`, `five_minute_linear_extrapolation_v1`, `five_minute_trailing_mean_3_v1`. A target is eligible only with exactly 3 consecutive history points and a label exactly 300 seconds later, identical across methods; warmup (<3 history) and gap targets are counted separately per section. Selection uses **training only** — exact hits desc, exact total absolute error asc, fixed method order — then freezes; test scores are diagnostic and never reselect. Test walk-forward uses observed prior test prices (first eligible test target's history may reach into training rows); current/future labels are never inputs. Nonpositive linear forecasts are kept unclamped, counted per method and scored on shared denominators. Explicit statuses: `insufficient_data` (section <4 observations), `no_eligible_targets`, `no_eligible_training_targets` (selection/test score then null). All arithmetic in `localcontext(128, half-even)`; forecasts/rates/MAE are exact-total-derived decimal strings; per-target predictions (timestamps + per-method prices) are published for audit. CLI opens the existing registry **read-only** via `sqlite3.connect(uri + "?mode=ro")` (no init/migration; absent file fails), no writes, no network, `objective.status=not_achieved` always; report marks evaluation as exploratory historical, not prospective, and asserts no >90% result.

**Verification this session:** New P02 suite **19 tests passed in ~4.5s** (round-trip incl. subprocess/`--json`/human parity and byte-identical DB + prices, mutated-suffix selection invariance, prefix-forecast/current-label no-lookahead, train/test target disjointness with exact indexes, gap-at-every-window-position, nonpositive unclamped scoring, precision under hostile caller context (`prec=2`, `ROUND_UP`, `Emax/Emin`, `Inexact` trap — report unchanged), 10,000-point/file-bound edges, read-only SQL/transaction neutrality, invalid args/percent/IDs/files, CLI failures before DB creation, insufficient-data reports). Full suite **285 tests passed in 27.216s** (266 prior + 19). Ruff, mypy (17 source files), `compileall -q lele` and `git diff --check` passed. One failure during development: my test compared Decimal-equal zero strings `"0.0"` vs `"0"` — fixed the assertion to compare `Decimal` values; no production change. Read-only behavior proven by `PRAGMA query_only` + SQL trace (only SELECTs) + `total_changes` equality + unchanged file bytes. No live price requests, no commits, prior dirty work preserved; latest commit remains `24a29f3`.

**Limits:** Automated tests use synthetic fixtures; saved provider exports were evaluated separately below. Prices are not independently truth-verified; no prospective accuracy claim; hit rates on constant/stale series are inflated by construction; re-inspection of held-out scores with different splits compromises the holdout; nonpositive forecasts are scored but are not valid quotes; MAE/rate strings are rounded to 128 digits (exact hits use exact numerator equality).

**Recorded-data evaluation completed:** Ran the CLI on all five saved P01 exports with the fixed 70/30 split; reports are saved beside inputs as `*-compare.json` in `/tmp/opencode/lele-live-prices-9q_4g1zy/`. Training selected persistence for every asset. Held-out hits/eligible and rounded MAE: BTCUSDT 0/300 (0%, 57.52957 USDT/BTC); GC=F 8/280 (2.86%, 2.64428 USD/troy ounce); CL=F 17/280 (6.07%, 0.11957 USD/barrel); RB=F 3/237 (1.27%, 0.00390422 USD/US gallon); AAPL 0/91 (0%, 0.28356 USD/share). Gaps remain excluded. These short, already collected series are exploratory historical evidence, not prospective validation. A focused BTC diagnostic confirmed all 300 persistence forecasts equal the preceding actual close (including the training/test boundary), with 0 exact hits: price movements, not a lag alignment bug. No tuning to these scores or accuracy assertions added.

**Precise next task:** P03 prospective evaluation infrastructure: pre-register a frozen method, instruments, evaluation window and exact-match criterion; durably record forecasts and issuance/retrieval times before outcomes, then score only eligible future observations with gaps, failures and coverage visible. None of the compared methods meets 90% on this held-out sample. Do not promote historical comparisons to prospective evidence or relabel the objective achieved. Data rights, benchmark/contract identities and timestamp availability remain open. No commits authorized.

**Dirty files:** prior set unchanged plus new `lele/analysis/comparison.py` and `tests/test_comparison.py`; modified `lele/cli/main.py` (command entry, parser, read-only dispatch), `tests/test_cli.py` (command catalog), `README.md`, `DEVELOPMENT_PLAN.md`, `WORKLOG.md`.

## Previous handoff — P01 live exports verified

**Latest live result:** The corrected adapter successfully exported BTCUSDT **1000**, GC=F **951**, CL=F **951**, RB=F **905**, and AAPL **313** closes. Every export passed both `project` and `events` through the CLI (all exit 0). Retrieval times were 2026-09-17 22:36:40–45 UTC according to the runtime clock. Isolated registry, price files, provenance reports, downstream reports and summary are in `/tmp/opencode/lele-live-prices-9q_4g1zy/`; these temporary artifacts require separate backup. Existing user registry untouched; no commits.

**Limits and next task:** This verifies one bounded live ingestion run, not price truth, provider licensing, all-history coverage or predictive accuracy. Futures series are not spot/retail prices, Yahoo numeric precision and contract rolls remain caveats, and no flow/decision evidence was collected. Keep `objective.status=not_achieved`. Next: ~~P02 baseline/model comparisons~~ — implemented 2026-09-18, see Current handoff; then P03 prospective validation. Preserve source-rights review and fixed-expiry/benchmark selection as unresolved requirements.

## Previous handoff — P01 snapshot guard tightened

**Status:** Review exception accepted: the earlier snapshot rule trusted any final-row `_epoch` failure, which could have masked negative/out-of-range timestamps. `_yahoo` now requires every Yahoo timestamp to be an exact int in 0..253402300499 first; the off-grid exception applies **only** when the final row's timestamp is an exact int equal to an exact-int `meta.regularMarketTime` with a positive numeric close. The meta `regularMarketPrice` is deliberately not compared (provider rounding); fixture keeps it deliberately different (100.499 vs close 100.5) to prove no float-equality requirement. Negative/out-of-range final timestamps and non-final off-grid rows fail. No live calls made this session.

**Verification:** P01 suite **36 tests passed in 5.740s** (added: final negative/out-of-range rejection, unmatched/None/string/float `regularMarketTime` rejection, valid snapshot fixture sets `regularMarketTime` and a differing `regularMarketPrice`). Full suite **266 tests in 23.696s**; Ruff, mypy (16 source files), compileall and `git diff --check` passed. One run failure (float 1789691160.0 compared equal to the int) was fixed by requiring exact int on `regularMarketTime`, then all checks reran clean.

**Precise next task:** unchanged — main agent may re-run the live harness for GC=F/CL=F/RB=F (BTCUSDT 1000 and AAPL 313 already recorded), run `project`/`events` on the futures exports, keep artifacts under `/tmp/opencode`, then proceed to P02/P03. No commits authorized.

**Dirty files:** unchanged set; latest commit remains `24a29f3`.

Previous handoff follows; the P01 status above supersedes older Next pointers and counts.

## Previous handoff — P01 live Yahoo trailing-snapshot fix

**Status:** Live verification ran (main harness `/tmp/opencode/verify_live_prices.py`, artifacts `/tmp/opencode/lele-live-prices-uuouta5q/`): Binance BTCUSDT exported **1000** closes and passed `project`/`events`; Yahoo AAPL exported **313**; Yahoo GC=F, CL=F and RB=F all failed CLI validation with `_epoch` "candle opening must be on a UTC five-minute boundary". Root cause established with a one-request bounded probe (`/tmp/opencode/inspect_yahoo_prices.py`, artifacts `/tmp/opencode/lele-yahoo-inspect-l36_b_2d/`, canonical-response SHA-256 `350b42e2…a96f`): GC=F returned exactly **one** off-grid row, the **final** row, epoch 1789682860 (22:07:40Z, seconds 160), while all 999 other timestamps were exact 300-second boundaries; it carried equal open/high/low/close 4381.2001953125, volume 0, and matched meta `regularMarketTime` 1789682860 and `regularMarketPrice` 4381.2. Conclusion: Yahoo's trailing live-quote snapshot row, not a candle; no rounding or synthetic boundary applied.

**Change:** `_yahoo` now permits an off-grid timestamp **only as the last row, only when it exactly equals an exact-int `meta.regularMarketTime`, and only with a positive valid numeric close**; it is excluded from the export, counted as `skipped.trailing_snapshots` and documented in report limitations. Every Yahoo timestamp must otherwise be an exact int in the supported range; non-final off-grid timestamps, snapshot rows with null/non-numeric/invalid prices, negative or out-of-range values, and all other malformed timestamps fail unchanged. The meta price is not compared (rounding). `_binance` rows carry the same `(opening, close, trailing)` tuple.

**Verification this session:** Initial edits failed tests (tuple mismatch 6 failures/14 errors, then residual issues) because I stacked changes without running; each failure was diagnosed from direct output and fixed: 3-tuple contract in both producers, a duplicated Yahoo parsing block removed, one missing mock assignment, and the limitations sentence added. Final: P01 suite **35 tests passed in 5.572s**; full suite **265 tests in 23.780s**, then **24.609s** after final documentation edits, with Ruff, mypy (16 source files), compileall and `git diff --check` passing. Live re-run of futures symbols after the fix has **not** been executed by this subagent and remains the next step. Regression tests cover: accepted final snapshot counted (report `received` 5, skipped 1, output ends 00:20:00), non-final off-grid rejected, malformed snapshot variants (null/string/negative close, "bad"/None/float timestamp) rejected.

**Limitations (unchanged + new):** Yahoo chart remains unsupported, unlicensed, float-precision, no roll/adjustment repair; elapsed candle end is not publication availability; USDT is not USD; objective remains `not_achieved`. New: the trailing-snapshot rule is verified against one GC=F live observation and offline fixtures only; other symbols/dates may exhibit different provider edge behavior. Live futures re-verification still pending.

**Precise next task:** Re-run the live harness for GC=F/CL=F/RB=F (BTCUSDT/AAPL results already recorded), then run `project`/`events` on the futures exports; keep provenance artifacts under `/tmp/opencode`; then update the P01 queue status and proceed to P02/P03. No commits authorized.

**Dirty files:** unchanged set plus this fix: `lele/fetchers/prices.py`, `tests/test_prices.py` (35 tests), `README.md`, `DEVELOPMENT_PLAN.md`, `WORKLOG.md`, prior engine/importer/sources/CLI/HTTP edits, projection/events modules and their tests; latest commit remains `24a29f3`. Live artifacts under `/tmp/opencode/lele-live-prices-uuouta5q/` and `/tmp/opencode/lele-yahoo-inspect-l36_b_2d/` are disposable backups, not git content.

Previous handoff follows; the P01 status above supersedes older Next pointers and counts.

## Previous handoff — P01 bounded price export

**Status:** Implemented `fetch-prices ID SOURCE SYMBOL --output PATH [--limit 288] [--end ISO8601] [--force]` for user-approved free public APIs in order Binance BTCUSDT, Yahoo GC=F, CL=F/RB=F, AAPL. New `lele/fetchers/prices.py` and `tests/test_prices.py`; existing CLI, constants and HTTP allowlist extended narrowly. Prior dirty work preserved; no commits, reset, stash or live price requests. Documentation-only network reads consulted Binance REST docs and yfinance public chart implementation/access caveats; the Binance developer page returned no useful content, so its official GitHub REST documentation was read instead. No yfinance dependency added.

**Behavior:** Exact existing entity key, unchanged projection/events `instrument`/`prices` schema; one logical HTTP request with existing at-most-three retry policy, 2 MiB response cap, 2–1000 elapsed five-minute slots, no cache even under environment overrides. Candles must be closed by request start, retrieval and requested end; timestamps are END boundaries (opening + 300s). Binance close-time validation; Yahoo metadata/array validation; invalid prices/timestamps or duplicate openings fail, unordered rows sort, null closes and partial/future/out-of-window rows are counted/omitted without filling gaps. At least two closes required. Atomic export conventions and no-overwrite default reused; registry/journals and aliases protected, no SQLite price writes. Returned report includes retrieval/request UTC times, source URL, selected key, window/counts, provider metadata, canonical parsed-response SHA-256 (not wire bytes), exact output-file SHA-256/bytes and limitations. Save returned JSON separately for durable provenance; no automatic sidecar.

**Verification:** Baseline 230 tests passed in 17.577s. New P01 suite 32 tests passed in 5.490s, then 5.189s on repeat. Full suite **262 tests passed in 23.865s**. Ruff, mypy (16 source files), compileall and `git diff --check` passed before final documentation edits; final full rerun after documentation edits passed **262 tests in 23.546s**, Ruff, mypy (16 files), compileall and diff-check. The isolated all-five-symbol CLI export/project/events round-trip also passed (1 test in 0.789s). Mocked CLI round-trip passed with exactly one HTTP call, two end-stamped closes and retrieval provenance; automated round-trips cover all five symbols through both project and events, unchanged DB bytes, bounds/malformed responses and output/alias/race protection. `sources` stays the offline institution catalog. Initial ad hoc probe incorrectly used `get_conn` without a context manager and failed with AttributeError; corrected probe passed. Ruff found one unused test import, removed before clean full checks. `git diff --no-index /dev/null lele/fetchers/prices.py` exited 1 because it displayed a new-file diff, not a check failure. No unrelated events validation changes.

**Limitations:** Offline fixtures only; no live availability, freshness, market truth or prospective accuracy certification. Binance USDT is not USD. Yahoo chart is unsupported, access/licensing/redistribution terms are not certified permissive; no cookies, alternate hosts or block bypass. Futures-series symbols are not verified fixed-expiry contracts, spot gold/oil or retail pump fuel. Yahoo numeric closes pass through float; no split/distribution/roll/session-calendar repairs, and elapsed candle end is not publication availability. Existing positive-price schema excludes nonpositive historical futures prices. `objective.status` remains `not_achieved`.

**Precise next task:** Main agent may run an explicitly isolated live verification using appropriate existing entity IDs and new temporary output paths: BTCUSDT first, then GC=F, CL=F/RB=F, AAPL. Review terms for intended use, stop on access denials, preserve returned provenance reports, inspect end stamps/source delay/venue/contract identity and run project/events on resulting files. Do not claim P01 live gate or 90% accuracy achieved from fixture checks. Then P02/P03 remain pending.

**Dirty files:** This P01 work changes `README.md`, `WORKLOG.md`, `DEVELOPMENT_PLAN.md`, `lele/cli/main.py`, `lele/core/constants.py`, `lele/fetchers/http.py`, `tests/test_cli.py`; adds `lele/fetchers/prices.py`, `tests/test_prices.py`. Prior dirty `AGENTS.md`, engine/importer/sources changes, analysis/fetcher tests, projection/events modules and their tests, and SEC provenance tests remain intact. Latest commit remains `24a29f3`.

Previous handoff follows; the P01 status above supersedes older Next pointers and counts.

## Previous handoff — evidence mapping

**Latest status:** The `events` scanner now maps documented money routes and decisions. Flow/transfer/liquidation observations accept an optional `mapping` with both `from` and `to` (partial routes rejected; direction comes from supplied evidence, not signed net flows; amount must be positive with a currency/unit). New `decision` kind carries `actor`, `action`, `reason` and `reason_basis` — `stated_reason`/`documented_mandate` require actor plus `source_locator`, `analyst_hypothesis` is interpretation, `unknown` leaves reason null. `related_ids` cross-references decisions to flows; dangling, duplicate, self-referencing or foreign-entity IDs are rejected, so every reported direction resolves. Mappings are preserved in reports and included in precursor windows. Evidence may also be supplied as CSV (22 documented columns; blank mapping columns yield no mapping). README documents the schemas with a synthetic example. No real routing/decision dataset exists yet; attribution limits are unchanged.

**Latest verification:** Full unittest suite **230 tests passed in 20.507s**; the events suite is **17 tests** (an earlier "18" here was wrong). The round-trip test for instrument-keyed routing, both-way `related_ids` links and the `p. 1` locator passes in isolation (0.016s). Ruff, mypy (15 source files), compileall and `git diff --check` passed. A standalone repro passed 6/6 checks (original no-mapping input, routed flow, attributed decision, window inclusion, CSV parity, dangling-ID rejection). During development my repeated hand-edits introduced a duplicated `elif` (syntax error) and a dead code block in `_mapping`; both were removed after reading the whole function, then mypy passed. A test initially wrote CSV content to a `.json` path and had a `flow`/`flow_row` NameError and short CSV rows; all were fixture bugs, fixed by generating rows with csv.DictWriter and a `.csv` path. Test-runner reminder: unittest only; piped runs hang, always redirect to a file.

**Cleanup:** Prior cleanup notes stand (no `price` command, no commits). All dirty files remain uncommitted.

**Precise next task:** Unchanged from the previous handoff — obtain licensed recorded prices and timestamped flow/liquidation/decision evidence for a user-chosen instrument/venue/date range, then run the scanner on real inputs and add non-event controls and held-out testing before using precursors in projections. Do not infer net injections from volume or market cap; do not present analyst hypotheses as actors' actual reasons.

**Current additional dirty files:** `lele/analysis/events.py`, `lele/analysis/projection.py`, `tests/test_events.py`, `tests/test_projection.py`, in addition to the historical pending list below. All remain uncommitted.

Previous handoff follows (latest status above supersedes it).

**Status:** F03 implemented and verified offline; prior uncommitted F01/F02, manager-edge and continuity changes preserved. Latest existing commit remains `24a29f3`. No commit, stash, reset or live issuer API requests. A research subagent fetched FASB 2025 taxonomy XML/schema to verify definitions and period types.

**Next:** P01 — verified live five-minute price ingestion (gold, oil/fuel, bitcoin, listed equities). Requires user-approved licensed sources with recorded UTC timestamps and venue/price-type provenance; do not guess sources, prices or contacts. Then P02 model comparisons and P03 prospective 90% exact-match validation; `objective.status` remains `not_achieved` until independent out-of-sample multi-asset evidence exists. F04 remains pending after source selection.

**Changed this resume (P01 groundwork + projection objective):** Recorded the user's standing objective (five-minute-ahead price projection for gold, oil/fuel, bitcoin and listed shares; 90% of predictions exactly accurate) in the development plan as a target to reach and verify, explicitly not a promise and never to be redefined. Implemented `analysis/projection.py` and the `project` CLI command: historical as-of persistence baseline (`five_minute_persistence_v1`) over supplied recorded prices (2 MiB/10,000 points, strictly increasing UTC five-minute timestamps, exact decimal prices, instrument key must match the local entity). Walk-forward evaluation over consecutive five-minute pairs only (no gap bridging, no look-ahead) reports exact hits, exact-match rate, MAE (Decimal, 128-digit half-even when nonterminating) and `historical_target_met` at 0.9; `objective.status` stays `not_achieved` regardless of historical rate, since user-supplied fixtures cannot establish prospective multi-asset accuracy. Research context (entity + finmap) is attached with `used_in_forecast=false`. File bytes are hashed (sha256), validation is syntax-only, `truth_verified=false`; inputs are not modified; no network, no live quotes. 20 offline tests cover precision, gaps, bounds, malformed input, no-lookahead prefix aggregation, read-only behavior and transaction neutrality; CLI catalog and dispatch coverage added (213 tests total).

**Current verification (P01 groundwork):** 20 projection tests passed in 1.370s; `tests.test_cli` 23 tests passed; full unittest suite 213 tests passed in 20.781s (run directly to a log file, no pipe; a piped `| tail` attempt hung, consistent with the documented quirk). Ruff, mypy (14 source files), compileall and `git diff --check` passed. A manual demo run failed with exit code 1 "invalid data or registry schema" because the demo instrument used `entity_key` "demo:btc" against entity key "demo:btc-usd"; the CLI correctly rejected the mismatch. No live price data, no commits.

**Changed this resume (F02):** `engine.finmap` and CLI/menu `finmap ID [ID ...]` return bounded financial-position reports for 1–10 unique local IDs and at most 1000 stored metric rows per issuer. Separate issuer/period/source groups expose seven canonical stock/flow positions, validation status/reasons, full observation evidence and raw stored inputs. Stock/flow alignment is checked separately; incomplete or mismatched sets stay unknown without suppressing individually valid facts. Legacy and conflicting/ambiguous inputs cannot become usable positions. No totals, aggregation, inferred transfers, network requests or metric writes. Debt remains unsupported in F01 envelopes. Added 26 offline tests with two explicitly synthetic issuer fixtures, fresh import/CLI round-trips and read-only/boundary checks; updated the CLI command-catalog assertion. Documentation describes the implemented scope.

**Changed this resume (F01):** SEC selected metrics now carry validated `sec_fact_v1` JSON envelopes with numeric value, USD/currency/scale, taxonomy/tag, accession/form, filed/start/end, period type, source URL, retrieval timestamp, selection rule and ambiguity flags. Structured offline imports serialize evidence deterministically; `show` retains serialized values and `analyze` exposes parsed observation evidence. Ratios block legacy SEC scalars, unknown provenance, quarter/YTD and accession/form mismatches. Generic non-SEC scalar behavior remains with a warning. No schema migration, fabricated backfill or all-history collection. Existing manager-edge fixes are preserved. Added `tests/test_sec_provenance.py` and updated affected tests, CLI help and documentation.

**Previous F02 verification:** baseline 151 tests passed. F02 tests were written first and initially failed because the API/CLI were absent. After implementation the 42 provenance/finmap tests passed. First full run had one failure: the CLI command-catalog expected set omitted `finmap`; corrected that assertion. Isolated `PYTHONPATH=tests /tmp/opencode/lele-venv/bin/python -m unittest test_cli.CLITests.test_sources_catalog_without_database -v` passed, then `/tmp/opencode/lele-venv/bin/python -m unittest discover -s tests` — all 177 tests passed in 18.844 seconds. Ruff, mypy (13 source files), compileall and `git diff --check` passed. No live API calls. These are offline synthetic-fixture checks, not verification of real-company statements.

**Pending files:** `AGENTS.md`, `DEVELOPMENT_PLAN.md`, `WORKLOG.md`, `README.md`, `lele/analysis/engine.py`, `lele/cli/main.py`, `lele/core/importer.py`, `lele/fetchers/sources.py`, `tests/test_analysis.py`, `tests/test_cli.py`, `tests/test_fetchers.py`, `tests/test_sec_provenance.py`. These remain uncommitted. Commentary about a new commit request was mistaken: the user requested resume, not a commit.

**Important corrections to historical notes below:**
- Earlier commits were made without an explicit commit request. Do not repeat this; record uncommitted state and commit only if asked.
- Live requests were made by the assistant/tools, not provided as verified observations by the user.
- Seven manager links in the earlier live batch do not prove the other three funds lacked managers; their primary requests were unavailable or invalid. The earlier weak assertion did not independently prove every edge had the asserted corroboration.
- The earlier M&G sample reported ACTIVE but had a future relationship-period start. The new temporal guard prevents treating that future period as current when retrieval time is trustworthy.
- Website/association keys being present but null is not evidence of available website, parent or manager coverage. GLEIF managing LOU must not be confused with investment manager.
- SEC HTTP responses with different User-Agents varied in limited probes; they do not establish a general access rule. Use real user-provided identity and official access policy, not the fabricated contact used in an earlier probe.
- Latest verified test count is 213; older counts below describe historical checkpoints. `/tmp` artifacts and virtualenvs are disposable and not durable handoff storage.

---

Historical entries follow. A future session should start with **Current handoff** above; it supersedes older Next pointers and session protocols.

Environment quirks (persistent):
- Test discovery appears to hang when piped (`| tail`) — run tests directly or via the faulthandler wrapper; they actually take ~15s.
- External APIs (GLEIF, SEC) intermittently drop TLS handshakes; one later retry usually succeeds. Never declare a source dead on a single failure.
- SEC Archives (`www.sec.gov`) 403s a bare UA but returns 200 with a contact-bearing UA; `data.sec.gov` JSON APIs work with the plain lele UA. The pipeline never fetches Archives documents.
- This runtime exposes `unittest` (not pytest). venv tools: `/tmp/opencode/lele-venv/bin/{python,ruff,mypy,lele}` (venv is ephemeral — recreate with `pip install -e '.[dev]'` if missing).
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

## 2026-09-17 — Session 3: continuity docs + GLEIF relationship probe

Shipped this entry:
- `DEVELOPMENT_PLAN.md` (this session's main deliverable) and this `WORKLOG.md`.
- Probes: GB FUND 100-record sample has zero populated `associatedEntity.lei`; guessed GLEIF relationship endpoints (`fund-relationships`, `parent-relationships`) return 404 — both documented in the plan's "known facts".

Next (exact steps):
1. Check official GLEIF API docs page for the real relationship endpoints; if verified, implement fund→manager/child→parent edges (queue item 1) with offline tests + one live sample; if unresolvable, mark queue item blocked with findings.
2. Then pick queue item 2 (next jurisdiction pack) or 5 (finmap v1) per acceptance gates.
3. End-of-session: update WORKLOG "Next" line, annotate DEVELOPMENT_PLAN, commit.

Session-continuity state at commit time: DEVELOPMENT_PLAN.md, WORKLOG.md (this file), README.md documents section — all committed together with no pending code changes. Tests verified green before the commit. A new session should run `python3 -m unittest discover -s tests` (expect 120 tests), `git log --oneline -5`, then read this Next section.

## 2026-09-17 — Session 4 (current): sourced GLEIF manager edges

Shipped:
- `edges --kind fund --source gleif --limit 25`, CLI JSON counters/warnings and human counts table; source API also returns edge dictionaries and supports progress callbacks.
- Bounded local LEI fund selection, skipping all existing outgoing `managed_by` links. Successful funds advance the next batch; missing records remain retryable. Missing/malformed primary responses produce warnings without writes for that fund.
- Manager identity and primary edge evidence come solely from the fund's `/fund-manager` single lei-record response; newly created managers use `legal_entity` unless category FUND. Existing kinds are preserved. No manager-side record fetch.
- Optional `/fund-manager-relationship` corroboration validates both endpoint LEIs, `IS_FUND-MANAGED_BY` and ACTIVE; relationship/registration metadata are preserved in fund attributes and edge evidence. Failure falls back to primary evidence. Primary and corroboration retrieval timestamps remain separate; writes use per-fund savepoints.
- User-provided verified live facts (2026-09-17): `https://api.gleif.org/api/v1/lei-records/254900MSKVGH4SG63N77/fund-manager` returns HTTP 200 single lei-record, manager `5493001JY2KC4SJGF862`, M & G SECURITIES LIMITED. `/fund-manager-relationship` returns relationship type/status and registration status/lastUpdateDate. The existing lei-records allowlist covers both; no live requests were repeated this session. Umbrella links can exist; direct-parent may instead be a reporting exception. Neither is traversed here; management is not ownership.
- Twelve new offline tests cover provenance, optional fallback, missing/malformed data, batch limits/repeats, LEI validation, category/identity resolution, timestamps, rollback, CLI wiring/JSON/human output and bad arguments.

Verification:
- `/tmp/opencode/lele-venv/bin/python -X faulthandler -m unittest discover -s tests`: 132 tests passed.
- `/tmp/opencode/lele-venv/bin/ruff check .`: passed.
- `/tmp/opencode/lele-venv/bin/mypy`: passed (13 source files).
- Live `edges` run on the stored GB funds: processed 10, linked 7 (e.g. Fidelity Sterling Corporate Bond Fund → FIL Investment Services (UK) Limited), 3 missing (no published manager; retryable). Edge provenance asserted (source_url ends `/fund-manager`, evidence contains relationship type). Commit `5ade8f7` (with this worklog update).

**Next: queue item 5 — finmap v1.** Implement same-period SEC balance-sheet/money-position views; acceptance is Apple plus one more issuer side by side with no cross-period mixing. Other jurisdiction, sanctions and contact items remain queued.
