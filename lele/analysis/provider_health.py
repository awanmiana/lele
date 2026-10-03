"""Read the recorded ingest runs and say which sources changed shape.

`ingest_runs` has recorded a request hash, a record hash and a set of counts for
every completed fetch since D01, and for the whole of that time nothing read them.
The development plan carried "provider health monitoring" as a queue item on that
basis. This is it, and building it began by measuring what the table can actually
answer, because the premise behind the item is only half true.

**What the hashes are.** 25 call sites write a run. 17 of them pass no
`records_sha256` at all, and the 8 that do hash *counts and page metadata*, never
the records themselves. So there is no content fingerprint to compare: a provider
that starts returning different numbers is visible, and one that returns the same
numbers with different values underneath it is not. The plan said the table records
"a request hash and a record hash per run"; the first is real and the second is a
count hash present on a third of the fetches. This module therefore promises
exactly one thing, and says so in its own output: **it detects change and silence,
never wrongness.**

**What a changed request hash means.** `request_sha256` is what makes an outcome
comparison mean anything. Two runs that asked the same thing and got different
answers are evidence about the provider; two runs that asked different things are
evidence about nothing. Every comparison here is therefore gated on the request
identity being stable, and a series where it is not is reported as
`request_changed` rather than quietly compared. The column is also not uniformly a
request hash — `sanctions` stores the downloaded file's SHA-256 in it — so a
difference there means the *input* changed, which is the same conclusion for this
purpose and is stated in the report rather than assumed.

**What cannot be seen at all, and is said every time.** A failed run rolls back
with its transaction and is never recorded, which `lele runs` has always said. So
absence of a new run is not evidence that a source works, and this monitor cannot
report a source as failing. The flag list contains no `failed` and no `healthy`,
because both would be claims this table cannot support. Staleness is published as an
age in hours and flagged as `stale`, with the note that an old run is either a
source that stopped producing or an operator who stopped asking, and that the table
cannot say which.

Three rules govern every comparison, each already used elsewhere in this project.

1. *Nothing is compared across a different request.* Stated as a flag, not applied
   silently.
2. *A heuristic threshold is named and published beside the raw numbers it fired
   on.* `COLLAPSE_FRACTION` is a rule of thumb for a fetched count that fell
   sharply, not a verdict about a provider, and `fetched`, `stored` and `pages` are
   always in the output as first/last/min/max so a reader can apply a different one.
3. *A bound that hides data is reported.* The run read is capped, and the report says
   whether the cap was reached, because "this source has no runs" and "this source's
   runs fell outside the bound" are different claims.

Every time, nothing here is evidence about the world: it is evidence about what a
provider last returned to this program, which is a weaker and different thing.
"""
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from ..core import clock, registry

METHOD = "provider_health_v1"

#: Newest runs read. The report says whether this bound was reached, because a
#: source whose runs all fall outside it is not the same as a source with no runs.
MAX_RUNS = 1000
MAX_SERIES = 500

#: A fetched count that falls to this fraction of an earlier fetched count, with
#: the request identity unchanged, is flagged as `count_collapse`. A rule of thumb
#: published with the numbers it fired on, not a judgement about a provider: half
#: is where a halved feed stops looking like ordinary variation, and a reader who
#: wants another threshold has the first/last/min/max counts to apply it to.
COLLAPSE_FRACTION = Decimal("0.5")

#: Age after which a series is flagged `stale`. Seven days is a reporting default,
#: not a claim about how often a provider should publish: a quarterly filing feed and
#: a live flight feed have entirely different cadences, and this table does not know
#: either.
DEFAULT_STALE_HOURS = 168

#: Named reasons a series is flagged. Each is reachable from some input, which a
#: test asserts, because a flag nothing can raise is one a reader cannot rely on.
FLAGS = (
    "request_changed",
    "count_collapse",
    "stored_nothing_once",
    "stored_zero_while_fetched",
    "returned_nothing",
    "never_stored",
    "always_truncated",
    "no_record_hash",
    "stale",
)

#: What this monitor is, stated in its own output rather than only here.
NOT_A_CHECK = (
    "This is not a health check of a provider. It reports what the recorded runs say "
    "about what a source last returned to this program, which is a different thing from "
    "whether the provider is correct.",
    "A failed run rolls back with its transaction and is never recorded, so a source that "
    "errors leaves no trace here. Absence of a new run is not evidence that a source works, "
    "and nothing in this report can call a source failing or healthy.",
    "No run records a hash of the records themselves: 17 of 25 recording sites pass no "
    "records_sha256 and the rest hash counts and page metadata. A provider returning the "
    "same number of different records is invisible here.",
    "`request_sha256` is not uniformly a request hash -- sanctions stores the downloaded "
    "file's SHA-256 in it -- so `request_changed` means the recorded request identity "
    "changed, which may be a changed query or a changed payload.",
    "Staleness is an age in hours. An old run is either a source that stopped producing or "
    "an operator who stopped asking, and this table cannot say which.",
    "A run recorded as truncated stored a prefix of what the provider offered, so coverage "
    "is partial by construction. `completeness` for a source is unknown, never true.",
)


def _time(text) -> datetime | None:
    """An aware instant from stored text, or None.

    None rather than an exception: a row whose timestamp is unreadable belongs in
    the report as unplaceable, not as a command that fails.
    """
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else None


def _iso(moment: datetime | None) -> str | None:
    return moment.astimezone(UTC).isoformat() if moment is not None else None


def _hours(span: timedelta) -> Decimal:
    with localcontext(Context(prec=20, rounding=ROUND_HALF_EVEN)):
        return (Decimal(int(span.total_seconds())) / Decimal(3600)).quantize(Decimal("0.01"))


def _spread(values: list[int]) -> dict:
    """First, last, min and max of a count series, as the report publishes it.

    The summary is what a reader needs to apply their own threshold to, which is why
    it is published rather than reduced to the one the flags fired on.
    """
    if not values:
        return {"first": None, "last": None, "min": None, "max": None}
    return {"first": values[0], "last": values[-1], "min": min(values), "max": max(values)}


def validate(since_hours: int, stale_after_hours: int, limit: int) -> None:
    """Refuse an unusable window before a connection is opened."""
    if type(since_hours) is not int or since_hours < 0:
        raise ValueError("since hours must be a nonnegative integer, 0 for every recorded run")
    if type(stale_after_hours) is not int or stale_after_hours < 0:
        raise ValueError("stale after hours must be a nonnegative integer, 0 to disable")
    if type(limit) is not int or not 1 <= limit <= MAX_SERIES:
        raise ValueError(f"limit must be an integer from 1 to {MAX_SERIES}")


def _group(runs: list[dict]) -> dict:
    """Group runs into the series they describe: one source, one query, one scope."""
    grouped: dict = {}
    for run in runs:
        key = (run.get("source", ""), run.get("query", ""), run.get("country", ""),
               run.get("indicator", ""), run.get("category", ""))
        grouped.setdefault(key, []).append(run)
    return grouped


def _series(key: tuple, runs: list[dict], *, stale_after: Decimal, now) -> dict:
    """One source-query series, oldest run first, with its flags."""
    source, query, country, indicator, category = key
    ordered = sorted(runs, key=lambda row: row.get("started_at") or "")
    fetched = [int(row.get("fetched") or 0) for row in ordered]
    stored = [int(row.get("stored") or 0) for row in ordered]
    skipped = [int(row.get("skipped") or 0) for row in ordered]
    pages = [int(row.get("pages") or 0) for row in ordered]
    missing = [int(row.get("missing") or 0) for row in ordered]
    identities = [row.get("request_sha256") or "" for row in ordered]
    hashes = {value for value in identities if value}
    flags: list[dict] = []

    def flag(name: str, detail: str, **extra) -> None:
        flags.append({"flag": name, "detail": detail, **extra})

    stable = len(hashes) <= 1
    if not stable:
        flag("request_changed",
             f"{len(hashes)} recorded request identities across {len(ordered)} runs, so an "
             "outcome difference between them is evidence about the request, not the provider",
             recorded_identities=len(hashes))
    peak = max(fetched, default=0)
    latest_fetched = fetched[-1]
    if stable and peak > 0:
        with localcontext(Context(prec=20, rounding=ROUND_HALF_EVEN)):
            ratio = Decimal(latest_fetched) / Decimal(peak)
            threshold = COLLAPSE_FRACTION
        if ratio <= threshold and latest_fetched < peak:
            flag("count_collapse",
                 f"fetched fell from a peak of {peak} to {latest_fetched} across "
                 f"{len(ordered)} runs of the same recorded request",
                 peak=peak, latest=latest_fetched,
                 ratio=str(ratio.quantize(Decimal("0.001"))), threshold=str(threshold))
    if max(stored) > 0 and min(stored) == 0:
        # Not a current-shape flag: the series recovered, and a transient that a
        # series recovered from is a fact about the past. It is here because the
        # recorded request was the same both times, so either the provider or this
        # program changed and this table cannot say which, and because `stored`
        # min/max is published either way.
        flag("stored_nothing_once",
             f"an earlier run of the same recorded request stored nothing while a later one "
             f"stored {max(stored)}; the same request identity stored a different number of rows "
             "at a different time, and this table cannot say whether the source or this program "
             "changed")
    if latest_fetched > 0 and stored[-1] == 0:
        flag("stored_zero_while_fetched",
             f"the most recent run fetched {latest_fetched} rows and stored none")
    if latest_fetched == 0 and peak > 0:
        flag("returned_nothing",
             f"the most recent run fetched nothing while an earlier run of the same recorded "
             f"request fetched {peak}")
    if stored and max(stored) == 0:
        flag("never_stored",
             f"none of the {len(ordered)} recorded runs stored a row, which is what a source "
             "with nothing to report and a source that stopped answering both look like here")
    truncated = sum(1 for row in ordered if row.get("truncated"))
    if truncated == len(ordered) and truncated:
        flag("always_truncated",
             f"all {truncated} runs were truncated, so stored coverage is a prefix of what the "
             "provider offered and completeness is unknown",
             truncated_runs=truncated)
    with_hash = sum(1 for row in ordered if row.get("records_sha256"))
    if with_hash < len(ordered):
        flag("no_record_hash",
             f"{len(ordered) - with_hash} of {len(ordered)} runs carry no record hash, and the "
             "ones that do hash counts rather than record contents, so a content change for "
             "this series cannot be detected from this table")
    started = [_time(row.get("started_at", "")) for row in ordered]
    started = [moment for moment in started if moment is not None]
    last = started[-1] if started else None
    age = _hours(now - last) if last is not None else None
    if stale_after > 0 and age is not None and age > stale_after:
        flag("stale",
             f"the most recent recorded run started {_iso(last)}, {age} hours before this "
             "report, which is either a source that stopped producing or an operator who "
             "stopped asking",
             last_run=_iso(last), age_hours=str(age), stale_after_hours=str(stale_after))
    warnings: list[str] = []
    for row in ordered:
        for warning in row.get("warnings") or []:
            if warning not in warnings:
                warnings.append(warning)
    return {
        "source": source,
        "query": query,
        "country": country,
        "indicator": indicator,
        "category": category,
        "runs": len(ordered),
        "first_started_at": _iso(started[0]) if started else None,
        "last_started_at": _iso(last),
        "age_hours": str(age) if age is not None else None,
        "request_identity_stable": stable,
        "recorded_request_identities": len(hashes),
        "records_hashed_runs": with_hash,
        "fetched": _spread(fetched),
        "stored": _spread(stored),
        "skipped": _spread(skipped),
        "missing": _spread(missing),
        "pages": _spread(pages),
        "truncated_runs": truncated,
        "warnings": warnings,
        "flags": flags,
    }


def report(conn, *, source: str = "", since_hours: int = 0,
           stale_after_hours: int = DEFAULT_STALE_HOURS, limit: int = 100,
           now=None) -> dict:
    """What the recorded runs say about each source-query series.

    Read-only and bounded. `since_hours` narrows which runs are compared, `source`
    narrows which are read, and `stale_after_hours` is the age at which a series is
    flagged. Nothing is written and no clock is read unless `now` is omitted.
    """
    validate(since_hours, stale_after_hours, limit)
    moment = now if now is not None else clock.now()
    runs = registry.list_ingest_runs(conn, limit=MAX_RUNS)
    # A full page cannot be distinguished from a complete one, so the bound is
    # reported as reached when the read filled it: that is the honest direction to
    # be wrong in, because it says a series may exist that this report does not show.
    at_bound = len(runs) >= MAX_RUNS
    if source:
        runs = [row for row in runs if row.get("source") == source]
    floor = None if not since_hours else moment - timedelta(hours=since_hours)
    considered, outside = [], 0
    for row in runs:
        started = _time(row.get("started_at", ""))
        if floor is not None and (started is None or started < floor):
            outside += 1
            continue
        considered.append(row)
    grouped = _group(considered)
    stale_after = Decimal(stale_after_hours)
    series = [_series(key, rows, stale_after=stale_after, now=moment)
              for key, rows in sorted(grouped.items())]
    if len(series) > limit:
        series = series[:limit]
        truncated_series = True
    else:
        truncated_series = False
    flagged = [row for row in series if row["flags"]]
    by_flag: dict = {}
    for row in series:
        for entry in row["flags"]:
            by_flag[entry["flag"]] = by_flag.get(entry["flag"], 0) + 1
    sources: dict = {}
    for row in series:
        bucket = sources.setdefault(row["source"], {"series": 0, "flagged": 0, "runs": 0,
                                                    "last_started_at": None})
        bucket["series"] += 1
        bucket["runs"] += row["runs"]
        bucket["flagged"] += 1 if row["flags"] else 0
        if row["last_started_at"] and (bucket["last_started_at"] is None
                                       or row["last_started_at"] > bucket["last_started_at"]):
            bucket["last_started_at"] = row["last_started_at"]
    return {
        "method": METHOD,
        "generated_at": moment.astimezone(UTC).isoformat(),
        "window": {"since_hours": since_hours, "stale_after_hours": stale_after_hours,
                   "source_filter": source or "every recorded source",
                   "runs_considered": len(considered), "runs_outside_window": outside,
                   "runs_read": len(runs), "run_read_bound": MAX_RUNS,
                   "run_read_bound_reached": at_bound,
                   "series": len(series), "series_bound": limit,
                   "series_bound_reached": truncated_series},
        "sources": dict(sorted(sources.items())),
        "series": series,
        "flagged_series": len(flagged),
        "flags": dict(sorted(by_flag.items())),
        "status": "flags_present" if flagged else ("no_recorded_runs" if not series
                                                   else "no_flags"),
        "collapse_threshold": str(COLLAPSE_FRACTION),
        "flags_named": list(FLAGS),
        "what_this_is": "what the recorded ingest runs say about what each source last "
                        "returned to this program, compared only across an unchanged recorded "
                        "request",
        "what_this_is_not": list(NOT_A_CHECK),
        "completeness": "unknown",
        "completeness_note": "a bounded run history cannot show that a source has nothing else "
                             "to report, and a source absent from it may be one this registry "
                             "never fetched",
    }


def summary(conn, **kwargs) -> dict:
    """The same report reduced to what `doctor` shows, plus what it would suggest.

    Bounded on purpose: `doctor` is the command run when something is wrong, so it
    names the flags and the counts and leaves the series to `lele providers`.
    """
    full = report(conn, **kwargs)
    flagged = [row for row in full["series"] if row["flags"]]
    return {
        "method": full["method"],
        "sources": len(full["sources"]),
        "series": full["window"]["series"],
        "runs_considered": full["window"]["runs_considered"],
        "flagged_series": full["flagged_series"],
        "flags": full["flags"],
        "run_read_bound_reached": full["window"]["run_read_bound_reached"],
        "status": full["status"],
        "worst": [{"source": row["source"], "query": row["query"],
                   "flags": [entry["flag"] for entry in row["flags"]],
                   "last_started_at": row["last_started_at"]}
                  for row in flagged[:10]],
        "what_this_is_not": list(NOT_A_CHECK),
    }
