# Lele audit

A review of the whole tree: what the tool is for, whether it can do it, every
defect found, and what was changed. Written after the code, not instead of it.

Each finding below is either **fixed** with a test that fails without the fix,
or **recorded** as open with the reason it is still open. Nothing is left as
"should be looked at".

---

## 1. What this tool is

Two things live in one codebase, and conflating them is the single biggest
source of confusion in the history documents.

**A public-source research registry.** Ingest institutions, identifiers and
filed observations from official APIs; keep every claim attached to a source
URL, a retrieval time and the reason it is believed; never present a sample as
a complete population; never infer an organisation's website, a manager, a
motive or a money flow from anything other than a record that says so. This is
achievable, and a large part of it is built.

**A market-analysis harness.** Store price history, detect moves, look for
conditions that preceded them, and record prospective forecasts in a
hash-chained ledger before the outcome is known. This is achievable as
*apparatus* — a thing that can accumulate honest evidence about a question.
It is not achievable as *answer*, for the question the docs ask.

## 2. Can it meet the stated objective? No, and no engineering changes that.

The standing objective, recorded in `DEVELOPMENT_PLAN.md`, is **90% accuracy on
5-minute-ahead price direction across bitcoin, gold, oil and stocks**.

That number is not reachable. At a five-minute horizon on liquid markets, the
next bar's direction is very close to a coin flip with a small drift; a 90%
directional hit rate would require an information source that does not exist
in public data. No amount of implementation quality changes this. The
prose in the project's own documents already concedes the point, and the
recorded live results are honest negatives: stablecoin supply and the fear and
greed index carry **no** discriminative power for pre-move context at any
window length, and the promising-looking result that preceded it was a coverage
confound, correctly identified and removed.

Three things follow, and they are the honest deliverable.

1. **The number must not move.** `lele/scripts/realtime_prospective.py` had
   a pre-registered `target_rate` of `0.5` hard-coded while the documented
   objective was `0.9`, and it listed only `bitcoin` among the required asset
   classes. That is precisely the failure mode the whole prospective harness
   exists to prevent: lower the bar, meet the bar, report success. It is fixed.
   The target is `0.9` in one place, every named class is required, and
   `tests/test_realtime_prospective.py` fails if either drifts.

2. **The apparatus must be trustworthy.** Everything below is about that. A tool
   that reports a spurious result is worse than no tool, because the spurious
   result gets believed.

3. **The registry mission is the achievable part**, and it is what a user should
   rely on today. The analysis is instrumentation for a research question, not a
   source of predictions.

There is a second, narrower objective that *is* reachable and is worth
separating out: for a measured historical series, "find every move above 3% with
its preceding conditions, correctly windowed and honestly bounded" is a
data-processing task. That part now works, and `tests/test_price_gaps.py` pins
the part that was silently wrong (see A4).

---

## 3. Defects found and fixed

### A. Storage and transactions

| # | Defect | Consequence | Status |
| --- | --- | --- | --- |
| A1 | Every command opened the registry by replaying the whole 27-table schema inside `BEGIN IMMEDIATE` | `lele list` **wrote to the database**: created the file if absent, took a write lock, and grew it by a page. Reads could not run on a read-only mount, and concurrent processes serialised on the lock. | fixed |
| A2 | 12 `conn.commit()` calls scattered through analysis and fetcher modules, alongside `registry.get_conn`'s own commit | A failure mid-run left half a fetch committed. Ownership of the transaction was ambiguous. | fixed — `get_conn` is the only owner |
| A3 | A migration that hit orphaned rows failed with an opaque `IntegrityError`, and because the migration is one transaction, **every later command failed too** — the registry was unopenable with no recovery path | bricked database | fixed — orphans are detected first and named, with the `PRAGMA foreign_key_check` to run |
| A4 | Move detection measured a return as the difference between two closes a fixed number of bars apart, with no check that the bars were contiguous | A "24-hour move" could silently span eight days, and was then counted, compared against a baseline built on the same distorted series, and stored | fixed — cadence is measured, gaps are found, and any window that spans one or whose real elapsed time is not the requested horizon is excluded and reported |
| A5 | `read_connect` reported `lele_readonly = False` even on a successful read-only open (the success branch that set it had been lost) | the read-only guarantee was nominal | fixed, and the fallback now records why it was used |
| A6 | The read-only open assigned a second connection over the first without closing it | a leaked handle per fallback | fixed |
| A7 | `sqlite3.Connection.__enter__` manages transactions, not lifetime; several sites used it as if it closed | leaked handles, silent growth in a long cron run | fixed — `closing()` and `get_read_conn` |
| A8 | `conn.total_changes` was used to count inserted rows in five places | it is cumulative for the life of the connection, so after the first insert **every** attempted row counted as inserted, including ignored duplicates. Reported counts were wrong. | fixed — `cursor.rowcount` |
| A9 | `moves` and `causes` and `rag` and `indicators` opened hand-built `mode=ro`/`mode=rw` URI connections, inconsistent with everything else | inconsistent errors, and a place to forget foreign keys or busy timeouts | fixed — all connections come from `core/db.py` |
| A10 | `add_edge` silently did nothing when `src_id == dst_id` or an id was falsey | a lost relationship with no error | unchanged, deliberately: a self-edge and an unknown id are different cases and both are already refused upstream; noted rather than changed because the callers rely on the current behaviour |
| A11 | `doctor` reported a schema version but not whether the promised tables and indexes were actually present | a partially restored database reported `ok` and then failed later with "no such table" | fixed |
| A12 | `":memory:"` was passed through `abspath` | a real 370 KB file called `:memory:` appeared in the working directory, so every assertion of the form "no database was created" was quietly false wherever a test used it | fixed |

### B. Network

| # | Defect | Consequence | Status |
| --- | --- | --- | --- |
| B1 | Cache filename was `sha256(url) % 128` | every request landing in the same slot overwrote another's entry; the cache could never hold more than 128 series, so most hits were misses and repeat runs re-downloaded | fixed — name derived from the whole URL, sharded by prefix |
| B2 | Pacing held one process-wide lock **while sleeping** | a five-second wait for GDELT stalled every unrelated request in the process, and two clients corrupted each other's timestamps | fixed — one lock per host |
| B3 | `get_text` never stamped a retrieval time | provenance recorded from a news feed was the time of some earlier, unrelated request | fixed — every response path stamps |
| B4 | `download` wrote directly to the target path | a partial write left a truncated sanctions list or filing index that the next run read as if it were the whole document | fixed — temporary file plus rename |
| B5 | Three near-duplicate request paths with slightly different retry, deadline and byte-limit behaviour | the guarantees applied to some sources and not others | fixed — one `_request`, one `_read_body`, one retry schedule |
| B6 | Retry backoff was `1s, 2s` regardless of the host's own minimum interval | a provider that answered "slow down" was hammered three times in three seconds | fixed — doubling backoff, never below the host interval, honouring `Retry-After` |
| B7 | A compressed response was refused outright | any provider ignoring `Accept-Encoding: identity` was unusable | fixed — bounded `gzip`/`deflate` decode; unknown encodings still refused |
| B8 | `opensky` reached into the client's **private** pacing state and copied its whole read loop | bypassed retries, decompression and the deadline; broke outright when the internals changed (it did, during this work) | fixed — `HTTPClient.request_json(url, headers=...)`, one path |
| B9 | `parse_un_consolidated` had no DTD guard while its two sibling parsers did | Python's expat expands internal entities, so a small declarations-only document is a memory-exhaustion bomb, and the bytes come from a third party | fixed — one `reject_dtd` used by every XML path |

### C. Correctness of the analysis

| # | Defect | Consequence | Status |
| --- | --- | --- | --- |
| C1 | `history` reported `"currency": "USDT" if symbol == "BTCUSDT" else "USDT"` | a conditional with two identical arms, implying a per-symbol distinction that does not exist | fixed — stated once, plainly |
| C2 | `history` and `moves` both reported `all_history: False` — a constant | a field that looks like a coverage claim and can never be anything else | replaced with measured cadence, gap count, contiguous fraction, and an explicit `completeness: unknown` |
| C3 | Three modules caught `Exception` and `continue`d | a `TypeError` in the loop body silently dropped the row, and the count still looked complete | fixed — `sqlite3.Error` is caught, counted, and raised, because "could not be written" is a different claim from "does not exist" |
| C4 | `_selection` in `causes` defined a closure over a variable rebound on each loop pass | correct today, and one edit away from not being | rewritten as two explicit checks |
| C5 | `"null"` used as a JSON key for the permutation null | reads as JSON `null` beside JSON `null`s | renamed `permutation_null` |
| C6 | A duplicated word in a `causes` docstring | — | fixed |

### D. Time

| # | Defect | Consequence | Status |
| --- | --- | --- | --- |
| D1 | ~90 call sites read the system clock directly, deep inside functions | the same stored data analysed tomorrow gave a different answer; a test written against a fixed date quietly changed meaning when that date was reached. The suite had **already** been bitten — `test_crypto_context` was failing when this work started, and `test_rag` had failed the same way earlier. | fixed — one `core/clock.py`, `clock.now()` everywhere, `freeze()` for tests, `LELE_NOW` for reproducible scripts |
| D2 | Provider reach, control-window floors and "not in the future" refusals read the clock internally | a historical run could not be reproduced | fixed — explicit `now` parameters on `moves.windows` and `signals.news_signals`; `_sentiment_observation` and `_stablecoin_observation` take the instant the read was made |

This is the defect class that most directly produced the visible red test. A
suite that depends on today's date is a suite that reports a failure it did not
find, and trains the reader to ignore red.

### E. The prospective runner

| # | Defect | Consequence | Status |
| --- | --- | --- | --- |
| E1 | Hard-coded absolute path to one home directory | the runner read and wrote one person's registry regardless of `LELE_DB` or `--db`, and failed confusingly for anyone else | fixed — the path is a parameter |
| E2 | Pre-registered `target_rate: 0.5` against a documented objective of `0.9`, and `required_asset_classes: ["bitcoin"]` | a run could declare the target met on a single class and a lowered bar | fixed — `0.9`, all four classes, asserted by a test |
| E3 | `preregister` passed `force=True` | discarded a run in progress and its hash chain | fixed — refused unless asked for |
| E4 | The five "unit tests" for this module hit the network and a real home-directory database | they only passed on the machine that wrote them | rewritten fully offline |

### F. Quality gates that did not gate

| # | Defect | Consequence | Status |
| --- | --- | --- | --- |
| F1 | `ruff` was configured with `select = ["E4", "E7", "E9", "F"]` | syntax errors and unused imports. "All checks passed" said almost nothing. | fixed — 18 rule families, each ignore carrying its reason in a comment |
| F2 | `mypy` ran with `check_untyped_defs` only, and 542 of 540 functions had no annotations | a type error in an unannotated module was invisible | fixed — the foundation modules (`core/db`, `core/clock`, `core/schema`, `fetchers/http`) are now required to be fully annotated; the historical debt is stated, not hidden |
| F3 | `pyproject.toml` was not valid TOML | `lint.per-file-ignores` as a bare dotted key with a hyphen. Only ruff's lenient parser accepted it; `mypy` and any strict reader rejected the file outright | fixed — proper `[tool.ruff.lint.per-file-ignores]` section |
| F4 | No single command ran everything | four commands in three documents, none of which failed the build | fixed — `.tools/check.sh` |
| F5 | Five `except Exception: continue` and a bare `except:` idiom, unchecked | — | covered by the gate |
| F6 | Enabling strict typing immediately found two real bugs: the `opensky` private-attribute reference (B8) and a `Path \| None` division in the cache path | — | fixed |

### G. Diagnostics

| # | Defect | Consequence | Status |
| --- | --- | --- | --- |
| G1 | Every failure printed one of five generic strings, exit code 1 | a bug, a bad parameter, a dead provider and a locked database were indistinguishable | fixed — the failure is classified (missing registry, read-only mount, lock, integrity, network, bad data, unexpected defect) with a next action, and the message **never echoes the exception**, because an exception can carry a token, a password or a request body |
| G2 | No way to get a traceback | debugging was guesswork | fixed — `LELE_DEBUG=1` |

The no-echo rule is load-bearing and the existing suite already enforced it
(`test_errors_do_not_leak_exception_secrets`). An earlier version of this work
broke that test; the rule won.

### H. Dead ends and placeholders removed

- `moves.current` and `moves.detect` each rebuilt a 20 000-bar series and a
  return series on every call. Unchanged for now, but the cause is recorded:
  the bar window is derived once per run and gaps are precomputed rather than
  rescanned per candidate.
- `history.fetch_price_history` carried an `already_covered` counter and a
  `floor` guard that can never fire, because `endTime` is set one bar before
  the oldest already seen. Removed.
- `causes.profile` reported `"moves_with_no_categorized_reason"` and
  `"moves_with_no_reason"` as two different counts of the same thing. Left
  alone deliberately: they are both in the output contract and both are correct
  as named.

---

## 4. What is verified

```
$ .tools/check.sh
all checks passed
```

- **1013 offline tests**, `ResourceWarning` promoted to an error, so a leaked
  database handle fails the run. Last run 2026-10-03.
- `ruff check` clean over `lele/` and `tests/` under the strict profile.
- `mypy` clean over **61 source files**, with `disallow_untyped_defs` enforced on
  the foundation modules.
- `python -m compileall -q lele tests` clean.
- Live end-to-end on 2026-09-28: `init` → `fetch fdic` (50 institutions) →
  `fetch-history 1 BTCUSDT` (799 real daily bars) → `moves` (124 candidates,
  100 retained, all exactly 24.0000 h, 0 gaps) → `scan` and `causes context`.
  Where nothing is stored, the output says so rather than inventing a result.
- Live end to end on 2026-10-03 for retention: `prune plan` and `prune apply`
  against a **copy** of the §12 registry, and `explain` afterwards for a window
  inside the removed period (§15.7).

New test files, each of which fails without its fix:
`tests/test_db_guarantees.py` (19), `tests/test_clock.py` (12),
`tests/test_price_gaps.py` (9), `tests/test_window_queries.py` (35),
`tests/test_window_cli.py` (20), `tests/test_retention.py` (44),
`tests/test_capability.py` (31), `tests/test_context_measures.py` (50), plus
rewritten `tests/test_realtime_prospective.py` (8, offline).

---

## 5. One incident, recorded

While refactoring `fetchers/opensky.py` I wrote the file back without newlines
and destroyed it. The file was untracked, so git had no copy. It was recovered
from the `.pyc` left by the previous successful `compileall`, reconstructed,
and then **verified against that compiled original by comparing disassembly**:
`_iso_date` and `_observation` are byte-identical, and the two fetch functions
differ by exactly three instructions each — the removal of a function-local
`import hashlib` that is now at module level. `_fetch_with_auth` was replaced
intentionally, 35 duplicated lines that bypassed the shared request path
became one call to `client.request_json`.

The lesson is in the check script: `compileall` runs before the tests, so a
syntax error is the *first* thing noticed, not the last.

---

## 6. Asking about a window, and tracking capital — added after the audit

The audit answered "is it sound". A follow-up question asked whether the tool can
actually do the thing it exists for: *find the events or records that could bear
on a price move, for any given window, and track money flow.* It could not, and
three specific things were missing.

**1. Every window command demanded a file the user had to make.** `events`,
`episodes` and `volatility-analyze` all took a positional `PRICES_JSON`, even
though the tool had been storing OHLC bars in `price_bars` for months. Two
parallel paths to the same measurement, and the one a user is asked to use
required them to hand-assemble an input the tool already had.

**2. Nothing accepted a time range.** The question "what is recorded between
these two instants" had no query behind it. `list_observations` took an entity
and a limit; the window was something a caller filtered for themselves after
pulling a bounded prefix.

**3. Nothing was ever persisted from the futures evidence.** `fetch-evidence`
read open interest, funding, long/short ratios, taker flow and book depth from
Binance and wrote a file, then stopped. So the closest thing to real positioning
data for an asset could be fetched but never asked about again — and "what was
leverage doing in the week the price fell 8%" was unanswerable.

### What was added

| Command | What it does |
| --- | --- |
| `explain ID --from --to [--pre-hours] [--interval]` | The whole window answer: price measured from stored bars, threshold episodes, stored moves by tier, and every stored record in the window and in the hours before it, each with source, URL and availability time. Then a coverage block naming what is missing and the command that would fill it. |
| `capital ID --from --to` | The capital and positioning view: every measured series in the window, with the change over the window, and a label for each saying what the number actually is. |
| `store-evidence ID SOURCE SYMBOL` | Persists the futures evidence as observations so it becomes queryable by window, without changing a single number. |

And the time-window reads they are built on: `observations_in_window`,
`observation_coverage`, `observation_kinds_ever`, `anomalies_in_window`,
`volatility_in_window`, `stored_bar_span`.

### The three properties that make the answer trustworthy

**Nothing is ever called a cause.** Every record carries `is_cause: false` and a
`relation` that can only be `coincident` (inside the window) or `precursor`
(before it). A timestamp can establish ordering; it cannot establish a
mechanism. The report says so in a `what_this_is` block that no code path can
overwrite.

**Absence is reported as absence.** A channel with no records is
`no_coverage` and carries the command that would fill it. A kind that was never
stored appears in `coverage.never_recorded`, which is different from a kind that
was stored and had nothing in the window. "Nothing was recorded" and "nothing
happened" are different claims, and only the first is knowable from a registry.

**A number says what it is.** `capital` labels every series. A stablecoin
supply total reads "a supply total, not a net flow into this asset". Market
capitalisation change reads "rises with price as well as with demand, and cannot
separate buying from selling, new capital from rotation, or spot from derivative
notional". Two classes of number are refused a percent change, because both
produced a figure that looked like a result and was not.

* A **bounded unit**: 74 to 75 on a 0–100 index is not "1.4% more of anything".
* A **per-interval delta**: `order_flow`, `open_interest` and `market_activity`
  all report a change over their own interval, not a level, so the percent
  change between two arbitrary intervals is a ratio of near-zero numbers. These
  report `net_over_window` instead, which is the sum and the quantity of
  interest.

On the live window that turned a meaningless "+445.88%" into a **net taker flow
of -404,170,647 USDT over 172 five-minute intervals**, and a meaningless
"+265.27%" into a **net open-interest change of -37,282,118 USDT**. Both are real
numbers about what participants did; the percentages they replaced were neither.

A channel with nothing in it
names three things this build genuinely cannot produce: net exchange flow for a
crypto asset, investor-level flow for a listed equity, and order-level
attribution.

### What it still cannot do

1. **Historical attribution across years.** The public headline indexes reach
   back days, not years, and there is no licensed archive wired. A 2021 window
   returns 0 records and says so; it does not invent a reason. This is a data
   limit, not a code limit, and no amount of building removes it.
2. **Causation.** Not available from these sources, and the output is built so
   it cannot be misread as available.
3. **Non-crypto assets end to end.** `fetch-history` is Binance-only, so a
   listed equity has no stored bars and therefore no measured window. The SEC
   extractors store filings, insider trades and holdings, which `explain` reads
   and reports — but there is no equity price history behind them yet. This is
   the single largest remaining gap and it is the obvious next build.
   **Partly closed 2026-09-28 — see §8: `import-history` gives a non-Binance
   instrument stored bars and therefore a measured window, but the *source* is
   the user's export rather than a wired provider, because no free keyless daily
   OHLC source permits automated use.**
4. **Attribution quality, not just availability.** Even where records exist,
   `explain` reports coincidence. Whether a policy publication preceded a move
   and whether it caused it are different questions, and only the first is
   answerable here.

---

## 7. Still open, with reasons

Each item is a limit this build cannot lift, with the reason it is still there.

1. **The 90% five-minute direction objective.** Not attainable at that horizon
   from public data. Not an engineering task, and the target is never lowered to
   make a number pass.
2. **Causation.** Unavailable from these sources. The output is built so it
   cannot be misread as available: nothing is ever marked `is_cause`.
3. **Historical attribution across years.** The public headline indexes reach
   back days, not years, and no licensed archive is wired.
4. **Market-price coverage outside Binance spot.** No free, keyless daily OHLC
   source permits automated use, Yahoo is unsupported, and the keyed providers
   need a real key. `import-history` takes the export from the user instead.
5. **Provider health.** `ingest_runs` records request and record hashes; nothing
   consumes them, so a source returning garbage is noticed by a human.
6. **Unbounded growth.** **Closed for price history 2026-10-03** (§15):
   `price_bars`, `move_events`, `volatility_estimates`, `cause_scans` and
   `price_anomalies` can be cut by a recorded retention cut. Still open for
   `context_measures`, which is bounded by its parent series and therefore by how
   far a provider reaches, and for the observations themselves: cutting filed
   evidence is a decision about evidence that this build will not take
   automatically.
7. **Type coverage.** The gate requires full annotations on the foundation
   modules and prevents the debt growing there; the historical modules still
   carry bodies that are checked only by execution. **Measured 2026-10-03: 612 of
   743 functions in `lele/` still lack a complete signature** (`ast`, counting
   any unannotated parameter or return). The earlier figure of 542 predates the
   modules added since.

---

## 8. A non-Binance instrument, and two gates that were not gating

Added 2026-09-28, after §6.

### The gap was rights, not code

§6 listed "non-crypto assets end to end" as the largest remaining gap, because
`fetch-history` reaches only Binance spot pairs: a listed equity had stored SEC
filings, insider trades and holdings that `explain` reported, and no price to
measure any window against.

Reaching for a provider is where this stopped. Each candidate fails on rights,
not on reachability — TLS to all of them was verified from this host:

| Candidate | Finding |
| --- | --- |
| Yahoo chart API | Already recorded in this project's own inventory as unsupported with license unverified. Not cleared for stored history. |
| Stooq | Free, keyless, daily OHLC. Probed on 2026-09-28: **every** request, including the plain `q/d/l` CSV download, returns a JavaScript proof-of-work browser check whose `/__verify` step exists to keep non-browser clients out. Passing it would be defeating an access control, so it is recorded as blocked and no bypass was attempted. |
| Alpha Vantage, FMP, Polygon, Twelve Data, EODHD, Nasdaq Data Link | Clear terms, but each requires an API key — the same rule that leaves FEC and FRED unwired. No contact address was invented to obtain one. |
| CME, ICE, LBMA, licensed vendors | Correct data, licensed and paid. Not to be scraped around. |

So the provider is the user. `import-history ID PATH [--interval]` stores a
bounded OHLC export they took from a source they may use, and makes no network
call at all. 33 offline tests in `tests/test_price_import.py`.

### What it does not take on trust

- **Validation precedes every write.** A file with one bad bar stores nothing.
  The tests put the bad row *last*, so a validator that only inspected the
  opening bars would fail them.
- **Timestamps are canonicalized to UTC before the idempotency key is taken.**
  `price_bars` is keyed on open time as text, so one session written as
  `09:30-05:00` and as `14:30Z` would otherwise be two rows and a re-import would
  duplicate rather than refine. Verified load-bearing: removing the
  canonicalization fails that test.
- **`adjustment` is required, not inferred.** A split repairs nothing and
  silently corrupts every return that crosses it, so it must be stated
  `adjusted`, `unadjusted` or `unknown`, and `unknown` is reported as a known
  unknown.
- **Absence stays absence.** An omitted venue, asset class or rights basis
  appears in `unknowns` and is stored as `unknown`; a symbol is never used to
  guess a venue, a currency or an asset class.
- **Cadence is measured, gaps are counted.** An equity's daily bars are not one
  day apart throughout — weekends and holidays alternate the spacing between one
  day and three — so the report gives the measured dominant spacing and the hole
  count. This is what lets `moves` exclude a window spanning a weekend instead
  of reporting it as a 24-hour move.
- **A bar that has not closed is refused.** `close_time` in the future is not a
  measurement; when `close_time` is omitted it is derived from `open_time` plus
  the interval and the stored row records that it was derived.
- **Nothing is verified.** The report carries `rights_verified: false`
  unconditionally, and the report's `unknowns` and `limitations` name what it
  cannot establish. A test asserts the module holds no HTTP client, so the
  no-network claim is a property rather than a promise.

Verified end to end offline: import 30 daily bars for an equity, then `moves`
measures the −14% session as one 24-hour move with cadence 86400 and zero gaps.
Not verified live, because it takes no network path and there was no user export
to run. **Not achieved:** the data half still needs a licensed source or a user
export, and no bar from this path has been checked against an independent price.

### Two gate defects, the same class as §3F

The audit already found once that the gates were not gating. Two more instances,
found while doing the above:

1. **The bytecode step had been a silent no-op since the rename.**
   `.tools/check.sh` ran `compileall -q finworld`, and the rename had already
   deleted that directory. `compileall -q` on a missing path prints
   `Can't list 'finworld'` and **exits 0**, so the gate reported success while
   checking nothing — which is precisely the lesson it was added for, since
   `compileall` running before the tests is what makes a syntax error the first
   thing noticed. It now compiles `lele` and `tests`, and
   `tests/test_gate.py` fails if the named target is not the importable package.
2. **The gate was not in version control.** `.gitignore` excluded `.tools/`, so
   the one command that decides whether the tree is shippable existed only on the
   machine that wrote it, and the fix above could not have survived a clone. The
   ignore is now `.tools/*` with `!.tools/check.sh`, and a test asserts both
   that the gate exists and that it is re-admitted.

`tests/test_gate.py` (6 tests) pins: every gate step names an existing target;
the compileall target is the importable package; no step acts on an undeclared
directory; `compileall` exits 0 on a missing path, so the no-op cannot re-arm
silently; the gate never names the previous package; and the gate is
version-controlled. Three of them fail against the pre-fix script, verified by
reverting it.

---

## 9. One hang, cause narrowed

`AGENTS.md` recorded that prior `| tail` pipelines hung in this environment and
that "the cause was never established". It is now narrowed, which is enough to
act on. `.tools/check.sh --fast 2>&1 | tail -20` blocks indefinitely while the
identical unpiped command finishes in about nine seconds: `time` shows the work
completes — user time matches the unpiped run — and then the read end waits for
an EOF that never arrives, and `ps` shows no stray child left behind by the gate.
The host runs this distribution under **PRoot** on Termux, whose ptrace-based
syscall interception is the remaining suspect; a plain `tail | sed` on a file is
fine, so it is the script-plus-children case rather than pipes in general.

The operational rule follows from the evidence rather than from the mystery:
redirect to a file and read the file, do not pipe.

## 10. Schema v17 — what a column name claimed, and what a command could never find

Three defects, all of the same species: a name or a query that asserted more than
the code behind it supported. Found while adding the volatility layer.

### A misnamed column, and a field that was never volatility

`move_events.realized_volatility_percent` held `(high - low) / low * 100` for the
**terminal bar of the move**. That is a bar's range as a percent of its low. It is
not a volatility estimate, it was never one, and the column name claimed a quantity
the computation did not produce — in a table called `move_events`, in a tool whose
whole subject is measurement.

Renamed to `terminal_bar_range_percent`. The value was correct under the correct
name, so the migration **carries it across unchanged** rather than recomputing, and
records the rename in each row's `evidence` plus a note in `meta.migration_notes`.
A column rename that leaves no trace is indistinguishable from a recompute, and
`tests/test_instruments.py` asserts the carried value and the note. Adding a real
estimator beside a field that lied about what it held would have been worse than
having neither.

### `rag detect-anomalies` could only ever return zero

`rag.detect_price_anomalies` queried `observations WHERE source = 'price'`. **No
fetcher in this project has ever written such a row.** The query could not match,
so the command reported zero anomalies from a data set that did not exist. Its test
passed, because the test *manufactured* `source='price'` rows by hand — it pinned the
implementation instead of the behaviour, which is the failure mode a test written
alongside the code rather than against it invites.

It had two further problems, both of which would have survived being merely wired
up: it z-scored **price levels**, which is not a statement about unusual movement
because prices are not stationary, so a rising asset is "anomalous" forever; and it
built its baseline from the **same window it was judging**, which deflates the
spread and compresses the score toward zero.

Rewritten to read `price_bars`, to compare **returns over a window**, and to build
the baseline from moves strictly **before** the window. The instrument list now comes
from `price_bars` rather than from the column nothing wrote. This also gives
`price_anomalies` its only writer, which matters beyond the command: `rag
event_indicator_v1` weighted an anomaly component that was structurally always zero,
so its composite score silently covered two thirds of what it claimed.

### `causes.scan` raised on entry and had no caller

`causes.py` passed `medium_percent` where `moves.validate` expects a *sequence* of
thresholds, so every argument after `move_hours` landed in the wrong slot and the
function could only ever raise `ValueError`. It had no caller and no test, so
nothing noticed. Corrected, and given three tests; the first was confirmed to fail
against the pre-fix code.

### Two defects the new code introduced, caught before merge

Both were found by running the commands rather than by the unit tests, and both are
now pinned by tests that would have caught them.

- **A `Decimal` in a JSON report.** The new helpers quantized a `Decimal` and
  returned it. Every unit test compared values in Python and passed; the first
  `lele volatility` a user ran raised `TypeError: Object of type Decimal is not JSON
  serializable` from inside the encoder. `tests/test_instruments.py::SerializationTests`
  now serializes every report variant with `allow_nan=False`.
- **Text compared against a float.** Severity is derived from a reported percentile,
  which is text so the report survives `json.dumps`. Comparing that text to `99.5`
  raised `TypeError` on the first command run against data containing an anomaly.

The general lesson is the one this project keeps relearning: **a test that asserts a
value is not a test that asserts a contract.** Serializability, precision and
convention are contracts, and the arithmetic tests alone would not have found either.

### What the volatility layer adds, and what it deliberately does not

Nine realized-volatility estimators, all `Decimal`, each storing the convention that
produced it. `lpv` is the default because the R² of every range estimator against
next-period realized volatility is nearly identical (43–46%); the high/low data buys
calibration, not prediction.

What is **refused rather than approximated**, and why each refusal is the honest
answer:

| Refused | Because |
|---|---|
| a window spanning a missing bar | it covers more elapsed time than it names — the original defect A4, now inherited deliberately |
| a non-positive Garman-Klass or Rogers-Satchell total | those per-bar terms are signed; clamping to epsilon reports an invented volatility |
| an all-identical-price window | a zero variance is not a volatility of zero to divide by later |
| realized kernel, bipower jump detection | both need an intraday grid; on daily bars they are unavailable, not approximable |
| an annualization with no recorded asset class | a guessed 252 is a plausible-looking wrong number |

Two traps are pinned by tests because they are commonly implemented wrongly:
**Bollinger uses the population deviation (n), not n−1**, contradicting the
demeaned close-to-close estimator's n−2, which is why there is one `variance()`
taking an explicit `ddof` rather than one shared default; and **`natr` is not
invariant to an additive splice** while absolute `atr` is, which matters for any
spliced futures or crypto series.

## 11. Allocation frameworks, recorded as citations and not as advice

The question "how would a trader or a multi-millionaire trade this asset" has a bad
answer, and the honest answer is a research finding rather than a recommendation.
`lele/analysis/framework_notes.py` holds the documented positions, each with a
primary source, an evidence grade, and **the documented criticism of it** — an entry
without a counter-result would be marketing.

The excluded list is longer than the included one and is the more useful half: it
records claims this project declines to assert because no primary source was reached.
These circulate with citations attached and are therefore the most likely to be
wrong: the Paul Tudor Jones 1/5/6 rule; "volatility-managed portfolios doubled the
Sharpe ratio" (the abstract says Sharpe ratios increased — the specific multiple is
in tables not read); "halving the equity allocation doubles the required return"; the
Linder/UBS diversification paper; Jay Cooke's private-equity multiples; Bogle's
Sharpe difference; the Greenwald, Bernhard, Damodaran and magic-formula material;
and unsourced quotations from Buffett, Munger, Soros, Dalio and Klarman.

Two corrections to the project's own earlier assumptions belong here:

1. **The pattern-day-trader rule no longer exists as recorded.** FINRA Regulatory
   Notice 26-10 (effective 2026-06-04, underlying SEC order 91 FR 20731) replaced the
   day-trade count requirements and the $25,000 minimum equity requirement "in their
   entirety", phasing in intraday margin level standards to 2027-10-20. Nothing in
   this project hard-codes the old threshold, and the notice is recorded so it is not
   reintroduced.
2. **Yahoo Finance is not merely "unsupported" — it is explicitly prohibited.** ToS
   §4 forbids collecting data "using any automated means, devices, programs,
   algorithms or methodologies ... for any purpose without our express, prior
   permission", and separately forbids building a competing aggregated data source.
   The `yfinance` documentation itself says the API is "intended for personal use
   only", which is a disclaimer of liability rather than a grant of permission. The
   earlier audit entry understated this.

Neither correction changes what this project does; both change how firmly it can say
why. The 0.9 five-minute objective stays exactly where it was, recorded as an
aspiration rather than a gate: a target that cannot be met provides no gradient and
is precisely the kind of constant that gets quietly lowered.

## 12. Re-establishing M04/M05, and the two defects the re-run found

`DEVELOPMENT_PLAN.md` opened with one item ahead of everything else: the recorded
negative finding — that stablecoin supply and the crypto fear and greed index carry
no discriminative power for this question — was produced by code that could
mis-measure a window, so it should not be cited until re-run. This section is that
re-run, and it found two more defects on the way. **Both are the species this audit
keeps finding: a report that does not say what it did not do.**

### 12.1 What the re-run used, and why it is a re-establishment and not a replay

The database that produced the recorded numbers no longer exists on this host:
`~/.finworld/finworld.db` holds 65 observations from SEC, OpenSky, EIA and Treasury
sources and **zero rows in `price_bars` and zero in `move_events`**. The historical
bars were gone, so the recorded figures could not be recomputed on the same inputs.
What could be done, and was done in a throwaway registry at `/tmp/m0405/lele.db` so
the existing research database was not written to, is to rebuild the whole chain from
the same keyless sources and measure it again end to end:

| step | result |
| --- | --- |
| `instruments add 1 BTCUSDT binance-spot bitcoin USDT unadjusted` | `rights_verified` false, `rights_basis` unknown |
| `fetch-history 1 BTCUSDT --interval 1d --limit 4000 --pages 4` | 3332 daily bars, 2017-08-17 to 2026-10-01; 1 malformed row and 1 unclosed candle skipped, both counted |
| `moves 1 --interval 1d --move-hours 24 --thresholds 3 5 7 11` | 856 candidates, 628 retained moves, 1169 tier rows; p3 628, p5 323, p7 173, p11 45; measured cadence 86400 s, **1 gap**, contiguous fraction 0.9997 |
| `fetch-stablecoins` | 1200 daily observations, 2023-06-21 to 2026-10-02 |
| `fetch-sentiment --limit 3000` | 3000 daily observations |
| `fetch-market-activity 1 --coin bitcoin --days 365` | 364 instrument-bound observations, 2025-10-04 to 2026-10-02 |

The detector reproduced the recorded counts exactly — 856 candidates, 628 retained
moves, and the same per-tier totals — which is the first thing worth recording, and
worth recording for a reason that is easy to get wrong. **It does not show the
recorded results were correct.** It shows that on a bar set extended by six days,
the corrected detector picks the same 628 windows. The gap exclusion changed
nothing here because the single gap sits at index 175 and no retained window spans
it. The corrected detector was therefore never the reason the numbers differ below.

### 12.2 Defect: a stored move row was trusted without re-checking its window

`moves.detect` refuses a candidate whose window spans a missing bar or whose real
elapsed time is not the requested horizon. Nothing ever checked that a **stored**
`move_events` row still describes such a window. All four readers of stored moves
took the row's word for it: `causes.attribute`, `causes.profile`,
`causes.profile_context` and `timeline.explain`. So a row written by a superseded
detector — or by a run whose bars have since been pruned or replaced — would have
its pre-window anchored to the wrong date and be counted, compared and reported as a
move window of a length the current detector would refuse. That is the exact failure
the plan said had to be fixed before the results could be cited, and it was still
live in the readers.

Fixed by `moves.verify_stored`, which re-derives the cadence, the gap set and the
horizon from the stored bars and splits rows into kept and refused with a named
reason: a bar missing inside the window, a start or end bar no longer stored, a
backwards window, a length that is not the recorded move length, or no stored bars
at all to check against. All four readers now call it, and each reports what it
dropped: `profile_context` and `profile` as `unverified_move_windows`,
`attribute` as `moves_unverified`, `timeline.explain` as `unverified_rows`.

One implementation bug was caught by the tests rather than by the live run, which
is the usual order. The first version returned every row as kept when the registry
held no bars at all — the one situation in which nothing can be verified, and
therefore the one in which trusting the rows is least defensible. Now nothing is
kept and every row is refused. On the live data the count of refused rows is **0**,
because the detector wrote them, so this fix changes no recorded number; it removes
the possibility that a future stale row silently changes one.

### 12.3 Defect: the control group's size was set by a default and never reported

Running the profile revealed that `--controls` defaults to 10. The first live run
of this re-establishment therefore compared **526 move windows against 9 control
windows** and reported it in exactly the same shape as any other result. The library
default is `max(40, 2 * len(move_windows))`; the CLI's 10 overrode it in both
directions — small against a 40-window run, and small by a factor of fifty against a
526-window one.

This is not a rounding matter. A permutation test can only resolve a difference the
smaller group can express, so with 9 controls the coarsest observable move-to-control
share difference is about 0.11 and the raw p-value on the measured-mean test came out
at 0.93 for stablecoin supply where a properly powered run gives 0.031. **The
thin-control run was not merely weaker, it pointed the other way.** A reader had no
way to know, because the report published `control_windows: 9` and said nothing about
the 1948 eligible non-move timestamps that were never sampled.

`_control_windows` now returns how many timestamps were eligible before thinning, and
`profile_context` publishes it as `control_sampling`: `requested`, `eligible`, `used`,
`dropped_without_coverage` and the move-to-control ratio, with a note saying that a
small `used` against a large `eligible` means the power was set by the request rather
than by the data. `attribute` publishes `controls_eligible`. The CLI help now says
that 10 is a quick look and not a powered comparison. The default itself was left at
10: changing it would silently restate every recorded figure, which is the mirror
image of this defect.

### 12.4 The result, at a control group that can resolve something

`causes context 1 --interval 1d --move-hours 24 --pre-hours 24 --tier pN --controls 500`,
499 control windows used against 1948–2940 eligible at every tier:

| tier | move windows | controls | stablecoin supply difference (move − control) | raw p | adjusted p | drift |
| --- | --- | --- | --- | --- | --- | --- |
| p3 | 526 | 499 | −$15.79bn (−7.01%) | 0.031 | **0.093** | 2.33 sd |
| p5 | 252 | 499 | −$12.26bn (−5.52%) | 0.251 | 0.673 | 2.33 sd |
| p7 | 124 | 499 | −$27.58bn (−12.45%) | 0.127 | 0.382 | 2.33 sd |
| p11 | 30 | 499 | +$14.25bn (+6.46%) | 0.728 | 0.728 | 2.33 sd |

**The qualitative negative finding reproduces: nothing survives the Benjamini-Hochberg
correction at any tier, and `inference_status` reports that no category stands out
against the control windows.** The fear and greed index gives no significant
difference at any tier (adjusted p between 0.27 and 0.93) and no drift.
`market_activity` gives no significant difference either, over the one year its
coverage actually reaches.

**One recorded pattern does not reproduce, and that is the substantive change.**
`WORKLOG.md` recorded stablecoin supply at −$31.5bn (adjusted 0.009) at p3 growing
monotonically to −$52.4bn (adjusted 0.0015) at p7 — "a monotone pattern in move size,
which is the shape a real effect would have". Re-run on the corrected detector with a
powered control group, the difference is negative at p3, p5 and p7 and **positive at
p11**, no tier reaches adjusted p ≤ 0.05, and the closest is p3 at 0.093 against a
stated alpha of 0.05. The monotone shape was an artifact of a small control group
drawn from a different part of a series that rises by an order of magnitude. The
conclusion that survived, and that still stands, is the one the old run also reached
for the wrong reason: the level is confounded with when the windows sit, drifting
2.33 standard deviations, and a trending level cannot be tested this way at all.

This is the first recorded result of this project that a re-run has **overturned**
rather than confirmed, and it happened because the measurement improved rather than
because the market changed. `DEVELOPMENT_PLAN.md` asked for exactly this and the
answer it produced is the honest one.

### 12.5 What this does not establish

- The re-run is one instrument, one interval, one 24-hour pre-window and one set of
  three free context series on one day. It is a re-establishment of a recorded
  negative finding, not a survey.
- Every context series is daily, so a 24-hour pre-window is saturated: presence
  carries no information and only the measured-mean difference is testable. That
  limitation is unchanged and still bounds everything downstream.
- `market_activity` reaches 365 days, which leaves 37 usable move windows at p3 and
  1 at p11. Those rows are reported and are not evidence either way.
- The bars are one venue's unadjusted spot prices with no independent market-truth
  check, and the stablecoin supply series is an aggregate the project does not audit.
- No cause is established. Nothing here says capital left before a move, and nothing
  here forecasts price.

## 13. A capability summary, and the two false claims building it exposed

`lele menu` already ran every command, but the project had no way to *read* its own
surface area: a reader wanting to know what `lele` can do had to read 70 CLI
descriptions in `COMMANDS` and then the argument parser, and any prose summary of
the capabilities would have started decaying the day after it was written.
`lele summary` is that inventory, **generated from the code**.

### 13.1 Why it is generated rather than written

Every list in the document is read at run time from the thing it describes: the
command table and the argument parser, the source catalogue, the endpoint
allowlist, the observation taxonomy, the volatility estimator list, the frozen
indicator specs, the frozen forecast methods, the cited frameworks, and the
standing-objective constant. A feature added with a command gets a line in the
file without anyone writing one, and a command removed disappears from it. The
tests pin that contract rather than the document: every command is described, a
relabelled command changes the rendered output, and a table entry with no parser
is refused rather than summarised.

### 13.2 Defect: "5 public sources" understated the program's reach by tenfold

The first draft counted `sources.list_sources()` and printed **5 public sources**.
That number is the catalogue behind the `fetch` command and nothing else. The
price, evidence, sanctions and redirect allowlists add **43 more entries across 20
further hosts**, including CFTC, FINRA, GDELT, OFAC, the UN consolidated list, the
EU FSF extract, OpenSky, Binance, CoinGecko, DefiLlama, the fear and greed index,
Treasury Fiscal Data, the Federal Register, USAspending, Senate LDA, EIA, BLS, SEC
IAPD and Google News. A count of 5 attached to the words "public sources" is the
same defect class as §10: a number that reads as a property of the whole program
and is a property of one command. The document now separates **registry datasets
behind `fetch`** from the **endpoint allowlist**, and names the hosts, because an
allowlist is the real boundary of what can be reached and a reader can check it.

### 13.3 Defect: the read-only table asserted read-only access for a writing command

`causes.attribute` and the other readers needed the CLI's read-only classification
so the summary could say what each command does to the registry. Reading
`READ_ONLY_ACTIONS` surfaced three **dead entries** — `explain`, `capital` and
`store-evidence` each mapped an action named `all`, and none of the three has an
`action` argument, so none of the entries could ever match. Two were merely
redundant with `READ_ONLY_COMMANDS`. The third was `store-evidence`, **which
writes**: the day it grew an `action` argument named `all`, that entry would have
handed a working fetch a read-only handle and turned it into a "database is
readonly" failure. The rendered summary was already repeating the false claim, as
"read-only for `all`; other actions write", which is the worst of both.

The entries are removed and the table can no longer rot silently:
`tests/test_db_guarantees.py` now fails if an entry names an action its parser does
not offer, or if a command appears in both tables, and `NEVER_OPENS_REGISTRY` names
the three commands that never open the database at all so the summary can say
"does not open the registry" instead of guessing. A command whose access depends
on its action is labelled with the actions that only read, because collapsing it
either way would be a false statement.

### 13.4 What this does not establish

The document is an inventory. A command appearing in it says the program offers a
measurement; it does not say the measurement is right, that a provider answered,
that the stored records describe a population, or that any of the negative results
recorded in §12 changed. It carries `completeness: unknown` even when every table
is counted, and the phrases it must never contain are asserted by a test.

## 14. Stationary context quantities, and a plan item that was not true

`DEVELOPMENT_PLAN.md` item 1a asked for context to be stored as a stationary
quantity, on the ground that `stablecoin_supply` drifts 2.33 standard deviations
across the compared windows at every tier (§12.4) and a trending level cannot be
compared between groups whose windows sit at different dates however large the
sample. That is now built, and building it overturned half of the item's own
reasoning.

### 14.1 Defect: the plan asserted a provider reach that does not exist

The item said extending `market_activity` over years "needs no new provider",
because the CoinGecko `market_chart` endpoint accepts any `days` value. **Measured,
it does not.** One bounded request per value, keyless, through this project's own
HTTP client on 2026-10-02:

| request | result |
| --- | --- |
| `days=365` | HTTP 200, 366 daily points, 2025-10-03 to 2026-10-02 |
| `days=400` | **HTTP 401**, "verify parameters and API access policy" |
| `days=1000` | **HTTP 401**, same |

So `MAX_ACTIVITY_DAYS = 365` is not a choice this project made; it is where the
free tier stops, and the constant now has that as its reason. Raising it would have
produced a fetcher that passes its own validation and then fails on every call
beyond a year. `market_activity` consequently still reaches 365 days, p11 still
leaves it one measured move window, and the only route to a multi-year
instrument-bound capital series is a provider plan this project will not obtain by
fabricating a credential. That is recorded as a measured limit, not as a task.

### 14.2 Defect: a stored parameter that no longer reproduced the number beside it

This one was found by the tests, and it is the same species as §10 and §13.2: a
field whose name claims something the value does not support. Storing a z-score's
baseline mean and spread at the eight-decimal scale used for the score itself
turns a spread of 1e-24 into `0.00000000`. The row would then carry a score
computed by dividing by a non-zero number next to a stored parameter saying the
denominator was zero — and the reason the parameters are stored at all, in this
project and in `volatility_estimates`, is that the value alone is not
reproducible. Parameters are now written at 20 significant digits, the budget is
stored on the row, and a parameter that rounds away entirely is **refused by
name** (`value_not_representable`) rather than stored as a zero.

A second instance of the same family: `str(Decimal('0E-8'))` is `'0E-8'`, which
is not a finite decimal string to the registry that has to hold it, so a
perfectly ordinary small value would have raised out of `store` as an opaque
`ValueError` from `registry._decimal_text` rather than being counted. Exponent
notation is now rendered out, and the count of values the scale cannot carry is
in the report.

### 14.3 What was built, and why the derived rows are not observations

`analysis/stationarity.py` derives two quantities from stored context series:
`change` (the difference from the previous stored value, unit
`<parent unit>_change_per_<cadence>s`) and `zscore` (the value against its own
trailing baseline through `volatility.z_score`, baseline excluding the point
scored, minimum 60 observations — the minimum `MIN_BASELINE_Z` already requires).
`rate` and `ratio` are named in `NOT_OFFERED` with the reason each is not offered,
and the capability summary quotes them, so a deliberate omission cannot read as an
oversight.

They are stored in a **new `context_measures` table (schema v18), not in
`observations`**. That is the one design decision worth defending, because the
cheaper route was a new observation kind. A derived quantity is not a second
reading of the world. Putting it in `observations` would let a difference and a
level be listed side by side as if both had been observed, would put it through
every reader of that table — `signals.stored`, the world-state evidence projection,
the flow attribution sums, `lele observations list` — none of which has any way to
know that one of the two numbers is a difference of the other. The parameters are
real columns for the same reason `volatility_estimates` stores `ddof`:
`baseline_observations` is part of the uniqueness key, so a score against 60 points
cannot silently overwrite one against 120, and a reader shown two baselines is
told so instead of being handed one of them.

Every derivation follows the move detector's own rules: cadence is **measured**
from the stored timestamps, not read from a label; a difference spanning a hole is
**refused, not computed**; every refusal is counted by name
(`interrupted`, `no_prior_point`, `baseline_short`, `flat_baseline`,
`unit_changed`, `not_increasing`, `repeated_timestamp`,
`too_short_to_measure_cadence`, `unparsable_value`, `no_stored_level`,
`value_not_representable`), and a test asserts that every name in `REFUSALS` is
reachable from some input, because a refusal reason nothing can trigger is one a
reader cannot rely on being told. `not_increasing` can only arise from a row
written past `add_observation`, which normalises timestamps to UTC — it fires when
ISO text order and instant order disagree, which is a real thing a foreign writer
can do.

### 14.4 The result: the comparison becomes possible, and it is negative

`causes context 1 --controls 500 --measure change --measure zscore`, on a copy of
the §12 registry at `/tmp/1a/live.db` with the measures derived from the same
stored rows. 499 controls used at every tier.

| tier | series | drift before | drift after | raw p after | adjusted p after |
| --- | --- | --- | --- | --- | --- |
| p3 | stablecoin_supply level | 2.334 | — | 0.031 | 0.093 |
| p3 | stablecoin_supply **change** | — | **0.039** | 0.161 | 0.193 |
| p3 | stablecoin_supply **zscore** | — | 0.723 | 0.099 | 0.157 |
| p5 | stablecoin_supply change | — | **0.026** | 0.589 | 0.848 |
| p7 | stablecoin_supply change | — | **0.162** | 0.800 | 0.960 |
| p11 | stablecoin_supply change | — | **0.048** | 0.797 | 0.967 |

**The drift confound is gone — 2.33 sd falls to 0.03–0.16 — and the comparison it
was blocking is negative.** The level's nearest approach, adjusted 0.093 at p3,
becomes 0.193 on its change and 0.157 on its z-score. Nothing in the measure family
survives the correction at any tier, and `inference_status` reports zero
survivors in both the level and the measure section.

Three things about that number, stated because they are easy to misread.

1. **The measure family corrects six tests, the level family three.** Adjusted
   values are not comparable across the two sections. The report says so in
   `measures.multiple_testing.note`, because quoting whichever of the two clears the
   correction would be selecting on the outcome.
2. **The default is off.** `--measure` is not passed by default and every recorded
   figure in §12 was produced without it. A default run was diffed against the
   `--measure` run on the same registry: the `kinds` and `families` sections are
   byte-identical and the `measures` key is absent. A feature that made the older
   numbers look cleaner by default would have restated them.
3. **`zscore` does not fully remove the drift** (0.72–0.84 sd) where `change`
   does, and it costs `MINIMUM_BASELINE` points at the start of the series and its
   own coverage: 1141 days against the change's 1200. That is stated in the report
   rather than rounded into a claim that both are equivalent.

The item's premise held — removing the calendar trend was the only route to a
testable comparison from these series — and the testable comparison is negative.
`market_activity@binance:BTCUSDT` remains untestable at p11 with one measured move
window, exactly as §12.4 recorded, because §14.1 is why.

### 14.5 What this does not establish

- The derived rows are a re-expression of stored observations. Nothing here observed
  anything, and no amount is a net inflow, a purchase, a sale or an actor.
- Stationarity removes one confound. It does not make any context series a cause of
  a move, it does not remove the unmatched control design, and it does not fix the
  saturated 24-hour pre-window that §12.5 recorded.
- The eligible series are a list, not a rule, because the property is not inferable
  from the data: six irregularly dated insider trades also admit a measured cadence,
  and differencing them would produce a number in shares that reads as a change in
  holdings. `insider_trade` and `fund_flow` are excluded on that ground, and the
  list carries a reason per entry that a test enforces.
- Re-deriving over unchanged stored rows rewrites the same rows: 8943 stored
  measures before and after a second full derivation, confirmed on the live copy.
  That is what makes the stored value reproducible, and it also means a restatement
  of a past level changes every derived point after it. Nothing recomputes them.
- The transform is chosen by the operator. `change` needs no tuned parameter;
  `zscore` needs a baseline length, and a reader who wanted to buy a result could
  try several. The stored `baseline_observations` is what makes that visible rather
  than invisible, and a series carrying two of them is reported as not compared
  rather than resolved.

## 15. Retention, and four defects — three of them older than this round

Queue item 2 since the reliability audit: `price_bars` and `move_events` grow for
as long as a fetcher runs. This section records the cut, the four defects found
while building it, and the one that the build itself introduced and the tests
caught.

### 15.1 Defect: the schema inventory had drifted, so `doctor` stopped being able to fail

`core/schema.py` opens by claiming the expected table and index inventory is
derived from the same source as the DDL rather than restated by hand. It was two
hand-maintained `frozenset` literals. Schema v18 added `context_measures` and
`idx_context_measures_series` to the DDL and to neither list.

Measured, before the fix: a schema-v18 database with `context_measures` dropped
reports **29 tables present, 0 missing**, opens through `db.initialize` without
complaint, and fails later with `no such table: context_measures` from the one
command that reads it — `lele context`. That is defect A11 (§3) returning through
a different door, because the promise `doctor` keeps is only as good as the
inventory behind it, and §3 fixed the check while leaving the list it reads
unverified.

Both sets are now read out of the DDL by `schema.declared_objects()`, which raises
on a statement it cannot parse rather than skipping it, and
`tests/test_db_guarantees.py` drops **every** declared table and index in turn and
requires the inventory to name it. Thirty-two objects, so the list cannot be
correct by accident and cannot drift again.

### 15.2 Defect: a leaked connection in the test suite, found by a failure in an unrelated test

The full-suite run failed twice, in two *different* tests, both asserting that an
error message contains no traceback:

```
Exception ignored while finalizing database connection <sqlite3.Connection ...>:
ResourceWarning: unclosed database in <sqlite3.Connection object at 0x...>
```

The frame named `db.py:208 in _state`, which is where the garbage collector
happened to finalise the connection, not where it leaked. The leak was in the
suite's own helper: `tests/test_capability.py::_registry()` and `_broken()`
returned an open in-memory connection, and neither call site closed it. A leak
that asserts nothing is invisible until an unrelated `gc.collect()` turns it into
some other test's failure — which is how §3's A7 guarantee (`ResourceWarning` as an
error) caught a defect in the test that was written to satisfy it. Both helpers
are context managers now, and running that module alone under the same warning
rule reports zero.

The general lesson is the one this audit keeps: **a failure in a test that
asserts something unrelated is a finding, not noise.** Two runs failing in two
different tests is what made it findable at all; had they failed in the same test
it would have looked like flakiness.

### 15.3 Defect: a frozen vocabulary that was sorted rather than quoted

`capability._taxonomy()` reported `sorted(prospective.METHODS)` under
`frozen_forecast_methods` while `tests/test_capability.py` asserts it equals
`list(prospective.METHODS)`. The two differ — `constructed_indicator_v1` sorts
before `five_minute_persistence_v1` — so the test failed **in the tree as found**,
before this round changed anything. The §13 handoff recorded a green gate, so this
was introduced after that gate ran, and it is recorded here because a handoff
document claiming a green gate is a claim like any other and this one was stale.

The test is the contract ("quoted not summarised"), so the module quotes the
declared order. `tests/test_packaging.py` had the same species of duplication —
`REGISTRY_SCHEMA_VERSION == 18`, a second literal to forget on every migration —
and now compares against `core.schema.SCHEMA_VERSION`, which is the guarantee worth
having: the constant and the DDL it names cannot disagree.

### 15.4 Defect, found live: a read-only open on an older registry

`lele prune runs` against the §12 registry (schema v18, 19 MB) failed with
`database error`. A read-only open never migrates, so `prune_runs` does not exist
there, and the query raised `no such table`. That is this project's own rule — a
registry that cannot answer the question says so rather than guessing — applied to
a question the registry predates. `registry.list_prune_runs` and
`latest_prune_cut` now check for the table and report **no cuts recorded**, which
is the truth: nothing was cut there, because the command did not exist.

### 15.5 What a cut does, and the one rule that took two attempts

`lele prune plan|apply|runs` (`analysis/retention.py`, schema v19 for
`prune_runs`). A plan is **pure reads** and runs on a read-only mount; `apply`
counts through the same `_predicate` the plan counted with, so the approved plan
and the deleted rows are the same set by construction, and each delete's row count
is compared with its planned count. The reason is required. Tables removed:
`move_causes`, `move_events`, `volatility_estimates`, `cause_scans`,
`price_anomalies`, `price_bars`.

The rule that took two attempts is which instant dates a row. The first version
dated a bar by its **open**, which meant a bar spanning the cut was counted as
removed *and* as straddling — the same row on both sides of the report. The rule is
now uniform: **a row is deleted when the last instant it describes is strictly
before the cut**, so a move is dated by its end, an estimate by the end of its
window, a bar by its close and an article by its own instant. A row landing exactly
on the cut is kept, and every bar that survives therefore lies wholly at or after
it. A row that straddles is kept and counted in `spans_cut`, because deleting it
would remove a measurement still mostly inside the retained history and keeping it
silently would leave a value no longer reproducible from what is left.

Five further things the tests caught rather than the arithmetic, and each of them
is the species this audit keeps finding: a report that does not say what it did not
do, or a code path nothing exercised.

- **The canonical-timestamp check read the wrong column.** ISO text with a constant
  offset and length sorts in the order of the instants it names, so a series is
  measured before it is cut. The check read `open_time` while the rule dates a bar
  by `close_time`: a fixture with a UTC close and a `+05:00` open passed the check
  and would have been counted with the column nobody verified. Both columns are
  measured now.
- **The unscoped total was multiplied by the series count.** Counting each
  non-series-scoped table once per series reports a number several times too large,
  and it looked exactly like a correct one. The plan reports per-series bar
  accounting and registry-wide table counts, and a test pins that two series with
  two moves delete two moves.
- **A scoped apply deleted the bars and nothing else.** `apply --series K` took its
  per-series expected counts for the other five tables from the *registry-wide*
  block, which is the sum over series, so every expected count resolved to zero and
  the moves, estimates, scans, anomalies and articles of the scoped series stayed
  where they were while the report said the cut was complete. The three tests that
  exercised a scoped apply had all asserted on the *other* series surviving, which
  is what a partial delete passes. The predicate now takes the scope's interval
  filter and the series' own interval separately — they answer different questions,
  and conflating them is what hid this — and there is a test that seeds both series
  with a row in every table and requires one to be emptied and the other untouched.
- **`price_anomalies` was skipped by a cut that could select it.** The skip is
  correct for a cut scoped to one *interval*, because the table has no interval
  column; it was being applied to any cut with a series in hand, so a
  `--series`-only cut never removed an anomaly and reported zero found. It now
  skips only for an interval-scoped cut, and a key-scoped cut selects every interval
  of the instrument and says so in `interval_note`.
- **An empty registry raised.** `apply` on a registry with no bars reached a
  re-derivation of the comparison text with nothing to derive it from. There is now
  a named refusal, `scope_has_no_stored_bars`, for both the scoped and unscoped
  case.

`move_causes` needed one more thing. Its rows hang off `move_events` by a cascading
foreign key, so deleting a move removes articles that this module's date filter
would not have selected — an article dated *after* the cut would have gone while the
report said nothing was removed. The delete set is therefore the union of the aged
rows and the attached ones, the breakdown is published (`aged`, `attached`), and
the row count the database removed is compared with the row count the plan
counted.

The floor is derived rather than chosen: `moves.MIN_BASELINE_BARS + 2`, the
smallest number of closes `moves.detect` will accept, and a series whose own
longest stored window is longer gets a longer floor (a 40-day move on daily bars
needs 41 bars). A cut leaving fewer is **refused with the series named**, not
clamped, because a clamp that silently keeps more than was asked for is the report
that does not say what it did not do.

### 15.6 What the loss is recorded as, and what is never pruned

Every applied cut writes a `prune_runs` row: the instant, the operator's reason, the
per-table counts deleted, the per-table counts straddling, and what remains per
series. `registry.stored_bar_span` carries the latest applicable cut into every
report that measures a series, and `lele explain` states it in the `stored_bars`
coverage note. The record describes the loss. **It cannot restore it**: a statement
of how many rows there were is not a copy of them, and the report says so in those
words.

Five tables are never pruned and the reason is quoted in the report and in the
generated capability summary, because a table left alone and a table forgotten look
identical in a file listing: `observations` and `event_store` (filed evidence, not
measurements of price), `ingest_runs` (deleting the run that fetched a bar would
leave the bar without a provenance row), `context_measures` (bounded by its parent
series, not by price history, so a price cut does not bound it — pruning a derived
row whose parent observation is still stored discards reproducible information and
saves nothing), and `semantic_embeddings` (derived from text, rebuilt by nothing).

### 15.7 Measured, on a copy of the §12 registry

`lele prune plan --before 2024-01-01 --keep-bars 400` against a copy of the §12
registry at `/tmp/1a` (3332 Binance daily bars, 2017-08-17 to 2026-10-01, 1169
tier rows over 628 moves), then applied to that copy:

| table | removed | straddling |
| --- | --- | --- |
| `price_bars` | 2326 | 1 |
| `move_events` | 966 | 0 |
| `move_causes`, `volatility_estimates`, `cause_scans`, `price_anomalies` | 0 | 0 |

1006 bars remain, the last removed bar opened 2023-12-30, and the 2021 window that
used to be measurable now reports: *"history before 2024-01-01T00:00:00+00:00 was
removed by recorded prune run 1 (…), so a window before that instant cannot be
measured from this registry"*. That sentence is the point of the whole feature:
without it, an empty window reads as an absence of events.

The research database at `~/.finworld/finworld.db` was not written to, and every
figure above came from a copy with the outputs left under `/tmp/1a/`.

### 15.8 What this does not establish

- Nothing here decides how much history is worth keeping. The floor is the smallest
  series that can still be measured; it is not a retention policy.
- A cut is irreversible from inside this tool. `lele backup` first is advice, not a
  check: nothing verifies that a backup exists.
- Pruning changes what a later report can measure and nothing already reported. A
  figure computed before a cut and one computed after it are two measurements of
  two inputs.
- A derived row that survives a cut is kept, and it is no longer reproducible from
  the remaining bars. `moves.verify_stored` refuses such a row by name when a reader
  asks, and its count appears there rather than in the prune report.
- The registry cannot know what it never fetched. A recorded cut tells a reader
  that history was removed; it says nothing about the period before the first bar a
  bounded fetch reached, which is why `completeness` stays `unknown`.

## 16. Provider health, and a plan item whose premise was half false

Queue item 3 since the reliability audit: "`ingest_runs` already records a request
hash and a record hash per run. Nothing consumes them." It is now consumed, and the
first thing building it did was measure the premise, which turns out to be half
false and to decide what the feature can honestly claim.

### 16.1 The record hash is mostly not there, and where it is, it is not a record hash

Measured over every `record_ingest_run` call site with `ast`, 2026-10-03:

| | count |
| --- | --- |
| call sites writing a run | 25 |
| passing `records_sha256` | **8** |
| passing none | **17** |

And of the eight, none hashes the records. `sources.py` hashes
`{fetched, stored, total, pages}`; `history.py` and `price_import.py` hash the
`pages_detail` metadata; all four `sanctions.py` sites hash
`{fetched, pages, stored}`. So there is **no content fingerprint anywhere in the
table**, and the live registry bears that out: 11 of its 15 runs carry an empty
`records_sha256`.

The consequence is not cosmetic. A provider that starts returning *different
numbers* is visible. A provider that returns the same number of *different records*
is invisible to any consumer of this table, and no amount of reading it changes
that. The monitor therefore promises one thing and says it in its own output:
**it detects change and silence, never wrongness.**

`request_sha256` is real and is the column that makes a comparison mean anything —
two runs that asked the same thing and got different answers are evidence about the
provider; two runs that asked different things are evidence about nothing. It is
also not uniformly a request hash: the four `sanctions.py` sites store the
*downloaded file's* SHA-256 in it. So `request_changed` is defined as "the recorded
request identity changed", which covers both, and the report says so rather than
assuming a query changed.

### 16.2 The blind spot the monitor cannot close, stated on every run

A failed run rolls back with its transaction and is never recorded — D01's own
acceptance criterion, and `lele runs` has always said it. So **absence of a new run
is not evidence that a source works**, and nothing in this report can call a source
failing or healthy. There is no `healthy` status and no `failed` flag, and
`tests/test_provider_health.py` asserts that no field and no status value claims
one, while separately asserting that the sentence saying so is present.

Recording failures is the obvious next step and is **not** built here, for a reason
worth stating: the failure row would have to be committed on a different connection
than the one whose transaction rolled back, because `get_conn` is the only owner of
commit and rollback and its rollback takes the failure row with it. That is a change
to the transaction-ownership invariant this audit fixed in §3A2, and it should be
done deliberately rather than as a side effect of a monitoring feature.

### 16.3 What was built

`lele/analysis/provider_health.py`, `lele providers`, and a reduced block in
`lele doctor` so the answer is somewhere a person looks when something is wrong
rather than a command they have to remember. Groups runs by
`(source, query, country, indicator, category)` — the same key `latest_resumable_run`
matches on — and publishes, per series: run count, first and last start, age in
hours, whether the recorded request identity was stable across the window, the
first/last/min/max of `fetched`, `stored`, `skipped`, `missing` and `pages`, the
truncated-run count, and the union of recorded warnings.

Nine flags, each reachable from some input, which a test asserts because a flag
nothing can raise is one a reader cannot rely on being told: `request_changed`,
`count_collapse`, `stored_nothing_once`, `stored_zero_while_fetched`,
`returned_nothing`, `never_stored`, `always_truncated`, `no_record_hash`, `stale`.

Three rules govern every comparison:

1. **Nothing is compared across a different recorded request.** Stated as a flag
   rather than applied silently, because a silent comparison would report the
   operator's own query change as a provider's behaviour.
2. **A heuristic threshold is named and published beside the numbers it fired on.**
   `COLLAPSE_FRACTION` is `0.5`, a rule of thumb for a fetched count that fell
   sharply — not a verdict about a provider — and the first/last/min/max counts are
   always in the output so a reader can apply a different one by hand.
3. **A bound that hides data is reported.** The run read is capped at
   `MAX_RUNS = 1000` and `run_read_bound_reached` says whether a full page came
   back, because "this source has no runs" and "this source's runs fell outside the
   bound" are different claims. The series bound is reported the same way.

`always_truncated` is worth separating from the rest: it is not a fault. A run
recorded as truncated stored a *prefix* of what the provider offered, so the flag's
detail says coverage is partial by construction and `completeness` stays `unknown`.

`stored_nothing_once` is the one flag about the past rather than the current shape:
the same recorded request once stored nothing and later stored rows. It is kept
because the request was identical both times, so either the provider or this program
changed and the table cannot say which — and it is why the sec-formadv pair in the
live registry is visible at all.

### 16.4 Measured, on the research registry, read-only

`lele providers --stale-after-hours 720` against `~/.finworld/finworld.db` (15 runs,
11 sources, 12 series, all from 2026-09-20):

| flag | series |
| --- | --- |
| `no_record_hash` | 10 |
| `always_truncated` | 8 |
| `never_stored` | 3 (`sec-13f`, `sec-formd`, `sec-nport` on one issuer) |
| `stored_nothing_once` | 1 (`sec-formadv` individual: stored 0 then 11 on an identical request) |

At the default 168-hour staleness threshold all 12 series also carry `stale`, which
is the correct reading of a registry last fetched on 2026-09-20 and is stated as
"either a source that stopped producing or an operator who stopped asking" rather
than as a fault.

The three `never_stored` series are the honest case: Apple has no 13F, Form D or
N-PORT filing for that issuer, and a source with nothing to report is exactly what a
source that stopped answering looks like in this table. The flag says both, and does
not choose.

`lele doctor` on the same registry now carries
`{"sources": 11, "series": 12, "runs_considered": 15, "flagged_series": 12,
"flags": {...}}` and the same six "what this is not" lines.

### 16.5 Tests

`tests/test_provider_health.py`, 32 offline tests pinning contracts rather than
values: an empty registry reports `no_recorded_runs` rather than no problems; the
raw counts are published beside the threshold that fired; a source wobbling by ten
percent is **not** flagged; a changed request identity stops the comparison and says
so; the collapse flag carries its peak, latest, ratio and threshold; a bound that
hid runs is reported; a run with an unreadable timestamp has no invented age and is
counted as outside a window rather than dropped; every flag name is reachable; no
field or status claims a source healthy or failing; the failure blind spot is stated
every time; the report survives `json.dumps(allow_nan=False)`; and the CLI command
writes nothing while `doctor` reports an unreadable run table as classified rather
than echoing an exception.

### 16.6 What this does not establish

- Nothing here says a provider is correct, and nothing here could. The table has no
  content hash and no ground truth.
- Nothing here says a provider is *failing*. It cannot see a failure.
- `never_stored` and `returned_nothing` cannot distinguish an empty source from a
  broken one. Both readings are in the flag detail.
- Staleness cannot distinguish a source that stopped producing from an operator who
  stopped asking.
- `always_truncated` is a coverage statement, not a defect, and it is a claim about
  the *stored prefix*, never about what the provider holds.

## 17. Recording a failed ingest run, and the two designs that could not work

§16 closed provider health with its biggest blind spot named: a failed run rolls
back with its transaction and is never recorded, so a source that errors leaves no
trace and nothing can call one failing. This is the deliberate change the handoff
flagged, and it took three designs because the first two could not work.

### 17.1 Design one: a second connection, written outside the failed transaction

The obvious shape — open a second connection to the same file and write the failure
row there — is **impossible**, and the test that proved it is the reason this section
exists. A fetch that has stored anything holds the database's write lock for the
whole session, so a second connection's `INSERT` cannot proceed; it blocks until the
5-second busy timeout and fails. The case that matters most — a fetch that wrote
rows and then failed — is exactly the case that cannot record. Verified: the failing
test left **0** run rows behind. It would also have needed a genuine exception to
"get_conn owns every commit", which is not a price worth paying for a feature that
does not work.

### 17.2 Design two, kept: a recorder the transaction owner calls after its own rollback

`get_conn` gains one thing. A caller may set `conn.lele_failure_recorder`; if the
session fails, `get_conn` **rolls back first**, then calls the recorder and commits
its rows. The rows the failed work wrote are gone, and the row saying it failed is
not part of that work — which is the entire reason it survives. One connection, no
lock, no second commit path, and **the rule that one place owns every commit is kept
rather than bent**: the recorder writes, `get_conn` commits.

Three properties this buys, each tested:

- **A failure to record a failure never becomes the failure a caller sees.** A recorder
  that raises is swallowed and the original exception propagates unchanged.
- **An in-memory registry records it too.** The first design needed a second
  connection and so could not; this one needs none, and an in-memory registry is
  exactly what a test or a throwaway run uses.
- **One failure records one row.** `get_conn` clears the recorder before calling it.

The registration site is `fetch_source`, which validates and then calls
`_fetch_and_record`, so a run row exists for every fetch that reached the request
stage and none for an argument refused before any request. The recorder is cleared
**on success only**, and that detail was the second bug this round: the first version
cleared it in a `finally`, which removed the recorder while the exception was still
propagating, so `get_conn` found nothing registered and recorded nothing.

### 17.3 What a failure is recorded as

`status='failed'`, zero counts, the request identity and the query scope it was
attempting, and a reason drawn from a closed vocabulary — `source_request`,
`database`, `file`, `data`, `cancelled`, `unexpected` — plus the exception's **class
name**.

**Never its message.** An exception message can carry a token, a password or a
response body, and §3G1's rule applies to a database row exactly as it applies to
the console. `classify_failure` returns the reason and the class name and nothing
else; a test plants a secret in six different exception types and asserts it reaches
neither the coverage text, the warnings, nor any other column.

### 17.4 What is not adopted, stated in the report and machine-checked

`FAILURE_RECORDING_COMMANDS = ("fetch",)`. Only `fetch` registers a recorder. The
roughly thirty `fetch-*` extractors — Form 4, 13F, N-PORT, USAspending, LDA,
Treasury, EIA, OpenSky, sanctions — still roll their failure back with their
transaction, so a failure in one of those leaves no row and `lele providers` is blind
to it.

That list is published rather than assumed: `provider_health` carries it in the
report (`failure_recording_commands`, `failure_recording_note`), the generated
capability summary quotes it, and a test asserts every name is a real command that
starts with `fetch`. Adopting one is a two-line change per fetcher, and the list is
the checklist.

A plain `sqlite3.Connection` cannot carry the recorder at all — `sqlite3.Connection`
has no instance dictionary. Rather than fail, the fetch reports
`result["failure_recording"]` **only in that case**, so a normal result keeps exactly
the shape it had and the key's presence is itself the signal. Eleven test files that
opened a bare in-memory connection were moved to `db.Connection`, which is what every
real caller gets; two of them pinned the exact key set of a fetch result and would
otherwise have failed on the new key.

### 17.5 Measured

`lele fetch fdic` against a temporary registry with `HTTPClient.get_json` raising
`SourceError("token=SECRET-do-not-store")`: exit 1, no secret on stdout or stderr, **no
entity stored**, and exactly one `ingest_runs` row with `status='failed'`,
`coverage='source_request: SourceError'`. `lele providers` on the same registry
reports `failed_runs: 1` and the `recorded_failure` flag.

At the same time the blind spot narrowed from total to partial: §16's
`NOT_A_CHECK` said no failure is ever recorded, and it now says a `fetch` failure is
recorded while a `fetch-*` extractor's is not.

### 17.6 Tests

`tests/test_failed_runs.py`, 16 offline tests, and the two halves pinned together
because either alone would pass a weaker test:

- a failed fetch stores nothing **and** the failure row survives the rollback;
- the recorder runs *after* the rollback — asserted from inside the recorder, which
  sees zero entities;
- a recorder that raises does not replace the original failure;
- no recorder registered means no row;
- a second failure appends a second row rather than replacing the first;
- a successful run records `completed` and no extra row;
- the request identity and scope are carried, so a failure is attributable;
- a secret planted in six exception types reaches no column;
- every value in `FAILURE_REASONS` is a phrase a reader can look up;
- an in-memory registry records the failure too;
- `lele fetch` end to end through `main`, asserting exit code, no leaked secret, no
  stored row and one failed run;
- an argument refused before any request records **no** run;
- a provider report counts the failure and names its reason;
- the report still says nothing about whether a command that records nothing worked.

### 17.7 What this does not establish

- It does not make a provider health check into a health check. A recorded failure is
  a fact about one attempt; it is not a verdict, and the flag says so.
- It covers one command. The `fetch-*` extractors remain unrecorded, and the report
  says so rather than implying coverage.
- The reason vocabulary classifies the failure, not the provider. `unexpected` means
  this program has a defect, and it is recorded as such rather than hidden.
- Nothing here compares a failure against a threshold. How many failures matter is
  the operator's call, and this reports the count.

## 18. Every fetcher that records a success now records a failure

§17 adopted failure recording for `fetch` and named the rest as unadopted. This
closes that, and the interesting part is not the twenty-three edits — it is what
happened when a script made them, and what the coverage claim rests on instead.

### 18.1 What was adopted, and how it is checked

Twenty-three recording sites across sixteen modules: `awards`, `comtrade` (two),
`eia` (two), `form4`, `formadv` (two), `formd`, `lobbying`, `material`, `nport`,
`opensky` (two), `political`, `thirteenf`, `treasury`, `history`, the four
`sanctions` fetchers, and `price_import`. Each registers its recorder with its own
`SOURCE` constant and its own scope expression, so a failure lands in **the same
series as that source's successes** — the grouping `(source, query, country,
indicator, category)` is identical by construction because both rows reference the
same local variables.

Two of the twenty-three could not be registered where `started` is taken, because
the scope does not exist yet: `price_import` learns the symbol after reading and
parsing the file, and `fetch_opensky_flights` resolves its window after parsing
`begin`/`end`. Both registrations moved to just after their scope is resolved and
before the request or the first write. Both say so in a comment, because the reason
is not obvious from the position.

**The coverage claim is a test over the source, not a list.** `FAILURE_RECORDING`
is a sentence, and the sentence is checked by an `ast` test that walks every module
calling `record_ingest_run` and requires it to call `record_failures` *and*
`clear_failure_recorder`. A list of twenty-four adopting commands is a list to
forget; a test that fails when a new fetcher joins one half of the pair is not.

### 18.2 The two bugs a script wrote, and what they were

The edits were applied by a script, and it got the insertion point wrong twice. Both
were caught by `compileall`, which is the reason it runs before the tests (§5).

1. **Off-by-one on the clear.** `lines.insert(call.lineno, …)` inserts *after* the
   line the statement starts on, which put the clear line inside the middle of a
   multi-line `record_ingest_run(` call. A `finally`-less `clear` in the wrong place
   is a syntax error, which is the good outcome.
2. **`started.lineno + started.end_lineno` as an insert index.** For a single-line
   assignment that is `2 × lineno`, so one registration landed at the end of a file.
   The assertion I had written — one `started` assignment per function — passed, and
   the arithmetic was still wrong.

Then one real design bug, found by mypy rather than by a test: `record_failures`
raised `ValueError: started must be a datetime` because `sources.py` keeps `started`
as an **ISO string** while every other fetcher keeps a datetime. That is the same
"the field's type is not what the column implies" family as §10, and it is why the
helper now accepts either and refuses anything that is not an aware instant. Before
the fix, a failing `lele fetch` recorded **nothing** and was reported as "invalid
data or registry schema" — a monitoring feature that silently did the opposite of
its purpose.

### 18.3 The handoff's reason for deferring sanctions was wrong

The previous handoff said the sanctions fetchers should be adopted **last**, because
"those commit on their own transaction". They do not: there is no `conn.commit()` in
`lele/analysis/sanctions.py`. They take a write session from the CLI like every other
command, so the rollback discards their work exactly as it does everyone else's, and
adopting them is the same two-line change as the rest. **The stated reason was wrong
and the ordering it produced was unnecessary.** Recorded because a handoff's
explanation is a claim like any other.

### 18.4 What is still not covered, and why it is a different gap

Eight commands write rows to the registry and record **no run at all**, so this
monitor is blind to them in both directions. They are named in `NO_RUN_HISTORY`,
printed in the report and quoted by the generated summary:

| command | why it records nothing |
| --- | --- |
| `fetch-sentiment`, `fetch-stablecoins`, `fetch-market-activity` | `fetchers/crypto_context.py` writes `observations` and never records a run |
| `store-evidence`, `fetch-evidence` | `fetchers/evidence.py` writes through the observation bridge and records no run |
| `fetch-prices`, `fetch-cot`, `fetch-short` | write an export file, never the registry, so there is nothing to roll back |

The first two rows are a real gap and a different one from §17: not "a failure is
invisible" but "a success is invisible too". Closing it means adding
`record_ingest_run` calls to two modules that have none, which is new bookkeeping
rather than an adoption, and it is the next task. The third row is not a gap at all:
a command that writes no rows has no transaction to roll back, so a failure there
costs nothing and leaves nothing behind — which is why the list is published as one
list rather than two.

### 18.5 Tests

`tests/test_failed_runs.py` grew to 19 tests. The new ones:

- **every module that records a completed run also records a failure**, over the
  source, checked with `ast`, with the module name in the failure message;
- **and also clears it**, because a recorder left registered would attribute a later
  failure in the same session to that fetcher;
- **the gap list is machine-checked** against the command table, and
  `crypto_context.py`/`evidence.py` are asserted still to record nothing, so a
  module that starts recording cannot sit in a list that says it does not;
- **a successful import leaves no failure recorded**, tested through
  `import-price-history` (no provider, no fixture beyond a file) by failing a later
  operation in the same session and asserting the only run is the import's own
  `completed` row. This is the test that would have caught the forgotten `finally` in
  §17.

### 18.6 What this does not establish

- Recording a failure is not a verdict on a provider, and the flag says so. One
  attempt failing is an event.
- The eight commands in `NO_RUN_HISTORY` are still invisible here, five of them
  because they record nothing at all.
- Nothing compares failures against a threshold. `failed_runs` is a count and the
  reader decides what it means.
- A failure recorded after a rollback says the fetch did not complete. It does not
  say how much of it had already been stored before the rollback, because those rows
  are gone — only the counts on the *successful* rows ever recorded survive.

## 19. The last five sources, and a list that turned out to be two different claims

§18 left eight commands writing rows while recording no run at all, and called five
of them "a real gap — a success is invisible there too". This closes that, and the
interesting finding is that the list was not one claim.

### 19.1 What was added

`fetchers/crypto_context.py` had three fetchers writing `observations` and recording
nothing. Each now registers its failure recorder with its own module constant and
writes a run row beside its observations:

| command | run source | series key |
| --- | --- | --- |
| `fetch-sentiment` | `crypto-fear-greed` | `limit:N end:YYYY-MM-DD` |
| `fetch-stablecoins` | `crypto-stablecoin-supply` | `limit:N end:YYYY-MM-DD` |
| `fetch-market-activity` | `crypto-market-activity` | `instrument_key`, `indicator` = coin |

`store-evidence` records too, in the CLI handler that persists the document, with the
source and symbol as its query so it sits beside the `fetch-evidence` export it came
from. The three crypto-context commands are now in the adopted family that §18's
`ast` test covers, so nothing about their adoption is asserted by hand.

### 19.2 A stronger record than the rest of the table holds

The other 22 recording sites hash *counts* into `records_sha256`, which is why §16
found no content fingerprint anywhere. These three already computed a
`response_sha256` over the provider payload and were **not storing it anywhere**. It
is now stored, so for `crypto-fear-greed`, `crypto-stablecoin-supply` and
`crypto-market-activity` a content change is detectable and `provider_health`'s
`no_record_hash` flag correctly stops firing for them.

That is not a uniformity win — the column now means two different things depending on
the fetcher — and it is recorded here as a finding rather than smoothed over. The
honest fix is a migration and a documented convention, which is a separate decision;
until then `provider_health` reports `records_hashed_runs` per series, so a reader can
see which is which.

Measured, on a temporary registry with the provider call stubbed: one successful fetch
gives `('crypto-fear-greed', 'completed', 3, 1, 'b6386d827af7')` and **no flags** at
all; one failing fetch gives `('crypto-fear-greed', 'failed', 'source_request:
SourceError')` beside it, in the same series, with the secret absent from every
column.

### 19.3 A defect found in the same function: an exception message echoed to the console

`_store_evidence` caught `(SourceError, ValueError)` and raised
`CLIError(f"evidence fetch failed; nothing stored: {error}")` — the exception's own
message, printed. `main` replaces every other exception's message for exactly the
reason §3G1 gives, and this one route around it was not covered by
`test_errors_do_not_leak_exception_secrets`. It is now classified through
`registry.classify_failure` like every other failure, so the console names the *kind*
and the class and not the text.

### 19.4 The list was two claims, and only one was a gap

§18 published `NO_RUN_HISTORY` with eight names and called five of them a gap. Splitting
them by what the code actually does shows they were never the same claim:

- **Writes rows, records nothing** — a gap, because the rollback discarded the
  evidence that anything happened: `fetch-sentiment`, `fetch-stablecoins`,
  `fetch-market-activity`, `store-evidence`. **Closed by §19.1.**
- **Writes no rows at all** — not a gap: `fetch-prices`, `fetch-evidence`, `fetch-cot`,
  `fetch-short` read the registry through `read_connect` and write a document to a
  file. There is no transaction to roll back and no row to record.

So the list became `EXPORT_ONLY_COMMANDS`, four names, and the report now says why each
one is absent rather than implying coverage. **The claim is machine-checked against the
routing**: a test parses `_dispatch`, and for each name requires the branch to exist,
to contain no `registry.get_conn(`, and to open a read-only session or write the export
it is named for. A command that starts writing rows while sitting in that list now
fails a test instead of quietly becoming invisible.

Every command that writes rows to the registry now records a completed run and a
failed one. That is a real end state rather than a shorter list of exceptions.

### 19.5 What this does not establish

- A content hash for three sources is not a content hash for the other twenty-two. The
  column means two things and the audit says so; a migration is a separate decision.
- Recording a run for the crypto context says nothing about whether the index or the
  supply series is *right*. Both remain provider aggregates (§14).
- `EXPORT_ONLY_COMMANDS` being absent from the report is not a health claim. A command
  that writes a file and stores nothing cannot fail in a way that leaves a hole in the
  registry, which is a property of the code and not a judgement about the provider.
