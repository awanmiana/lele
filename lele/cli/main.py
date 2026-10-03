import argparse
import csv
from contextlib import closing
from datetime import UTC, datetime
import platform
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import sys
import tempfile
import traceback
from urllib.error import URLError

from ..analysis import (causes, capability, comparison, engine, episodes, events,
                        evidence_store, flows, moves, observations, price_import, projection,
                        prospective, rag, framework_notes, provider_health, retention, sanctions,
                        signals, stationarity, timeline, volatility, volatility_anomaly, worldstate)
from ..core import clock, importer, registry
from ..core import constants
from ..core.constants import (APP_NAME, APP_VERSION, DB_PATH, FETCH_MAX_LIMIT,
                              REGISTRY_SCHEMA_VERSION)
from ..fetchers import (awards, comtrade, crypto_context, evidence, eia, form4, formadv, formd, history, lobbying, material, news, news_rss, nport, opensky, prices,
                          sources, thirteenf, treasury, political)
from ..fetchers.http import SourceError


COMMANDS = {
    "init": "Initialize registry",
    "sources": "Available data sources",
    "kinds": "List documented entity kinds and their definitions",
    "version": "Show application and runtime versions",
    "list": "Find entities",
    "show": "Show entity details",
    "stats": "Registry statistics",
    "countries": "List countries",
    "fetch": "Fetch official source data",
    "fetch-prices": "Export bounded public five-minute candle closes for an existing entity",
    "fetch-evidence": "Export world-state evidence for an existing entity",
    "store-evidence": "Store world-state evidence as observations, queryable by window",
    "fetch-cot": "Export bounded CFTC Commitments of Traders positioning for an existing entity",
    "fetch-short": "Export bounded FINRA consolidated short interest for an existing entity",
    "fetch-news": "Export bounded GDELT news events for an existing entity",
    "fetch-form4": "Extract bounded SEC Form 4 insider transactions into observations",
    "fetch-13f": "Extract bounded SEC Form 13F manager holdings into observations",
    "fetch-material": "Extract bounded SEC material-filing events into observations",
    "fetch-formd": "Extract bounded SEC Form D private-fundraising events into observations",
    "fetch-nport": "Extract bounded SEC N-PORT fund holdings into observations",
    "fetch-awards": "Extract bounded USAspending federal awards for a recipient into observations",
    "fetch-lobbying": "Extract bounded Senate LDA lobbying income into observations",
    "fetch-treasury": "Extract bounded Treasury Fiscal Data daily operating cash balance into observations",
    "fetch-political": "Extract bounded Federal Register political/regulatory events into observations",
    "fetch-formadv": "Extract bounded SEC Form ADV investment adviser firms into observations",
    "fetch-formadv-individual": "Extract bounded SEC Form ADV investment adviser individuals into observations",
    "fetch-comtrade": "Extract bounded UN Comtrade international trade flows into observations",
    "fetch-census-trade": "Extract bounded US Census international trade flows into observations",
    "fetch-eia": "Extract bounded EIA energy data series into observations",
    "fetch-bls": "Extract bounded BLS labor statistics into observations",
    "fetch-opensky": "Extract bounded OpenSky Network flight data into observations",
    "runs": "List durable ingest runs with counts, coverage and evidence hashes",
    "providers": "Report what the recorded ingest runs say about each source's shape",
    "edges": "Build sourced relationships from stored entities",
    "import": "Import local JSON",
    "import-history": "Store a user-supplied OHLC export in price history",
    "analyze": "Analyze stored entity data",
    "finmap": "View bounded stored financial positions, not transfers",
    "project": "Project five minutes ahead and evaluate a recorded-price baseline",
    "compare": "Compare fixed five-minute methods with chronological held-out evaluation",
    "prospective": "Pre-register, record, settle and score prospective five-minute forecasts",
    "worldstate": "Extract evidence-only world-state features and study 5-minute patterns",
    "rag": "Event store, graph, money-flow attribution, price anomalies and composite indicator",
    "indicators": "Constructed indicator specs, computation, and world-state projection",
    "episodes": "Segment pump/dump and bull/bear episodes with 72-hour precursor evidence",
    "events": "Study large price moves and evidenced 24/48/72-hour precursors",
    "volatility-analyze": "Detect volatility instances and compute anomaly indicator from historical patterns",
    "fetch-history": "Store multi-year OHLC price history for an existing instrument",
    "fetch-sentiment": "Store the daily crypto fear and greed index as market-wide sentiment",
    "fetch-stablecoins": "Store daily aggregate stablecoin supply as market-wide money context",
    "fetch-market-activity": "Store daily per-asset market capitalisation change and volume",
    "fetch-news-feed": "Export bounded multi-publisher headlines for an existing entity",
    "explain": "What is recorded for an instrument in a time window, and what is not",
    "capital": "Capital, supply and positioning records for an instrument in a time window",
    "moves": "Detect tiered non-overlapping price moves from stored history",
    "instruments": "Record and list what a tradable thing is, so price series are comparable",
    "volatility": "Estimate realized volatility of stored history per estimator, with convention",
    "framework": "Cited record of documented allocation frameworks, their evidence and their critiques",
    "causes": "Attribute candidate reasons to stored moves and profile them against controls",
    "context": "Derive and read stationary quantities from stored context series",
    "prune": "Plan, apply and list retention cuts on stored price history",
    "scan": "Detect what is happening now with the parameters used for history",
    "sentiment": "Record supplied-text sentiment",
    "relationships": "Show recorded relationships",
    "tree": "Show a bounded consolidation tree from stored parent edges",
    "backup": "Write a consistent copy of the registry database",
    "doctor": "Check runtime, registry integrity and schema status",
    "resolve": "Review duplicate candidates and record reversible entity aliases",
    "sanctions": "Import official sanctions listings and review candidate matches",
    "flows": "Import documented cross-entity money flows and list them",
    "observations": "Import normalized decision/flow observations and project them",
    "links": "Record verified website/social links with field-level provenance",
    "export": "Export entity list",
    "summary": "Write a generated summary of every capability, and print it",
    "menu": "Interactive menu",
}
MENU_LABELS = {k: v for k, v in COMMANDS.items() if k != "menu"} | {
    "help": "Command help", "back": "Back to menu", "quit": "Quit",
}
ENTITY_FIELDS = ("id", "key", "kind", "name", "country", "website", "lei", "notes", "created_at")


class CLIError(Exception):
    def __init__(self, message, status=1):
        super().__init__(message)
        self.status = status


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)


def _debug():
    """Whether an unexpected failure should print its traceback.

    `lele` is a research tool whose whole value is that a result can be
    traced back to a cause, so a failure with no diagnostic is the worst
    possible outcome: a bug looks identical to a bad parameter or a dead
    provider. `LELE_DEBUG=1` shows the traceback; the last line is always
    printed, so the message is never empty even when the traceback is not.
    """
    flag = os.environ.get("LELE_DEBUG") or os.environ.get("FINWORLD_DEBUG") or ""
    return flag.strip().lower() in ("1", "true", "yes", "on")


HISTORY_INTERVALS = {"1d": 86400, "4h": 14400, "1h": 3600}
EVIDENCE_SOURCES = ("binance-futures",)


def _evidence_slots(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("slots must be an integer") from None
    if not 2 <= number <= 1000:
        raise argparse.ArgumentTypeError("slots must be an integer from 2 to 1000")
    return number


def _hours(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("hours must be an integer") from None
    if not 1 <= number <= 8760:
        raise argparse.ArgumentTypeError("hours must be an integer from 1 to 8760")
    return number


def _hours24(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("hours must be an integer") from None
    if not 1 <= number <= 8760:
        raise argparse.ArgumentTypeError("hours must be an integer from 1 to 8760")
    return number


def _pct(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("percent must be an integer") from None
    if not 1 <= number <= 1000:
        raise argparse.ArgumentTypeError("percent must be an integer from 1 to 1000")
    return number


def _instant(value):
    """An aware ISO-8601 instant, checked before a database is touched."""
    text = value.strip()
    if not text or "\x00" in text:
        raise argparse.ArgumentTypeError("timestamp must be a nonempty ISO-8601 instant")
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not an ISO-8601 instant; use 2026-01-01T00:00:00+00:00") from None
    if moment.tzinfo is None:
        raise argparse.ArgumentTypeError(
            "an instant needs a timezone offset; a bare date or time is ambiguous, and this "
            "window is compared against recorded observation times")
    return moment.astimezone(UTC).isoformat()


def _limit(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("limit must be an integer from 1 to 1000") from None
    if not 1 <= number <= 1000:
        raise argparse.ArgumentTypeError("limit must be an integer from 1 to 1000")
    return number


def _eid(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("ID must be a positive SQLite integer") from None
    if not 1 <= number <= 2**63 - 1:
        raise argparse.ArgumentTypeError("ID must be a positive SQLite integer")
    return number


def _path(value):
    if not value.strip() or "\x00" in value:
        raise argparse.ArgumentTypeError("path must be nonempty and contain no NUL")
    return value


def _filters(parser):
    parser.add_argument("--query", default="", help="Name, key, LEI or notes search")
    parser.add_argument("--kind", default="", help="Exact entity kind")
    parser.add_argument("--country", default="", help="Broad country or jurisdiction search")
    parser.add_argument("--legal-country", default="", help="Exact legal-address country code")
    parser.add_argument("--jurisdiction", default="", help="Exact ISO jurisdiction code (GLEIF iso_jurisdiction)")
    parser.add_argument("--hq-country", default="", help="Exact GLEIF headquarters-address country code")
    parser.add_argument("--source", default="", help="Exact stored ingestion source, e.g. gleif")
    parser.add_argument("--limit", type=_limit, default=50, help="Maximum rows, 1..1000 (default: 50)")


def build_parser():
    parser = Parser(
        prog="lele", description="Financial institution registry and stored-data analysis.",
        epilog="Global --db PATH and --json must precede the command. With no command: menu on a TTY, help otherwise.",
    )
    parser.add_argument("--db", type=_path, default=DB_PATH, metavar="PATH", help="SQLite registry path (root position only)")
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON results (root position only)")
    subs = parser.add_subparsers(dest="command", title="commands")
    parsers = {name: subs.add_parser(name, help=label, description=label) for name, label in COMMANDS.items()}
    _filters(parsers["list"])
    for command in ("show", "analyze", "sentiment", "relationships", "explain", "capital"):
        parsers[command].add_argument("id", type=_eid, metavar="ID")
    for command in ("explain", "capital"):
        window = parsers[command]
        window.add_argument("--from", dest="start", required=True, type=_instant, metavar="ISO",
                            help="Window start, aware ISO-8601 "
                                 "(e.g. 2026-01-01T00:00:00+00:00)")
        window.add_argument("--to", dest="end", required=True, type=_instant, metavar="ISO",
                            help="Window end, exclusive, aware ISO-8601")
    store_evidence = parsers["store-evidence"]
    store_evidence.add_argument("id", type=_eid, metavar="ID")
    store_evidence.add_argument("source", choices=sorted(EVIDENCE_SOURCES), help="Evidence source")
    store_evidence.add_argument("symbol", metavar="SYMBOL", help="Instrument symbol, e.g. BTCUSDT")
    store_evidence.add_argument("--limit", type=_evidence_slots, default=288, metavar="2..1000",
                                help="Five-minute slots to read (default: 288)")
    store_evidence.add_argument("--end", default="", metavar="ISO",
                                help="Aware ISO-8601 end boundary; default: now")
    store_evidence.add_argument("--max-rows", type=_pct, default=20000, metavar="1..100000",
                                help="Maximum observations to store (default: 20000)")
    parsers["explain"].add_argument("--interval", default="1d", choices=sorted(HISTORY_INTERVALS),
                                    help="Stored bar interval to measure the window on "
                                         "(default: 1d)")
    parsers["explain"].add_argument("--pre-hours", type=_hours, default=72, metavar="1..8760",
                                    help="Hours before the window to include as precursors "
                                         "(default: 72)")
    parsers["explain"].add_argument("--threshold-percent", type=_pct, default=3,
                                    metavar="1..1000", help="Move threshold for the window "
                                                          "and its episodes (default: 3)")
    parsers["explain"].add_argument("--move-hours", type=_hours24, default=24, metavar="1..8760",
                                    help="Move horizon for stored tiered moves (default: 24)")
    parsers["explain"].add_argument("--thresholds", type=_pct, nargs="+",
                                    default=[3, 5, 7, 11], metavar="PCT",
                                    help="Stored move tier ladder (default: 3 5 7 11)")
    parsers["explain"].add_argument("--limit", type=_limit, default=2000, metavar="1..1000",
                                    help="Maximum records returned per bucket (default: 2000)")
    parsers["explain"].add_argument("--no-market-wide", action="store_true",
                                    help="Exclude market-wide series such as aggregate "
                                         "stablecoin supply, which are not instrument-level")
    parsers["explain"].add_argument("--summary", action="store_true",
                                    help="One human-readable line instead of the full report")
    parsers["capital"].add_argument("--limit", type=_limit, default=2000, metavar="1..1000",
                                    help="Maximum records returned (default: 2000)")
    parsers["capital"].add_argument("--no-market-wide", action="store_true",
                                    help="Instrument-bound records only")
    parsers["capital"].add_argument("--summary", action="store_true",
                                    help="One human-readable line instead of the full report")
    parsers["finmap"].add_argument("ids", type=_eid, nargs="+", metavar="ID", help="1..10 unique local entity IDs")
    parsers["finmap"].epilog = "At most 1000 stored metric rows per issuer; separate periods/sources and stock/flow labels, no aggregation. JSON includes evidence and unknowns."
    parsers["project"].add_argument("id", type=_eid, metavar="ID")
    parsers["project"].add_argument("path", type=_path, metavar="PRICES_JSON", help="Recorded instrument and five-minute prices, 2 MiB / 10000 points maximum")
    parsers["project"].epilog = "Historical as-of persistence baseline, not live prediction. Reports exact-match rate and MAE; research context is attached, not used as a predictive feature. No guarantee of 90% accuracy."
    compare = parsers["compare"]
    compare.add_argument("id", type=_eid, metavar="ID")
    compare.add_argument("path", type=_path, metavar="PRICES_JSON", help="Recorded instrument and five-minute prices, 2 MiB / 10000 points maximum")
    compare.add_argument("--train-percent", type=int, choices=range(1, 100), default=70, metavar="1..99", help="Chronological observation prefix percentage, floored (default: 70)")
    compare.epilog = "Fixed persistence, linear extrapolation and trailing 3-point mean; selection only on training exact hits then MAE, frozen for test. Exploratory historical, not prospective. Opens an existing registry read-only without initialization or migration."
    prospective_parser = parsers["prospective"]
    prospective_parser.add_argument("action", choices=("preregister", "forecast", "settle", "score"))
    prospective_parser.add_argument("--ledger", type=_path, required=True, metavar="PATH", help="Append-only hash-chained forecast ledger")
    prospective_parser.add_argument("--config", type=_path, metavar="CONFIG_JSON", help="Pre-registration config for preregister")
    prospective_parser.add_argument("--id", type=_eid, metavar="ID", help="Existing local entity ID for forecast/settle")
    prospective_parser.add_argument("--path", type=_path, metavar="PRICES_JSON", help="Recorded instrument and five-minute prices for forecast/settle")
    prospective_parser.add_argument("--run", metavar="RUN_ID", help="Score a specific pre-registered run (default: latest)")
    prospective_parser.add_argument("--force", action="store_true", help="Replace an existing ledger on preregister")
    prospective_parser.add_argument("--evidence", type=_path, metavar="EVIDENCE_JSON", help="World-state evidence for the evidence method on forecast")
    prospective_parser.epilog = "Frozen method, instruments and direction/tolerance criterion are pre-registered before forecasts. Prospective status requires target time after issuance; gaps stay pending. Self-recorded, not independently verified."
    world = parsers["worldstate"]
    world.add_argument("id", type=_eid, metavar="ID")
    world.add_argument("path", type=_path, metavar="PRICES_JSON")
    world.add_argument("--evidence", type=_path, required=True, metavar="EVIDENCE_JSON")
    world.add_argument("--window-seconds", type=int, choices=range(1, 86401), default=300, metavar="1..86400", help="Observation window before each boundary (default: 300)")
    world.add_argument("--limit", type=int, choices=range(1, 1001), default=200, metavar="1..1000")
    world.epilog = "Evidence-only features for each five-minute boundary, point-in-time by observed_at and available_at. The price is the scored outcome, never a predictive input. Exploratory historical patterns, not prospective validation."
    rag_parser = parsers["rag"]
    rag_parser.add_argument("action", choices=("store-events", "build-graph", "attribute-flows", "detect-anomalies", "indicator"))
    rag_parser.add_argument("args", nargs="*", metavar="ARG")
    rag_parser.add_argument("--source", default="political-event", help="Event source for store-events (default: political-event)")
    rag_parser.add_argument("--window-hours", type=int, choices=range(1, 169), default=24, metavar="1..168", help="Time window for build-graph and attribute-flows (default: 24)")
    rag_parser.add_argument("--min-strength", type=float, default=0.1, metavar="0.0..1.0", help="Minimum relationship strength for build-graph (default: 0.1)")
    rag_parser.add_argument("--instrument-key", help="Instrument key for detect-anomalies and indicator")
    rag_parser.add_argument("--anomaly-type", default="volatility_spike", help="Unused; retained for compatibility with earlier invocations")
    rag_parser.add_argument("--mode", default="percentile", choices=tuple(volatility.THRESHOLD_MODES),
                            help="How a move is judged unusual (default: percentile)")
    rag_parser.add_argument("--level", default="95", metavar="LEVEL",
                            help="Percentile 1..100, or a z-score, or an absolute percent "
                                 "(default: 95, a 95th-percentile move)")
    rag_parser.add_argument("--window-bars", type=int, default=24, metavar="2..2000",
                            help="Bars in the window being judged (default: 24)")
    rag_parser.add_argument("--baseline-bars", type=int, default=120, metavar="1..10000",
                            help="Earlier moves forming the baseline, which never contains "
                                 "the window being judged (default: 120)")
    rag_parser.add_argument("--interval-seconds", type=int, default=86400, metavar="60..31536000",
                            help="Stored bar interval to read (default: 86400)")
    rag_parser.add_argument("--limit", type=int, choices=range(1, 101), default=20, metavar="1..100", help="Limit for indicator (default: 20)")
    rag_parser.epilog = "RAG pipeline for political/regulatory/market events: store-events imports events into event_store; build-graph creates event_relationships within a time window; attribute-flows links money_flows to events; detect-anomalies flags stored-bar windows whose move is unusual for that instrument against a baseline that excludes the window itself; indicator computes a composite event_indicator_v1 score. All operations preserve provenance and use point-in-time discipline. An anomaly is an unusual move, not a cause and not a forecast."
    indicators_parser = parsers["indicators"]
    indicators_parser.add_argument("action", choices=("list", "spec", "compute", "project"))
    indicators_parser.add_argument("args", nargs="*", metavar="ARG")
    indicators_parser.add_argument("--entity-id", type=int, help="Entity ID for compute and project (default: 1)")
    indicators_parser.add_argument("--indicator", help="Specific indicator name for spec and compute")
    indicators_parser.add_argument("--output", type=_path, metavar="PATH", help="Output path for project action")
    indicators_parser.add_argument("--force", action="store_true", help="Atomically replace existing output")
    indicators_parser.set_defaults(format="json")
    indicators_parser.epilog = ("Constructed indicator pipeline (P09): list shows all frozen specs; spec shows one spec detail; "
        "compute runs indicator computation for an entity; project projects indicators into world-state evidence format "
        "for prospective validation. Indicators are defined BEFORE outcomes with frozen specs: insider net-buy/cluster, "
        "filing momentum, macro release momentum, labor stress, flight activity. Each has defined inputs, window, "
        "normalization, direction convention, and expected sign.")
    episode = parsers["episodes"]
    episode.add_argument("id", type=_eid, metavar="ID")
    episode.add_argument("path", type=_path, metavar="PRICES_JSON")
    episode.add_argument("--evidence", type=_path, metavar="EVIDENCE_JSON", help="Optional world-state evidence for 72-hour precursors")
    episode.add_argument("--threshold-percent", type=int, choices=range(1, 1001), default=episodes.THRESHOLD_DEFAULT, metavar="1..1000", help="ZigZag reversal threshold in percent (default: 3)")
    episode.add_argument("--horizon-hours", type=int, choices=range(1, 73), default=episodes.HORIZON_HOURS_DEFAULT, metavar="1..72", help="Precursor lookback hours (default: 72)")
    episode.add_argument("--limit", type=int, choices=range(1, 1001), default=episodes.EPISODES_DEFAULT, metavar="1..1000")
    episode.add_argument("--control-stride", type=int, choices=range(1, episodes.CONTROL_STRIDE_MAX + 1),
                         default=episodes.CONTROL_STRIDE_DEFAULT, metavar="1..10000",
                         help="Sample every Nth non-episode control point (default: 12)")
    episode.epilog = "Threshold-defined ZigZag legs (pumps and dumps) plus each leg's preceding horizon of evidence limited to availability before the leg started, and non-episode controls outside every leg span and preceding horizon. Overlapping episodes and controls are not independent; not a validated predictor."
    event = parsers["events"]
    event.add_argument("id", type=_eid, metavar="ID")
    event.add_argument("path", type=_path, metavar="PRICES_JSON")
    event.add_argument("--evidence", type=_path, help="Optional timestamped flow/liquidation/context evidence JSON")
    event.add_argument("--move-hours", type=int, choices=range(1, 73), default=24)
    event.add_argument("--thresholds", type=int, nargs="+", default=[3, 5, 7, 11], help="Strict absolute percentage thresholds, distinct integers 1..1000")
    event.add_argument("--limit", type=int, choices=range(1, 101), default=100)
    event.epilog = "One supplied instrument; newest bounded matching events. Precursor evidence precedes move START and must have been available then. No inferred money origins or guaranteed liquidation levels."
    volatility_analyze = parsers["volatility-analyze"]
    volatility_analyze.add_argument("id", type=_eid, metavar="ID")
    volatility_analyze.add_argument("path", type=_path, metavar="PRICES_JSON")
    volatility_analyze.add_argument("--evidence", type=_path, help="Optional world-state evidence JSON")
    volatility_analyze.add_argument("--threshold-percent", type=int, choices=range(1, 1001), default=3, metavar="1..1000", help="ZigZag reversal threshold in percent (default: 3)")
    volatility_analyze.add_argument("--horizon-hours", type=int, choices=range(1, 73), default=72, metavar="1..72", help="Precursor lookback hours (default: 72)")
    volatility_analyze.add_argument("--min-similarity", type=float, default=0.5, metavar="0.0..1.0", help="Minimum cosine similarity for historical matches (default: 0.5)")
    volatility_analyze.add_argument("--max-matches", type=int, choices=range(1, 101), default=10, metavar="1..100", help="Maximum historical matches to return (default: 10)")
    volatility_analyze.epilog = "Detects volatility instances via ZigZag, computes world-state features, finds similar historical patterns via cosine similarity, and returns anomaly score with top matches. Exploratory historical, not prospective validation."
    history_fetch = parsers["fetch-history"]
    history_fetch.add_argument("id", type=_eid, metavar="ID")
    history_fetch.add_argument("symbol", choices=tuple(history.SYMBOLS))
    history_fetch.add_argument("--interval", choices=tuple(history.INTERVALS), default="1d",
                               help="Bar interval (default: 1d)")
    history_fetch.add_argument("--limit", type=int, choices=range(2, history.MAX_BARS + 1),
                               default=1000, metavar="2..5000",
                               help="Total bars to walk back (default: 1000)")
    history_fetch.add_argument("--pages", type=int, choices=range(1, history.MAX_PAGES + 1),
                               default=1, metavar="1..10",
                               help="Bounded backward pages of 1000 bars (default: 1)")
    history_fetch.add_argument("--end", help="Aware ISO8601 end boundary; default: now")
    history_fetch.set_defaults(format="json")
    history_fetch.epilog = ("Bounded Binance spot OHLC history stored in price_bars and idempotent "
        "on (instrument, interval, open time), so re-running refines rather than duplicates. Unlike "
        "fetch-prices this reaches years back, which is what move detection needs. Spot is quoted "
        "in USDT, bars are unadjusted, and a short result means the provider history start, not a "
        "complete record. No accuracy claim.")
    sentiment_fetch = parsers["fetch-sentiment"]
    sentiment_fetch.add_argument("--limit", type=int, choices=range(1, crypto_context.MAX_SENTIMENT_DAYS + 1),
                                 default=365, metavar="1..3650",
                                 help="Days of daily index to store (default: 365)")
    sentiment_fetch.add_argument("--end", help="Aware ISO8601 end boundary; default: now")
    sentiment_fetch.set_defaults(format="json")
    sentiment_fetch.epilog = ("Daily crypto fear and greed index stored as market-wide "
        "social_sentiment observations with no instrument key, so any instrument scan reads it. "
        "A composite provider index, not a cause and not a signal with a validated threshold. The "
        "daily value is treated as available from the following UTC day. No accuracy or trading "
        "claim.")
    stablecoin_fetch = parsers["fetch-stablecoins"]
    stablecoin_fetch.add_argument("--limit", type=int, choices=range(1, crypto_context.MAX_STABLECOIN_DAYS + 1),
                                  default=1200, metavar="1..4000",
                                  help="Days of daily supply to store (default: 1200)")
    stablecoin_fetch.add_argument("--end", help="Aware ISO8601 end boundary; default: now")
    stablecoin_fetch.set_defaults(format="json")
    stablecoin_fetch.epilog = ("Daily aggregate stablecoin supply across the stablecoins the "
        "provider tracks, stored as market-wide stablecoin_supply observations with no instrument "
        "key. Per-peg-currency provider objects are summed, and the USD-pegged part is kept "
        "separately. Supply is not a net inflow, is not a purchase of any asset and identifies no "
        "actor. Treated as available from the following UTC day. No accuracy or trading claim.")
    activity_fetch = parsers["fetch-market-activity"]
    activity_fetch.add_argument("id", type=_eid, metavar="ID")
    activity_fetch.add_argument("--coin", choices=tuple(crypto_context.COINS),
                                default="bitcoin", help="Provider coin id (default: bitcoin)")
    activity_fetch.add_argument("--days", type=int, choices=range(1, crypto_context.MAX_ACTIVITY_DAYS + 1),
                                default=90, metavar="1..365",
                                help="Days of daily points to store (default: 90)")
    activity_fetch.set_defaults(format="json")
    activity_fetch.epilog = ("Daily market capitalisation change, volume and close for one coin, "
        "bound to the selected entity. Market capitalisation is price times supply, so a change "
        "is a capital-activity proxy that rises with price as well as demand; it cannot separate "
        "buying from selling, new capital from rotation, or spot from derivative notional. Also a "
        "second public price source for cross-checking the exchange series. Treated as available "
        "from the following UTC day. No accuracy or trading claim.")
    news_feed = parsers["fetch-news-feed"]
    news_feed.add_argument("id", type=_eid, metavar="ID")
    news_feed.add_argument("topic", metavar="TOPIC", help="Plain keyword phrase, for example bitcoin")
    news_feed.add_argument("--hours", type=int, choices=range(1, news_rss.MAX_HOURS + 1), default=24,
                           metavar="1..168", help="Lookback hours (default: 24)")
    news_feed.add_argument("--limit", type=int, choices=range(1, news_rss.MAX_ARTICLES + 1),
                           default=100, help="Maximum articles (default: 100)")
    news_feed.add_argument("--end", help="Aware ISO8601 end timestamp; default: now")
    news_feed.add_argument("--output", type=_path, required=True, metavar="PATH")
    news_feed.add_argument("--force", action="store_true",
                           help="Atomically replace an existing output, never the registry")
    news_feed.set_defaults(format="json")
    news_feed.epilog = ("Multi-publisher news feed for one plain keyword phrase, emitted as "
        "news_event observations with the publisher name, headline and link. A second headline "
        "source so reason coverage does not rest on one index, which throttles and lags. "
        "Headlines are a publisher's framing, timestamps are the provider's index timestamps, and "
        "syndicated duplicates are not collapsed. No license, accuracy or trading claim.")
    move_parser = parsers["moves"]
    move_parser.add_argument("id", type=_eid, metavar="ID")
    move_parser.add_argument("--interval", choices=tuple(history.INTERVALS), default="1d",
                             help="Stored bar interval to read (default: 1d)")
    move_parser.add_argument("--move-hours", type=int, default=24, metavar="1..8760",
                             help="Move window length in hours (default: 24)")
    move_parser.add_argument("--thresholds", type=int, nargs="+", default=list(moves.DEFAULT_THRESHOLDS),
                             metavar="1..1000", choices=range(1, 1001),
                             help="Ascending distinct absolute percentage thresholds; a move is "
                                  "recorded at every threshold it clears (default: 3 5 7 11)")
    move_parser.add_argument("--baseline-bars", type=int, default=90, metavar="20..2000",
                             help="Trailing returns used for z-score and percentile (default: 90)")
    move_parser.add_argument("--limit", type=int, default=500, metavar="1..5000",
                             help="Maximum retained non-overlapping moves (default: 500)")
    move_parser.add_argument("--no-store", action="store_true",
                             help="Report only; do not write move_events")
    move_parser.set_defaults(format="json")
    move_parser.epilog = ("Fixed-horizon returns between stored bar closes, ranked by absolute "
        "change and selected so no two moves share a bar, so one crash is not counted many times. "
        "Each move is scored against the baseline-bars returns strictly before it, never itself. "
        "A move is a price outcome label, not a cause and not a forecast.")
    instrument_parser = parsers["instruments"]
    instrument_parser.add_argument("action", choices=("add", "list", "show"))
    instrument_parser.add_argument("id", type=_eid, nargs="?", metavar="ID")
    instrument_parser.add_argument("--symbol", default="", help="Provider symbol such as BTCUSDT")
    instrument_parser.add_argument("--venue", default="",
                                   help="Trading venue or data provider that publishes the symbol")
    instrument_parser.add_argument("--asset-class", default="",
                                   choices=sorted(projection.ASSET_CLASSES),
                                   help="Asset class; an unrecognized class cannot be annualized")
    instrument_parser.add_argument("--quote-currency", default="",
                                   help="Currency the price is quoted in")
    instrument_parser.add_argument("--contract-multiplier", default="",
                                   help="Contract size, for a series quoted per contract")
    instrument_parser.add_argument("--expiry", default="", help="Expiry, for a dated contract")
    instrument_parser.add_argument("--adjustment", default="unknown",
                                   choices=tuple(registry.ADJUSTMENT_BASES),
                                   help="Whether the series is split/dividend adjusted "
                                        "(default: unknown)")
    instrument_parser.add_argument("--rights-basis", default="unknown",
                                   help="Recorded basis of the right to use and redistribute "
                                        "this series (default: unknown)")
    instrument_parser.add_argument("--notes", default="")
    instrument_parser.add_argument("--limit", type=int, default=200, metavar="1..5000")
    instrument_parser.set_defaults(format="json")
    instrument_parser.epilog = ("A symbol is not an identity: a continuous futures series is not "
        "a fixed-expiry contract, the same symbol on two venues is two instruments, and a "
        "back-adjusted series is not the unadjusted one. This records that distinction so price "
        "bars and volatility estimates can be compared across instruments. rights_verified is "
        "always false: nothing in this project verifies a redistribution right.")
    volatility_parser = parsers["volatility"]
    volatility_parser.add_argument("id", type=_eid, metavar="ID")
    volatility_parser.add_argument("--interval", choices=tuple(history.INTERVALS), default="1d",
                                   help="Stored bar interval to read (default: 1d)")
    volatility_parser.add_argument("--window-bars", type=int, default=30, metavar="2..2000",
                                   help="Bars in the estimation window (default: 30)")
    volatility_parser.add_argument("--estimator", action="append", default=[],
                                   choices=tuple(volatility.ESTIMATORS),
                                   help="Estimator to run; repeatable. Default: every estimator")
    volatility_parser.add_argument("--asset-class", default="",
                                   choices=sorted(projection.ASSET_CLASSES),
                                   help="Override the asset class used for annualization "
                                        "(default: the registered instrument's class)")
    volatility_parser.add_argument("--store", action="store_true",
                                   help="Write volatility_estimates rows")
    framework_parser = parsers["framework"]
    framework_parser.add_argument("action", choices=("list", "show", "excluded", "all"))
    framework_parser.add_argument("key", nargs="?", metavar="KEY",
                                  help="Framework key for show")
    framework_parser.add_argument("--grade", default="", choices=framework_notes.GRADES,
                                  help="Filter by evidence grade: primary, secondary, vendor")
    framework_parser.add_argument("--topic", default="",
                                  help="Filter by the question a note bears on, such as "
                                       "backtesting or position_sizing")
    framework_parser.set_defaults(format="json")
    framework_parser.epilog = ("A documented record of how money is allocated, with the "
        "documented criticism of each entry and an explicit list of widely circulated "
        "claims this project declines to assert because no primary source was reached. "
        "Every entry is a cited position, not a validated technique. This command "
        "produces no signal, no score, no ranking and no position, and nothing in it is "
        "consumed by any detector or estimator.")
    volatility_parser.set_defaults(format="json")
    volatility_parser.epilog = ("Realized volatility from stored OHLC: close-to-close, Parkinson, "
        "Garman-Klass, Rogers-Satchell, Yang-Zhang, the LPV average, and ATR/NATR. Each reports "
        "the convention that produced it, because the estimator name alone is not reproducible. "
        "A window spanning a missing bar is refused, not measured. Anything not computable at "
        "this bar resolution is reported as unknown. A volatility is a risk measure; it is not "
        "a direction, a cause, or a forecast.")
    cause_parser = parsers["causes"]
    cause_parser.add_argument("action", choices=("attribute", "profile", "context"))
    cause_parser.add_argument("id", type=_eid, metavar="ID")
    cause_parser.add_argument("--interval", choices=tuple(history.INTERVALS), default="1d")
    cause_parser.add_argument("--move-hours", type=int, default=24)
    cause_parser.add_argument("--topic", default="bitcoin",
                              help="Plain keyword phrase for the news index (default: bitcoin)")
    cause_parser.add_argument("--pre-hours", type=int, default=24, metavar="1..2160",
                              help="Hours of news before each move start (default: 24)")
    cause_parser.add_argument("--tier", default="all", metavar="pN",
                             help="Percent tier label such as p3, p5, p7 or p11, or all")
    cause_parser.add_argument("--direction", choices=("up", "down", "all"), default="all")
    cause_parser.add_argument("--moves", type=int, default=10, metavar="1..5000",
                              help="Newest servable moves to attribute (default: 10)")
    cause_parser.add_argument("--controls", type=int, default=10, metavar="0..500",
                              help="Non-move control windows to sample; attribute caps at 200, "
                                   "context accepts up to 500 (default: 10, which is a quick "
                                   "look and not a powered comparison: a 10-control run against "
                                   "hundreds of move windows cannot resolve a small "
                                   "difference, and control_sampling in the report says how "
                                   "many were eligible)")
    cause_parser.add_argument("--articles", type=int, default=200, metavar="1..250",
                              help="Maximum articles per provider request (default: 200)")
    cause_parser.add_argument("--news-source", choices=signals.NEWS_PROVIDERS,
                              default=signals.NEWS_PROVIDERS[0],
                              help="Headline provider for attribution (default: gdelt)")
    cause_parser.add_argument("--measure", action="append", default=[],
                              choices=list(stationarity.MEASURES),
                              help="Also compare stored stationary quantities over the same "
                                   "windows; repeatable. Off by default, because every recorded "
                                   "figure in this project was produced without it")
    cause_parser.set_defaults(format="json")
    cause_parser.epilog = ("attribute fetches bounded headlines strictly before each stored move, "
        "classifies each into a reason category and records the per-move category set; profile "
        "compares category presence before moves with stride-sampled non-move controls under a "
        "stated permutation null. A category is a keyword match, not a demonstrated cause, and "
        "moves older than the provider reach are reported unavailable rather than treated as quiet. "
        "context reads stored market context; --measure adds a stationary re-expression of each "
        "stored series, which is the only way a level that drifts with the calendar can be "
        "compared, and it is a second view of the same windows rather than a second experiment.")
    context_parser = parsers["context"]
    context_parser.add_argument("action", choices=("derive", "series", "show"))
    context_parser.add_argument("kind", nargs="?", default="stablecoin_supply",
                                choices=sorted(stationarity.LEVEL_SERIES_KINDS),
                                help="Stored series to read (default: stablecoin_supply)")
    context_parser.add_argument("--instrument", default="",
                                help="Instrument key for a series bound to one asset, such as "
                                     "binance:BTCUSDT; omit for a market-wide series")
    context_parser.add_argument("--measure", action="append", default=[],
                                choices=list(stationarity.MEASURES),
                                help="Measure to derive or compare; repeatable. Default: every "
                                     "measure")
    context_parser.add_argument("--baseline", type=int, default=stationarity.MINIMUM_BASELINE,
                                metavar=f"{stationarity.MINIMUM_BASELINE}..2000",
                                help=f"Trailing baseline points a z-score is taken against "
                                     f"(default: {stationarity.MINIMUM_BASELINE}, the minimum "
                                     f"volatility.z_score already requires)")
    context_parser.add_argument("--limit", type=int, choices=range(1, 1001), default=50,
                                metavar="1..1000", help="Stored points to show (default: 50)")
    context_parser.set_defaults(format="json")
    context_parser.epilog = ("derive stores a stationary re-expression of a stored context "
        "series: change is the difference from the previous stored value, zscore is the value "
        "in units of its own trailing baseline, with the baseline excluding the point scored. "
        "A level that drifts with the calendar cannot be compared between groups whose windows "
        "sit at different dates, which is why a measure exists. A difference spanning a hole "
        "is refused rather than computed. rate and ratio are not offered and "
        "`context series` states why. A derived quantity is not a new reading and is not a "
        "cause of a move. Deriving twice over unchanged stored rows rewrites the same rows.")
    prune_parser = parsers["prune"]
    prune_parser.add_argument("action", choices=("plan", "apply", "runs"))
    prune_parser.add_argument("--before", dest="cut", type=_instant, metavar="ISO",
                              help="Aware ISO8601 instant; rows describing an earlier instant are "
                                   "removed and a row landing exactly on it is kept")
    prune_parser.add_argument("--series", dest="instrument_key", default="",
                              help="Restrict the cut to one instrument key, such as "
                                   "binance:BTCUSDT; omit to cut every stored series")
    prune_parser.add_argument("--interval", choices=tuple(history.INTERVALS), default="",
                              help="Restrict the cut to one interval (default: every interval)")
    prune_parser.add_argument("--keep-bars", type=int, default=retention.DEFAULT_KEEP_BARS,
                              metavar=f"N>={retention.KEEP_BARS_FLOOR}",
                              help=f"Bars left on each series (default: "
                                   f"{retention.DEFAULT_KEEP_BARS}, the smallest number the "
                                   "move detector will still measure; a smaller value is "
                                   "refused rather than quietly raised)")
    prune_parser.add_argument("--reason", default="",
                              help="Why the history is being removed; required by apply, because "
                                   "this deletes measurements with their source and retrieval "
                                   "time attached")
    prune_parser.add_argument("--limit", type=int, choices=range(1, 1001), default=50,
                              metavar="1..1000", help="Recorded cuts to list (default: 50)")
    prune_parser.set_defaults(format="json")
    prune_parser.epilog = ("plan counts and writes nothing; apply deletes exactly what the plan "
        "counted and records the cut as a prune run. Both remove price history before the cut and "
        "the derived rows whose own window ended before it: move_events, move_causes, "
        "volatility_estimates, cause_scans and price_anomalies. A bar, move or estimate that "
        "straddles the cut is kept and reported as straddling, because deleting it would remove a "
        "measurement still mostly inside the retained history and keeping it silently would leave "
        "a value that can no longer be reproduced. A cut that would leave a series shorter than "
        "the detector can measure is refused with the series named. Filed observations, ingest "
        "runs, evidence events and context measures are never pruned and the report says why. The "
        "record of a cut states how many rows went, not what they contained: only a backup holds "
        "them.")
    scan_parser = parsers["scan"]
    scan_parser.add_argument("id", type=_eid, metavar="ID")
    scan_parser.add_argument("--interval", choices=tuple(history.INTERVALS), default="1d")
    scan_parser.add_argument("--move-hours", type=int, default=24)
    scan_parser.add_argument("--topic", default="bitcoin")
    scan_parser.add_argument("--pre-hours", type=int, default=24, metavar="1..2160")
    scan_parser.add_argument("--horizons", type=int, nargs="+", default=[24, 48, 72],
                             metavar="H", help="Lookback horizons in hours (default: 24 48 72)")
    scan_parser.add_argument("--channels", nargs="+", default=list(signals.CHANNELS),
                             choices=list(signals.CHANNELS),
                             help="Channels to read (default: all four)")
    scan_parser.add_argument("--articles", type=int, default=200, metavar="1..250")
    scan_parser.add_argument("--news-source", choices=signals.NEWS_PROVIDERS,
                             default=signals.NEWS_PROVIDERS[0],
                             help="Headline provider for the news channel (default: gdelt)")
    scan_parser.add_argument("--no-store", action="store_true",
                             help="Report only; do not write cause_scans")
    scan_parser.set_defaults(format="json")
    scan_parser.epilog = ("Reads the newest stored bars, then reports every visible signal in each "
        "lookback horizon: stored money flows and policy events, classified headlines with "
        "sentiment, futures leverage state, and price and volume behaviour. Nested horizons are not "
        "additive. Reports current conditions only and forecasts nothing.")
    price_fetch = parsers["fetch-prices"]
    price_fetch.add_argument("id", type=_eid, metavar="ID")
    price_fetch.add_argument("source", choices=("binance", "yahoo"))
    price_fetch.add_argument("symbol", choices=tuple(prices.INSTRUMENTS))
    price_fetch.add_argument("--limit", type=_limit, default=288, help="2..1000 five-minute time slots, not guaranteed observations (default: 288)")
    price_fetch.add_argument("--end", help="Aware ISO8601 five-minute END boundary; default: latest elapsed boundary")
    price_fetch.add_argument("--output", type=_path, required=True, metavar="PATH")
    price_fetch.add_argument("--force", action="store_true", help="Atomically replace an existing output, never the registry")
    price_fetch.set_defaults(format="json")
    price_fetch.epilog = "One request; closed candles stamped at END, gaps preserved, at least two closes required. Binance BTCUSDT or PAXGUSDT (tokenized gold) only; Yahoo GC=F/CL=F/RB=F futures and AAPL equity only. Yahoo unsupported; review access terms. Returned report includes retrieval timestamp and hashes; save it separately."
    evidence_fetch = parsers["fetch-evidence"]
    evidence_fetch.add_argument("id", type=_eid, metavar="ID")
    evidence_fetch.add_argument("source", choices=tuple(evidence.SOURCES))
    evidence_fetch.add_argument("symbol", choices=tuple(sym for syms in evidence.SOURCES.values() for sym in syms))
    evidence_fetch.add_argument("--limit", type=_limit, default=288, help="2..300 five-minute slots (default: 288)")
    evidence_fetch.add_argument("--end", help="Aware ISO8601 five-minute END boundary; default: latest elapsed boundary")
    evidence_fetch.add_argument("--output", type=_path, required=True, metavar="PATH")
    evidence_fetch.add_argument("--force", action="store_true", help="Atomically replace an existing output, never the registry")
    evidence_fetch.set_defaults(format="json")
    evidence_fetch.epilog = "Binance USD-M futures public order flow, open interest, funding, long/short account and top-position ratios plus one retrieval-time order-book depth snapshot, as point-in-time world-state evidence for worldstate/prospective. Symbols: BTCUSDT and PAXGUSDT (a gold-backed token, used as a tokenized-gold proxy, not COMEX gold). One request per endpoint; the last min(limit,120) ratio bars are kept. Liquidations are not available: Binance has no public historical liquidation REST endpoint. available_at equals observed_at because provider lag is unverified. No license, counterparty identity or accuracy claim."
    cot_fetch = parsers["fetch-cot"]
    cot_fetch.add_argument("id", type=_eid, metavar="ID")
    cot_fetch.add_argument("source", choices=("cftc",))
    cot_fetch.add_argument("symbol", choices=tuple(evidence.COT_MARKETS))
    cot_fetch.add_argument("--limit", type=int, choices=range(1, 53), default=evidence.COT_DEFAULT_REPORTS,
                           metavar="1..52", help="Weekly reports to request (default: 12)")
    cot_fetch.add_argument("--end", help="ISO date YYYY-MM-DD; default: today UTC")
    cot_fetch.add_argument("--output", type=_path, required=True, metavar="PATH")
    cot_fetch.add_argument("--force", action="store_true", help="Atomically replace an existing output, never the registry")
    cot_fetch.set_defaults(format="json")
    cot_fetch.epilog = "CFTC Commitments of Traders legacy futures-only: total open interest and net non-commercial positioning for one pinned contract market (BITCOIN-CME, GOLD-COMEX or WTI-NYMEX). Weekly and measured-only. available_at is the conservative local retrieval time because the API does not expose the exact publication time. No license, counterparty identity or accuracy claim."
    short_fetch = parsers["fetch-short"]
    short_fetch.add_argument("id", type=_eid, metavar="ID")
    short_fetch.add_argument("source", choices=("finra",))
    short_fetch.add_argument("symbol", metavar="SYMBOL", help="Uppercase FINRA symbol code, for example AAPL")
    short_fetch.add_argument("--months", type=int, choices=range(1, 37), default=evidence.SHORT_DEFAULT_MONTHS,
                             metavar="1..36", help="Lookback months (default: 12)")
    short_fetch.add_argument("--end", help="ISO date YYYY-MM-DD; default: today UTC")
    short_fetch.add_argument("--output", type=_path, required=True, metavar="PATH")
    short_fetch.add_argument("--force", action="store_true", help="Atomically replace an existing output, never the registry")
    short_fetch.set_defaults(format="json")
    short_fetch.epilog = "FINRA consolidated short interest (twice-monthly settlement dates) for one equity symbol, emitted as the measured-only short_interest kind in shares. available_at is the conservative local retrieval time because the API does not expose the publication time. One symbol; position stock, not flow. No license or accuracy claim."
    news_fetch = parsers["fetch-news"]
    news_fetch.add_argument("id", type=_eid, metavar="ID")
    news_fetch.add_argument("topic", metavar="TOPIC", help="Plain keyword phrase, for example bitcoin")
    news_fetch.add_argument("--hours", type=int, choices=range(1, news.MAX_HOURS + 1), default=24,
                            metavar="1..2160", help="Lookback hours (default: 24)")
    news_fetch.add_argument("--limit", type=int, choices=range(1, news.MAX_ARTICLES + 1), default=100,
                            metavar="1..250", help="Maximum articles (default: 100)")
    news_fetch.add_argument("--end", help="Aware ISO8601 end timestamp; default: now")
    news_fetch.add_argument("--output", type=_path, required=True, metavar="PATH")
    news_fetch.add_argument("--force", action="store_true", help="Atomically replace an existing output, never the registry")
    news_fetch.set_defaults(format="json")
    news_fetch.epilog = "GDELT DOC 2.0 article list for one plain keyword phrase, emitted as news_event observations (headline, domain, country; no measurement). GDELT rate-limits to about one request per five seconds and its throttle reply is not JSON, so the client paces the host and retries it. Relevance and see-date availability are unverified; empty results mean unknown coverage. No license, accuracy or trading claim."
    form4_fetch = parsers["fetch-form4"]
    form4_fetch.add_argument("id", type=_eid, metavar="ID")
    form4_fetch.add_argument("--limit", type=int, choices=range(1, form4.MAX_FILINGS + 1),
                             default=form4.MAX_FILINGS_DEFAULT, metavar="1..200",
                             help="Recent Form 4 filings to read (default: 25)")
    form4_fetch.epilog = ("Reads one existing SEC issuer's recent submissions, fetches up to --limit "
                          "original Form 4 documents and stores one insider_trade observation per "
                          "transaction. Only recent filings are read and Form 4/A amendments are not "
                          "applied; direction is the filed acquired/disposed code in shares, an "
                          "assumption rather than a forecast. Reporting owners are recorded as "
                          "sec-owner:<CIK> keys without creating person entities. No counterparty, "
                          "motive or accuracy claim.")
    thirteenf_fetch = parsers["fetch-13f"]
    thirteenf_fetch.add_argument("id", type=_eid, metavar="ID")
    thirteenf_fetch.add_argument("--limit", type=int, choices=range(1, thirteenf.MAX_FILINGS + 1),
                                 default=thirteenf.MAX_FILINGS_DEFAULT, metavar="1..40",
                                 help="Recent Form 13F-HR filings to read (default: 4)")
    thirteenf_fetch.epilog = ("Reads one existing SEC investment manager's recent submissions and "
                              "stores one holding observation per reported position, bound to the "
                              "manager as actor with the issuer recorded by name/CUSIP only. Only "
                              "recent original 13F-HR filings are read; 13F-HR/A amendments are not "
                              "applied. Reported value is a filer-provided USD position snapshot, "
                              "not a money flow; no issuer instrument, counterparty or motive is "
                              "inferred and no ownership-delta signal is claimed.")
    material_fetch = parsers["fetch-material"]
    material_fetch.add_argument("id", type=_eid, metavar="ID")
    material_fetch.add_argument("--limit", type=int, choices=range(1, material.MAX_FILINGS + 1),
                                default=material.MAX_FILINGS_DEFAULT, metavar="1..200",
                                help="Recent material filings to read (default: 25)")
    material_fetch.epilog = ("Reads one existing SEC issuer's recent submissions and stores one "
                             "measurement-optional filing_event observation per 8-K, S-1, 424B*, "
                             "DEF 14A or DEFA14A filing, with form, filing/report dates, acceptance "
                             "time and document URL. Only filing metadata from recent submissions is "
                             "read; document contents are not downloaded or interpreted, amendments "
                             "stay separate events, and no money amount or motive is inferred.")
    formd_fetch = parsers["fetch-formd"]
    formd_fetch.add_argument("id", type=_eid, metavar="ID")
    formd_fetch.add_argument("--limit", type=int, choices=range(1, formd.MAX_FILINGS + 1),
                             default=formd.MAX_FILINGS_DEFAULT, metavar="1..200",
                             help="Recent Form D filings to read (default: 10)")
    formd_fetch.epilog = ("Reads one existing SEC issuer's recent submissions and parses each "
                          "original Form D cover XML into a measurement-optional filing_event "
                          "observation carrying the reported total amount sold (USD) when present, "
                          "offering totals, industry, exemptions, related persons and dates. Only "
                          "recent submissions are read; exhibits are not parsed, Form D/A "
                          "amendments are separate events that must not be summed with the "
                          "original, and investors, counterparties and motives are never inferred.")
    nport_fetch = parsers["fetch-nport"]
    nport_fetch.add_argument("id", type=_eid, metavar="ID")
    nport_fetch.add_argument("--limit", type=int, choices=range(1, nport.MAX_FILINGS + 1),
                             default=nport.MAX_FILINGS_DEFAULT, metavar="1..20",
                             help="Recent public NPORT-P filings to read (default: 1)")
    nport_fetch.epilog = ("Reads one existing SEC fund's recent submissions and parses each public "
                          "NPORT-P cover XML into one holding observation per portfolio security, "
                          "bound to the fund as actor with the security recorded by "
                          "name/LEI/CUSIP/ISIN. Holdings carry the reported USD value, balance, "
                          "units, percentage, asset/issuer category and country. Only recent public "
                          "filings are read, NPORT-P/A and confidential NPORT-NP data are not "
                          "applied, no issuer instrument is inferred and no positions are summed.")
    awards_fetch = parsers["fetch-awards"]
    awards_fetch.add_argument("id", type=_eid, metavar="ID")
    awards_fetch.add_argument("--recipient-uei", nargs="+", required=True, metavar="UEI",
                              help="1..20 explicit 12-character recipient UEIs; identity is never inferred from a name")
    awards_fetch.add_argument("--search", default=None, metavar="TEXT",
                              help="USAspending recipient name search text (default: the entity name); only narrows candidates")
    awards_fetch.add_argument("--limit", type=int, choices=range(1, awards.MAX_LIMIT + 1),
                              default=awards.MAX_LIMIT_DEFAULT, metavar="1..100",
                              help="Awards to request per award group, largest first (default: 25)")
    awards_fetch.add_argument("--start", default=None, metavar="YYYY-MM-DD", help="Period start (default: 365 days before --end)")
    awards_fetch.add_argument("--end", default=None, metavar="YYYY-MM-DD", help="Period end (default: today UTC)")
    awards_fetch.epilog = ("Reads public USAspending federal contract and assistance awards for one "
                           "or more explicit recipient UEIs, binding each positive award amount to "
                           "the selected registry entity as a fund_flow observation with the "
                           "awarding agency as a textual origin key. USAspending ignores exact "
                           "recipient-id filters, so a name search only narrows candidates and only "
                           "awards whose Recipient UEI exactly matches an explicit UEI are kept. "
                           "Amounts are obligation figures, not settled transfers; one bounded page "
                           "per group is queried and no identity or motive is inferred.")
    lobbying_fetch = parsers["fetch-lobbying"]
    lobbying_fetch.add_argument("id", type=_eid, metavar="ID")
    lobby_party = lobbying_fetch.add_mutually_exclusive_group(required=True)
    lobby_party.add_argument("--client-id", type=int, metavar="N", help="Explicit Senate LDA client id to bind")
    lobby_party.add_argument("--registrant-id", type=int, metavar="N", help="Explicit Senate LDA registrant id to bind")
    lobbying_fetch.add_argument("--filing-year", type=int, default=None, metavar="YYYY", help="LDA filing year (default: current year)")
    lobbying_fetch.add_argument("--limit", type=int, choices=range(1, lobbying.MAX_LIMIT + 1),
                                default=lobbying.MAX_LIMIT_DEFAULT, metavar="1..100",
                                help="Filings requested per quarter (default: 25)")
    lobbying_fetch.epilog = ("Reads public Senate LDA quarterly activity filings (Q1-Q4) for one "
                             "explicit client or registrant id and stores each positive "
                             "registrant-reported income as a routed fund_flow observation bound to "
                             "the selected registry entity, with the other party as a textual key. "
                             "Identity is never inferred from a name; amounts are filed lobbying "
                             "income, not settled transfers, and are not summed across periods.")
    treasury_fetch = parsers["fetch-treasury"]
    treasury_fetch.add_argument("--limit", type=int, choices=range(1, treasury.MAX_LIMIT + 1),
                                  default=treasury.MAX_LIMIT_DEFAULT, metavar="1..100",
                                  help="Records to request (default: 25)")
    treasury_fetch.add_argument("--start", default=None, metavar="YYYY-MM-DD", help="Period start (default: 365 days before --end)")
    treasury_fetch.add_argument("--end", default=None, metavar="YYYY-MM-DD", help="Period end (default: today UTC)")
    treasury_fetch.epilog = ("Reads public Treasury Fiscal Data daily operating cash balance "
                               "and stores each positive balance as a macro_release observation. "
                               "No counterparty or recipient identity is required since the data "
                               "is a macro aggregate (Treasury General Account). "
                               "Amounts are reported balances, not settled cash transfers.")
    political_fetch = parsers["fetch-political"]
    political_fetch.add_argument("--limit", type=int, choices=range(1, political.MAX_LIMIT + 1),
                                     default=political.MAX_LIMIT_DEFAULT, metavar="1..100",
                                     help="Documents to request (default: 25)")
    political_fetch.add_argument("--start", default=None, metavar="YYYY-MM-DD", help="Period start (default: 365 days before --end)")
    political_fetch.add_argument("--end", default=None, metavar="YYYY-MM-DD", help="Period end (default: today UTC)")
    political_fetch.epilog = ("Reads public Federal Register documents and stores each "
                                  "government publication as a political_event or regulatory_action "
                                  "observation. Documents include regulations, notices, and "
                                  "presidential documents with documented government actions. "
                                  "No monetary amount is implied; the event captures the documented "
                                  "government action and its published rationale.")
    formadv_fetch = parsers["fetch-formadv"]
    formadv_fetch.add_argument("--query", default="", metavar="TEXT", help="Search query for firm name (default: all)")
    formadv_fetch.add_argument("--limit", type=int, choices=range(1, 1001), default=100, metavar="1..1000")
    formadv_fetch.add_argument("--start", type=int, default=0, metavar="OFFSET", help="Pagination offset (default: 0)")
    formadv_fetch.epilog = ("SEC IAPD API: investment adviser firm registrations from Form ADV. "
        "Free public API at api.adviserinfo.sec.gov. Returns firm name, SEC number, address, "
        "disclosure flag, and branches. Each firm stored as investment_adviser observation. "
        "occurred_at is retrieval date; available_at is retrieval time.")
    formadv_ind_fetch = parsers["fetch-formadv-individual"]
    formadv_ind_fetch.add_argument("--query", default="", metavar="TEXT", help="Search query for individual name (default: all)")
    formadv_ind_fetch.add_argument("--limit", type=int, choices=range(1, 1001), default=100, metavar="1..1000")
    formadv_ind_fetch.add_argument("--start", type=int, default=0, metavar="OFFSET", help="Pagination offset (default: 0)")
    formadv_ind_fetch.epilog = ("SEC IAPD API: investment adviser individual (representative) registrations. "
        "Free public API. Returns individual name, CRD number, IA SEC number, scope, "
        "current firms and employment history. Each individual stored as investment_adviser observation. "
        "occurred_at is retrieval date; available_at is retrieval time.")
    comtrade_fetch = parsers["fetch-comtrade"]
    comtrade_fetch.add_argument("id", type=_eid, metavar="ID")
    comtrade_fetch.add_argument("--reporter", default="842", metavar="CODE", help="Reporter country code (default: 842=USA)")
    comtrade_fetch.add_argument("--partner", default="0", metavar="CODE", help="Partner country code (default: 0=World)")
    comtrade_fetch.add_argument("--hs-code", default="", metavar="CODE", help="HS commodity code (default: all)")
    comtrade_fetch.add_argument("--limit", type=int, choices=range(1, 1001), default=100, metavar="1..1000")
    comtrade_fetch.add_argument("--start", default=None, metavar="YYYY-MM", help="Period start (default: 12 months ago)")
    comtrade_fetch.add_argument("--end", default=None, metavar="YYYY-MM", help="Period end (default: current month)")
    comtrade_fetch.add_argument("--freq", choices=("M", "A"), default="M", help="Frequency: M=monthly, A=annual")
    comtrade_fetch.epilog = ("UN Comtrade API v1 preview endpoint (comtradeapi.un.org/public/v1/preview) appears to have changed; "
        "check https://comtrade.un.org/data/ for current API access. Free tier may require registration. "
        "Amounts in USD (primaryValue) or quantity. occurred_at is period start; available_at is retrieval time. "
        "If you get 'source request failed', the endpoint may have moved or require a subscription.")

    census_trade_fetch = parsers["fetch-census-trade"]
    census_trade_fetch.add_argument("id", type=_eid, metavar="ID")
    census_trade_fetch.add_argument("--limit", type=int, choices=range(1, 1001), default=100, metavar="1..1000")
    census_trade_fetch.add_argument("--start", default=None, metavar="YYYY-MM", help="Period start (default: 12 months ago)")
    census_trade_fetch.add_argument("--end", default=None, metavar="YYYY-MM", help="Period end (default: current month)")
    census_trade_fetch.add_argument("--commodity", default="", metavar="CODE", help="NAICS/HS commodity code")
    census_trade_fetch.add_argument("--country", default="", metavar="CODE", help="Partner country code")
    census_trade_fetch.epilog = ("US Census Bureau trade API (api.census.gov/data/timeseries/intltrade) may have changed endpoint; "
        "check https://api.census.gov/data.html for current trade endpoints. May require API key. "
        "Both imports and exports are fetched. Amounts in USD. occurred_at is period start; available_at is retrieval time. "
        "If you get 'source request failed', the endpoint path may have changed or require a key.")

    eia_fetch = parsers["fetch-eia"]
    eia_fetch.add_argument("id", type=_eid, metavar="ID")
    eia_fetch.add_argument("series", nargs="+", metavar="SERIES_ID", help="EIA series IDs (e.g., PET.RWTC.D NG.RNGWHHD.D)")
    eia_fetch.add_argument("--api-key", required=True, metavar="KEY", help="EIA API key (free from eia.gov)")
    eia_fetch.add_argument("--limit", type=int, choices=range(1, 5001), default=100, metavar="1..5000")
    eia_fetch.add_argument("--start", default=None, metavar="YYYY-MM-DD", help="Start date (default: 1 year ago)")
    eia_fetch.add_argument("--end", default=None, metavar="YYYY-MM-DD", help="End date (default: today)")
    eia_fetch.epilog = ("EIA API v2: energy data series (petroleum, natural gas, electricity, coal, renewables). "
        "REQUIRES free API key from https://www.eia.gov/opendata/register.php — sign up, get key, pass via --api-key. "
        "Common series: PET.RWTC.D (WTI crude daily), NG.RNGWHHD.D (Henry Hub natgas daily), ELEC.PRICE.ALL-US.A (US electricity annual). "
        "Frequency varies by series (daily/weekly/monthly). occurred_at is period date; available_at is retrieval time.")

    bls_fetch = parsers["fetch-bls"]
    bls_fetch.add_argument("id", type=_eid, metavar="ID")
    bls_fetch.add_argument("series", nargs="+", metavar="SERIES_ID", help="BLS series IDs (e.g., LNS14000000 JTS00000000JOL)")
    bls_fetch.add_argument("--limit", type=int, choices=range(1, 5001), default=100, metavar="1..5000")
    bls_fetch.add_argument("--start", default=None, metavar="YYYY", help="Start year (default: previous year)")
    bls_fetch.add_argument("--end", default=None, metavar="YYYY", help="End year (default: current year)")
    bls_fetch.add_argument("--registration-key", default=None, metavar="KEY", help="BLS registration key for higher limits (optional)")
    bls_fetch.epilog = ("BLS Public API v2: US labor statistics (employment, unemployment, JOLTS, wages, etc.). "
        "Works without key (25 req/day); get free registration key at https://data.bls.gov/registrationEngine/ for 500 req/day. "
        "Common series: LNS14000000 (unemployment rate), JTS00000000JOL (job openings), CES0000000001 (total nonfarm payrolls). "
        "Data is monthly/quarterly/annual. occurred_at is period date; available_at is retrieval time. Uses POST as required by API.")

    opensky_fetch = parsers["fetch-opensky"]
    opensky_fetch.add_argument("id", type=_eid, metavar="ID")
    opensky_fetch.add_argument("action", choices=("states", "flights"), help="Query type: states=current positions, flights=interval")
    opensky_fetch.add_argument("--limit", type=int, choices=range(1, 10001), default=100, metavar="1..10000")
    opensky_fetch.add_argument("--time", type=int, default=None, metavar="UNIX_TS", help="Unix timestamp for states query (default: now)")
    opensky_fetch.add_argument("--icao24", default="", metavar="CODE", help="Filter by aircraft ICAO24 (states only)")
    opensky_fetch.add_argument("--bbox", default="", metavar="LAMIN,LOMIN,LAMAX,LOMAX", help="Bounding box filter (states only)")
    opensky_fetch.add_argument("--begin", default=None, metavar="ISO|UNIX_TS", help="Start datetime for flights query")
    opensky_fetch.add_argument("--end", default=None, metavar="ISO|UNIX_TS", help="End datetime for flights query")
    opensky_fetch.add_argument("--username", default=None, metavar="USER", help="OpenSky username (required for flights)")
    opensky_fetch.add_argument("--password", default=None, metavar="PASS", help="OpenSky password (required for flights)")
    opensky_fetch.epilog = ("OpenSky Network API: real-time and historical ADS-B flight data. "
        "Anonymous: 10 req/min, limited history (states only). Register free at https://openskynetwork.org/ for higher limits and flights endpoint. "
        "states=current positions; flights=interval summaries (requires login). "
        "occurred_at is position/flight time; available_at is retrieval time. "
        "No API key needed for basic states access; username/password for registered users.")

    fetch = parsers["fetch"]
    fetch.add_argument("source", choices=tuple(item["id"] for item in sources.list_sources()))
    fetch.add_argument("--query", default="", help="Name or identifier; SEC: 10-digit CIK or previously stored SEC ticker (1..10 letters); OSFI: fulltext search, not bank-only; not supported by World Bank")
    fetch.add_argument("--country", default="", help="GLEIF: ISO2; FDIC: US; SEC: must be empty; OSFI: blank/CA/CAN (regulatory jurisdiction, not headquarters); World Bank: economy code or ALL")
    fetch.epilog = "OSFI: monthly public federal list, not all Canadian institutions or historical coverage; stored excludes the regulator. Representative offices carry no automatic supervision claim. Renames are not resolved; absent records are not deleted."
    fetch.add_argument("--limit", type=_limit, default=50, help="Input record budget; SEC: maximum recent filings stored per fetch, 1..1000 (default: 50)")
    fetch.add_argument("--indicator", help="World Bank only (default: NY.GDP.MKTP.CD)")
    fetch.add_argument("--category", choices=("FUND", "SOLE_PROPRIETOR"), default="", help="GLEIF only: entity category; FUND does not identify VC")
    fetch.add_argument("--financials", action="store_true", help="SEC only: selected us-gaap USD snapshots with filing/date/unit evidence; latest(end,filed), longest duration, tag priority; no form/frame preference; ambiguous inputs block ratios; 20 MiB cap instead of default 2 MiB; failure aborts ingestion")
    fetch.add_argument("--resume", action="store_true", help="Continue the latest resumable truncated run for the same parameters, starting at its saved page/offset")
    edges = parsers["edges"]
    edges.add_argument("--kind", required=True, choices=["fund", "parent"])
    edges.add_argument("--source", required=True, choices=["gleif"])
    edges.add_argument("--limit", type=_limit, default=25, help="Maximum entities, 1..1000 (default: 25)")
    edges.add_argument("--level", choices=["direct", "ultimate"], default="direct", help="Parent level for --kind parent (default: direct)")
    edges.add_argument("--refresh", action="store_true", help="Re-verify already-linked entities and retract edges no longer published")
    edges.epilog = "--kind fund follows GLEIF fund-manager links into managed_by edges. --kind parent follows GLEIF direct or ultimate parent relationships (--level) into subsidiary_of / ultimate_subsidiary_of edges, distinguishing no-parent (404), a reported-but-unusable relationship (exception, stored as an attribute) and errors. Without --refresh, entities lacking an active edge are processed and failures stay retryable; with --refresh, linked entities are re-checked and a stale edge is marked retracted, never deleted. Fund corroboration is optional; the exact GLEIF relationship type is preserved in evidence."
    tree = parsers["tree"]
    tree.add_argument("id", type=_eid, metavar="ID")
    tree.add_argument("--depth", type=int, choices=range(1, 21), default=3, metavar="1..20")
    tree.add_argument("--direction", choices=("both", "parents", "subsidiaries"), default="both")
    tree.add_argument("--max-nodes", type=int, choices=range(1, 10001), default=1000, metavar="1..10000")
    tree.epilog = "Read-only traversal of stored subsidiary_of/ultimate_subsidiary_of edges up and down from one entity. A missing edge is unknown coverage, not proof of no parent; a GLEIF consolidation link is not proof of control or ownership. Bounded by depth and node count; cycles are skipped and counted."
    links_parser = parsers["links"]
    links_parser.add_argument("action", choices=("set", "list", "remove"))
    links_parser.add_argument("args", nargs="*", metavar="ARG")
    links_parser.add_argument("--type", choices=("website", "social", "registry", "other"), default="")
    links_parser.add_argument("--label", default="", help="Optional human label")
    links_parser.add_argument("--source-url", default="", help="Page that publishes or verifies the link")
    links_parser.add_argument("--observed-at", default="", help="Aware ISO8601 time the link was observed")
    links_parser.add_argument("--evidence", default="", help="Free-text evidence note")
    links_parser.add_argument("--limit", type=int, choices=range(1, 1001), default=50, metavar="1..1000")
    links_parser.epilog = "Records verified, registry-published or organization-controlled links with field-level provenance. A public HTTPS URL and the --source-url that verifies it are required; nothing is guessed and unknown links stay empty. list shows recorded links; remove deletes one."
    flows_parser = parsers["flows"]
    flows_parser.add_argument("action", choices=("import", "list", "summary"))
    flows_parser.add_argument("args", nargs="*", metavar="ARG")
    flows_parser.add_argument("--limit", type=int, choices=range(1, 1001), default=50, metavar="1..1000")
    flows_parser.epilog = "import ingests a user-supplied JSON of documented flows (src, dst, type, amount, currency, occurred_at, source_url, evidence); both counterparties must already exist as entities, each flow needs a positive amount, a currency and a provenance URL, and balances never create a flow. list shows directed flows, optionally for one entity. summary reports per-currency inflow/outflow/net and counterparties for one entity; amounts are never summed or netted across currencies and net is not a position."
    observations_parser = parsers["observations"]
    observations_parser.add_argument("action", choices=("import", "list", "evidence"))
    observations_parser.add_argument("args", nargs="*", metavar="ARG")
    observations_parser.add_argument("--limit", type=int, choices=range(1, 1001), default=50, metavar="1..1000")
    observations_parser.add_argument("--output", type=_path, metavar="PATH")
    observations_parser.add_argument("--force", action="store_true", help="Atomically replace an existing output")
    observations_parser.set_defaults(format="json")
    observations_parser.epilog = "import ingests a user-supplied JSON of normalized decision/flow observations (source, external_id, kind, description, actor, counterparty, instrument, action, reason, reason_basis, amount, unit, currency, basis, occurred_at, observed_at, available_at, source_url, evidence). Only decision and routed flow kinds may carry actor, action or a reason; unknown actor, counterparty, amount and motive stay unknown and are never inferred. evidence projects the observations bound to one existing instrument entity into the validated world-state evidence file consumed by worldstate and prospective; only explicitly instrument-bound observations are projected."
    sanctions_parser = parsers["sanctions"]
    sanctions_parser.add_argument("action", choices=("fetch", "import", "candidates", "link",
                                                     "unlink", "listings", "links"))
    sanctions_parser.add_argument("args", nargs="*", metavar="ARG")
    sanctions_parser.add_argument("--source-url", default="", help="Official publication URL recorded for every imported listing")
    sanctions_parser.add_argument("--retrieved-at", default="", help="Aware ISO8601 retrieval time for the imported file")
    sanctions_parser.add_argument("--fuzzy", action="store_true", help="candidates: also match on shared name tokens after stripping legal suffixes (requires one entity id)")
    sanctions_parser.add_argument("--reason", default="", help="Recorded reason for a reviewed link")
    sanctions_parser.add_argument("--limit", type=int, choices=range(1, 1001), default=50, metavar="1..1000")
    sanctions_parser.add_argument("--active-only", action="store_true", help="listings: hide delisted entries")
    sanctions_parser.epilog = "fetch downloads an official list (ofac-sdn CSV, un-consolidated XML, uk-ofsi CSV, or eu-consolidated XML via allowlisted redirects where required) with bounded streaming and a recorded file hash; import ingests a user-supplied JSON file of official listings (source, key, name, type, program, country, basis, status, dates, evidence) with the file SHA-256 and source URL recorded. candidates lists exact-normalized-name matches as unreviewed candidates and never links automatically. link/unlink record a human-approved, reversible association; a name match alone is never treated as guilt or identity. Only the pinned official OFAC endpoint is fetched, under a bounded allowlisted redirect; no other list is fetched or invented."
    resolve = parsers["resolve"]
    resolve.add_argument("action", choices=("candidates", "merge", "unmerge", "list"))
    resolve.add_argument("ids", nargs="*", type=_eid, metavar="ID")
    resolve.add_argument("--fuzzy", action="store_true", help="candidates: also match on shared name tokens after stripping legal suffixes (requires one entity id)")
    resolve.add_argument("--reason", default="", help="Recorded reason for a merge")
    resolve.add_argument("--source-url", default="", help="Recorded evidence URL for a merge")
    resolve.add_argument("--limit", type=int, choices=range(1, 1001), default=50, metavar="1..1000")
    resolve.epilog = "candidates lists unreviewed duplicate pairs (same LEI, same name and country, or same name) and never merges automatically. merge records a human-approved alias from ALIAS_ID to CANONICAL_ID; unmerge removes it; list shows recorded aliases. Merges are reversible aliases only and do not rewrite existing edges, metrics or filings."
    backup = parsers["backup"]
    backup.add_argument("path", type=_path, metavar="PATH")
    backup.add_argument("--force", action="store_true", help="Replace an existing backup file")
    parsers["doctor"].epilog = "Read-only: reports Python/SQLite support, registry presence, PRAGMA integrity_check, foreign-key violations, schema version (migration_pending if behind), entity/edge counts, and a provider-health block reduced from the recorded ingest runs. A source health block is not a coverage claim and cannot report a source as failing, because a failed run rolls back and is not recorded. An absent registry is reported and never created."
    backup.epilog = "Consistent SQLite online backup of the existing registry; refuses an absent source and never creates it. The target must not be the registry or its journal files, its parent must already exist, and an existing target is refused without --force. Restore by pointing --db at the backup (replace the live file only when no process is using it)."
    runs = parsers["runs"]
    runs.add_argument("--limit", type=int, choices=range(1, 1001), default=50, metavar="1..1000")
    runs.epilog = "Completed source ingestion runs recorded in the registry with start/finish times, fetched/stored/skipped/missing counts, pages, truncation, a recorded request SHA-256, a stored-record SHA-256 where the fetcher supplies one, warnings and coverage text. Failed runs roll back with their transaction and are not listed, so an absent run is not evidence a source works."
    providers = parsers["providers"]
    providers.add_argument("--source", default="",
                           help="Restrict the report to one recorded source id, such as "
                                "sec-form4 or opensky")
    providers.add_argument("--since-hours", type=int, default=0, metavar="H",
                           help="Compare only runs started in the last H hours (default: 0, "
                                "every recorded run)")
    providers.add_argument("--stale-after-hours", type=int,
                           default=provider_health.DEFAULT_STALE_HOURS, metavar="H",
                           help=f"Flag a series whose newest run is older than H hours "
                                f"(default: {provider_health.DEFAULT_STALE_HOURS}; 0 disables)")
    providers.add_argument("--limit", type=int, default=100,
                           choices=range(1, provider_health.MAX_SERIES + 1),
                           metavar=f"1..{provider_health.MAX_SERIES}",
                           help="Source-query series to report (default: 100)")
    providers.epilog = ("Read-only. Groups the recorded ingest runs by source, query, country, "
        "indicator and category, and compares each series only where the recorded request identity "
        "is unchanged. Flags: request_changed, count_collapse, stored_zero_while_fetched, "
        "returned_nothing, never_stored, always_truncated, no_record_hash, stale. This detects "
        "change and silence, never wrongness: no run records a hash of the records themselves, and "
        "a failed run rolls back with its transaction and is never recorded, so nothing here can "
        "call a source failing or healthy.")
    parsers["import"].add_argument("path", type=_path, metavar="PATH", help="Importer JSON with entities, provenance and relationships")
    history_import = parsers["import-history"]
    history_import.add_argument("id", type=_eid, metavar="ID")
    history_import.add_argument("path", type=_path, metavar="PATH",
                                help="OHLC export JSON with instrument, source, source_url, retrieved_at and bars")
    history_import.add_argument("--interval", choices=tuple(price_import.INTERVALS), default="1d",
                                help="Bar interval the file describes (default: 1d)")
    history_import.set_defaults(format="json")
    history_import.epilog = (
        "Stores a bounded OHLC export in price_bars, idempotent on (instrument, interval, open "
        "time), so re-running refines rather than duplicates. No network call is made and no "
        "field is verified: this records the export's claims, with adjustment, venue, asset "
        "class and rights basis reported as unknown when the file omits them. Cadence and gaps "
        "are measured from the stored bars rather than assumed from --interval, because an "
        "equity's daily bars skip weekends. Use it for a listed equity or exchange-traded "
        "commodity that fetch-history cannot reach; no accuracy claim.")
    sentiment = parsers["sentiment"]
    text = sentiment.add_mutually_exclusive_group(required=True)
    text.add_argument("--text", help=f"Supplied English text, at most {engine.MAX_TEXT_LEN} characters")
    text.add_argument("--file", type=_path, metavar="PATH", help="UTF-8 text file")
    sentiment.add_argument("--source", required=True, help="Nonempty provenance label")
    sentiment.add_argument("--ref", default="", help="Optional reference")
    export = parsers["export"]
    export.add_argument("--format", choices=("json", "csv"), required=True)
    export.add_argument("--output", type=_path, required=True, metavar="PATH", help="Destination in an existing parent directory")
    export.add_argument("--force", action="store_true", help="Atomically replace an existing file")
    _filters(export)
    export.epilog = "Exports flat entity rows, not an importer backup. CSV formula-like cells are prefixed with an apostrophe."
    summary = parsers["summary"]
    summary.add_argument("--output", type=_path, default="lele-summary.md", metavar="PATH",
                         help="Destination file in an existing parent directory (default: lele-summary.md)")
    summary.add_argument("--format", choices=("markdown", "json"), default="markdown",
                         help="Rendered document format; the JSON form carries the same data (default: markdown)")
    summary.add_argument("--force", action="store_true", help="Atomically replace an existing file")
    summary.add_argument("--quiet", action="store_true",
                         help="Write the file and print only its path, not the document")
    summary.set_defaults(format="markdown")
    summary.epilog = (
        "Builds a capability inventory from the program itself: every command with its "
        "syntax, every public source with its own coverage limit, the observation taxonomy, "
        "the volatility estimators, the frozen indicator and forecast methods, the cited "
        "allocation frameworks, the standing objective, and what the project declines to do. "
        "It prints the document and writes it to --output. The file is refused without "
        "--force if it exists and may not be the registry or its journal files. The "
        "inventory says what the program can attempt; it is not evidence that anything is "
        "correct and makes no coverage claim.")
    parsers["menu"].epilog = "Choose a number or command; supply arguments using CLI quoting. No shell is executed. Use back or quit at any prompt."
    return parser


def _validate_fetch(args):
    query, country = args.query.strip(), args.country.strip().upper()
    if len(query) > 200 or any(ord(c) < 32 for c in query):
        raise CLIError("fetch query must be at most 200 characters without controls", 2)
    if args.limit > FETCH_MAX_LIMIT:
        raise CLIError("fetch limit exceeds the source record budget", 2)
    if args.indicator is not None and args.source != "worldbank":
        raise CLIError("--indicator is supported only for worldbank", 2)
    if args.category and args.source != "gleif":
        raise CLIError("--category is supported only for gleif", 2)
    if args.financials and args.source != "sec":
        raise CLIError("--financials is supported only for sec", 2)
    if args.source == "sec":
        if country:
            raise CLIError("SEC --country must be empty", 2)
        if not re.fullmatch(r"(?:[A-Z]{1,10}|[0-9]{10})", query.upper()):
            raise CLIError("SEC --query must be a ticker (1..10 letters) or a 10-digit CIK", 2)
    if args.source == "gleif" and country and not re.fullmatch(r"[A-Z]{2}", country):
        raise CLIError("GLEIF country must be a two-letter code", 2)
    if args.source == "fdic" and country not in ("", "US", "USA"):
        raise CLIError("FDIC supports only US country coverage", 2)
    if args.source == "osfi" and country not in ("", "CA", "CAN"):
        raise CLIError("OSFI supports only CA country coverage", 2)
    if args.source == "worldbank":
        if query:
            raise CLIError("World Bank does not support --query; use --country and --indicator", 2)
        if country and not re.fullmatch(r"[A-Z0-9]{2,3}", country):
            raise CLIError("World Bank country must be a single economy code or ALL", 2)
        indicator = args.indicator.strip() if args.indicator is not None else "NY.GDP.MKTP.CD"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", indicator):
            raise CLIError("invalid World Bank indicator code", 2)


def _sentiment_text(args):
    for label, value, limit in (("source", args.source, engine.MAX_SOURCE_LEN), ("ref", args.ref, engine.MAX_REF_LEN)):
        if len(value) > limit or (label == "source" and not value.strip()) or "\x00" in value:
            raise CLIError(f"{label} must be valid text of at most {limit} characters", 2)
    if args.file is not None:
        try:
            with open(args.file, encoding="utf-8-sig") as stream:
                text = stream.read(engine.MAX_TEXT_LEN + 1)
        except UnicodeError:
            raise CLIError("sentiment file must be UTF-8 text", 2) from None
    else:
        text = args.text
    if not text.strip() or len(text) > engine.MAX_TEXT_LEN or "\x00" in text:
        raise CLIError(f"sentiment text must be nonempty and at most {engine.MAX_TEXT_LEN} characters without NUL", 2)
    return text


def _rows(conn, args):
    return [dict(row) for row in registry.find_entities(
        conn, q=args.query, kind=args.kind, country=args.country, limit=args.limit,
        legal_country=args.legal_country, jurisdiction=args.jurisdiction,
        headquarters_country=args.hq_country, source=args.source,
    )]


def _csv_cell(value):
    if value is None:
        return ""
    text = str(value)
    stripped = text.lstrip()
    if stripped.startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def _export_target(args):
    output = Path(args.output).absolute()
    if not output.parent.is_dir():
        raise CLIError("export parent directory must already exist")
    if os.path.lexists(output) and not args.force:
        raise CLIError("output already exists; use --force to replace it")
    if output.is_dir():
        raise CLIError("export output must be a file")
    if not args.force and not hasattr(os, "link"):
        raise CLIError("atomic no-overwrite export is unavailable on this runtime; use a Python runtime with os.link support")
    return output


def _export(rows, args):
    output = _export_target(args)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=output.parent, prefix=".lele-", delete=False) as stream:
            temporary = Path(stream.name)
            if args.format == "json":
                json.dump(rows, stream, ensure_ascii=True, allow_nan=False, indent=2)
                stream.write("\n")
            else:
                writer = csv.DictWriter(stream, fieldnames=ENTITY_FIELDS)
                writer.writeheader()
                writer.writerows({key: _csv_cell(row.get(key)) for key in ENTITY_FIELDS} for row in rows)
            stream.flush()
            os.fsync(stream.fileno())
        if args.force:
            os.replace(temporary, output)
        else:
            try:
                os.link(temporary, output)
            except FileExistsError:
                raise CLIError("output already exists; use --force to replace it") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"exported": len(rows), "format": args.format, "output": args.output}


def _summary(args, parser):
    """Write and print the generated capability inventory.

    The report is built from the program rather than from prose, so a capability
    added or removed shows up here without anyone updating a document. Opening
    the registry is optional: the vocabulary and the command table exist without
    a database, and an absent registry is reported as absent rather than as a set
    of zeros, so a fresh install and an empty registry are not the same claim.
    """
    if not isinstance(args.output, str) or not args.output.strip() or len(args.output) > 4096 \
            or "\x00" in args.output:
        raise CLIError("summary output must be a nonempty path without NUL", 2)
    subparsers = parser._subparsers._group_actions[0].choices
    def build(conn):
        return capability.build(
            conn, labels=COMMANDS, read_only=READ_ONLY_COMMANDS,
            read_only_actions=READ_ONLY_ACTIONS, no_registry=NEVER_OPENS_REGISTRY,
            usage_for=lambda name: capability.usage_for(subparsers[name]))
    report = build(None)
    if os.path.isfile(args.db):
        with closing(registry.read_connect(args.db)) as conn:
            report = build(conn)
    text = capability.render(report, args.format)
    _protect_database(args)
    path = _write_document(text, args)
    if not args.quiet:
        print(text, end="" if text.endswith("\n") else "\n")
    return {"method": report["method"], "output": str(path), "format": args.format,
            "output_bytes": len(text.encode("utf-8")),
            "output_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "printed": not args.quiet, "commands": report["counts"]["commands"],
            "registry_sources": report["counts"]["registry_sources"],
            "allowlisted_endpoints": report["counts"]["allowlisted_endpoints"],
            "registry_state": report["registry"]["status"],
            "completeness": "unknown",
            "completeness_note": "this file inventories what the program can attempt; it is "
                                 "not evidence of correctness and not a coverage claim"}


def _write_document(text, args):
    """Write the rendered document atomically, refusing to clobber by accident.

    The same guarantees the export and backup paths make: the parent must exist,
    an existing file is refused without --force, and the replacement is a rename
    rather than a truncate in place, so an interrupted run cannot leave a
    half-written document where a readable one was.
    """
    output = Path(args.output).absolute()
    if not output.parent.is_dir():
        raise CLIError("summary parent directory must already exist")
    if output.is_dir():
        raise CLIError("summary output must be a file")
    if os.path.lexists(output) and not args.force:
        raise CLIError("summary output already exists; use --force to replace it")
    if not args.force and not hasattr(os, "link"):
        raise CLIError("atomic no-overwrite summary is unavailable on this runtime; "
                       "use a Python runtime with os.link support")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                         dir=output.parent, prefix=".lele-summary-",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if args.force:
            os.replace(temporary, output)
        else:
            try:
                os.link(temporary, output)
            except FileExistsError:
                raise CLIError("summary output already exists; use --force to replace it") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return output


def _protect_database(args):
    output = Path(args.output).absolute()
    database = Path(args.db).resolve()
    for protected in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm"), Path(str(database) + "-journal")):
        if output.resolve() == protected or (output.exists() and protected.exists() and output.samefile(protected)):
            raise CLIError("export output must not replace the registry or its journal files")


def _protect_ledger(args):
    ledger = Path(args.ledger).absolute()
    database = Path(args.db).resolve()
    for protected in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm"), Path(str(database) + "-journal")):
        if ledger.resolve() == protected or (ledger.exists() and protected.exists() and ledger.samefile(protected)):
            raise CLIError("ledger must not replace the registry or its journal files")


def _validate_prospective(args):
    if args.action == "preregister":
        if args.config is None:
            raise CLIError("preregister requires --config", 2)
        if args.id is not None or args.path is not None or args.run is not None or args.evidence is not None:
            raise CLIError("preregister accepts only --ledger, --config and --force", 2)
    elif args.action == "forecast":
        if args.id is None or args.path is None:
            raise CLIError("forecast requires --id and --path", 2)
        if args.config is not None or args.run is not None or args.force:
            raise CLIError("forecast accepts only --ledger, --id, --path and optional --evidence", 2)
    elif args.action == "settle":
        if args.id is None or args.path is None:
            raise CLIError("settle requires --id and --path", 2)
        if args.config is not None or args.run is not None or args.force or args.evidence is not None:
            raise CLIError("settle accepts only --ledger, --id and --path", 2)
    else:
        if args.config is not None or args.id is not None or args.path is not None or args.force or args.evidence is not None:
            raise CLIError("score accepts only --ledger and optional --run", 2)
        if args.run is not None and not re.fullmatch(r"[0-9a-f]{64}", args.run):
            raise CLIError("--run must be a 64-character lowercase hex run ID", 2)


def _validate_sanctions(args):
    ids = args.args
    if args.action == "fetch":
        if len(ids) != 1:
            raise CLIError("sanctions fetch requires exactly one SOURCE", 2)
    elif args.action == "import":
        if len(ids) != 2 or not args.source_url:
            raise CLIError("sanctions import requires SOURCE, PATH and --source-url", 2)
    elif args.action == "candidates":
        if len(ids) > 1:
            raise CLIError("sanctions candidates accepts at most one entity ID", 2)
        if ids and not re.fullmatch(r"[0-9]+", ids[0]):
            raise CLIError("entity ID must be a positive integer", 2)
    elif args.action in ("link", "unlink"):
        if len(ids) != 2 or not all(re.fullmatch(r"[0-9]+", item) for item in ids):
            raise CLIError(f"sanctions {args.action} requires LISTING_ID and ENTITY_ID", 2)
    elif ids:
        raise CLIError(f"sanctions {args.action} accepts no positional arguments", 2)


def _validate_flows(args):
    ids = args.args
    if args.action == "import":
        if len(ids) != 1:
            raise CLIError("flows import requires exactly one PATH", 2)
    elif args.action == "summary":
        if len(ids) != 1 or not re.fullmatch(r"[0-9]+", ids[0]):
            raise CLIError("flows summary requires exactly one entity ID", 2)
    elif len(ids) > 1 or (ids and not re.fullmatch(r"[0-9]+", ids[0])):
        raise CLIError("flows list accepts at most one entity ID", 2)


def _validate_observations(args):
    ids = args.args
    if args.action == "import":
        if len(ids) != 1:
            raise CLIError("observations import requires exactly one PATH", 2)
        if args.output is not None or args.force:
            raise CLIError("observations import accepts only PATH", 2)
    elif args.action == "evidence":
        if len(ids) != 1 or not re.fullmatch(r"[0-9]+", ids[0]):
            raise CLIError("observations evidence requires exactly one entity ID", 2)
        if args.output is None:
            raise CLIError("observations evidence requires --output", 2)
    elif args.action == "list":
        if len(ids) > 1 or (ids and not re.fullmatch(r"[0-9]+", ids[0])):
            raise CLIError("observations list accepts at most one entity ID", 2)
        if args.output is not None or args.force:
            raise CLIError("observations list accepts no --output or --force", 2)


def _validate_links(args):
    items = args.args
    if args.action == "set":
        if (len(items) != 2 or not re.fullmatch(r"[0-9]+", items[0]) or not args.type
                or not args.source_url):
            raise CLIError("links set requires ID, URL, --type and --source-url", 2)
    elif args.action == "list":
        if len(items) > 1 or (items and not re.fullmatch(r"[0-9]+", items[0])):
            raise CLIError("links list accepts at most one entity ID", 2)
    elif len(items) != 2 or not re.fullmatch(r"[0-9]+", items[0]):
        raise CLIError("links remove requires ID and URL", 2)


def _validate_rag(args):
    if args.action == "store-events":
        if len(args.args) != 1:
            raise CLIError("rag store-events requires exactly one PATH to events JSON", 2)
    elif args.action in ("build-graph", "attribute-flows"):
        pass
    elif args.action == "detect-anomalies":
        if not args.instrument_key:
            raise CLIError("--instrument-key is required for detect-anomalies", 2)
    elif args.action == "indicator":
        pass
    else:
        raise CLIError("unknown rag action", 2)


def _validate_indicators(args):
    if args.action == "list":
        pass
    elif args.action == "spec":
        if not args.indicator:
            raise CLIError("--indicator is required for spec action", 2)
    elif args.action == "compute":
        if not args.indicator:
            raise CLIError("--indicator is required for compute action", 2)
    elif args.action == "project":
        if not args.entity_id:
            raise CLIError("--entity-id is required for project action", 2)
    else:
        raise CLIError("unknown indicators action", 2)


def _validate_volatility_analyze(args):
    pass


def _validate_import_history(args):
    # Checked before the registry is opened, so a bad request never creates or
    # locks a database to then reject the arguments.
    try:
        price_import.validate_request(args.interval, price_import._now())
        if not os.path.isfile(args.path):
            raise ValueError(f"no such file: {args.path}")
    except ValueError as exc:
        raise CLIError(str(exc), 2) from exc


def _validate_fetch_history(args):
    try:
        history.validate_request(args.symbol, args.interval, args.limit, args.pages, args.end,
                                 history._now())
    except ValueError as exc:
        raise CLIError(str(exc), 2) from exc


def _validate_moves(args):
    try:
        moves.validate(args.move_hours, tuple(args.thresholds), args.baseline_bars, args.limit,
                       not args.no_store)
    except ValueError as exc:
        raise CLIError(str(exc), 2) from exc


def _validate_instruments(args):
    if type(args.limit) is not int or not 1 <= args.limit <= 5000:
        raise CLIError("limit must be an integer from 1 to 5000", 2)
    if args.action == "show" and args.id is None:
        raise CLIError("instruments show needs an ID", 2)
    if args.action == "add":
        if not args.symbol.strip():
            raise CLIError("instruments add needs --symbol", 2)
        if not args.asset_class:
            raise CLIError("instruments add needs --asset-class", 2)


def _validate_volatility(args):
    interval_seconds = history.INTERVALS[args.interval]
    if type(args.window_bars) is not int or not (volatility.MIN_WINDOW_BARS <= args.window_bars
                                                 <= volatility.MAX_WINDOW_BARS):
        raise CLIError(f"window bars must be an integer from {volatility.MIN_WINDOW_BARS} to "
                       f"{volatility.MAX_WINDOW_BARS}", 2)
    unknown = [name for name in args.estimator if name not in volatility.ESTIMATORS]
    if unknown:
        raise CLIError(f"estimator must be one of {', '.join(volatility.ESTIMATORS)}", 2)
    if args.asset_class and args.asset_class not in projection.ASSET_CLASSES:
        raise CLIError(f"asset class must be one of {', '.join(sorted(projection.ASSET_CLASSES))}",
                       2)
    del interval_seconds


def _validate_causes(args):
    interval_seconds = history.INTERVALS[args.interval]
    if args.measure and args.action != "context":
        raise CLIError(
            "--measure compares stored stationary quantities and is only available to "
            "`causes context`; on attribute or profile it would be accepted and ignored", 2)
    if args.action == "context":
        try:
            causes.validate_profile(interval_seconds=interval_seconds,
                                    move_hours=args.move_hours, pre_hours=args.pre_hours,
                                    permutations=causes.PERMUTATIONS,
                                    tier=None if args.tier == "all" else args.tier,
                                    direction=None if args.direction == "all" else args.direction)
            if not 1 <= args.controls <= 500:
                raise ValueError("controls must be an integer from 1 to 500")
        except ValueError as exc:
            raise CLIError(str(exc), 2) from exc
        return
    if args.action == "attribute":
        try:
            causes.validate_attribute(
                interval_seconds=interval_seconds, move_hours=args.move_hours,
                topic=args.topic, pre_hours=args.pre_hours,
                tiers=("all",) if args.tier == "all" else (args.tier,),
                directions=("all",) if args.direction == "all" else (args.direction,),
                controls=args.controls, articles=args.articles, move_limit=args.moves)
        except ValueError as exc:
            raise CLIError(str(exc), 2) from exc
        return
    try:
        causes.validate_profile(interval_seconds=interval_seconds, move_hours=args.move_hours,
                                pre_hours=args.pre_hours, permutations=causes.PERMUTATIONS,
                                tier=None if args.tier == "all" else args.tier,
                                direction=None if args.direction == "all" else args.direction)
    except ValueError as exc:
        raise CLIError(str(exc), 2) from exc


def _validate_context(args):
    try:
        if not 1 <= args.baseline <= 2000:
            raise ValueError("baseline must be an integer from 1 to 2000")
        for measure in (args.measure or ["change"]):
            stationarity.validate(measure, args.kind, args.baseline)
    except ValueError as exc:
        raise CLIError(str(exc), 2) from exc


def _derive_context(conn, args):
    """Derive each requested measure of one stored series and store the result."""
    measures = tuple(args.measure) or stationarity.MEASURES
    moment = clock.now()
    series, total = [], 0
    refused: dict = {}
    for measure in measures:
        derived = stationarity.derive(conn, args.kind, instrument_key=args.instrument,
                                      measure=measure, baseline=args.baseline, now=moment)
        written = stationarity.store(conn, derived)
        series.append({key: value for key, value in derived.items() if key != "rows"})
        series[-1]["written"] = written["written"]
        total += written["written"]
        for name, count in derived["refused"].items():
            refused[name] = refused.get(name, 0) + count
    return {"method": stationarity.METHOD, "action": "derive", "kind": args.kind,
            "instrument_key": args.instrument, "measures": list(measures),
            "baseline": args.baseline, "series": series, "written": total,
            "refused": dict(sorted(refused.items())),
            "derived_at": moment.isoformat(),
            "note": "a derived quantity is a re-expression of stored observations, not a new "
                    "reading. Deriving again over unchanged stored rows rewrites the same rows "
                    "and changes no count, which is what makes the stored value reproducible.",
            "limitations": list(stationarity.LIMITATIONS)}


def _validate_prune(args):
    """Refuse an unusable cut before a connection is opened."""
    if args.action == "runs":
        if args.cut:
            raise CLIError("`prune runs` lists recorded cuts and takes no --before", 2)
        return
    if not args.cut:
        raise CLIError(f"`prune {args.action}` needs --before, an aware ISO8601 instant", 2)
    if args.keep_bars < retention.KEEP_BARS_FLOOR:
        raise CLIError(
            f"--keep-bars must be at least {retention.KEEP_BARS_FLOOR}, the smallest number of "
            "bars the move detector will still measure; a smaller series cannot be detected, so "
            "the floor is raised rather than honoured", 2)
    if args.action == "apply" and not args.reason.strip():
        raise CLIError(
            "`prune apply` needs --reason. It deletes stored measurements with their source and "
            "retrieval time attached, and nothing in this registry is removed without a stated "
            "reason. Run `lele prune plan` first to see the counts.", 2)


def _validate_scan(args):
    try:
        signals.validate(tuple(args.horizons), tuple(args.channels), args.topic, None,
                         args.articles, True, provider=args.news_source)
    except ValueError as exc:
        raise CLIError(str(exc), 2) from exc


def _instrument(conn, args):
    entity = registry.get_entity(conn, args.id)
    if entity is None:
        raise CLIError(f"entity {args.id} not found", 2)
    return entity


def _tiers(args):
    if args.tier == "all":
        return ("all",)
    if registry.move_tier_percent(args.tier) is None:
        raise CLIError("--tier must be a percent label such as p3, p5, p7 or p11, or all", 2)
    return (args.tier,)


def _directions(args):
    return ("all",) if args.direction == "all" else (args.direction,)


#: Commands whose read-only behaviour depends on the action. Every entry must be
#: live: the command has an `action` argument and the named values are among its
#: choices. An entry that names an action the parser does not have is a claim the
#: code does not support -- it never fires, so it looks harmless while quietly
#: asserting a read-only guarantee, and `store-evidence` already had one that
#: would have made a writing command read-only the day it grew an action.
#: `tests/test_db_guarantees.py` fails if an entry goes stale or overlaps
#: `READ_ONLY_COMMANDS`.
READ_ONLY_ACTIONS = {
    "links": frozenset({"list"}),
    "flows": frozenset({"list", "summary"}),
    "observations": frozenset({"list", "evidence"}),
    "sanctions": frozenset({"candidates", "listings", "links"}),
    "resolve": frozenset({"candidates", "list"}),
    "rag": frozenset({"build-graph", "indicator"}),
    "indicators": frozenset({"list", "spec", "project"}),
    "prospective": frozenset({"score"}),
    "causes": frozenset({"profile", "context"}),
    "instruments": frozenset({"list", "show"}),
    "context": frozenset({"series", "show"}),
    "prune": frozenset({"plan", "runs"}),
}

READ_ONLY_COMMANDS = frozenset({
    "list", "show", "stats", "countries", "runs", "tree", "relationships", "analyze",
    "finmap", "events", "project", "export", "doctor", "providers", "sources", "kinds",
    "worldstate", "compare", "episodes", "volatility-analyze",
    "explain", "capital", "summary",
})

#: Commands that never open the registry at all, so there is nothing to classify
#: as read-only or writing. Stated here rather than only in a test because the
#: generated capability summary has to say something true about them: a command
#: that returns a constant cannot honestly be labelled "may write".
NEVER_OPENS_REGISTRY = frozenset({"version", "menu", "framework"})


def _read_only(command, args):
    """Whether this invocation can only read.

    A read command used to open the registry read-write and replay the schema on
    every call, so it took a write lock, grew the file and could not run against
    a read-only mount. Deciding here, from the command and its action, lets the
    connection be opened `mode=ro` and makes a write attempted during a read an
    immediate, visible error rather than a silent mutation.
    """
    if command in READ_ONLY_COMMANDS:
        return True
    allowed = READ_ONLY_ACTIONS.get(command)
    if allowed is None:
        return False
    return getattr(args, "action", None) in allowed


def _store_evidence(args):
    """Fetch evidence and persist it, so a later window query can find it."""
    if args.max_rows > 20000:
        raise CLIError("--max-rows must not exceed 20000", 2)
    with registry.get_conn(args.db) as conn:
        entity = _instrument(conn, args)
        try:
            payload, report = evidence.fetch_evidence(
                conn, args.id, args.source, args.symbol, limit=args.limit,
                end=args.end or None)
        except (SourceError, ValueError) as error:
            raise CLIError(f"evidence fetch failed; nothing stored: {error}", 1) from error
        result = evidence_store.store(
            conn, payload, entity["key"], args.id, args.source,
            retrieved_at=report.get("retrieved_at", ""), limit=args.max_rows)
    return {"evidence": {key: report[key] for key in
                         ("source", "symbol", "retrieved_at", "source_urls", "records")
                         if key in report}, **result}


def _explain(args):
    """`explain` and `capital`: read-only, so they open the registry read-only."""
    interval = HISTORY_INTERVALS[args.interval] if args.command == "explain" else 86400
    # Checked here as well as in the analysis layer, so a reversed or absurd
    # window is reported as bad input rather than as a data error.
    try:
        timeline.validate(args.id, args.start, args.end, 0, args.limit, 3, interval)
    except ValueError as error:
        raise CLIError(str(error), 2) from error
    with registry.get_read_conn(args.db) as conn:
        if command_entity_missing(conn, args.id):
            raise CLIError(f"entity {args.id} not found", 2)
        if args.command == "explain":
            report = timeline.explain(
                conn, args.id, args.start, args.end, interval_seconds=interval,
                pre_hours=args.pre_hours, threshold_percent=args.threshold_percent,
                move_hours=args.move_hours, thresholds=tuple(args.thresholds),
                limit=args.limit, include_market_wide=not args.no_market_wide)
        else:
            report = timeline.capital(
                conn, args.id, args.start, args.end, limit=args.limit,
                include_market_wide=not args.no_market_wide)
    if args.summary:
        print(timeline.summary_line(report) if args.command == "explain"
              else timeline.capital_summary(report))
        return None
    return report


def command_entity_missing(conn, entity_id) -> bool:
    return registry.get_entity(conn, entity_id) is None


def _dispatch(args, parser=None):
    command = args.command
    if command == "version":
        return {"name": APP_NAME, "version": APP_VERSION, "logo": constants.LOGO,
                "tagline": constants.TAGLINE,
                "python": ".".join(str(part) for part in sys.version_info[:3]),
                "implementation": platform.python_implementation(),
                "sqlite": sqlite3.sqlite_version,
                "platform": sys.platform,
                "machine": platform.machine(),
                "schema_version": REGISTRY_SCHEMA_VERSION}
    if command == "sanctions":
        _validate_sanctions(args)
    if command == "flows":
        _validate_flows(args)
    if command == "observations":
        _validate_observations(args)
    if command == "links":
        _validate_links(args)
    if command == "rag":
        _validate_rag(args)
    if command == "indicators":
        _validate_indicators(args)
    if command == "sources":
        return sources.list_sources()
    if command == "kinds":
        return registry.list_kinds()
    if command == "compare":
        with closing(registry.read_connect(args.db)) as conn:
            return comparison.compare(conn, args.id, args.path, args.train_percent)
    if command == "prospective":
        _validate_prospective(args)
        _protect_ledger(args)
        if args.action == "preregister":
            return prospective.preregister(args.config, args.ledger, args.force)
        if args.action == "score":
            return prospective.score(args.ledger, args.run)
        with closing(registry.read_connect(args.db)) as conn:
            if args.action == "forecast":
                return prospective.forecast(conn, args.id, args.path, args.ledger,
                                            evidence_path=args.evidence)
            return prospective.settle(conn, args.id, args.path, args.ledger)
    if command == "worldstate":
        with closing(registry.read_connect(args.db)) as conn:
            return worldstate.study(conn, args.id, args.path, args.evidence,
                                    args.window_seconds, args.limit)
    if command == "volatility-analyze":
        with closing(registry.read_connect(args.db)) as conn:
            return volatility_anomaly.compute_anomaly_score(
                conn, args.id, args.path, args.evidence,
                threshold_percent=args.threshold_percent,
                horizon_hours=args.horizon_hours,
            )
    if command == "import-history":
        _validate_import_history(args)
        with registry.get_conn(args.db) as conn:
            _instrument(conn, args)
            return price_import.import_price_history(conn, args.id, args.path, args.interval)
    if command == "fetch-history":
        _validate_fetch_history(args)
        with registry.get_conn(args.db) as conn:
            entity = _instrument(conn, args)
            return history.fetch_price_history(conn, args.id, args.symbol, args.interval,
                                              args.limit, args.pages, args.end)
    if command == "fetch-sentiment":
        try:
            crypto_context.validate_sentiment(args.limit, args.end, crypto_context._now())
        except ValueError as exc:
            raise CLIError(str(exc), 2) from exc
        with registry.get_conn(args.db) as conn:
            payload, report = crypto_context.fetch_sentiment(conn, args.limit, args.end)
            return {"observations": len(payload["observations"]), **report,
                    "observation_kinds": {"social_sentiment": len(payload["observations"])}}
    if command == "fetch-stablecoins":
        try:
            crypto_context.validate_stablecoins(args.limit, args.end, crypto_context._now())
        except ValueError as exc:
            raise CLIError(str(exc), 2) from exc
        with registry.get_conn(args.db) as conn:
            payload, report = crypto_context.fetch_stablecoin_supply(conn, args.limit,
                                                                     args.end)
            return {"observations": len(payload["observations"]), **report,
                    "observation_kinds": {"stablecoin_supply": len(payload["observations"])}}
    if command == "fetch-market-activity":
        entity = None
        with registry.get_conn(args.db) as conn:
            entity = _instrument(conn, args)
            try:
                crypto_context.validate_activity(args.coin, args.days, args.id, entity["key"])
            except ValueError as exc:
                raise CLIError(str(exc), 2) from exc
            payload, report = crypto_context.fetch_market_activity(
                conn, args.id, entity["key"], args.coin, args.days)
            return {"observations": len(payload["observations"]), **report,
                    "observation_kinds": {"market_activity": len(payload["observations"])}}
    if command == "fetch-news-feed":
        try:
            if not news_rss.TOPIC_RE.fullmatch(args.topic):
                raise ValueError("topic must be 1..64 letters, digits, spaces, hyphens or "
                                 "underscores")
            news_rss.request_window(args.hours, args.end, news_rss._now())
        except ValueError as exc:
            raise CLIError(str(exc), 2) from exc
        with registry.get_conn(args.db) as conn:
            payload, report = news_rss.fetch_news_feed(conn, args.id, args.topic, hours=args.hours,
                                                       limit=args.limit, end=args.end)
        raw = (json.dumps(payload, ensure_ascii=True, allow_nan=False, indent=2) + "\n").encode("utf-8")
        if len(raw) > projection.MAX_FILE_BYTES:
            raise CLIError("news feed export exceeds 2 MiB")
        _protect_database(args)
        _export(payload, args)
        return {**report, "output": args.output, "output_bytes": len(raw),
                "output_sha256": hashlib.sha256(raw).hexdigest()}
    if command == "store-evidence":
        return _store_evidence(args)
    if command in ("explain", "capital"):
        return _explain(args)
    if command == "moves":
        _validate_moves(args)
        with (registry.get_read_conn(args.db) if args.no_store
              else registry.get_conn(args.db)) as conn:
            entity = _instrument(conn, args)
            return moves.detect(conn, entity["key"], history.INTERVALS[args.interval],
                                args.move_hours, tuple(args.thresholds), args.baseline_bars,
                                args.limit, not args.no_store)
    if command == "instruments":
        _validate_instruments(args)
        if args.action == "list":
            with registry.get_read_conn(args.db) as conn:
                return {"method": "instrument_inventory_v1",
                        "instruments": registry.list_instruments(
                            conn, entity_id=None if args.id is None else args.id,
                            asset_class=args.asset_class, symbol=args.symbol, limit=args.limit)}
        if args.action == "show":
            if args.id is None:
                raise ValueError("instruments show needs an ID")
            with registry.get_read_conn(args.db) as conn:
                entity = _instrument(conn, args)
                rows = registry.list_instruments(conn, entity_id=entity["id"])
                return {"method": "instrument_inventory_v1", "entity_key": entity["key"],
                        "instruments": rows,
                        "note": ("no instrument is registered for this entity, so its price "
                                 "bars carry no venue, currency or adjustment basis"
                                 if not rows else None)}
        if not args.symbol:
            raise ValueError("instruments add needs --symbol")
        if not args.asset_class:
            raise ValueError("instruments add needs --asset-class")
        with registry.get_conn(args.db) as conn:
            entity = _instrument(conn, args)
            registry.add_instrument(
                conn, entity_id=entity["id"], symbol=args.symbol, venue=args.venue,
                asset_class=args.asset_class, quote_currency=args.quote_currency,
                contract_multiplier=args.contract_multiplier, expiry=args.expiry,
                adjustment_basis=args.adjustment, rights_basis=args.rights_basis,
                rights_verified=False, first_seen_at=clock.now().replace(
                    microsecond=0).isoformat(),
                last_seen_at=clock.now().replace(microsecond=0).isoformat(),
                notes=args.notes)
            return {"method": "instrument_inventory_v1", "entity_key": entity["key"],
                    "stored": 1, "rights_verified": False,
                    "rights_note": "this project never verifies redistribution rights, so the "
                                   "recorded basis stays as supplied and the flag stays false",
                    "instruments": registry.list_instruments(conn, entity_id=entity["id"])}
    if command == "framework":
        if args.action == "show":
            if not args.key:
                raise CLIError("framework show needs a KEY", 2)
            note = framework_notes.get_note(args.key)
            if note is None:
                raise CLIError(f"no framework note with key {args.key!r}", 2)
            return {"method": framework_notes.METHOD, "note": note,
                    "not_a_signal": list(framework_notes.NOT_A_SIGNAL)}
        if args.action == "excluded":
            return {"method": framework_notes.METHOD,
                    "note": "claims this project declines to assert, and why",
                    "excluded": framework_notes.excluded()}
        if args.action == "all":
            return framework_notes.report()
        notes = framework_notes.list_notes(grade=args.grade, topic=args.topic)
        return {"method": framework_notes.METHOD, "count": len(notes),
                "grade_filter": args.grade or None, "topic_filter": args.topic or None,
                "notes": notes, "not_a_signal": list(framework_notes.NOT_A_SIGNAL)}
    if command == "volatility":
        _validate_volatility(args)
        store = bool(args.store)
        with (registry.get_conn(args.db) if store
              else registry.get_read_conn(args.db)) as conn:
            entity = _instrument(conn, args)
            return volatility.measure(
                conn, entity["key"], history.INTERVALS[args.interval],
                estimators=tuple(args.estimator) or None, window_bars=args.window_bars,
                asset_class=args.asset_class, store=store)
    if command == "causes":
        _validate_causes(args)
        with (registry.get_conn(args.db) if args.action == "attribute"
              else registry.get_read_conn(args.db)) as conn:
            entity = _instrument(conn, args)
            interval_seconds = history.INTERVALS[args.interval]
            if args.action == "context":
                return causes.profile_context(
                    conn, entity["key"], interval_seconds, args.move_hours, args.pre_hours,
                    causes.PERMUTATIONS,
                    None if args.tier == "all" else args.tier,
                    None if args.direction == "all" else args.direction, args.controls,
                    measure=tuple(args.measure))
            if args.action == "attribute":
                return causes.attribute(
                    conn, args.id, entity["key"], interval_seconds, args.move_hours,
                    args.topic, args.pre_hours, _tiers(args), _directions(args),
                    args.controls, "English", args.articles, args.moves,
                    news_provider=args.news_source)
            return causes.profile(conn, entity["key"], interval_seconds, args.move_hours,
                                  args.pre_hours, causes.PERMUTATIONS,
                                  None if args.tier == "all" else args.tier,
                                  None if args.direction == "all" else args.direction)
    if command == "context":
        _validate_context(args)
        if args.action == "derive":
            with registry.get_conn(args.db) as conn:
                return _derive_context(conn, args)
        with registry.get_read_conn(args.db) as conn:
            if args.action == "series":
                return stationarity.report(
                    conn, args.kind, instrument_key=args.instrument,
                    measure=args.measure[0] if args.measure else "",
                    baseline=args.baseline, now=clock.now())
            rows = registry.list_context_measures(
                conn, args.kind, instrument_key=args.instrument,
                measure=args.measure[0] if args.measure else "",
                limit=args.limit, order="desc")
            return {"method": stationarity.METHOD, "kind": args.kind,
                    "instrument_key": args.instrument,
                    "measure": args.measure[0] if args.measure else "every",
                    "rows": len(rows), "observations": rows,
                    "series": registry.context_measure_series(conn),
                    "note": "derived quantities, re-expressions of stored observations; a stored "
                            "measure is not a new reading and not a cause of anything",
                    "limitations": list(stationarity.LIMITATIONS)}
    if command == "prune":
        _validate_prune(args)
        if args.action == "runs":
            with registry.get_read_conn(args.db) as conn:
                return retention.history(conn, limit=args.limit)
        if args.action == "plan":
            with registry.get_read_conn(args.db) as conn:
                return retention.plan(
                    conn, args.cut, keep_bars=args.keep_bars,
                    instrument_key=args.instrument_key,
                    interval_seconds=history.INTERVALS[args.interval] if args.interval else 0,
                    reason=args.reason)
        with registry.get_conn(args.db) as conn:
            return retention.apply(
                conn, args.cut, reason=args.reason, keep_bars=args.keep_bars,
                instrument_key=args.instrument_key,
                interval_seconds=history.INTERVALS[args.interval] if args.interval else 0)
    if command == "scan":
        _validate_scan(args)
        with (registry.get_read_conn(args.db) if args.no_store
              else registry.get_conn(args.db)) as conn:
            entity = _instrument(conn, args)
            interval_seconds = history.INTERVALS[args.interval]
            end = moves.current(conn, entity["key"], interval_seconds, args.move_hours)
            if end.get("status") != "ok":
                return {"method": signals.METHOD, "status": end["status"],
                        "current_move": end, "signals": [],
                        "note": "no usable stored bars; run fetch-history first"}
            return signals.detect(conn, args.id, entity["key"], interval_seconds,
                                 end=end["end_time"], horizons=tuple(args.horizons),
                                 channels=tuple(args.channels), topic=args.topic,
                                 articles=args.articles, store=not args.no_store,
                                 role="control", news_provider=args.news_source)
    if command == "rag":
        with (registry.get_conn(args.db) if args.action in ("store-events", "build-graph",
                                                           "attribute-flows", "detect-anomalies")
              else registry.get_read_conn(args.db)) as conn:
            if args.action == "store-events":
                events_path = args.args[0]
                with open(events_path, "rb") as stream:
                    raw = stream.read(projection.MAX_FILE_BYTES + 1)
                if len(raw) > projection.MAX_FILE_BYTES:
                    raise CLIError("events JSON exceeds 2 MiB", 2)
                try:
                    events_list = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=importer._pairs,
                                             parse_constant=importer._constant)
                except (UnicodeError, RecursionError) as exc:
                    raise CLIError("events JSON is not valid UTF-8") from exc
                if not isinstance(events_list, list):
                    raise CLIError("events JSON must be a list", 2)
                return rag.store_events(conn, events_list, args.source)
            if args.action == "build-graph":
                return {"created": rag.build_event_graph(conn, args.window_hours, args.min_strength)}
            if args.action == "attribute-flows":
                return {"attributed": rag.attribute_money_flows(conn, window_hours=args.window_hours)}
            if args.action == "detect-anomalies":
                if not args.instrument_key:
                    raise CLIError("--instrument-key is required for detect-anomalies", 2)
                try:
                    volatility.validate_threshold(args.mode, args.level)
                except ValueError as exc:
                    raise CLIError(str(exc), 2) from exc
                return rag.detect_anomalies(
                    conn, interval_seconds=args.interval_seconds, mode=args.mode,
                    level=args.level, window_bars=args.window_bars,
                    baseline_bars=args.baseline_bars, instrument_key=args.instrument_key)
            if args.action == "indicator":
                return rag.event_indicator_v1(conn, args.instrument_key, args.limit)
            raise CLIError("unknown rag action", 2)
    if command == "indicators":
        with (registry.get_conn(args.db) if args.action in ("compute", "project")
              else registry.get_read_conn(args.db)) as conn:
            from lele.analysis import indicators as ind_mod
            entity_id = args.entity_id or 1
            if args.action == "list":
                return {"specs": ind_mod.list_indicator_specs()}
            if args.action == "spec":
                if not args.indicator:
                    raise CLIError("--indicator is required for spec action", 2)
                return ind_mod.get_indicator_spec(args.indicator).to_dict()
            if args.action == "compute":
                if not args.indicator:
                    raise CLIError("--indicator is required for compute action", 2)
                return ind_mod.compute_indicator(conn, entity_id, args.indicator)
            if args.action == "project":
                result = ind_mod.project_indicators_to_worldstate(conn, entity_id)
                if args.output:
                    _export_target(args)
                    _export({"observations": result["observations"]}, args)
                    return {"projected": len(result["observations"]), "output": args.output}
                return result
            raise CLIError("unknown indicators action", 2)
    if command == "episodes":
        with closing(registry.read_connect(args.db)) as conn:
            return episodes.analyze(conn, args.id, args.path, args.evidence,
                                    args.threshold_percent, args.horizon_hours, args.limit,
                                    args.control_stride)
    if command == "finmap" and (len(args.ids) > engine.FINMAP_MAX_ISSUERS or len(set(args.ids)) != len(args.ids)):
        raise CLIError("finmap requires 1..10 unique entity IDs", 2)
    if command == "fetch":
        _validate_fetch(args)
    if command in ("export", "fetch-prices", "fetch-evidence"):
        _protect_database(args)
        _export_target(args)
    if command == "fetch-prices":
        try:
            prices.request_window(args.source, args.symbol, args.limit, args.end, prices._now())
        except ValueError as exc:
            raise CLIError(str(exc), 2) from exc
    if command in ("fetch-evidence", "fetch-cot", "fetch-short"):
        try:
            if command == "fetch-evidence":
                evidence.request_window(args.limit, args.end, evidence._now(), args.symbol)
            elif command == "fetch-cot":
                evidence.request_cot_window(args.limit, args.end, evidence._now())
            else:
                if not evidence.SHORT_SYMBOL_RE.fullmatch(args.symbol):
                    raise ValueError("symbol must be an uppercase FINRA code of 1..10 characters")
                evidence.request_short_window(args.months, args.end, evidence._now())
        except ValueError as exc:
            raise CLIError(str(exc), 2) from exc
        with closing(registry.read_connect(args.db)) as conn:
            if command == "fetch-evidence":
                payload, report = evidence.fetch_evidence(conn, args.id, args.source, args.symbol,
                                                          limit=args.limit, end=args.end)
            elif command == "fetch-cot":
                payload, report = evidence.fetch_cot(conn, args.id, args.source, args.symbol,
                                                     reports=args.limit, end=args.end)
            else:
                payload, report = evidence.fetch_short(conn, args.id, args.source, args.symbol,
                                                       months=args.months, end=args.end)
        raw = (json.dumps(payload, ensure_ascii=True, allow_nan=False, indent=2) + "\n").encode("utf-8")
        if len(raw) > projection.MAX_FILE_BYTES:
            raise CLIError("evidence export exceeds 2 MiB")
        _protect_database(args)
        _export(payload, args)
        return {**report, "output": args.output, "output_bytes": len(raw),
                "output_sha256": hashlib.sha256(raw).hexdigest()}
    if command == "fetch-news":
        try:
            if not news.TOPIC_RE.fullmatch(args.topic):
                raise ValueError("topic must be 1..64 letters, digits, spaces, hyphens or underscores")
            news.request_news_window(args.hours, args.end, news._now())
        except ValueError as exc:
            raise CLIError(str(exc), 2) from exc
        with closing(registry.read_connect(args.db)) as conn:
            payload, report = news.fetch_news(conn, args.id, args.topic, hours=args.hours,
                                              limit=args.limit, end=args.end)
        raw = (json.dumps(payload, ensure_ascii=True, allow_nan=False, indent=2) + "\n").encode("utf-8")
        if len(raw) > projection.MAX_FILE_BYTES:
            raise CLIError("evidence export exceeds 2 MiB")
        _protect_database(args)
        _export(payload, args)
        return {**report, "output": args.output, "output_bytes": len(raw),
                "output_sha256": hashlib.sha256(raw).hexdigest()}
    if command == "backup":
        try:
            return registry.backup_database(args.db, args.path, args.force)
        except ValueError as exc:
            raise CLIError(str(exc), 1) from exc
    if command == "doctor":
        health = registry.health_check(args.db)
        if health.get("registry", {}).get("present"):
            try:
                with registry.get_read_conn(args.db) as conn:
                    health["providers"] = provider_health.summary(conn)
            except sqlite3.Error:
                # Classified, not echoed: the run history could not be read, so no
                # claim is made about any source either way.
                health["providers"] = {
                    "status": "unreadable",
                    "note": "the recorded ingest runs could not be read, so no source is "
                            "reported here in either direction"}
            except registry.RegistryError:
                health["providers"] = {
                    "status": "unreadable",
                    "note": "the registry holding the recorded ingest runs could not be read, "
                            "so no source is reported here in either direction"}
        return health
    if command == "providers":
        with registry.get_read_conn(args.db) as conn:
            return provider_health.report(
                conn, source=args.source, since_hours=args.since_hours,
                stale_after_hours=args.stale_after_hours, limit=args.limit)
    if command == "summary":
        return _summary(args, parser if parser is not None else build_parser())
    if command == "init":
        registry.init_db(args.db)
        return {"initialized": True}
    session = registry.get_read_conn(args.db) if _read_only(command, args) \
        else registry.get_conn(args.db)
    with session as conn:
        if (command in ("show", "analyze", "sentiment", "relationships")
                and registry.get_entity(conn, args.id) is None):
            raise CLIError(f"entity {args.id} not found")
        if command == "list":
            return _rows(conn, args)
        if command == "show":
            return registry.entity_payload(conn, args.id)
        if command == "stats":
            return registry.stats(conn)
        if command == "countries":
            return registry.list_countries(conn)
        if command == "fetch":
            return sources.fetch_source(conn, args.source, query=args.query, country=args.country,
                                        limit=args.limit, indicator=args.indicator if args.indicator is not None else "NY.GDP.MKTP.CD",
                                        category=args.category, financials=args.financials,
                                        resume=args.resume)
        if command == "edges":
            if args.kind == "parent":
                result = sources.edge_parents(conn, limit=args.limit, refresh=args.refresh,
                                              level=args.level)
            else:
                result = sources.edge_fund_managers(conn, limit=args.limit, refresh=args.refresh)
            return {"source": args.source, "kind": args.kind,
                    **{key: value for key, value in result.items() if key != "edges"}}
        if command == "import":
            return importer.import_json(conn, args.path)
        if command == "runs":
            return registry.list_ingest_runs(conn, args.limit)
        if command == "fetch-prices":
            payload, report = prices.fetch_prices(conn, args.id, args.source, args.symbol,
                                                   limit=args.limit, end=args.end)
            raw = (json.dumps(payload, ensure_ascii=True, allow_nan=False, indent=2) + "\n").encode("utf-8")
            if len(raw) > projection.MAX_FILE_BYTES:
                raise CLIError("price export exceeds 2 MiB")
            _protect_database(args)
            _export(payload, args)
            return {**report, "output": args.output, "output_bytes": len(raw),
                    "output_sha256": hashlib.sha256(raw).hexdigest()}
        if command == "fetch-form4":
            return form4.fetch_form4(conn, args.id, args.limit)
        if command == "fetch-13f":
            return thirteenf.fetch_thirteenf(conn, args.id, args.limit)
        if command == "fetch-material":
            return material.fetch_material(conn, args.id, args.limit)
        if command == "fetch-formd":
            return formd.fetch_formd(conn, args.id, args.limit)
        if command == "fetch-nport":
            return nport.fetch_nport(conn, args.id, args.limit)
        if command == "fetch-awards":
            return awards.fetch_awards(conn, args.id, args.recipient_uei, args.limit,
                                       args.start, args.end, args.search)
        if command == "fetch-lobbying":
            return lobbying.fetch_lobbying(conn, args.id, args.client_id, args.registrant_id,
                                           args.limit, args.filing_year)
        if command == "fetch-treasury":
            return treasury.fetch_treasury(conn, args.limit, args.start, args.end)
        if command == "fetch-political":
            return political.fetch_political(conn, args.limit, args.start, args.end)
        if command == "fetch-formadv":
            return formadv.fetch_formadv(conn, args.query, args.limit, args.start)
        if command == "fetch-formadv-individual":
            return formadv.fetch_formadv_individual(conn, args.query, args.limit, args.start)
        if command == "fetch-comtrade":
            return comtrade.fetch_comtrade(conn, args.reporter, args.partner, args.hs_code,
                                           args.limit, args.start, args.end, args.freq)
        if command == "fetch-census-trade":
            return comtrade.fetch_census_trade(conn, args.limit, args.start, args.end,
                                               args.commodity, args.country)
        if command == "fetch-eia":
            return eia.fetch_eia(conn, args.series, args.api_key, args.limit, args.start, args.end)
        if command == "fetch-bls":
            return eia.fetch_bls(conn, args.series, args.limit, args.start, args.end, args.registration_key)
        if command == "fetch-opensky":
            return opensky.fetch_opensky_states(conn, args.limit, args.time, args.icao24, args.bbox,
                                                args.username, args.password) if args.action == "states" else \
                   opensky.fetch_opensky_flights(conn, args.limit, args.begin, args.end,
                                                 args.username, args.password)
        if command == "events":
            return events.study(conn, args.id, args.path, args.evidence,
                                args.move_hours, args.thresholds, args.limit)
        if command == "project":
            return projection.project(conn, args.id, args.path)
        if command == "finmap":
            return engine.finmap(conn, args.ids)
        if command == "analyze":
            return engine.analyze_entity(conn, args.id)
        if command == "sentiment":
            return engine.record_sentiment(conn, args.id, _sentiment_text(args), args.source, args.ref)
        if command == "relationships":
            return registry.entity_payload(conn, args.id)["relationships"]
        if command == "tree":
            return registry.entity_tree(conn, args.id, args.depth, args.max_nodes, args.direction)
        if command == "links":
            if args.action == "set":
                if not args.args[1].startswith("https://"):
                    raise CLIError("link URL must be public HTTPS", 1)
                importer._provenance(args.args[1], "link url")
                importer._provenance(args.source_url, "link source_url")
                if args.observed_at:
                    projection._timestamp(args.observed_at)
                return registry.set_entity_link(conn, int(args.args[0]), args.type, args.args[1],
                                                args.label, args.source_url, args.observed_at,
                                                args.evidence)
            if args.action == "list":
                return registry.list_entity_links(conn, int(args.args[0]) if args.args else None,
                                                  args.limit)
            return {"removed": registry.remove_entity_link(conn, int(args.args[0]), args.args[1])}
        if command == "flows":
            if args.action == "import":
                return flows.import_flows(conn, args.args[0])
            if args.action == "summary":
                return registry.money_flow_summary(conn, int(args.args[0]), args.limit)
            return registry.list_money_flows(conn, int(args.args[0]) if args.args else None,
                                             args.limit)
        if command == "observations":
            if args.action == "import":
                return observations.import_observations(conn, args.args[0])
            if args.action == "evidence":
                payload, report = observations.evidence_payload(conn, int(args.args[0]))
                raw = (json.dumps(payload, ensure_ascii=True, allow_nan=False, indent=2)
                       + "\n").encode("utf-8")
                if len(raw) > projection.MAX_FILE_BYTES:
                    raise CLIError("evidence export exceeds 2 MiB")
                _protect_database(args)
                _export(payload, args)
                return {**report, "output": args.output, "output_bytes": len(raw),
                        "output_sha256": hashlib.sha256(raw).hexdigest()}
            return registry.list_observations(conn, int(args.args[0]) if args.args else None,
                                              args.limit)
        if command == "sanctions":
            if args.action == "fetch":
                return sanctions.fetch_sanctions(conn, args.args[0])
            if args.action == "import":
                return sanctions.import_sanctions(conn, args.args[0], args.args[1],
                                                  args.source_url, args.retrieved_at or None)
            if args.action == "candidates":
                return registry.sanctions_candidates(conn, int(args.args[0]) if args.args else None,
                                                     args.limit, args.fuzzy)
            if args.action == "link":
                return registry.link_sanctions(conn, int(args.args[0]), int(args.args[1]),
                                               args.reason)
            if args.action == "unlink":
                return {"unlinked": registry.unlink_sanctions(conn, int(args.args[0]),
                                                              int(args.args[1]))}
            if args.action == "listings":
                return registry.list_sanctions_listings(conn, args.limit, args.active_only)
            return registry.sanctions_links(conn, args.limit)
        if command == "resolve":
            if args.action == "candidates":
                if len(args.ids) > 1:
                    raise CLIError("resolve candidates accepts at most one ID", 2)
                return registry.matching_candidates(conn, args.ids[0] if args.ids else None,
                                                    args.limit, args.fuzzy)
            if args.action == "list":
                if args.ids:
                    raise CLIError("resolve list accepts no IDs", 2)
                return registry.list_aliases(conn, args.limit)
            if args.action == "merge":
                if len(args.ids) != 2:
                    raise CLIError("resolve merge requires ALIAS_ID and CANONICAL_ID", 2)
                return registry.merge_entity(conn, args.ids[0], args.ids[1], args.reason,
                                             args.source_url)
            if len(args.ids) != 1:
                raise CLIError("resolve unmerge requires exactly one ALIAS_ID", 2)
            return {"unmerged": registry.unmerge_entity(conn, args.ids[0])}
        if command == "export":
            rows = _rows(conn, args)
    if command == "export":
        return _export(rows, args)
    raise CLIError("unknown command", 2)


def _display_cell(value):
    text = "" if value is None else str(value)
    text = "".join(c if c.isprintable() else " " for c in text)
    return text if len(text) <= 60 else text[:57] + "..."


def _table(rows, fields):
    if not rows:
        print("No results.")
        return
    values = [[_display_cell(row.get(field)) for field in fields] for row in rows]
    widths = [max(len(field), *(len(row[i]) for row in values)) for i, field in enumerate(fields)]
    print("  ".join(field.upper().ljust(width) for field, width in zip(fields, widths)).rstrip())
    for row in values:
        print("  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip())


def _emit(result, args):
    if args.json:
        print(json.dumps(result, ensure_ascii=True, allow_nan=False, separators=(",", ":")))
    elif args.command == "list":
        _table(result, ("id", "kind", "name", "country"))
    elif args.command == "version":
        print(f"{constants.LOGO}\n  {constants.TAGLINE}\n")
        _table([result], ("name", "version", "tagline"))
    elif args.command == "sources":
        _table(result, ("id", "name", "status", "coverage"))
    elif args.command == "edges":
        _table(rows=[result], fields=("processed", "linked", "missing", "retracted", "skipped"))
        for warning in result["warnings"]:
            print("Warning: " + _display_cell(warning), file=sys.stderr)
    elif args.command == "relationships":
        _table(result, ("dir", "rel", "other_id", "other_name"))
    elif args.command == "countries":
        print("\n".join(_display_cell(country) for country in result) if result else "No results.")
    else:
        print(json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2))


def _prompt(message):
    print(message, end="", file=sys.stderr, flush=True)
    return input().strip()


def _menu(args, parser):
    prefix = ["--db", args.db] + (["--json"] if args.json else [])
    choices = list(MENU_LABELS)
    try:
        while True:
            print(f"\n{constants.LOGO}\n  {constants.TAGLINE}\n", file=sys.stderr)
            print("Menu", file=sys.stderr)
            for index, command in enumerate(choices, 1):
                print(f"{index:2}. {MENU_LABELS[command]} ({command})", file=sys.stderr)
            choice = _prompt("Choose a number or command: ").lower()
            if choice.isascii() and choice.isdecimal() and len(choice) < 4 and 1 <= int(choice) <= len(choices):
                choice = choices[int(choice) - 1]
            if choice in ("quit", "q", "exit"):
                return 0
            if choice in ("back", "", "b"):
                continue
            if choice == "help":
                parser.print_help(file=sys.stderr)
                continue
            if choice not in COMMANDS or choice == "menu":
                print("Unknown menu choice; use help or quit.", file=sys.stderr)
                continue
            tokens = []
            if choice not in ("init", "sources", "stats", "countries"):
                line = _prompt(f"Arguments for {choice} (--help for syntax; back/quit): ")
                if line.lower() in ("quit", "q", "exit"):
                    return 0
                if line.lower() in ("back", "b"):
                    continue
                try:
                    tokens = shlex.split(line)
                except ValueError:
                    print("Invalid quoting; operation cancelled.", file=sys.stderr)
                    continue
            main(prefix + [choice] + tokens)
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return 0


def _fail(summary, hint=None, status=1):
    """Report a failure without echoing anything an exception might carry.

    An exception message can contain a URL with a token, a connection string
    with a password, or a request body, and this output is what ends up in a
    shell history, a CI log and a pasted bug report. So the classification of
    the failure is printed and the exception itself is not. `LELE_DEBUG=1`
    is the operator explicitly asking to see it on their own terminal.
    """
    message = summary if hint is None else f"{summary}\n  {hint}"
    print(f"lele: {message}", file=sys.stderr)
    if _debug():
        traceback.print_exc()
    return status


def main(argv=None):
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        if args.command is None:
            if sys.stdin.isatty() and not args.json:
                return _menu(args, parser)
            if args.json:
                print(json.dumps({"commands": COMMANDS, "menu_labels": MENU_LABELS, "usage": parser.format_usage().strip(), "global_options": "--db PATH and --json precede the command"}))
            else:
                parser.print_help()
            return 0
        if args.command == "menu":
            return _menu(args, parser)
        result = _dispatch(args, parser)
        if result is not None:
            _emit(result, args)
        return 0
    except SystemExit as exc:
        if exc.code is None:
            return 0
        if isinstance(exc.code, int):
            return exc.code
        print(exc.code, file=sys.stderr)
        return 1
    except CLIError as exc:
        print(f"lele: {exc}", file=sys.stderr)
        return exc.status
    except (KeyboardInterrupt, EOFError):
        return _fail("operation cancelled", "no changes were committed")
    except registry.RegistryMissing:
        return _fail("registry not found", "run `lele init` first, or pass --db PATH")
    except (SourceError, URLError):
        return _fail("source request failed",
                     "check connectivity, whether the provider is up, and any API key this "
                     "source needs; set LELE_DEBUG=1 to see which request failed")
    except sqlite3.IntegrityError:
        return _fail("the registry rejected a write",
                     "run `lele doctor` to check integrity and referential state; set "
                     "LELE_DEBUG=1 to see the constraint that was violated")
    except sqlite3.OperationalError as exc:
        text = str(exc).lower()
        if "readonly" in text or "read-only" in text:
            return _fail("the registry is open read-only and this command writes to it",
                         "use a command that does not write, or point --db at a writable copy")
        if "locked" in text or "busy" in text:
            return _fail("the registry is locked by another process",
                         "wait for the other lele run to finish, or copy the database and "
                         "work on the copy")
        return _fail("database error", "run `lele doctor`; set LELE_DEBUG=1 for detail")
    except sqlite3.Error:
        return _fail("database error", "run `lele doctor`; set LELE_DEBUG=1 for detail")
    except OSError:
        return _fail("file or directory error",
                     "check the path, its permissions and free disk space; set "
                     "LELE_DEBUG=1 for detail")
    except (ValueError, UnicodeError, OverflowError, RecursionError):
        return _fail("invalid data or registry schema",
                     "check the input, and run `lele doctor` if the registry is involved; "
                     "set LELE_DEBUG=1 for detail")
    except Exception as exc:
        return _fail(f"unexpected {type(exc).__name__}",
                     "this is a defect, not a bad input; set LELE_DEBUG=1 for the full "
                     "traceback")
