"""Cut stored price history before a stated instant, and record that it went.

`price_bars` and `move_events` grow for as long as a fetcher runs, and on
five-minute bars across many instruments that is the difference between a
registry and a disk-full event. The development plan has carried pruning as a
queue item since the reliability audit; this is it.

Two things make it safe to run, and one of them is the reason the plan asked.

**A cut is a plan until it is applied.** `plan()` is nothing but reads, so
`lele prune plan` runs against any registry, including a read-only mount, and
prints exactly the rows that would go. `apply()` counts through the same
`_predicate` that `plan()` counts through -- one function builds the `WHERE`
clause both of them execute -- so the plan a reader approved and the rows that
disappear are the same set by construction rather than by two implementations
agreeing.

**The loss is recorded, and the record is what the readers use.** After a cut, a
window query over the removed period returns nothing, and "nothing was recorded"
and "nothing happened" are different claims of which only the first is knowable
from a registry. Every applied cut writes a `prune_runs` row naming the instant,
the reason the operator gave, the per-table counts and what remains, and
`registry.stored_bar_span` carries the latest applicable cut into every report
that measures a series. The record describes the loss. It cannot restore it, and
nothing here pretends otherwise: the deleted rows are gone, and a statement of
how many of them there were is not a copy of them.

Six rules govern every cut, each of them a rule some other part of this tool
already follows.

1. *A row is deleted when the last instant it describes is strictly before the
   cut.* A move is dated by the end of its window, an estimate by the end of its
   window, a bar by its close, an article by its own instant. So a row landing
   exactly on the cut is kept, and every bar that survives is a bar that lies
   wholly at or after the cut rather than one the retained history starts inside.
2. *A row that straddles the cut is kept, and counted as straddling.* A bar that
   opens before the cut and closes after it, a move that starts before it and
   ends after it, an estimate whose window begins before it, a control article
   whose window opened before it. Deleting those would remove a measurement that
   is still mostly inside the retained history; keeping them silently would be a
   stored value that can no longer be reproduced from what is left. They are
   reported, per table, in `spans_cut`.
3. *A cut that would leave a series too short to measure is refused.* The
   smallest useful series is the one `moves.detect` will still accept, which is
   `MIN_BASELINE_BARS + 2` closes, and the longest window already stored for that
   series, which has to survive as a window. The floor is derived from those two
   rather than invented, and the series that failed is named.
4. *The reason is required.* This tool does not delete a source URL, a retrieval
   time or a measurement without being told why, and a delete is the largest
   version of that.
5. *A deletion is counted, and the count is checked.* `move_causes` rows hang off
   `move_events` by a cascading foreign key, so deleting a move takes its articles
   with it whatever this module counts; the delete set is the union of the aged
   rows and the attached ones, the breakdown is reported, and the rows the
   database actually removed are compared with the rows the plan counted.
6. *What is not pruned is named, with the reason.* Filed `observations` are the
   registry's evidence and are not measurements of price; `context_measures` is
   bounded by its parent series and not by price history at all, so pruning a
   re-expression of rows that are still stored would discard reproducible
   information and save nothing. `NOT_PRUNED` is quoted in the report and in the
   generated capability summary, because a table left alone and a table forgotten
   look identical in a file listing.

The comparison is done on the stored text, which is the same as comparing instants
only while every row in the series carries one UTC offset and one precision.
`price_bars.open_time` is canonical UTC text written by this tool, but a foreign
writer can put anything there, so the plan measures the series before cutting it
and refuses one whose rows disagree about offset or precision by name, rather
than counting it with a comparison that would put its own boundary in the wrong
place without saying so. An unscoped cut needs one comparison text for every
series it would touch, so a registry holding two offset conventions is refused
rather than cut in two different senses. `moves.verify_stored` already refuses a
stored window it cannot re-derive for the same reason, so a reader meeting the
survivors of a cut is told which of them it can trust.
"""
from datetime import UTC, datetime, timedelta, timezone
import re
from typing import NamedTuple

from ..core import clock, registry
from . import moves

METHOD = "retention_cut_v1"

#: The smallest series `moves.detect` will accept: it refuses fewer than
#: `MIN_BASELINE_BARS + 2` closes, so a cut that leaves fewer bars has not shortened
#: a history, it has deleted the instrument's only price series while leaving the row
#: count looking plausible. The default cut keeps this much. A library caller
#: asking for fewer has it raised to the floor and is told, in `keep_bars`; the CLI
#: refuses it outright, because a number silently replaced by another one is not
#: what the operator typed.
KEEP_BARS_FLOOR = moves.MIN_BASELINE_BARS + 2
DEFAULT_KEEP_BARS = KEEP_BARS_FLOOR

#: Upper bound on the series one plan will walk, and on the control-window keys one
#: plan will read. The first refuses, because walking more would be slow; the
#: second is reported as truncated, because the rows it missed are reported
#: elsewhere.
MAX_SERIES = 500
MAX_CONTROL_KEYS = 20000


class Planned(NamedTuple):
    """One table in a cut, the column that dates a row, and the column that opens
    the window the row describes. An empty `spans` means the row is a point, so it
    falls entirely before the cut or entirely after it."""
    table: str
    dated: str
    spans: str
    note: str


#: The tables a cut touches, children before parents.
PLANNED = (
    Planned("move_causes", "observed_at", "",
            "one stored article per move window or control window, dated by the article"),
    Planned("move_events", "end_time", "start_time",
            "one row per detected move and tier, dated by the end of the move window"),
    Planned("volatility_estimates", "as_of", "window_start",
            "one row per estimator, window and instant, dated by the end of its window"),
    Planned("cause_scans", "as_of", "",
            "one row per scan of an instant, dated by the instant scanned"),
    Planned("price_anomalies", "occurred_at", "",
            "one row per flagged window, dated by the window it flags"),
    Planned("price_bars", "close_time", "",
            "one row per bar, dated by its close, and kept when it opened before the cut"),
)

#: Tables a cut deliberately leaves alone, with the reason. Quoted in the report
#: and in the generated capability summary, because a table left alone and a
#: table forgotten look identical in a file listing.
NOT_PRUNED = {
    "observations":
        "filed evidence with a source, a retrieval time and an availability time. This "
        "command removes measurements of price; it does not remove what a source reported, "
        "and a retention decision here should never quietly discard a filing",
    "context_measures":
        "bounded by its parent series rather than by price history, so a price cut does not "
        "bound it. Pruning a derived row whose parent observation is still stored discards a "
        "reproducible re-expression and saves nothing; cutting the parent observations is a "
        "decision about evidence, not about history",
    "ingest_runs":
        "the record of what was fetched and what came back, with the hashes provider health "
        "reads. Deleting the run that fetched a bar would leave the bar without a provenance "
        "row, which is the state this project treats as a defect",
    "event_store":
        "evidence events, not price measurements; the same reason as observations",
    "semantic_embeddings":
        "derived from stored text rather than from bars, and rebuilt by no command here",
}

#: Named reasons a cut is refused. Each is a list in the report rather than a
#: raised error, so one unusable series does not hide the state of the rest. A
#: test asserts every name here is reachable, because a refusal nothing can
#: trigger is one a reader cannot rely on being told.
REFUSALS = (
    "cut_not_in_the_past",
    "unstated_reason",
    "too_many_series",
    "timestamps_not_canonical",
    "cut_precision_not_stored",
    "series_below_floor",
    "scope_has_no_stored_bars",
)

LIMITATIONS = (
    "A cut is irreversible from inside this tool. The record states how many rows went and what "
    "remains; it is not a copy of them, so take a `lele backup` first if the history is worth "
    "keeping.",
    "A derived row that survives the cut is kept deliberately and is no longer reproducible "
    "from the remaining bars. `moves.verify_stored` refuses such a row by name when a reader "
    "asks, and the count appears in that report rather than here.",
    "Pruning changes what a later report can measure, and nothing that was already reported. A "
    "figure computed before a cut and one computed after it are two measurements of two "
    "different inputs, and neither is a restatement of the other.",
    "Nothing here decides how much history is worth keeping. The cut and the floor are the "
    "operator's; this command refuses only the ones that would leave a series unmeasurable, and "
    "reports what the rest would do.",
    "One refusal stops the whole cut rather than skipping the series that caused it, because a "
    "partial cut that quietly leaves one series behind is the failure this project keeps "
    "finding. Narrow the cut with --series to act on a single series.",
)

_OFFSET = re.compile(r"([+-]\d{2}:\d{2}|Z)$")


def _offset_suffix(text: str) -> str:
    match = _OFFSET.search(text or "")
    return match.group(1) if match else ""


def _instant(text: str) -> datetime:
    """An aware instant, or a refusal naming what is wrong with the text."""
    try:
        moment = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{text!r} is not an ISO-8601 instant") from exc
    if moment.tzinfo is None:
        raise ValueError("an instant needs a UTC offset; a bare date or time is not an instant")
    return moment


def _offset_from_suffix(suffix: str) -> timezone:
    if suffix in ("", "Z", "+00:00", "-00:00"):
        return UTC
    hours, minutes = int(suffix[1:3]), int(suffix[4:6])
    if hours > 23 or minutes > 59:
        raise ValueError(f"{suffix!r} is not a UTC offset")
    delta = timedelta(hours=hours, minutes=minutes)
    return timezone(delta if suffix[0] == "+" else -delta)


def _control_pattern(instrument_key: str, interval_seconds: int) -> str:
    """A LIKE pattern for the control keys of one series, wildcards escaped.

    `instrument_key` is operator-supplied text, so `%`, `_`, `|` and the escape
    character itself are escaped rather than treated as pattern characters that
    would select another series' articles.
    """
    escaped = (instrument_key.replace("\\", "\\\\").replace("%", "\\%")
               .replace("_", "\\_").replace("|", "\\|"))
    return f"{escaped}|{interval_seconds}|%" if interval_seconds else f"{escaped}|%"


def _control_window_start(control_key: str) -> str:
    """The window start a control key carries, or an empty string.

    `causes._control_windows` builds the key as
    `instrument_key|interval_seconds|window_start`, so its last field is the
    instant the control window opens at.
    """
    parts = str(control_key).rsplit("|", 1)
    return parts[1] if len(parts) == 2 else ""


def _instrument_scope(instrument_key: str, interval_seconds: int) -> tuple[str, list]:
    """The WHERE fragment naming one instrument and, where the table has such a
    column, one interval. `price_anomalies` is deliberately not narrowed by
    interval: it has no interval column, so a cut scoped to a single interval
    cannot select from it and does not pretend to."""
    if not instrument_key:
        return "1=1", []
    clause: str = "instrument_key=?"
    params: list = [instrument_key]
    if interval_seconds:
        clause, params = f"{clause} AND interval_seconds=?", [*params, interval_seconds]
    return clause, params


def _predicate(item: Planned, instrument_key: str, interval_filter: int, series_interval: int,
               cut_text: str) -> tuple[str, list]:
    """The one WHERE clause that selects the rows of `item` a cut removes.

    `plan` counts through it and `apply` deletes through it, which is what makes
    the approved plan and the deleted rows the same set by construction.

    Two intervals, because they answer different questions. `interval_filter` is
    what the operator scoped the cut to, and zero means every interval.
    `series_interval` is the series currently being walked, and it is what
    restricts the bars: a cut scoped to one instrument with no interval walks that
    instrument's series one at a time, and without it each pass would select the
    same bars again.
    """
    if item.table == "move_causes":
        # An article attached to a move that is going disappears with it through
        # the cascading foreign key, whatever this module counts, and an article
        # that predates the cut goes on its own date. The delete set is the union,
        # and the breakdown is reported so the cascade's share is visible.
        if instrument_key:
            return ("(move_event_id IN (SELECT id FROM move_events WHERE instrument_key=?"
                    " AND interval_seconds=? AND end_time<?))"
                    " OR ((role='control' AND control_key LIKE ? ESCAPE '\\')"
                    " AND observed_at<?)",
                    [instrument_key, interval_filter, cut_text,
                     _control_pattern(instrument_key, interval_filter), cut_text])
        return ("(move_event_id IN (SELECT id FROM move_events WHERE end_time<?))"
                " OR (observed_at<?)", [cut_text, cut_text])
    if item.table == "price_anomalies" and interval_filter:
        return "0", []
    if item.table == "price_bars" and (instrument_key or series_interval):
        return ("instrument_key=? AND interval_seconds=? AND close_time<?",
                [instrument_key, series_interval, cut_text])
    scope, params = _instrument_scope(instrument_key, interval_filter)
    return f"{scope} AND {item.dated}<?", [*params, cut_text]


def _count(conn, sql: str, params: list) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def _canonical(conn, instrument_key: str, interval_seconds: int) -> dict:
    """Whether this series' timestamps may be compared as text at all.

    ISO-8601 text with a constant offset and a constant length sorts in the same
    order as the instants it names. A series whose rows disagree about either does
    not, and a cut that counted it would place its own boundary in the wrong place
    without saying so. One indexed scan, and a refusal by name.

    Both columns a bar is compared on are measured, because a cut dates a bar by
    its close and its straddling test reads its open: a series storing an open in
    one convention and a close in another would pass a check on the first column
    and be counted with the second.
    """
    row = conn.execute(
        "SELECT open_time, close_time FROM price_bars WHERE instrument_key=?"
        " AND interval_seconds=? ORDER BY open_time LIMIT 1",
        (instrument_key, interval_seconds)).fetchone()
    if row is None:
        return {"canonical": True, "sample": None, "offset": "+00:00", "microseconds": False}
    sample, closing = row[0], row[1]
    suffix = _offset_suffix(sample)
    if not suffix or _offset_suffix(closing) != suffix:
        return {"canonical": False, "sample": sample,
                "reason": "the stored timestamps carry no single UTC offset, so their text "
                          "order is not their instant order"}
    others = _count(conn, "SELECT count(*) FROM price_bars WHERE instrument_key=?"
                          " AND interval_seconds=? AND (length(open_time)<>?"
                          " OR substr(open_time, -6)<>substr(?, -6)"
                          " OR length(close_time)<>? OR substr(close_time, -6)<>substr(?, -6))",
                    [instrument_key, interval_seconds, len(sample), sample, len(closing), closing])
    if others:
        return {"canonical": False, "sample": sample,
                "reason": f"{others} stored timestamp(s) differ in offset or precision from "
                          f"{sample!r}, so the text order is not the instant order"}
    return {"canonical": True, "sample": sample, "offset": suffix,
            "microseconds": "." in sample.split("T")[-1]}


def _cut_text(cut: datetime, canonical: dict) -> str:
    """The cut rendered in the series' own offset and precision.

    Rendering it in the shape the rows are stored in is what makes a text
    comparison a comparison of instants. Rendering it in another shape would
    compare a `Z` against a `+00:00` and put the boundary a second out.
    """
    moment = cut.astimezone(_offset_from_suffix(canonical.get("offset", "+00:00")))
    if canonical.get("microseconds"):
        return moment.isoformat(timespec="microseconds")
    return moment.replace(microsecond=0).isoformat()


def _series_inventory(conn, instrument_key: str, interval_seconds: int) -> list[dict]:
    where, params = _instrument_scope(instrument_key, interval_seconds)
    sql = ("SELECT instrument_key, interval_seconds, count(*) AS bars, min(open_time) AS first,"
           f" max(open_time) AS last FROM price_bars WHERE {where}"
           " GROUP BY instrument_key, interval_seconds"
           " ORDER BY instrument_key, interval_seconds")
    return [dict(row) for row in conn.execute(sql, params)]


def _longest_window_bars(conn, instrument_key: str, interval_seconds: int) -> int:
    hours = conn.execute("SELECT max(move_hours) FROM move_events WHERE instrument_key=?"
                         " AND interval_seconds=?",
                         (instrument_key, interval_seconds)).fetchone()[0]
    return int(hours) * 3600 // interval_seconds + 1 if hours else 0


def _table_plan(conn, item: Planned, instrument_key: str, interval_filter: int,
                series_interval: int, cut_text: str) -> dict:
    """Count what a cut removes from one table, and what it would leave straddling."""
    clause, params = _predicate(item, instrument_key, interval_filter, series_interval, cut_text)
    row: dict = {"delete": _count(conn, f"SELECT count(*) FROM {item.table} WHERE {clause}",
                                  params), "spans_cut": 0, "note": item.note}
    if item.spans:
        scope, scope_params = _instrument_scope(instrument_key, interval_filter)
        row["spans_cut"] = _count(conn, f"SELECT count(*) FROM {item.table} WHERE {scope}"
                                       f" AND {item.spans}<? AND {item.dated}>=?",
                                  [*scope_params, cut_text, cut_text])
    elif item.table == "price_bars":
        # A bar opens before the cut and closes after it: kept, because the rule
        # dates a bar by its close, and reported because the retained history now
        # starts inside it.
        scope, scope_params = _instrument_scope(instrument_key, series_interval)
        row["spans_cut"] = _count(conn, f"SELECT count(*) FROM {item.table} WHERE {scope}"
                                       " AND open_time<? AND close_time>=?",
                                  [*scope_params, cut_text, cut_text])
    if item.table == "move_causes":
        row.update(_move_cause_breakdown(conn, instrument_key, interval_filter, cut_text,
                                         row["delete"]))
    if item.table == "price_anomalies":
        row["interval_note"] = (
            "skipped: the table names no interval, so a cut scoped to one interval cannot select "
            "from it and does not try; run the cut without --interval to include it"
            if interval_filter else
            "the table names no interval, so this cut selects every interval of the instrument")
    return row


def _move_cause_breakdown(conn, instrument_key: str, interval_seconds: int, cut_text: str,
                          union: int) -> dict:
    """Say where the removed articles came from, and count the split windows.

    `aged` is the articles that predate the cut on their own date. `attached` is
    the articles that go because their move is going. `delete` is the union.
    `spans_cut` counts kept articles whose *window* opened before the cut: the
    article stays, and its window is now only partly stored.
    """
    if instrument_key:
        pattern = _control_pattern(instrument_key, interval_seconds)
        aged = _count(conn, "SELECT count(*) FROM move_causes WHERE role='control' AND"
                             " control_key LIKE ? ESCAPE '\\' AND observed_at<?",
                      [pattern, cut_text])
        attached = _count(conn, "SELECT count(*) FROM move_causes WHERE move_event_id IN"
                                " (SELECT id FROM move_events WHERE instrument_key=? AND"
                                " interval_seconds=? AND end_time<?)",
                          [instrument_key, interval_seconds, cut_text])
        keys = conn.execute("SELECT control_key FROM move_causes WHERE role='control' AND"
                            " control_key LIKE ? ESCAPE '\\' AND observed_at>=? LIMIT ?",
                            [pattern, cut_text, MAX_CONTROL_KEYS])
    else:
        aged = _count(conn, "SELECT count(*) FROM move_causes WHERE observed_at<?", [cut_text])
        attached = _count(conn, "SELECT count(*) FROM move_causes WHERE move_event_id IN"
                                " (SELECT id FROM move_events WHERE end_time<?)", [cut_text])
        keys = conn.execute("SELECT control_key FROM move_causes WHERE role='control' AND"
                            " observed_at>=? LIMIT ?", [cut_text, MAX_CONTROL_KEYS])
    spanning, examined = 0, 0
    for row in keys:
        examined += 1
        start = _control_window_start(row[0])
        if start and start < cut_text:
            spanning += 1
    return {"delete": union, "aged": aged, "attached": attached, "spans_cut": spanning,
            "control_windows_examined": examined,
            "control_windows_truncated": examined >= MAX_CONTROL_KEYS}


def _sum_counts(entries: list[dict], note: str) -> dict:
    """Sum per-series counts into the registry-wide block, keeping every key.

    A key present for one series and absent for another is kept rather than
    dropped, because a field that disappears at the aggregation boundary is a field
    a reader stops seeing: `aged`, `attached` and the control-window counters of
    `move_causes` all mean something at the registry level too.
    """
    totals: dict = {"delete": 0, "spans_cut": 0}
    for entry in entries:
        for name, value in entry.items():
            if name == "note":
                continue
            if isinstance(value, bool):
                totals[name] = totals.get(name, False) or value
            elif isinstance(value, int):
                totals[name] = totals.get(name, 0) + value
            else:
                totals.setdefault(name, value)
    totals["note"] = note
    if not entries:
        totals["skipped"] = "no series could be planned for this cut; see refusals"
    return totals


def plan(conn, cut: str, *, keep_bars: int = DEFAULT_KEEP_BARS, instrument_key: str = "",
         interval_seconds: int = 0, reason: str = "", now=None) -> dict:
    """What a cut at `cut` would remove, without removing anything.

    Every number here is a read, so a plan can be produced against a read-only
    mount. `apply` runs this function and then deletes exactly what it counted.
    """
    if type(keep_bars) is not int or keep_bars < 1:
        raise ValueError("keep_bars must be a positive integer")
    if type(interval_seconds) is not int or interval_seconds < 0:
        raise ValueError("interval_seconds must be a nonnegative integer")
    if instrument_key and not str(instrument_key).strip():
        raise ValueError("instrument_key must be nonempty text when given")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("reason must be text")
    moment = now if now is not None else clock.now()
    cut_instant = _instant(cut)
    scoped = bool(instrument_key)
    report: dict = {
        "method": METHOD,
        "cut": cut_instant.astimezone(UTC).isoformat(),
        "cut_rule": "a row is removed when the last instant it describes is strictly before the "
                    "cut, so a row landing exactly on the cut is kept and every surviving bar "
                    "lies wholly at or after it",
        "planned_at": moment.astimezone(UTC).isoformat(),
        "applied": False,
        "reason": reason or "",
        "scope": {"instrument_key": instrument_key, "interval_seconds": interval_seconds},
        "keep_bars": {"requested": keep_bars, "floor": KEEP_BARS_FLOOR,
                      "effective": max(keep_bars, KEEP_BARS_FLOOR),
                      "note": "the floor is the smallest number of bars the move detector will "
                              "still measure, so a smaller request is raised to it rather than "
                              "honoured; a series whose own longest stored window is longer is "
                              "given a higher floor in its entry below"},
        "series": [],
        "tables": {},
        "refusals": {},
        "refused": False,
        "not_pruned": dict(sorted(NOT_PRUNED.items())),
        "limitations": list(LIMITATIONS),
    }

    def refuse(name: str, detail) -> None:
        report["refused"] = True
        report["refusals"].setdefault(name, []).append(detail)

    if cut_instant >= moment:
        refuse("cut_not_in_the_past", {
            "cut": report["cut"], "now": moment.astimezone(UTC).isoformat(),
            "reason": "a cut at or after the present would remove the whole series, and the "
                      "present is an input to this tool rather than a guess"})

    inventory = _series_inventory(conn, instrument_key, interval_seconds)
    if len(inventory) > MAX_SERIES:
        refuse("too_many_series", {
            "series": len(inventory), "limit": MAX_SERIES,
            "reason": "narrow the cut with --series rather than walking every series in the "
                      "registry; nothing was counted"})
        inventory = []
    elif not inventory:
        refuse("scope_has_no_stored_bars", {
            "instrument_key": instrument_key or "every stored series",
            "interval_seconds": interval_seconds or "any",
            "reason": "no stored bars in this scope, so there is nothing to cut and nothing to "
                      "hold a floor against"})

    shapes: dict = {}
    for entry in inventory:
        key, interval = entry["instrument_key"], int(entry["interval_seconds"])
        canonical = _canonical(conn, key, interval)
        if not canonical["canonical"]:
            refuse("timestamps_not_canonical", {"instrument_key": key, "interval_seconds": interval,
                                                "reason": canonical["reason"]})
            continue
        if not canonical["microseconds"] and cut_instant.microsecond:
            refuse("cut_precision_not_stored", {
                "instrument_key": key, "interval_seconds": interval,
                "reason": "this series stores whole-second timestamps and the cut carries "
                          "sub-second precision, which the stored text cannot resolve"})
            continue
        cut_text = _cut_text(cut_instant, canonical)
        shapes[(key, interval)] = cut_text
        longest = _longest_window_bars(conn, key, interval)
        floor = max(report["keep_bars"]["effective"], longest + 1)
        bars = int(entry["bars"])
        scope, scope_params = _instrument_scope(key, interval)
        to_delete = _count(conn, f"SELECT count(*) FROM price_bars WHERE {scope}"
                                 " AND close_time<?", [*scope_params, cut_text])
        straddling = _count(conn, f"SELECT count(*) FROM price_bars WHERE {scope}"
                                    " AND open_time<? AND close_time>=?",
                           [*scope_params, cut_text, cut_text])
        remaining = bars - to_delete
        series_row = {
            "instrument_key": key,
            "interval_seconds": interval,
            "bars": bars,
            "bars_to_delete": to_delete,
            "bars_remaining": remaining,
            "bars_spanning_cut": straddling,
            "first_bar": entry["first"],
            "last_bar": entry["last"],
            "floor_bars": floor,
            "longest_stored_window_bars": longest,
            "meets_floor": remaining >= floor,
        }
        if to_delete:
            series_row["last_deleted_bar"] = conn.execute(
                "SELECT max(open_time) FROM price_bars WHERE instrument_key=? AND"
                " interval_seconds=? AND close_time<?",
                (key, interval, cut_text)).fetchone()[0]
        if remaining < floor:
            refuse("series_below_floor", {
                "instrument_key": key, "interval_seconds": interval, "bars_remaining": remaining,
                "floor_bars": floor,
                "reason": f"the cut would leave {remaining} bars where {floor} are needed: the "
                          f"move detector needs {KEEP_BARS_FLOOR} closes, and the longest window "
                          f"already stored for this series needs {longest} bars"})
        if scoped:
            series_row["tables"] = {
                item.table: _table_plan(conn, item, key, interval_seconds, interval, cut_text)
                for item in PLANNED}
        report["series"].append(series_row)

    cut_texts = set(shapes.values())
    if not scoped:
        if len(cut_texts) > 1:
            refuse("timestamps_not_canonical", {
                "reason": f"an unscoped cut needs one comparison text and this registry stores "
                          f"{len(cut_texts)}; narrow the cut with --series, or record the "
                          "timestamps in UTC as every writer in this tool does",
                "stored_forms": sorted(cut_texts)[:MAX_CONTROL_KEYS]})
        unscoped_cut = cut_texts.pop() if len(cut_texts) == 1 else None
    else:
        unscoped_cut = None
    for item in PLANNED:
        if scoped:
            entries = [row["tables"][item.table] for row in report["series"]
                       if "tables" in row]
            report["tables"][item.table] = _sum_counts(entries, item.note)
        else:
            report["tables"][item.table] = (
                _table_plan(conn, item, "", 0, 0, unscoped_cut) if unscoped_cut
                else {"delete": 0, "spans_cut": 0, "note": item.note,
                      "skipped": "no series could be planned for this cut; see refusals"})
    report["delete_total"] = sum(int(row["delete"]) for row in report["tables"].values())
    report["spans_total"] = sum(int(row["spans_cut"]) for row in report["tables"].values())
    report["bars_to_delete"] = sum(int(row["bars_to_delete"]) for row in report["series"])
    report["note"] = ("nothing has been removed. This is the plan `lele prune apply` would carry "
                      "out, and the reason a plan is a separate command is that a deletion cannot "
                      "be read back")
    return report


def apply(conn, cut: str, *, reason: str, keep_bars: int = DEFAULT_KEEP_BARS,
          instrument_key: str = "", interval_seconds: int = 0, now=None) -> dict:
    """Delete what `plan` counted, and record that the cut happened.

    The plan is computed first and every deletion goes through the same predicate
    it counted with, so the two cannot disagree; each delete's row count is
    compared with its planned count and a mismatch raises rather than being
    reported beside the plan. The caller owns the transaction, so a failure
    anywhere here leaves the registry as it was.
    """
    report = plan(conn, cut, keep_bars=keep_bars, instrument_key=instrument_key,
                  interval_seconds=interval_seconds, reason=reason, now=now)
    if not isinstance(reason, str) or not reason.strip():
        # Refused in the report rather than raised, so the refusal is one of the
        # named kinds a caller can read rather than an exception type it has to
        # know about.
        report["refused"] = True
        report["refusals"]["unstated_reason"] = [{
            "reason": "" if reason is None else reason,
            "note": "a cut removes stored measurements with their source and retrieval time "
                    "attached, and nothing here is removed without a stated reason"}]
        return report
    if report["refused"]:
        return report
    moment = now if now is not None else clock.now()
    applied_at = moment.astimezone(UTC).isoformat()
    scoped = bool(instrument_key)
    records = [{"instrument_key": entry["instrument_key"],
                "interval_seconds": entry["interval_seconds"], "bars": entry["bars"],
                "bars_deleted": entry["bars_to_delete"], "first_bar": entry["first_bar"],
                "last_bar": entry["last_bar"],
                "last_deleted_bar": entry.get("last_deleted_bar")} for entry in report["series"]]
    # One pass per scope. A scoped cut walks its own series; an unscoped one walks
    # the registry once, because a table that is not series-scoped has no per-series
    # cut to make and deleting it once per series would remove it on the first.
    if scoped:
        passes = [(entry["instrument_key"], int(interval_seconds),
                   int(entry["interval_seconds"]),
                   _cut_text(_instant(report["cut"]),
                             _canonical(conn, entry["instrument_key"],
                                        int(entry["interval_seconds"]))),
                   {"price_bars": int(entry["bars_to_delete"]),
                    **{name: int(counts["delete"])
                       for name, counts in entry.get("tables", {}).items()}})
                  for entry in report["series"]]
    else:
        passes = [("", 0, 0, _registry_cut_text(report, conn, report["series"]),
                   {item.table: int(report["tables"][item.table]["delete"]) for item in PLANNED})]
    deleted_by_table = {item.table: 0 for item in PLANNED}
    for key, interval_filter, series_interval, cut_text, planned in passes:
        for item in PLANNED:
            expected = int(planned.get(item.table, 0))
            if not expected:
                continue
            clause, params = _predicate(item, key, interval_filter, series_interval, cut_text)
            cursor = conn.execute(f"DELETE FROM {item.table} WHERE {clause}", params)
            removed = int(cursor.rowcount if cursor.rowcount is not None else 0)
            if removed != expected:
                raise RuntimeError(
                    f"planned {expected} {item.table} rows and removed {removed}; the transaction "
                    "is rolled back rather than the cut reported as done")
            deleted_by_table[item.table] += removed
    run_id = registry.record_prune_run(
        conn, cut=report["cut"], reason=reason, keep_bars=report["keep_bars"]["effective"],
        applied_at=applied_at, instrument_key=instrument_key, interval_seconds=interval_seconds,
        deleted=deleted_by_table,
        spans_cut={name: int(row["spans_cut"]) for name, row in report["tables"].items()},
        series=records)
    report["applied"] = True
    report["applied_at"] = applied_at
    report["prune_run_id"] = run_id
    report["deleted_by_table"] = deleted_by_table
    report["series_records"] = records
    if sum(deleted_by_table.values()) != report["delete_total"]:
        raise RuntimeError(
            f"the plan counted {report['delete_total']} rows and {sum(deleted_by_table.values())} "
            "were removed; the transaction is rolled back rather than the cut reported as done")
    report["note"] = (f"the counted rows have been removed and the cut is recorded as prune run "
                      f"{run_id}. That record states what went, not what it contained: the rows "
                      "themselves are gone, and only a backup holds them")
    return report


def _registry_cut_text(report: dict, conn, series: list[dict]) -> str:
    """The comparison text an unscoped cut deletes with, re-derived from the
    series it covers rather than from whichever series happened to be first."""
    shapes = {_cut_text(_instant(report["cut"]), _canonical(conn, row["instrument_key"],
                                                            int(row["interval_seconds"])))
              for row in series}
    if len(shapes) != 1:
        raise RuntimeError("an unscoped cut was planned with one comparison text and is being "
                           "applied with another")
    return shapes.pop()


def history(conn, limit: int = 50) -> dict:
    """Every recorded cut, newest first, with what each one removed.

    This is the answer to "why is there nothing before 2024", and it is a record of
    a removal rather than an inference: a registry cannot know what it never
    fetched.
    """
    runs = registry.list_prune_runs(conn, limit=limit)
    return {
        "method": f"{METHOD}_history",
        "runs": len(runs),
        "recorded": runs,
        "note": "each row is a cut this registry applied. A window before a cut returns no rows "
                "because they were removed, which is a different statement from a window that "
                "was never fetched; `lele explain` names the cut that applies",
        "not_pruned": dict(sorted(NOT_PRUNED.items())),
    }
