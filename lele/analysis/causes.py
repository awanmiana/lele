"""Attribute candidate reasons to detected price moves, then read the pattern back.

Three steps, all recorded:

1. `attribute` fetches bounded GDELT headlines for the window strictly before each
   move, classifies each headline against a fixed lexicon of cause categories,
   and stores the per-move category set. Headlines matching no term stay
   uncategorised rather than being forced into a bucket.
2. `profile` compares how often each category appears before moves with how often
   it appears before matched non-move control windows, and reports a permutation
   p-value under an explicit null.
3. `scan` reads the newest bars, classifies the current pre-move window, and
   reports which of the current categories also appear in the historical profile
   for the same tier and direction.

A category is a candidate reason published near a move, never a demonstrated
cause. Nothing here forecasts a price or a future move.
"""
import hashlib
import json
import math
import random
import re
from ..core import clock
from datetime import datetime, timedelta, UTC
from decimal import Decimal, InvalidOperation, localcontext

from ..core import registry
from ..fetchers import news, news_rss
from . import moves

METHOD = "news_cause_profile_v1"
PROVIDER_WINDOW_HOURS = 24 * 90
PROVIDER_REACH_HOURS = {
    "gdelt": news.MAX_HOURS,
    "google_news": news_rss.MAX_HOURS,
}
PERMUTATIONS = 2000
PERMUTATION_SEED = 20260927
ALPHA = 0.05
MIN_WINDOWS_FOR_INFERENCE = 8
MAX_ARTICLES_PER_SPAN = 250

CAUSE_LEXICON = {
    "macro_rates": (
        "federal reserve", "fed ", "fomc", "rate cut", "rate hike", "interest rate",
        "inflation", "cpi", "ppi", "jobs report", "nonfarm", "payrolls", "unemployment",
        "ecb", "boj", "central bank", "monetary policy", "treasury yield", "bond yield",
        "recession", "gdp", "stimulus", "quantitative easing", "dot plot", "hawkish",
        "dovish", "liquidity", "dollar index"),
    "regulation_legal": (
        "sec", "cftc", "regulation", "regulatory", "regulator", "lawsuit", "sue", "sues",
        "sued", "suing", "court", "judge", "ruling", "injunction", "ban", "banned", "bans",
        "crackdown", "subpoena", "congress", "senate", "legislation", "bill", "white house",
        "executive order", "finCEN", "money laundering", "sanction", "sanctions", "fine",
        "fined", "penalty", "penalties", "probe", "investigation", "antitrust", "compliance",
        "license", "tax", "taxation", "capital requirement", "securities law", "enforcement",
        "laws", "policy", "rule change", "moratorium"),
    "etf_institutional": (
        "etf", "spot bitcoin etf", "blackrock", "ibit", "fbtc", "grayscale", "fidelity",
        "inflows", "outflows", "institutional", "fund flow", "hedge fund", "microstrategy",
        "treasury company", "nasdaq", "NYSE", "s-1", "public company", "shareholder",
        "buyback", "index inclusion", "cme futures", "open interest"),
    "security_incident": (
        "hack", "hacked", "exploit", "breach", "stolen", "theft", "robbery", "laundering",
        "rug pull", "scam", "fraud", "phishing", "private key", "wallet drained",
        "bridge exploit", "freeze", "frozen", "seized", "seizure", "ransomware", "leak"),
    "exchange_incident": (
        "binance", "coinbase", "kraken", "ftx", "exchange outage", "withdrawal suspended",
        "withdrawals halted", "bankruptcy", "chapter 11", "insolvency", "delisting",
        "delisted", "liquidation", "liquidity crisis", "trading halt", "outage", "breach of trust"),
    "whale_onchain": (
        "whale", "on-chain", "onchain", "transfer", "transferred", "moved", "large holder",
        "satoshi", "unknown wallet", "exchange inflow", "exchange outflow", "whales",
        "large transaction", "dormant", "ledger", "glassnode", "cryptoquant", "coinglass"),
    "listing_trading": (
        "listing", "listed", "pair", "trading pair", "launch", "unlock", "vesting",
        "airdrop", "token", "stablecoin", "depeg", "peg", "mempool", "upgrade", "fork",
        "network", "mainnet", "testnet", "hard fork", "halving", "adoption", "mainstream",
        "reserve", "sovereign", "country buys", "nation"),
    "mining_energy": (
        "hashrate", "hash rate", "miner", "mining", "difficulty", "energy consumption",
        "power grid", "electricity", "halving", "ASIC", "pool", "compute", "energy price"),
    "corporate_earnings": (
        "earnings", "revenue", "profit", "guidance", "quarterly results", "quarterly",
        "forecast beats", "misses estimates", "dividend", "buyback", "ceo", "cfo", "steps down",
        "steps down", "resigns", "board", "merger", "acquisition", "acquires", "ipo",
        "bankruptcy filing", "valuation", "market cap"),
    "geopolitics": (
        "war", "conflict", "strike", "invasion", "sanctions on", "tariff", "trade war",
        "military", "nuclear", "coup", "election", "protest", "unrest", "embargo",
        "china", "russia", "ukraine", "israel", "iran", "middle east"),
    "market_sentiment": (
        "fomo", "rally", "crash", "plunge", "surge", "skyrocket", "slump", "sell-off",
        "selloff", "correction", "bull run", "bear market", "all-time high", "record high",
        "short squeeze", "squeeze", "momentum", "sentiment", "optimism", "fear", "panic",
        "correction", "volatility", "correction", "top", "bottom", "upside", "downside"),
}

def _pattern(term):
    """Word-boundary matcher for one lexicon term.

    Terms are stripped first so a term written with a trailing space, such as
    "fed ", still matches a following word, and the trailing lookahead is
    applied to the term itself rather than to the space.
    """
    stripped = term.strip()
    if not stripped:
        return None
    return re.compile(rf"(?<!\w){re.escape(stripped)}(?!\w)", re.IGNORECASE)


COMPILED = {category: tuple(
    pattern for pattern in (_pattern(term) for term in terms) if pattern is not None)
    for category, terms in CAUSE_LEXICON.items()}
TERMS = {category: tuple(term.strip() for term in terms if term.strip())
         for category, terms in CAUSE_LEXICON.items()}
UNCATEGORIZED = "uncategorized"


def categories():
    return sorted(CAUSE_LEXICON)


def classify(headline):
    """Return (categories, matches) for one headline.

    A headline may match several categories; that is deliberate, because the
    question is how many distinct candidate reasons a headline raises.
    """
    if not isinstance(headline, str) or not headline.strip():
        return [], {}
    found: dict[str, list[str]] = {}
    for category in sorted(COMPILED):
        hits = [term for term, pattern in zip(TERMS[category], COMPILED[category])
                if pattern.search(headline)]
        if hits:
            found[category] = hits
    return sorted(found), found


def validate_attribute(*, interval_seconds, move_hours, topic, pre_hours, tiers, directions,
                       controls, articles, move_limit):
    if type(interval_seconds) is not int or interval_seconds < 60:
        raise ValueError("interval seconds must be an integer of at least 60")
    if type(move_hours) is not int or not 1 <= move_hours <= 24 * 365:
        raise ValueError("move hours must be an integer from 1 to 8760")
    if not isinstance(topic, str) or not news.TOPIC_RE.fullmatch(topic):
        raise ValueError("topic must be 1..64 letters, digits, spaces, hyphens or underscores")
    if type(pre_hours) is not int or not 1 <= pre_hours <= PROVIDER_WINDOW_HOURS:
        raise ValueError(f"pre hours must be an integer from 1 to {PROVIDER_WINDOW_HOURS}")
    if type(articles) is not int or not 1 <= articles <= news.MAX_ARTICLES:
        raise ValueError(f"articles must be an integer from 1 to {news.MAX_ARTICLES}")
    if type(controls) is not int or not 0 <= controls <= 200:
        raise ValueError("controls must be an integer from 0 to 200")
    if type(move_limit) is not int or not 1 <= move_limit <= moves.MAX_MOVES:
        raise ValueError(f"move limit must be an integer from 1 to {moves.MAX_MOVES}")
    _selection(tiers, directions)


def validate_profile(*, interval_seconds, move_hours, pre_hours, permutations, tier, direction):
    if type(interval_seconds) is not int or interval_seconds < 60:
        raise ValueError("interval seconds must be an integer of at least 60")
    if type(move_hours) is not int or not 1 <= move_hours <= 24 * 365:
        raise ValueError("move hours must be an integer from 1 to 8760")
    if type(pre_hours) is not int or not 1 <= pre_hours <= PROVIDER_WINDOW_HOURS:
        raise ValueError(f"pre hours must be an integer from 1 to {PROVIDER_WINDOW_HOURS}")
    if type(permutations) is not int or not 100 <= permutations <= 100000:
        raise ValueError("permutations must be an integer from 100 to 100000")
    if tier is not None and registry.move_tier_percent(tier) is None:
        raise ValueError("tier must be a percent label such as p3, p5, p7 or p11")
    if direction is not None and direction not in ("up", "down"):
        raise ValueError("direction must be up or down")


def _selection(tiers, directions):
    """Validate the tier and direction filters as subsets of what exists.

    Written as two explicit checks rather than a loop that redefines a closure
    over a rebound name each pass: the second iteration then depends on the
    first having set a variable, which is easy to break and hard to see.
    """
    if (not isinstance(tiers, (tuple, list)) or not tiers
            or any(tier != "all" and registry.move_tier_percent(tier) is None for tier in tiers)):
        raise ValueError("tiers must be a nonempty subset of all, or percent labels such as "
                         "p3, p5, p7, p11")
    if (not isinstance(directions, (tuple, list)) or not directions
            or any(direction not in ("up", "down", "all") for direction in directions)):
        raise ValueError("directions must be a nonempty subset of up, down, all")


SENTIMENT_METHOD = "headline_lexicon_v1"
POSITIVE_HEADLINE = (
    "surge", "surges", "surged", "soar", "soars", "soared", "rally", "rallies", "rallied",
    "jump", "jumps", "jumped", "gain", "gains", "gained", "rise", "rises", "rose", "climb",
    "climbs", "climbed", "record high", "all-time high", "bullish", "optimism", "optimistic",
    "breakout", "momentum", "accumulation", "inflows", "buying", "demand", "approval",
    "approved", "adopts", "eases", "eased", "boost", "boosts", "boosted", "recovery",
    "rebound", "rebounds", "rebounding", "profit", "profits", "beat", "beats",
)
NEGATIVE_HEADLINE = (
    "plunge", "plunges", "plunged", "crash", "crashes", "crashed", "slump", "slumps",
    "slumped", "tumble", "tumbles", "tumbled", "fall", "falls", "fell", "drop", "drops",
    "dropped", "sink", "sinks", "sank", "slide", "slides", "slid", "collapse", "collapses",
    "collapsed", "bearish", "fear", "fears", "panic", "sell-off", "selloff", "dump",
    "dumps", "dumped", "liquidations", "liquidation", "hack", "hacked", "exploit", "stolen",
    "theft", "scam", "fraud", "ban", "banned", "crackdown", "lawsuit", "sues", "sued",
    "probe", "investigation", "fined", "penalty", "outage", "bankruptcy", "delisting",
    "delisted", "depeg", "capitulation", "correction", "warning", "warns", "loss",
    "losses", "halted", "suspended", "freeze", "frozen", "seized",
)
NEGATORS_HEADLINE = ("not", "no", "never", "without", "denies", "denied", "fails", "failed")


def headline_sentiment(headline):
    """Score a headline for emotion.

    The general sentiment engine is tried first. Financial headlines are full of
    domain words its English vocabulary does not know, so it declines them; only
    then does a finance-word lexicon score the headline, under its own method
    name and with the engine's refusal recorded as the reason it was needed. A
    headline carrying no sentiment word returns no score rather than zero,
    because an absence of words is not neutrality.
    """
    from ..analysis import engine
    primary: dict = dict(engine.analyze_sentiment(headline)) if headline else {}
    counts: dict = dict(primary.get("counts") or {})
    if primary.get("score") is not None:
        return {"method": str(primary.get("method") or "engine"), "status": "scored",
                "score": primary["score"], "direction": primary.get("direction"),
                "confidence": primary.get("confidence"),
                "positive": counts.get("positive", 0), "negative": counts.get("negative", 0),
                "engine_reason": primary.get("reason")}
    tokens = re.findall(r"[a-z']+", headline.lower()) if isinstance(headline, str) else []
    if not tokens:
        return {"method": SENTIMENT_METHOD, "status": "unknown", "score": None,
                "direction": None, "confidence": None, "positive": 0, "negative": 0,
                "reason": "no_tokens", "engine_reason": primary.get("reason")}
    text = " " + " ".join(tokens) + " "
    positive = sum(text.count(f" {term}") for term in POSITIVE_HEADLINE)
    negative = sum(text.count(f" {term}") for term in NEGATIVE_HEADLINE)
    negated = any(f" {term} " in text for term in NEGATORS_HEADLINE)
    if negated and negative:
        positive, negative = negative, positive
        flip = "negation_applied"
    else:
        flip = None
    if positive == 0 and negative == 0:
        return {"method": SENTIMENT_METHOD, "status": "unknown", "score": None,
                "direction": None, "confidence": None, "positive": 0, "negative": 0,
                "reason": "no_sentiment_term", "engine_reason": primary.get("reason")}
    total = positive + negative
    with localcontext() as context:
        context.prec = 40
        score = (Decimal(positive - negative) / Decimal(total)).quantize(Decimal("0.0001"))
    return {"method": SENTIMENT_METHOD, "status": "scored", "score": str(score),
            "direction": "positive" if positive > negative else "negative",
            "confidence": str(Decimal(min(1.0, total / 3.0)).quantize(Decimal("0.0001"))),
            "positive": positive, "negative": negative, "negation": flip,
            "engine_reason": primary.get("reason")}


def _window_scan(conn, entity_id, instrument_key, interval_seconds, topic, language, articles,
                 cache_seconds, end_iso, role, control_key, news_provider="gdelt"):
    """Read the news channel for one window through the shared signal detector.

    Routing attribution through the same detector the current scan uses keeps one
    definition of a window, one retry policy and one coverage vocabulary.
    """
    from . import signals
    scan = signals.detect(conn, entity_id, instrument_key, interval_seconds, end=end_iso,
                          horizons=(1,), channels=("news",), topic=topic, language=language,
                          articles=articles, include_structure=False,
                          news_cache_seconds=cache_seconds, role=role,
                          control_key=control_key, news_provider=news_provider)
    coverage = scan["coverage"]["channels"].get("news", {"status": "absent"})
    return [item for item in scan["signals"] if item["channel"] == "news"], coverage


def _provider_floor(news_provider="gdelt"):
    """How far back the selected provider can be asked for a window.

    Reach follows the provider that will actually be read. A window older than
    that cannot be attributed by construction, so it is reported unavailable
    instead of being requested and coming back empty, which would otherwise look
    like a quiet news day.
    """
    reach = PROVIDER_REACH_HOURS.get(news_provider, PROVIDER_WINDOW_HOURS)
    return clock.now() - timedelta(hours=reach), reach


def _control_windows(bars, move_windows, pre_hours, count, instrument_key, interval_seconds,
                     floor):
    """Pre-move-shaped windows at times with no detected move nearby.

    Every eligible timestamp is collected first and only then thinned evenly to
    `count`. Stopping at the first `count` candidates would place every control
    in the oldest part of the series, which silently removes the most recent
    period from the comparison.
    """
    starts = [datetime.fromisoformat(bar["open_time"]) for bar in bars]
    blocked = [(datetime.fromisoformat(row["start_time"]) - timedelta(hours=pre_hours),
                datetime.fromisoformat(row["end_time"])) for row in move_windows]
    window = timedelta(hours=pre_hours)
    eligible = []
    for start in starts:
        if start - window < starts[0] or start - window < floor:
            continue
        if any(start < end and start + window > begin for begin, end in blocked):
            continue
        eligible.append(start)
    if not eligible or count <= 0:
        return []
    if len(eligible) <= count:
        chosen = eligible
    else:
        stride = len(eligible) / count
        chosen = [eligible[min(len(eligible) - 1, int(index * stride))]
                  for index in range(count)]
    return [{"key": f"{instrument_key}|{interval_seconds}|"
                     f"{(start - window).isoformat()}",
             "start_inclusive": (start - window).isoformat(),
             "end_exclusive": start.isoformat()} for start in chosen]


def attribute(conn, entity_id, instrument_key, interval_seconds, move_hours=24,
              topic="bitcoin", pre_hours=24, tiers=("medium", "big"), directions=("up", "down"),
              controls=10, language="English", articles_per_span=MAX_ARTICLES_PER_SPAN,
              move_limit=25, store=True, news_cache_seconds=21600,
              news_provider="gdelt"):
    """Fetch and classify pre-move news for recent moves and for control windows."""
    validate_attribute(interval_seconds=interval_seconds, move_hours=move_hours, topic=topic,
                       pre_hours=pre_hours, tiers=tiers, directions=directions,
                       controls=controls, articles=articles_per_span, move_limit=move_limit)
    rows = registry.list_move_events(conn, instrument_key, interval_seconds, move_hours,
                                     limit=moves.MAX_MOVES)
    wanted_tiers = (None if "all" in tiers else
                    tuple(item for item in tiers if registry.move_tier_percent(item) is not None))
    wanted_directions = tuple(item for item in ("up", "down")
                              if "all" in directions or item in directions)
    candidates = [row for row in rows
                  if (wanted_tiers is None or row["tier"] in wanted_tiers)
                  and row["direction"] in wanted_directions]
    candidates.sort(key=lambda row: row["end_time"], reverse=True)
    floor, reach_hours = _provider_floor(news_provider)
    selected, unavailable = [], []
    for row in candidates:
        window_start = datetime.fromisoformat(row["start_time"]) - timedelta(hours=pre_hours)
        if window_start >= floor:
            selected.append(row)
            if len(selected) >= move_limit:
                break
        else:
            unavailable.append({"move_event_id": row["id"], "tier": row["tier"],
                               "direction": row["direction"], "start_time": row["start_time"],
                               "change_percent": row["change_percent"],
                               "reason": "pre-move window predates the selected provider's "
                                         "reach"})
    report = {
        "method": METHOD, "instrument_key": instrument_key,
        "parameters": {"interval_seconds": interval_seconds, "move_hours": move_hours,
                       "topic": topic, "pre_hours": pre_hours,
                       "tiers": list(wanted_tiers) if wanted_tiers is not None else ["all"],
                       "directions": list(wanted_directions), "language_filter": language,
                       "controls": controls, "articles_per_span": articles_per_span,
                       "move_limit": move_limit, "news_cache_seconds": news_cache_seconds,
                       "news_provider": news_provider},
        "coverage": {"status": "ok", "moves_available": len(selected),
                     "moves_attributed": 0, "moves_unavailable": 0,
                     "cause_status_note": "provider_unavailable means the window was not read, "
                                          "so it is never counted as a move without a reason",
                     "controls_requested": controls, "controls_attributed": 0,
                     "provider": news_provider,
                     "provider_reach_hours": reach_hours,
                     "provider_window_hours": PROVIDER_WINDOW_HOURS,
                     "truth_verified": False},
        "requests": [], "moves": [], "controls": [], "category_totals": {},
        "totals": {"reasons_per_move": [], "uncategorized_articles": 0},
        "limitations": [
            "A category is a keyword match on a headline published before the move; it is a "
            "candidate reason, not a cause, and the provider is not a verified ground truth.",
            "GDELT serves a bounded recent window, so only moves inside that window can be "
            "attributed; older moves are reported unavailable instead of treated as quiet.",
            "Headlines are short and keyword matching misses sarcasm, non-English coverage and "
            "causes with no article, so a move with no attributed category is unknown, not "
            "unexplained.",
            "Control windows are sampled at a fixed stride from bars outside every move window; "
            "they are not matched on volatility, volume or news density.",
            "Coverage is unknown: a capped article list means the provider returned only its "
            "newest records for that window, which is recorded per request.",
            "No prediction, no accuracy claim and no trading recommendation is made.",
        ],
    }
    if not selected:
        report["coverage"]["status"] = ("no_servable_moves" if candidates
                                        else "no_moves_stored")
        report["coverage"]["moves_unavailable"] = len(unavailable)
        report["coverage"]["unavailable_moves"] = unavailable[:20]
        return report
    bars = registry.list_price_bars(conn, instrument_key, interval_seconds, limit=20000)
    control_windows = _control_windows(bars, selected, pre_hours, controls, instrument_key,
                                        interval_seconds, floor)
    if not selected and not control_windows:
        report["coverage"]["status"] = "no_servable_window"
        return report
    now = clock.now().replace(microsecond=0).isoformat()
    totals: dict[str, int] = {}
    reasons: list[int] = []
    status_counts: dict[str, int] = {}
    report["coverage"]["moves_unavailable"] = len(unavailable)
    report["coverage"]["unavailable_moves"] = unavailable[:20]
    for row in selected:
        signals_found, coverage = _window_scan(
            conn, entity_id, instrument_key, interval_seconds, topic, language,
            articles_per_span, news_cache_seconds, row["start_time"], "move", "",
            news_provider)
        status = _cause_status(coverage, len(signals_found))
        status_counts[status] = status_counts.get(status, 0) + 1
        summary = _store_window(conn, row["id"], "move", signals_found, now, store)
        summary.update({"tier": row["tier"], "direction": row["direction"],
                        "start_time": row["start_time"], "end_time": row["end_time"],
                        "change_percent": row["change_percent"], "z_score": row["z_score"],
                        "cause_status": status, "provider": _provider_state(coverage)})
        report["moves"].append(summary)
        report["requests"].append(_request_record(row["start_time"], pre_hours, coverage))
        if status == "attributed":
            report["coverage"]["moves_attributed"] += 1
            reasons.append(summary["distinct_categories"])
            report["totals"]["uncategorized_articles"] += summary["uncategorized_articles"]
            for category in summary["categories"]:
                totals[category] = totals.get(category, 0) + 1
    for window in control_windows:
        signals_found, coverage = _window_scan(
            conn, entity_id, instrument_key, interval_seconds, topic, language,
            articles_per_span, news_cache_seconds, window["end_exclusive"], "control",
            window["key"], news_provider)
        status = _cause_status(coverage, len(signals_found))
        status_counts[f"control_{status}"] = status_counts.get(f"control_{status}", 0) + 1
        summary = _store_window(conn, None, "control", signals_found, now, store,
                                control_key=window["key"])
        summary["cause_status"] = status
        summary["provider"] = _provider_state(coverage)
        report["controls"].append(summary)
        report["requests"].append(_request_record(window["end_exclusive"], pre_hours, coverage))
        if status == "attributed":
            report["coverage"]["controls_attributed"] += 1
    report["category_totals"] = dict(sorted(totals.items()))
    report["totals"]["reasons_per_move"] = reasons
    report["totals"]["cause_status"] = dict(sorted(status_counts.items()))
    report["totals"]["mean_reasons_per_move"] = (
        round(sum(reasons) / len(reasons), 3) if reasons else None)
    report["totals"]["moves_with_no_reason"] = sum(1 for value in reasons if value == 0)
    if reasons and not all(reasons):
        report["totals"]["moves_with_no_categorized_reason"] = sum(
            1 for value in reasons if value == 0)
    report["coverage"]["status"] = "ok" if report["coverage"]["moves_attributed"] else (
        "provider_unavailable" if status_counts.get("provider_unavailable") else "no_articles")
    return report


def _cause_status(coverage, found):
    """Separate an unreadable window from a window that genuinely held no article."""
    status = coverage.get("status")
    if status in ("failed", "outside_provider_reach", "absent"):
        return "provider_unavailable"
    if status == "empty" or found == 0:
        return "no_articles"
    if coverage.get("truncated_requests"):
        return "attributed_truncated"
    return "attributed"


def _provider_state(coverage):
    return {"status": coverage.get("status"), "articles": coverage.get("articles_in_window"),
            "truncated": coverage.get("truncated_requests"),
            "requests": len(coverage.get("requests", [])),
            "errors": [item.get("errors") for item in coverage.get("requests", [])
                       if item.get("status") == "failed"][:2]}


def _request_record(end_iso, pre_hours, coverage):
    end = datetime.fromisoformat(end_iso)
    return {"start_inclusive": (end - timedelta(hours=pre_hours)).isoformat(),
            "end_exclusive": end_iso, "status": coverage.get("status"),
            "articles_in_window": coverage.get("articles_in_window"),
            "truncated_requests": coverage.get("truncated_requests"),
            "source_url": (coverage.get("requests") or [{}])[-1].get("source_url", ""),
            "retrieved_at": (coverage.get("requests") or [{}])[-1].get("retrieved_at", "")}


def _classify_articles(signal_rows):
    """Normalise detector news signals into the shape the store and report expect."""
    out = []
    for row in signal_rows:
        detail = row.get("detail") or {}
        out.append({"row": row, "categories": list(detail.get("categories", [])),
                    "matches": {"categories": detail.get("categories", [])},
                    "headline": row.get("label", ""), "domain": detail.get("domain", ""),
                    "observed_at": row.get("observed_at", ""),
                    "source_url": row.get("source_url", ""),
                    "sentiment": row.get("magnitude")})
    return out


def _store_window(conn, move_id, role, signal_rows, now, store, control_key=""):
    classified = _classify_articles(signal_rows)
    counts: dict[str, int] = {}
    for item in classified:
        for category in item["categories"] or (UNCATEGORIZED,):
            counts[category] = counts.get(category, 0) + 1
    if store:
        for item in classified:
            for category in item["categories"]:
                registry.add_move_cause(
                    conn, move_event_id=move_id, role=role, control_key=control_key,
                    category=category, article_id=f"{news.PLATFORM}:{hashlib.sha256(item['source_url'].encode('utf-8')).hexdigest()[:16]}",
                    observed_at=item["observed_at"], headline=item["headline"],
                    domain=item["domain"], source_url=item["source_url"],
                    source=news.PLATFORM, retrieved_at=now,
                    matched_terms=json.dumps(item["categories"], sort_keys=True,
                                             separators=(",", ":")))
    return {
        "move_event_id": move_id, "role": role, "control_key": control_key or None,
        "articles": len(signal_rows),
        "categories": sorted(item for item in counts if item != UNCATEGORIZED),
        "distinct_categories": len([item for item in counts if item != UNCATEGORIZED]),
        "uncategorized_articles": counts.get(UNCATEGORIZED, 0),
        "category_articles": dict(sorted(counts.items())),
        "sentiment": {"mean": _mean([item["sentiment"] for item in classified]),
                      "negative": sum(1 for item in classified
                                      if item["row"].get("direction") == "negative"),
                      "positive": sum(1 for item in classified
                                      if item["row"].get("direction") == "positive")},
        "headline_fingerprint": fingerprint(classified),
        "headlines": [{"observed_at": item["observed_at"], "title": item["headline"],
                       "domain": item["domain"], "url": item["source_url"],
                       "categories": item["categories"], "sentiment": item["sentiment"]}
                      for item in classified[:12]],
    }


def _mean(values):
    numbers = []
    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        try:
            numbers.append(float(Decimal(str(value))))
        except (InvalidOperation, ValueError, TypeError):
            continue
    if not numbers:
        return None
    with localcontext() as context:
        context.prec = 40
        return str((Decimal(sum(numbers)) / Decimal(len(numbers))).quantize(Decimal("0.0001")))


def _presence(flag_by_window, size):
    if not size:
        return 0.0
    return sum(1 for flag in flag_by_window if flag) / size


def profile(conn, instrument_key, interval_seconds, move_hours=24, pre_hours=24,
            permutations=PERMUTATIONS, tier=None, direction=None):
    """Compare category presence before moves with control windows.

    The permutation null is that per-window category flags are exchangeable between the
    move group and the control group. The p-value is the share of permutations
    at least as extreme as the observed difference.
    """
    validate_profile(interval_seconds=interval_seconds, move_hours=move_hours,
                     pre_hours=pre_hours, permutations=permutations, tier=tier,
                     direction=direction)
    rows = registry.list_move_events(conn, instrument_key, interval_seconds, move_hours,
                                     limit=moves.MAX_MOVES)
    if tier:
        rows = [row for row in rows if row["tier"] == tier]
    if direction:
        rows = [row for row in rows if row["direction"] == direction]
    attributed = {entry["window_id"]: entry for entry
                  in registry.list_cause_windows(conn, "move", instrument_key)}
    move_windows = [{"key": str(row["id"]), "id": row["id"], "tier": row["tier"],
                     "direction": row["direction"],
                     "categories": set(attributed.get(row["id"], {}).get("categories", []))}
                    for row in rows if row["id"] in attributed]
    control_windows = [{"key": entry["control_key"],
                        "categories": set(entry["categories"])}
                       for entry in registry.list_cause_windows(conn, "control", instrument_key)]
    if not move_windows:
        return {"method": METHOD, "status": "no_attributed_moves",
                "instrument_key": instrument_key, "move_hours": move_hours,
                "pre_hours": pre_hours, "note": "run causes attribute first; moves outside the "
                        "provider window carry no categories and are not counted as quiet",
                "move_windows": 0, "control_windows": len(control_windows)}
    names = categories()
    generator = random.Random(PERMUTATION_SEED)
    observed = []
    for name in names:
        move_flags = [1 if name in window["categories"] else 0 for window in move_windows]
        control_flags = [1 if name in window["categories"] else 0 for window in control_windows]
        move_share = _presence(move_flags, len(move_windows))
        control_share = _presence(control_flags, len(control_windows))
        enrichment = (None if control_share == 0 else
                      round(move_share / control_share, 4) if control_share else None)
        p_value = _permutation(move_flags, control_flags, permutations, generator)
        observed.append({
            "category": name, "moves_with_category": sum(move_flags),
            "controls_with_category": sum(control_flags),
            "move_share": round(move_share, 4), "control_share": round(control_share, 4),
            "enrichment": enrichment, "permutation_p": p_value,
        })
    adjusted = benjamini_hochberg([item["permutation_p"] for item in observed])
    for item, q_value in zip(observed, adjusted):
        item["adjusted_p"] = q_value
        item["significant_after_correction"] = bool(
            q_value is not None and q_value <= ALPHA)
    observed.sort(key=lambda item: (-item["move_share"],
                                    -(item["enrichment"] or 0), item["category"]))
    return {
        "method": METHOD, "status": "ok", "instrument_key": instrument_key,
        "interval_seconds": interval_seconds, "move_hours": move_hours,
        "pre_hours": pre_hours, "tier": tier, "direction": direction,
        "move_windows": len(move_windows), "control_windows": len(control_windows),
        "permutation_null": "per-window flags are exchangeable between moves and controls",
        "permutations": permutations, "permutation_seed": PERMUTATION_SEED,
        "categories": observed,
        "multiple_testing": {
            "categories_tested": len(observed),
            "method": "Benjamini-Hochberg step-up on the permutation p-values",
            "alpha": ALPHA,
            "raw_p_is_descriptive": True,
            "note": "raw permutation p-values are read as descriptive; adjusted_p is the value "
                    "to quote, and significant_after_correction is the only flag that may be "
                    "reported as a category that stands out against the controls",
        },
        "inference_status": _inference_status(len(move_windows), len(control_windows),
                                              adjusted, ALPHA),
        "limitations": [
            "Presence is per window, so one article carrying three terms counts once per "
            "category and a headline can raise several candidate reasons at once.",
            "Move windows are the largest non-overlapping moves, so this profile describes those "
            "moves and not the full distribution of moves.",
            "Control windows are stride-sampled non-move periods and are not matched on "
            "volatility, volume, hour of day or news density, so a difference mixes cause with "
            "selection.",
            "A keyword lexicon is fixed and English-only; categories it omits are invisible here.",
            "The permutation null assumes exchangeable windows; serially adjacent windows and a "
            "single news burst violate that, so p-values are optimistic.",
        ],
    }


def _time_trend(move_windows, control_windows, name, segments=3):
    """Whether a condition's recorded level simply drifts with calendar time.

    A level that rises steadily, such as aggregate stablecoin supply, will read
    as lower or higher in one group purely because that group's windows sit at
    different dates. Comparing the earliest and latest thirds of the pooled
    windows detects that, and a drifting level is reported as confounded rather
    than as a difference between moves and controls.
    """
    pairs = sorted((window["start_time"], window["values"][name])
                    for window in move_windows + control_windows
                    if name in window.get("values", {}))
    if len(pairs) < 3 * MIN_GROUP_FOR_PERMUTATION:
        return {"trending": False, "reason": "too few measured windows to test for drift"}
    size = max(2, len(pairs) // segments)
    first = [value for _, value in pairs[:size]]
    last = [value for _, value in pairs[-size:]]
    first_mean = sum(first) / len(first)
    last_mean = sum(last) / len(last)
    pooled = [value for _, value in pairs]
    overall = sum(pooled) / len(pooled)
    variance = sum((value - overall) ** 2 for value in pooled) / len(pooled)
    deviation = variance ** 0.5
    if deviation == 0:
        return {"trending": False, "reason": "no variation in the recorded level"}
    drift = abs(last_mean - first_mean) / deviation
    trending = drift >= 1.0
    return {
        "trending": trending,
        "earliest_third_mean": round(first_mean, 6), "latest_third_mean": round(last_mean, 6),
        "drift_in_standard_deviations": round(drift, 4),
        "windows_compared": len(pairs),
        "reason": ("the recorded level drifts with calendar time, so a difference between "
                   "groups can reflect when their windows sit rather than what happened"
                   if trending else "no material drift across the compared windows"),
    }


def _permutation_means(move_values, control_values, permutations, generator):
    """Two-sided permutation p-value for a difference in means.

    Shuffling the pooled values leaves both groups' internal structure intact, so
    this asks how often two groups this size differ by at least this much when
    they are drawn from the same population.
    """
    if len(move_values) < MIN_GROUP_FOR_PERMUTATION or \
            len(control_values) < MIN_GROUP_FOR_PERMUTATION:
        return None
    pooled = list(move_values) + list(control_values)
    size = len(move_values)
    observed = abs(sum(move_values) / size - sum(pooled[size:]) / len(pooled[size:]))
    total = sum(pooled)
    count = 0
    for _ in range(permutations):
        generator.shuffle(pooled)
        difference = abs(sum(pooled[:size]) / size - (total - sum(pooled[:size]))
                         / (len(pooled) - size))
        if difference >= observed - 1e-9:
            count += 1
    return round((count + 1) / (permutations + 1), 4)


def _inference_status(move_windows, control_windows, survivors, alpha):
    """Refuse to imply a finding when the window count cannot support one.

    Callers pass only the conditions that cleared the correction and are free of
    a time-trend confound, so the verdict and the per-row flag cannot disagree.
    """
    survivors = len(survivors)
    if move_windows < MIN_WINDOWS_FOR_INFERENCE or control_windows < MIN_WINDOWS_FOR_INFERENCE:
        return {
            "usable": False,
            "reason": f"at least {MIN_WINDOWS_FOR_INFERENCE} attributed move windows and "
                      f"{MIN_WINDOWS_FOR_INFERENCE} control windows are needed before any "
                      "category may be called out; this run has "
                      f"{move_windows} and {control_windows}",
            "categories_surviving_correction": survivors,
        }
    if not survivors:
        return {"usable": True, "categories_surviving_correction": 0,
                "reason": "no category survives the correction at this alpha; the comparison "
                          "found nothing that stands out against the control windows"}
    return {"usable": True, "categories_surviving_correction": survivors,
            "reason": f"{survivors} of the tested categories survive the correction; each is a "
                      "difference against control windows, not a demonstrated cause"}


def benjamini_hochberg(p_values, alpha=ALPHA):
    """Step-up adjusted p-values.

    Sorting ascending, the largest rank whose p-value is at or below
    `rank / total * alpha` sets the cut, and every value at or below that rank is
    reported. Raw permutation p-values from many categories are otherwise read
    as if one test had been run.
    """
    pairs = [(value, index) for index, value in enumerate(p_values) if value is not None]
    if not pairs:
        return [None] * len(p_values)
    pairs.sort()
    total = len(pairs)
    cut = 0
    for rank, (value, _) in enumerate(pairs, start=1):
        if value <= (rank / total) * alpha:
            cut = rank
    adjusted: list[float | None] = [None] * len(p_values)
    previous = 1.0
    for rank in range(total, 0, -1):
        value, index = pairs[rank - 1]
        candidate = min(1.0, value * total / rank)
        previous = min(previous, candidate)
        adjusted[index] = round(previous, 6)
    if cut == 0:
        for _, index in pairs:
            if adjusted[index] is None:
                adjusted[index] = 1.0
    return adjusted


MIN_GROUP_FOR_PERMUTATION = 2


def _permutation(move_flags, control_flags, permutations, generator):
    """Two-sided permutation p-value for the difference in presence.

    A group of one window has no permutation variance, so no p-value is
    produced rather than a number that would look like evidence.
    """
    if len(move_flags) < MIN_GROUP_FOR_PERMUTATION or \
            len(control_flags) < MIN_GROUP_FOR_PERMUTATION:
        return None
    pooled = move_flags + control_flags
    observed = abs(_presence(move_flags, len(move_flags))
                   - _presence(control_flags, len(control_flags)))
    count = 0
    move_size = len(move_flags)
    for _ in range(permutations):
        generator.shuffle(pooled)
        difference = abs(_presence(pooled[:move_size], move_size)
                         - _presence(pooled[move_size:], len(pooled) - move_size))
        if difference >= observed - 1e-12:
            count += 1
    return round((count + 1) / (permutations + 1), 4)


def _kind_coverage(conn, instrument_key, window_seconds):
    """For each stored kind, the period over which a full window could contain it.

    Comparing a series recorded for the last few weeks against controls sampled
    from a year of history turns "not yet recorded" into "did not happen", which
    reads as a strong finding. Each kind is therefore given its own coverage
    window, and its test uses only the move and control windows that fall inside
    it, so the two groups are always drawn from the same stretch of time for that
    kind. Kinds are consequently tested on different samples, which the report
    states per kind.
    """
    from . import signals
    rows = conn.execute(
        "SELECT DISTINCT kind, instrument_key FROM observations "
        "WHERE instrument_key=? OR instrument_key=''", (instrument_key,)).fetchall()
    scopes: dict[str, set] = {}
    for row in rows:
        scopes.setdefault(row["kind"], set()).add(row["instrument_key"] or "")
    coverage: dict[str, tuple] = {}
    for kind, keys in scopes.items():
        if kind not in signals.OBSERVATION_FAMILY:
            continue
        if kind not in signals.GLOBAL_CONTEXT_KINDS and instrument_key not in keys:
            continue
        placeholders = ",".join("?" for _ in keys)
        bounds = conn.execute(
            f"SELECT MIN(occurred_at) AS first, MAX(available_at) AS last FROM observations "
            f"WHERE kind=? AND instrument_key IN ({placeholders})",
            (kind, *sorted(keys))).fetchone()
        if bounds is None or not bounds["first"] or not bounds["last"]:
            continue
        first = datetime.fromisoformat(bounds["first"]).astimezone(UTC)
        last = datetime.fromisoformat(bounds["last"]).astimezone(UTC)
        if last - (first - window_seconds) < window_seconds:
            continue
        coverage[kind] = (first - window_seconds, last)
    return coverage


def _context_windows(conn, instrument_key, interval_seconds, move_hours, pre_hours, tier,
                     direction, controls):
    """Move and control windows for the stored-context profile.

    Unlike the headline profile this needs no provider, so it can reach as far
    back as the stored observations actually go. A window is kept only when the
    stored channel has at least one observation inside it, and that rule is
    applied identically to moves and controls. Applying it to moves alone would
    make every kind look enriched, because an older control window that predates
    a series would contribute a guaranteed absence.
    """
    from . import signals
    rows = registry.list_move_events(conn, instrument_key, interval_seconds, move_hours,
                                     limit=moves.MAX_MOVES)
    if tier:
        rows = [row for row in rows if row["tier"] == tier]
    if direction:
        rows = [row for row in rows if row["direction"] == direction]
    window_seconds = timedelta(hours=pre_hours)
    coverage = _kind_coverage(conn, instrument_key, window_seconds)
    if not coverage:
        return [], [], [{"reason": "no stored observation covers a window of this length"}], {}
    floor = min(span[0] for span in coverage.values())
    ceiling = max(span[1] for span in coverage.values())
    move_windows, uncovered = [], []
    for row in rows:
        boundary = datetime.fromisoformat(row["start_time"]).astimezone(UTC)
        start = boundary - window_seconds
        if start < floor or boundary > ceiling:
            continue
        found, _ = signals.stored(conn, instrument_key, start, boundary)
        if not found:
            continue
        move_windows.append({"key": str(row["id"]), "tier": row["tier"],
                             "direction": row["direction"], "start_time": row["start_time"],
                             "end_time": boundary.isoformat(),
                             "change_percent": row["change_percent"],
                             "categories": {item["kind"] for item in found},
                             "families": {item["family"] for item in found},
                             "values": _values(found),
                             "signals": len(found)})
    uncovered.append({"reason": "moves with no stored observation in the pre-move window",
                      "count": len(rows) - len(move_windows)})
    bars = registry.list_price_bars(conn, instrument_key, interval_seconds, limit=20000)
    bars = [bar for bar in bars
            if floor <= datetime.fromisoformat(bar["open_time"]) <= ceiling]
    windows = _control_windows(bars, rows, pre_hours,
                               controls if controls is not None else max(40, 2 * len(move_windows)),
                               instrument_key, interval_seconds, floor)
    control_rows, control_uncovered = [], 0
    for window in windows:
        start = datetime.fromisoformat(window["start_inclusive"])
        end = datetime.fromisoformat(window["end_exclusive"])
        found, _ = signals.stored(conn, instrument_key, start, end)
        if not found:
            control_uncovered += 1
            continue
        control_rows.append({"key": window["key"], "start_time": start.isoformat(),
                             "end_time": end.isoformat(),
                             "categories": {item["kind"] for item in found},
                             "families": {item["family"] for item in found},
                             "values": _values(found),
                             "signals": len(found)})
    if control_uncovered:
        uncovered.append({"reason": "control windows without stored coverage",
                          "count": control_uncovered})
    return move_windows, control_rows, uncovered, coverage


def _values(found):
    """Measured magnitude per kind in one window.

    Presence alone throws the measurement away, and a daily-cadence series is
    present in every window of a 24-hour lookback, so a presence test saturates
    and can find nothing. The per-window mean of the recorded magnitude keeps
    the information a series actually carries.
    """
    totals: dict[str, list] = {}
    for item in found:
        magnitude = item.get("magnitude")
        if magnitude is None:
            continue
        try:
            value = float(Decimal(str(magnitude)))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if not math.isfinite(value):
            continue
        totals.setdefault(item["kind"], []).append(value)
    return {kind: sum(values) / len(values) for kind, values in totals.items() if values}


def _within(window, span):
    return (datetime.fromisoformat(window["start_time"]) >= span[0]
            and datetime.fromisoformat(window["end_time"]) <= span[1])


def _contrast(observed, move_windows, control_windows, field, permutations, generator,
              names, coverage=None):
    rows = []
    for name in names:
        span = (coverage or {}).get(name)
        selected_moves = ([window for window in move_windows if _within(window, span)]
                          if span else move_windows)
        selected_controls = ([window for window in control_windows if _within(window, span)]
                             if span else control_windows)
        if not selected_moves or not selected_controls:
            rows.append({
                "name": name, "tested": False, "move_windows": len(selected_moves),
                "control_windows": len(selected_controls), "moves_with": 0, "controls_with": 0,
                "move_share": None, "control_share": None, "enrichment": None,
                "permutation_p": None, "magnitude_p": None, "test": "none",
                "move_windows_measured": 0, "control_windows_measured": 0,
                "move_mean": None, "control_mean": None, "mean_difference": None,
                "relative_difference": None, "confounded_by_time_trend": False,
                "reason": "no comparable window pair inside this kind's coverage",
            })
            continue
        move_flags = [1 if name in window[field] else 0 for window in selected_moves]
        control_flags = [1 if name in window[field] else 0 for window in selected_controls]
        move_share = _presence(move_flags, len(selected_moves))
        control_share = _presence(control_flags, len(selected_controls))
        enrichment = (None if control_share == 0 else round(move_share / control_share, 4))
        measured_moves = [window["values"][name] for window in selected_moves
                          if name in window.get("values", {})]
        measured_controls = [window["values"][name] for window in selected_controls
                             if name in window.get("values", {})]
        row = {
            "name": name, "tested": True, "move_windows": len(selected_moves),
            "control_windows": len(selected_controls), "moves_with": sum(move_flags),
            "controls_with": sum(control_flags), "move_share": round(move_share, 4),
            "control_share": round(control_share, 4), "enrichment": enrichment,
            "permutation_p": _permutation(move_flags, control_flags, permutations, generator),
        }
        if len(measured_moves) >= MIN_GROUP_FOR_PERMUTATION and \
                len(measured_controls) >= MIN_GROUP_FOR_PERMUTATION:
            move_mean = sum(measured_moves) / len(measured_moves)
            control_mean = sum(measured_controls) / len(measured_controls)
            trend = _time_trend(selected_moves, selected_controls, name)
            row.update({
                "confounded_by_time_trend": trend["trending"],
                "trend_detail": trend,
                "test": "difference_in_measured_mean",
                "move_windows_measured": len(measured_moves),
                "control_windows_measured": len(measured_controls),
                "move_mean": round(move_mean, 6), "control_mean": round(control_mean, 6),
                "mean_difference": round(move_mean - control_mean, 6),
                "relative_difference": (round((move_mean - control_mean) / abs(control_mean), 4)
                                        if control_mean else None),
                "magnitude_p": _permutation_means(measured_moves, measured_controls,
                                                  permutations, generator),
            })
        else:
            row.update({"test": "presence", "move_windows_measured": len(measured_moves),
                        "control_windows_measured": len(measured_controls),
                        "move_mean": None, "control_mean": None, "mean_difference": None,
                        "relative_difference": None, "magnitude_p": None})
        rows.append(row)
    presence = benjamini_hochberg([item["permutation_p"] for item in rows])
    for item, q_value in zip(rows, presence):
        item["adjusted_p"] = q_value
    magnitude = benjamini_hochberg([item["magnitude_p"] for item in rows])
    for item, q_value in zip(rows, magnitude):
        item["adjusted_magnitude_p"] = q_value
        item["significant_after_correction"] = bool(
            item["tested"] and q_value is not None and q_value <= ALPHA
            and not item.get("confounded_by_time_trend"))
    rows.sort(key=lambda item: (not item["tested"],
                                item.get("adjusted_magnitude_p") is None,
                                item.get("adjusted_magnitude_p") or 1.0,
                                -(item["move_share"] or 0), item["name"]))
    return rows


def profile_context(conn, instrument_key, interval_seconds, move_hours=24, pre_hours=24,
                    permutations=PERMUTATIONS, tier=None, direction=None, controls=None):
    """Profile stored market context before moves against control windows.

    The headline profile answers "which story types preceded a move". This one
    answers "which recorded conditions preceded a move": money movement, policy
    actions, sentiment readings and macro releases that a wired source already
    stored. It needs no news provider, so it is not limited by provider reach,
    and a missing condition is a missing record rather than a quiet news day.
    """
    validate_profile(interval_seconds=interval_seconds, move_hours=move_hours,
                     pre_hours=pre_hours, permutations=permutations, tier=tier,
                     direction=direction)
    move_windows, control_windows, uncovered, coverage = _context_windows(
        conn, instrument_key, interval_seconds, move_hours, pre_hours, tier, direction,
        controls)
    base = {
        "method": "stored_context_profile_v1", "instrument_key": instrument_key,
        "interval_seconds": interval_seconds, "move_hours": move_hours,
        "pre_hours": pre_hours, "tier": tier, "direction": direction,
        "move_windows": len(move_windows), "control_windows": len(control_windows),
        "uncovered_moves": len(uncovered),
        "uncovered_sample": uncovered[:10],
        "permutation_null": "per-window condition flags are exchangeable between "
                        "moves and controls",
        "permutations": permutations, "permutation_seed": PERMUTATION_SEED,
        "source": "stored observations only; no headline provider involved",
        "control_design": "moves and controls are both restricted to the period over which "
                          "every stored series could appear in a full pre-window; controls are "
                          "stride-sampled non-move timestamps inside that period and are kept "
                          "only when they carry at least one stored observation",
    }
    if not move_windows or not control_windows:
        base.update({"status": "insufficient_windows", "kinds": [], "families": [],
                     "kind_coverage_days": {key: round((span[1] - span[0]).total_seconds()
                                                        / 86400, 1)
                                            for key, span in sorted(coverage.items())},
                     "inference_status": _inference_status(len(move_windows),
                                                          len(control_windows), [], ALPHA),
                     "note": "a window is only counted when the stored channel has at least "
                             "one observation inside it, so an empty result means the stored "
                             "records do not reach these windows"})
        return base
    generator = random.Random(PERMUTATION_SEED)
    names = sorted({item for window in move_windows + control_windows
                    for item in window["categories"]})
    families = sorted({item for window in move_windows + control_windows
                      for item in window["families"]})
    base["status"] = "ok"
    base["kind_coverage_days"] = {key: round((span[1] - span[0]).total_seconds() / 86400, 1)
                                 for key, span in sorted(coverage.items())}
    base["kinds"] = _contrast(base, move_windows, control_windows, "categories",
                              permutations, generator, names, coverage)
    base["families"] = _contrast(base, move_windows, control_windows, "families",
                                 permutations, generator, families)
    base["inference_status"] = _inference_status(
        len(move_windows), len(control_windows),
        [item["adjusted_magnitude_p"] for item in base["kinds"]
         if item["significant_after_correction"]], ALPHA)
    base["multiple_testing"] = {
        "kinds_tested": len(base["kinds"]), "families_tested": len(base["families"]),
        "method": "Benjamini-Hochberg step-up on the permutation p-values", "alpha": ALPHA,
        "note": "adjusted_p is the value to quote; a surviving kind is a difference against "
                "control windows and never a demonstrated cause",
    }
    base["signal_density"] = {
        "moves": [window["signals"] for window in move_windows],
        "controls": [window["signals"] for window in control_windows],
        "mean_moves": round(sum(window["signals"] for window in move_windows)
                            / len(move_windows), 3),
        "mean_controls": round(sum(window["signals"] for window in control_windows)
                               / len(control_windows), 3),
    }
    base["limitations"] = [
        "Presence is per window, so one recorded condition counts once however many "
        "observations carry it.",
        "A move is only counted when at least one stored observation falls inside its "
        "pre-window, which biases the sample towards well-covered periods.",
        "Each kind is tested over its own coverage, so kinds are compared on different "
        "samples and their p-values are not jointly interpretable as one experiment.",
        "Control windows are stride-sampled non-move periods and are not matched on "
        "volatility, volume, hour of day or news density.",
        "A kind present in every window carries no information; the enrichment ratio is "
        "undefined for it and its p-value is not evidence of anything.",
        "Serially adjacent windows and a single policy or flow event violate the exchangeable "
        "null, so p-values are optimistic.",
        "A recorded condition is context, not a cause. A policy action recorded before a move "
        "does not show it moved the price.",
    ]
    return base


def scan(conn, entity_id, instrument_key, interval_seconds, move_hours=24, topic="bitcoin",
         pre_hours=24, medium_percent=5, big_percent=10, baseline_bars=90,
         language="English", articles=news.MAX_ARTICLES, store=True):
    """Read current conditions with the same parameters used for history.

    Reports the in-progress move, the candidate reasons published before it, and
    which of those categories also appear in the stored historical profile for
    the same tier and direction. It does not forecast.
    """
    if not isinstance(topic, str) or not news.TOPIC_RE.fullmatch(topic):
        raise ValueError("topic must be 1..64 letters, digits, spaces, hyphens or underscores")
    if type(pre_hours) is not int or not 1 <= pre_hours <= PROVIDER_WINDOW_HOURS:
        raise ValueError(f"pre hours must be an integer from 1 to {PROVIDER_WINDOW_HOURS}")
    if type(articles) is not int or not 1 <= articles <= news.MAX_ARTICLES:
        raise ValueError(f"articles must be an integer from 1 to {news.MAX_ARTICLES}")
    moves.validate(move_hours, medium_percent, big_percent, baseline_bars, 1, False)
    current = moves.current(conn, instrument_key, interval_seconds, move_hours)
    report = {
        "method": METHOD, "as_of": current.get("as_of"),
        "parameters": {"interval_seconds": interval_seconds, "move_hours": move_hours,
                       "topic": topic, "pre_hours": pre_hours, "medium_percent": medium_percent,
                       "big_percent": big_percent, "baseline_bars": baseline_bars,
                       "language_filter": language},
        "current_move": current,
        "status": current.get("status", "unknown"),
        "candidate_reasons": [], "matched_history": [], "absent_from_history": [],
        "history": {}, "news": {}, "truth_verified": False,
        "limitations": [
            "The current window is measured to the newest stored bar, not to the live market; "
            "refresh history first for a current reading.",
            "A matched category means the reason type also appears in the stored profile of "
            "similar historical moves. It is not evidence that the same reason applies today, "
            "and a low or zero historical count is not proof of novelty.",
            "Reasons are keyword matches on recent headlines; absence of a match is unknown "
            "coverage rather than the absence of a cause.",
            "This reports current conditions only. It makes no forecast of price direction, "
            "magnitude or timing.",
        ],
    }
    if current.get("status") != "ok":
        return report
    change = Decimal(current["change_percent"])
    magnitude = abs(change)
    tier = "big" if magnitude >= big_percent else "medium" if magnitude >= medium_percent else None
    direction = current["direction"]
    report["current_tier"] = tier
    end = current["start_time"]
    hours = min(PROVIDER_WINDOW_HOURS, max(1, pre_hours))
    try:
        payload, provider = news.fetch_news(conn, entity_id, topic, hours=hours, limit=articles,
                                            end=end, language=language)
    except Exception as error:
        report["news"] = {"status": "failed", "error": f"{type(error).__name__}: {error}"}
        return report
    start = datetime.fromisoformat(current["start_time"]) - timedelta(hours=pre_hours)
    finish = datetime.fromisoformat(current["start_time"])
    rows = []
    for row in payload["observations"]:
        seen = datetime.fromisoformat(row["observed_at"])
        if start <= seen < finish:
            rows.append(row)
    classified = _classify_articles(rows)
    counts: dict[str, int] = {}
    for item in classified:
        for category in item["categories"] or (UNCATEGORIZED,):
            counts[category] = counts.get(category, 0) + 1
    found = sorted(category for category in counts if category != UNCATEGORIZED)
    report["news"] = {"status": "ok", "articles_returned": len(payload["observations"]),
                      "articles_in_window": len(rows),
                      "truncated_at_cap": len(payload["observations"]) >= articles,
                      "source_url": provider["source_url"],
                      "retrieved_at": provider["retrieved_at"],
                      "window": {"start_inclusive": start.isoformat(),
                                 "end_exclusive": finish.isoformat()},
                      "categories": dict(sorted(counts.items()))}
    report["candidate_reasons"] = [{"category": category, "articles": counts[category]}
                                   for category in found]
    historical = profile(conn, instrument_key, interval_seconds, move_hours, pre_hours,
                         tier=tier, direction=direction)
    report["history"] = {"status": historical.get("status"),
                         "move_windows": historical.get("move_windows", 0),
                         "control_windows": historical.get("control_windows", 0),
                         "tier": tier, "direction": direction}
    lookup = {item["category"]: item for item in historical.get("categories", [])}
    matched, absent = [], []
    for category in found:
        item = lookup.get(category)
        if item is None or item["moves_with_category"] == 0:
            absent.append(category)
        else:
            matched.append({**item, "articles_now": counts[category]})
    report["matched_history"] = matched
    report["absent_from_history"] = absent
    if store:
        now = clock.now().replace(microsecond=0).isoformat()
        registry.add_cause_scan(
            conn, instrument_key=instrument_key, interval_seconds=interval_seconds,
            move_hours=move_hours, as_of=current["as_of"],
            change_percent=current["change_percent"], observed_at=now, available_at=now,
            tier=tier, direction=direction,
            historical_moves=historical.get("move_windows", 0),
            current_categories=json.dumps(found, separators=(",", ":")),
            matched_categories=json.dumps([item["category"] for item in matched],
                                          separators=(",", ":")),
            news_articles=len(rows),
            score=_coverage_score(counts, matched),
            source_url=report["news"].get("source_url", ""),
            evidence=json.dumps({"candidate_reasons": report["candidate_reasons"],
                                 "headlines": [{"observed_at": item["row"]["observed_at"],
                                                "title": item["row"].get("description", ""),
                                                "url": item["row"].get("source_url", ""),
                                                "categories": item["categories"]}
                                               for item in classified[:20]]},
                               sort_keys=True, separators=(",", ":")))
    return report


def _coverage_score(counts, matched):
    """Share of current candidate-reason articles whose category also has history."""
    articles = sum(value for key, value in counts.items() if key != UNCATEGORIZED)
    if not articles:
        return "0"
    covered = sum(item["articles_now"] for item in matched)
    with_val = Decimal(covered) / Decimal(articles) * 100
    return str(with_val.quantize(Decimal("0.01")))


def fingerprint(items):
    """Stable hash of a headline set, so a cause set can be compared across runs."""
    payload = json.dumps(sorted((item.get("observed_at", ""), item.get("source_url", ""))
                                for item in items), separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
