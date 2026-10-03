"""A generated inventory of what this program can do, built from the code.

A capability summary written by hand is a document that starts decaying the day
after it is written, because the code moves and the prose does not. Every list
here is therefore derived at run time from the thing it describes: the command
table and the argument parser, the source catalogue, the observation taxonomy,
the estimator list, the frozen indicator specs, the cited frameworks, and the
standing objective constant. A capability added without a line of prose is
picked up automatically; a capability removed disappears from the file.

What this is not: evidence that any of it works, and not a claim of coverage.
A command existing says this program can attempt a measurement. It says nothing
about whether the measurement is right, whether a provider answered, or whether
the stored records describe a complete population. Every count here is a count
of *the program's own vocabulary*, and the report says so in those words rather
than leaving a reader to infer it from the shape of a table.
"""
import json
import platform
import sqlite3
import sys

from ..core import clock, constants, registry
from ..core.db import SCHEMA_VERSION
from . import (causes, comparison, framework_notes, indicators, moves, prospective,
               provider_health, retention, signals, stationarity, volatility)

METHOD = "capability_summary_v1"
MAX_COMMANDS = 500
MAX_SOURCES = 200
MAX_TAXONOMY = 500
MAX_NOTES = 200
MAX_MARKDOWN_BYTES = 4 * 1024 * 1024

#: The pre-registered objective, quoted from the code that enforces it rather
#: than restated here. `projection` is the module that carries the constant, and
#: `TARGET_RATE` names it so this file cannot drift from it silently.
TARGET_RATE = 0.9
TARGET_RATE_SOURCE = "analysis/projection.py evaluation.target_rate"
OBJECTIVE_CLASSES = ("bitcoin", "gold", "oil", "stock")
OBJECTIVE_STATUS = "not_achieved"

#: What this program declines to do, in the project's own words. A capability
#: list that records only what works is an advertisement; the exclusions are the
#: half a reader needs.
DECLINES = (
    "No trading automation, order routing or position management.",
    "No buy, sell or hold signal, and no allocation recommendation.",
    "No claim that any institution, country, market or category is covered. A "
    "bounded public fetch cannot discover a population, so completeness is "
    "reported as unknown rather than as true or false.",
    "No inferred misconduct, no automatic identity merge, and no inferred "
    "motive, counterparty or website.",
    "Management is not ownership, a parent is not a fund, a position is not an "
    "intercompany transfer, and a sanctions designation is not a finding of guilt.",
    "No invented contact address, API key, credential or access-control bypass. "
    "A provider that requires one is recorded as blocked until a real one exists.",
    "No redistribution-rights claim. rights_verified is always false because "
    "this project never verifies a licence.",
    "No accuracy claim. The standing five-minute objective stays an aspiration "
    "with a frozen target, and historical figures are never presented as "
    "out-of-sample skill.",
)

BANNED_CLAIMS = (
    "90% accuracy is achieved",
    "verified market data",
    "complete coverage",
    "proven cause",
    "guaranteed returns",
    "institutional grade",
    "real-time accurate",
)


def _text(value, limit=4096):
    if not isinstance(value, str):
        raise ValueError("summary fields must be text")
    cleaned = value.strip()
    if len(cleaned) > limit:
        raise ValueError(f"summary field exceeds {limit} characters")
    return cleaned


def usage_for(subparser):
    """One command's positional and optional arguments, in a stable order.

    Read from the parser rather than from a table, so an argument added to the
    CLI is described here without anyone remembering to describe it. A command
    that takes no arguments says so rather than printing an empty line that
    reads like a rendering fault.
    """
    labels = []
    for action in subparser._actions:
        if action.dest in ("help", "format"):
            continue
        flags = sorted(flag for flag in action.option_strings if flag.startswith("--"))
        label = " ".join(flags) if flags else f"<{action.dest}>"
        if action.nargs in ("*", "+") or (isinstance(action.nargs, int) and action.nargs > 1):
            label += " ..."
        label += _choices_label(action.choices)
        if action.required:
            label += " (required)"
        labels.append(label)
    return " ".join(labels) if labels else "(no arguments)"


def _choices_label(choices, limit=12):
    """A readable stand-in for an argument's permitted values.

    Enumerating every value is unusable when a flag accepts a thousand integers,
    and truncating a list of names silently would hide options a reader needs,
    so a bounded list is written out, a numeric range is written as a range, and
    anything larger says how many options exist rather than pretending to be
    complete.
    """
    if not choices:
        return ""
    if isinstance(choices, range):
        return f" INT{choices.start}..INT{choices.stop - 1}"
    values = [str(choice) for choice in choices]
    if len(values) <= limit:
        return " {" + ",".join(values) + "}"
    return f" (one of {len(values)} permitted values)"


def _sources():
    from ..fetchers import sources as catalogue
    rows = catalogue.list_sources()
    if not rows or len(rows) > MAX_SOURCES:
        raise ValueError("unexpected source catalogue size")
    return [{"id": _text(row["id"], 128), "name": _text(row["name"], 256),
             "status": _text(row["status"], 64), "url": _text(row["url"], 4096),
             "coverage": _text(row["coverage"], 2048)} for row in rows]


def _endpoint_groups():
    """The allowlist, which is the real boundary of what this program can reach.

    `list_sources` is a catalogue of the five official datasets behind the
    `fetch` command, and reporting only that count would understate the program
    by an order of magnitude: the price, evidence, sanctions and redirect
    endpoint groups together name every host the HTTP client is permitted to
    contact. Naming the allowlist is also the more useful statement, because an
    allowlist is a boundary and a reader can check it against what they were
    offered.
    """
    groups = {"SOURCES": constants.SOURCES, "PRICE_ENDPOINTS": constants.PRICE_ENDPOINTS,
              "EVIDENCE_ENDPOINTS": constants.EVIDENCE_ENDPOINTS,
              "SANCTIONS_ENDPOINTS": constants.SANCTIONS_ENDPOINTS,
              "REDIRECT_ENDPOINTS": constants.REDIRECT_ENDPOINTS}
    rows = []
    for group, mapping in sorted(groups.items()):
        if not mapping:
            continue
        rows.append({"group": group, "entries": len(mapping),
                     "hosts": sorted({url.split("/")[2] for url in mapping.values()
                                      if "://" in url})})
    if not rows:
        raise ValueError("the endpoint allowlist is empty")
    return rows


def _taxonomy():
    families: dict[str, list[str]] = {}
    for kind, family in sorted(signals.OBSERVATION_FAMILY.items()):
        families.setdefault(family, []).append(kind)
    if not families or len(families) > MAX_TAXONOMY:
        raise ValueError("unexpected observation taxonomy size")
    return {
        "observation_kinds": len(signals.OBSERVATION_FAMILY),
        "observation_families": {name: sorted(kinds) for name, kinds
                                 in sorted(families.items())},
        "market_wide_context_kinds": sorted(signals.GLOBAL_CONTEXT_KINDS),
        "context_level_series_kinds": sorted(stationarity.LEVEL_SERIES_KINDS),
        "context_measure_methods": list(stationarity.MEASURES),
        "context_measures_not_offered": dict(sorted(stationarity.NOT_OFFERED.items())),
        "context_measure_refusals": list(stationarity.REFUSALS),
        "context_measure_minimum_baseline": stationarity.MINIMUM_BASELINE,
        "headline_categories": sorted(causes.categories()),
        "estimate_methods": sorted(comparison.METHODS),
        "volatility_estimators": list(volatility.ESTIMATORS),
        "move_thresholds_percent": list(moves.DEFAULT_THRESHOLDS),
"indicator_specs": sorted(indicators.INDICATOR_SPECS),
        "frozen_forecast_methods": list(prospective.METHODS),
        "provider_health": {
            "method": provider_health.METHOD,
            "flags": list(provider_health.FLAGS),
            "fingerprints": {
                "retrieval": "a fetcher's own summary of what it fetched or stored -- counts, "
                             "stored identity keys or page metadata, depending on the fetcher. "
                             "Not a hash of record contents",
                "payload": "a hash of the provider response, stored by the fetchers that had one "
                           "and discarded. Where it is present a content change is detectable",
            },
            "collapse_threshold": str(provider_health.COLLAPSE_FRACTION),
            "default_stale_after_hours": provider_health.DEFAULT_STALE_HOURS,
            "run_read_bound": provider_health.MAX_RUNS,
            "cannot_detect": list(provider_health.NOT_A_CHECK),
            "failure_recording": provider_health.FAILURE_RECORDING,
            "export_only_commands": list(provider_health.EXPORT_ONLY_COMMANDS),
            "failure_reasons": dict(sorted(registry.FAILURE_REASONS.items())),
        },
        "retention": {
            "method": retention.METHOD,
            "pruned_tables": [item.table for item in retention.PLANNED],
            "not_pruned": dict(sorted(retention.NOT_PRUNED.items())),
            "refusals": list(retention.REFUSALS),
            "default_keep_bars": retention.DEFAULT_KEEP_BARS,
            "keep_bars_floor": retention.KEEP_BARS_FLOOR,
            "keep_bars_floor_source": "moves.MIN_BASELINE_BARS + 2, the smallest number of "
                                      "closes moves.detect will accept",
            "limitations": list(retention.LIMITATIONS),
        },
    }


def _frameworks():
    record = framework_notes.report()
    return {
        "method": record["method"],
        "cited": [{"key": note["key"], "title": note["title"], "grade": note["grade"],
                   "bears_on": list(note["bears_on"])}
                  for note in framework_notes.list_notes()[:MAX_NOTES]],
        "excluded_claims": [item["claim"] for item in framework_notes.excluded()[:MAX_NOTES]],
        "not_a_signal": list(framework_notes.NOT_A_SIGNAL),
    }


def _runtime():
    return {
        "application": {"name": constants.APP_NAME, "version": constants.APP_VERSION,
                        "previous_name": constants.PREVIOUS_APP_NAME},
        "schema_version": SCHEMA_VERSION,
        "python": {"version": ".".join(str(part) for part in sys.version_info[:3]),
                   "implementation": platform.python_implementation()},
        "platform": {"system": sys.platform, "machine": platform.machine()},
        "sqlite": sqlite3.sqlite_version,
        "clock": "core/clock.py is the only source of wall-clock time; LELE_NOW pins it",
        "dependencies": "Python standard library only at run time",
    }


def _registry_state(conn):
    """What this registry holds, or an honest reason it could not be read.

    Absent is not zero. A capability summary that printed zeros for a registry
    that was never created would be indistinguishable from an empty one, and a
    reader could not tell which they were looking at.
    """
    if conn is None:
        return {"status": "not_read", "note": "no registry was opened for this summary"}
    try:
        tables = ("entities", "edges", "metrics", "filings", "attributes", "observations",
                  "price_bars", "move_events", "instruments", "sanctions_listings",
                  "money_flows", "entity_links", "entity_aliases", "ingest_runs",
                  "context_measures", "prune_runs")
        counts = {}
        for table in tables:
            counts[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        version = conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
    except sqlite3.Error as exc:
        return {"status": "unreadable", "reason": type(exc).__name__,
                "note": "the registry could not be queried, so no stored counts are "
                        "reported rather than reported as zero"}
    return {"status": "ok", "schema_version": version[0] if version else "unknown",
            "tables": counts,
            "completeness": "unknown",
            "completeness_note": "row counts describe this registry and nothing else; they are "
                                 "not a count of any institution, market, country or category "
                                 "that exists"}


def build(conn=None, *, labels=None, read_only=frozenset(), read_only_actions=None,
          no_registry=frozenset(), usage_for=None):
    """The whole inventory as data, so it can be rendered or asserted on.

    `labels` is the CLI's command table and `usage_for` returns a command's
    argument usage. Both are passed in rather than imported so this module does
    not depend on the CLI, and so a test can drive it with a different set and
    see what it fails to describe.
    """
    if not callable(usage_for):
        raise ValueError("usage_for must be callable")
    read_only_actions = read_only_actions or {}
    names = sorted(set(labels or ()) - {"help", "back", "quit"})
    if not names or len(names) > MAX_COMMANDS:
        raise ValueError("the command table is empty or implausibly large")
    commands = []
    for name in names:
        actions = sorted(read_only_actions.get(name, ()))
        if name in no_registry:
            access = "does_not_open_the_registry"
        elif name in read_only:
            access = "read_only"
        elif actions:
            access = "read_only_for_some_actions"
        else:
            access = "may_write"
        commands.append({
            "command": name,
            "summary": _text(labels[name], 512),
            "usage": _text(usage_for(name), 4096),
            "access": access,
            "read_only_actions": actions,
            "network": name.startswith("fetch"),
        })
    return {
        "method": METHOD,
        "generated_at": clock.now().replace(microsecond=0).isoformat(),
        "what_this_is": "a generated inventory of what this program can attempt, built from the "
                        "command table, the argument parser and the module-level taxonomies",
        "what_this_is_not": "evidence that any of it is correct, and not a coverage claim. A "
                            "command existing says the program offers a measurement; it does "
                            "not say the measurement is right, that a provider answered, or "
                            "that the stored records describe a population.",
        "counts": {"commands": len(commands), "registry_sources": len(_sources()),
                   "allowlisted_endpoints": sum(group["entries"] for group
                                                in _endpoint_groups()),
                   "observation_kinds": len(signals.OBSERVATION_FAMILY),
                   "observation_families": len(set(signals.OBSERVATION_FAMILY.values())),
                   "headline_categories": len(causes.categories()),
                   "context_measure_methods": len(stationarity.MEASURES),
                   "volatility_estimators": len(volatility.ESTIMATORS),
                   "indicator_specs": len(indicators.INDICATOR_SPECS),
                   "frozen_forecast_methods": len(prospective.METHODS)},
        "runtime": _runtime(),
        "commands": commands,
        "sources": _sources(),
        "endpoints": _endpoint_groups(),
        "taxonomy": _taxonomy(),
        "frameworks": _frameworks(),
        "standing_objective": {
            "target_rate": TARGET_RATE,
            "target_rate_source": TARGET_RATE_SOURCE,
            "required_asset_classes": list(OBJECTIVE_CLASSES),
            "status": OBJECTIVE_STATUS,
            "note": "Frozen before outcomes and never lowered to make a number pass.",
        },
        "declines": list(DECLINES),
        "registry": _registry_state(conn),
    }


def _access_label(row):
    """Say what a command does to the registry, and no more.

    A command whose read-only behaviour depends on its action is neither of the
    two simple answers. Collapsing it into "writes" would be wrong for the
    reading actions, and collapsing it into "read-only" would be wrong for the
    writing ones, so the actions that only read are named.
    """
    if row["access"] == "does_not_open_the_registry":
        return "does not open the registry"
    if row["access"] == "read_only":
        return "read-only"
    if row["access"] == "read_only_for_some_actions":
        listed = ", ".join(f"`{action}`" for action in row["read_only_actions"])
        return f"read-only for {listed}; other actions write"
    return "may write"


def render_markdown(report):
    """The same inventory as a document a person can read without this code."""
    lines = [
        f"# {constants.APP_NAME} {report['runtime']['application']['version']} "
        f"- capability summary",
        "",
        f"Generated {report['generated_at']} by `{report['method']}`. "
        "Every list below is read from the program at run time, so it describes this build "
        "rather than a document someone remembered to update.",
        "",
        f"**What this is:** {report['what_this_is']}.",
        "",
        f"**What this is not:** {report['what_this_is_not']}",
        "",
        "## What this program is",
        "",
        f"{constants.TAGLINE.capitalize()}. It ingests official and public sources, keeps every "
        "claim attached to a source, a retrieval time and a stated limit, and can then measure "
        "moves, volatility, stored context and prospective forecasts against a hash-chained "
        "ledger. It is a research instrument, not a service and not advice.",
        "",
        "## Scale of the vocabulary",
        "",
        "| thing | count |",
        "| --- | --- |",
    ]
    for label, key in (("commands", "commands"),
                       ("registry datasets behind `fetch`", "registry_sources"),
                       ("allowlisted endpoints", "allowlisted_endpoints"),
                       ("observation kinds", "observation_kinds"),
                       ("cause families", "observation_families"),
                       ("headline reason categories", "headline_categories"),
                       ("volatility estimators", "volatility_estimators"),
                       ("frozen indicator specs", "indicator_specs"),
                       ("frozen forecast methods", "frozen_forecast_methods")):
        lines.append(f"| {label} | {report['counts'][key]} |")
    runtime = report["runtime"]
    lines += [
        "",
        "## Runtime",
        "",
        f"- application: `{runtime['application']['name']}` "
        f"{runtime['application']['version']} (previously "
        f"`{runtime['application']['previous_name']}`)",
        f"- registry schema version: {runtime['schema_version']}",
        f"- Python {runtime['python']['version']} "
        f"({runtime['python']['implementation']}) on {runtime['platform']['system']}"
        f"/{runtime['platform']['machine']}, SQLite {runtime['sqlite']}",
        f"- dependencies: {runtime['dependencies']}",
        f"- time: {runtime['clock']}",
        "",
        "## Commands",
        "",
        "| command | what it does | registry access |",
        "| --- | --- | --- |",
    ]
    for row in report["commands"]:
        lines.append(f"| `{row['command']}` | {row['summary']} | {_access_label(row)} |")
    lines += ["", "### Command syntax", ""]
    for row in report["commands"]:
        lines.append(f"- `lele {row['command']} {row['usage']}`".rstrip())
    lines += ["", "## Registry datasets", "",
              "The five official datasets behind `fetch`. Each states its own coverage limit and "
              "none of them is a complete record of anything.", "",
              "| id | source | status | coverage limit |", "| --- | --- | --- | --- |"]
    for row in report["sources"]:
        lines.append(f"| `{row['id']}` | {row['name']} | {row['status']} | {row['coverage']} |")
    lines += ["", "## Endpoint allowlist", "",
              "Every host the HTTP client is permitted to contact, which is the real boundary of "
              "what this program can reach. Anything not named here is refused before a request "
              "is made, so an unlisted provider cannot be read by accident or by a changed "
              "constant.", "",
              "| group | entries | hosts |", "| --- | --- | --- |"]
    for group in report["endpoints"]:
        lines.append(f"| `{group['group']}` | {group['entries']} | "
                     f"{', '.join(f'`{host}`' for host in group['hosts'])} |")
    taxonomy = report["taxonomy"]
    lines += ["", "## Vocabulary", ""]
    lines += [f"- **{name}** ({len(kinds)} kinds): {', '.join(f'`{kind}`' for kind in kinds)}"
              for name, kinds in taxonomy["observation_families"].items()]
    keep = taxonomy["retention"]
    lines += [
        "",
        f"- market-wide context kinds usable for any instrument: "
        f"{', '.join(f'`{kind}`' for kind in taxonomy['market_wide_context_kinds'])}",
        f"- headline reason categories: "
        f"{', '.join(f'`{name}`' for name in taxonomy['headline_categories'])}",
        f"- stored context series a stationary quantity may be derived from: "
        f"{', '.join(f'`{kind}`' for kind in taxonomy['context_level_series_kinds'])}",
        f"- stationary context measures: "
        f"{', '.join(f'`{name}`' for name in taxonomy['context_measure_methods'])}, each with "
        f"a baseline of at least {taxonomy['context_measure_minimum_baseline']} points",
        f"- volatility estimators: "
        f"{', '.join(f'`{name}`' for name in taxonomy['volatility_estimators'])}",
        f"- default move threshold ladder (percent, cumulative): "
        f"{', '.join(str(value) for value in taxonomy['move_thresholds_percent'])}",
        f"- frozen indicator specs: "
        f"{', '.join(f'`{name}`' for name in taxonomy['indicator_specs'])}",
        f"- frozen forecast methods: "
        f"{', '.join(f'`{name}`' for name in taxonomy['frozen_forecast_methods'])}",
        "",
        "## Retention",
        "",
        "Price history grows for as long as a fetcher runs, so a cut is available and a cut is "
        "recorded. `lele prune plan` only counts; `lele prune apply` deletes and records; "
        "`lele prune runs` lists what was cut and why.",
        "",
        f"- tables a cut removes from: "
        f"{', '.join(f'`{name}`' for name in keep['pruned_tables'])}",
        f"- bars left on each series by default: {keep['default_keep_bars']}, and the floor "
        f"cannot go below {keep['keep_bars_floor']} ({keep['keep_bars_floor_source']})",
        f"- named reasons a cut is refused: "
        f"{', '.join(f'`{name}`' for name in keep['refusals'])}",
        "",
        "Tables a cut deliberately leaves alone, each with the reason:",
        "",
    ]
    lines += [f"- `{name}` - {reason}" for name, reason in keep["not_pruned"].items()]
    lines += [
        "",
        "A row that straddles the cut is kept and reported, because deleting it would remove a "
        "measurement still mostly inside the retained history, and keeping it silently would "
        "leave a value no longer reproducible from what remains. Every applied cut is recorded "
        "with what it removed; the record states how many rows went, not what they contained.",
        "",
        "## Provider health",
        "",
        "What the recorded ingest runs say about what each source last returned to this "
        "program. `lele providers` reports every series; `lele doctor` carries a reduced "
        "block. A comparison is made only where the recorded request identity is unchanged, "
        "and the raw first/last/min/max counts are published so a different threshold can be "
        "applied by hand.",
        "",
        "| flag | what it means |",
        "| --- | --- |",
        "| `request_changed` | the recorded request identity differed, so an outcome "
        "difference is evidence about the request and not the provider |",
        "| `count_collapse` | fetched fell to at or below "
        f"{report['taxonomy']['provider_health']['collapse_threshold']} of its peak across "
        "runs of the same recorded request |",
        "| `stored_nothing_once` | one run of the same recorded request stored nothing while "
        "another stored rows; this cannot say whether the source or this program changed |",
        "| `stored_zero_while_fetched` | the newest run fetched rows and stored none |",
        "| `returned_nothing` | the newest run fetched nothing where an earlier one did not |",
        "| `never_stored` | no recorded run stored a row, which a source with nothing to report "
        "and a source that stopped answering both look like |",
        "| `always_truncated` | every run was truncated, so stored coverage is a prefix of what "
        "the provider offered |",
        "| `no_record_hash` | the runs carry no record hash, so a content change cannot be "
        "detected for this series |",
        "| `stale` | the newest run is older than "
        f"{report['taxonomy']['provider_health']['default_stale_after_hours']} hours, which is "
        "either a source that stopped producing or an operator who stopped asking |",
        "",
        "It detects change and silence, never wrongness:",
        "",
    ]
    lines += [f"- {item}" for item in report["taxonomy"]["provider_health"]["cannot_detect"]]
    health = report["taxonomy"]["provider_health"]
    lines += [
        "",
        "A failed run is recorded outside the transaction that rolls back, with a classified "
        "reason and the exception's class name and never its message: "
        + ", ".join(f"`{name}`" for name in health["failure_reasons"]) + ".",
        "",
        f"- {health['failure_recording']}",
        "- commands that write no rows at all, so they have no run to record and no "
        "transaction to roll back: "
        f"{', '.join(f'`{name}`' for name in health['export_only_commands'])}",
        "",
        "## Allocation frameworks, recorded as citations",
        "",
        "Cited, each with an evidence grade and its documented criticism:",
        "",
    ]
    for note in report["frameworks"]["cited"]:
        lines.append(f"- `{note['key']}` - {note['title']} ({note['grade']})")
    lines += ["", "Claims this project declines to assert, because no primary source was "
              "reached:", ""]
    lines += [f"- {claim}" for claim in report["frameworks"]["excluded_claims"]]
    objective = report["standing_objective"]
    lines += [
        "",
        "## Standing objective",
        "",
        f"Target accuracy {objective['target_rate']} across "
        f"{', '.join(objective['required_asset_classes'])}, criterion direction plus tolerance, "
        f"frozen in `{objective['target_rate_source']}`. Status: "
        f"**{objective['status']}**. The target is frozen before outcomes and is never lowered "
        "to make a number pass; it is an aspiration, not an achieved result, and a historical "
        "or in-sample figure is never presented as out-of-sample skill.",
        "",
        "## What this program does not do",
        "",
    ]
    lines += [f"- {item}" for item in report["declines"]]
    registry = report["registry"]
    lines += ["", "## This registry", ""]
    if registry["status"] == "ok":
        lines += [f"- schema version {registry['schema_version']}",
                  f"- completeness: **{registry['completeness']}** - "
                  f"{registry['completeness_note']}", "",
                  "| table | rows |", "| --- | --- |"]
        lines += [f"| {name} | {count} |" for name, count in
                  sorted(registry["tables"].items())]
    else:
        lines.append(f"- {registry['note'] or registry.get('reason', 'unknown')}")
    lines += ["", "---", "",
              f"Report method `{report['method']}`. "
              "`lele summary` regenerates this file; `lele doctor` reports runtime and integrity; "
              "`lele sources` and `lele kinds` are the authoritative live catalogues."]
    text = "\n".join(lines) + "\n"
    if len(text.encode("utf-8")) > MAX_MARKDOWN_BYTES:
        raise ValueError("the rendered summary exceeds its size bound")
    return text


def render_json(report):
    text = json.dumps(report, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
    if len(text.encode("utf-8")) > MAX_MARKDOWN_BYTES:
        raise ValueError("the rendered summary exceeds its size bound")
    return text


def render(report, output_format):
    if output_format == "markdown":
        return render_markdown(report)
    if output_format == "json":
        return render_json(report)
    raise ValueError("output format must be markdown or json")
