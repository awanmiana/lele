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

- 698 offline tests, `ResourceWarning` promoted to an error, so a leaked
  database handle fails the run.
- `ruff check` clean over `lele/` and `tests/` under the strict profile.
- `mypy` clean over 53 source files, with `disallow_untyped_defs` enforced on
  the foundation modules.
- `python -m compileall -q lele` clean.
- Live end-to-end on 2026-09-28: `init` → `fetch fdic` (50 institutions) →
  `fetch-history 1 BTCUSDT` (799 real daily bars) → `moves` (124 candidates,
  100 retained, all exactly 24.0000 h, 0 gaps) → `scan` and `causes context`.
  Where nothing is stored, the output says so rather than inventing a result.

New test files, each of which fails without its fix:
`tests/test_db_guarantees.py` (18), `tests/test_clock.py` (12),
`tests/test_price_gaps.py` (9), `tests/test_window_queries.py` (35),
`tests/test_window_cli.py` (20), plus rewritten
`tests/test_realtime_prospective.py` (8, offline).

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
4. **Attribution quality, not just availability.** Even where records exist,
   `explain` reports coincidence. Whether a policy publication preceded a move
   and whether it caused it are different questions, and only the first is
   answerable here.

---

## 7. Still open, with reasons
