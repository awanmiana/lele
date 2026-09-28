import json
import math
import re
from typing import Any, NotRequired, TypedDict

from ..core import registry
from ..core.importer import SEC_CASH_FLOW_METRICS, SEC_METRIC_TAGS, _constant, _pairs, validate_metric_envelope

ENGINE_NAME = "lele.analysis.engine"
ENGINE_VERSION = "1.0"
FUNDAMENTAL_METHOD = "matched_stored_accounting_ratios_v1"
SENTIMENT_METHOD = "english_lexicon_negation_v1"
SIGNAL_KIND = "sentiment"
MAX_TEXT_LEN = 20000
MAX_SOURCE_LEN = 200
MAX_REF_LEN = 500
NEGATION_WINDOW = 3
WORD_CAP = 50

METRIC_ALIASES = {
    "total_assets": "total_assets", "assets": "total_assets",
    "total_liabilities": "total_liabilities", "liabilities": "total_liabilities",
    "total_equity": "total_equity", "equity": "total_equity",
    "net_income": "net_income", "net_profit": "net_income",
    "revenue": "revenue", "total_revenue": "revenue",
    "cash": "cash_and_equivalents",
    "cash_and_equivalents": "cash_and_equivalents",
    "cash_and_cash_equivalents": "cash_and_equivalents",
    "debt": "total_debt", "total_debt": "total_debt",
    "inventory_net": "inventory_net", "inventory": "inventory_net",
    "long_term_debt_current": "long_term_debt_current",
    "long_term_debt_noncurrent": "long_term_debt_noncurrent",
    "short_term_borrowings": "short_term_borrowings",
    "net_cash_from_operating_activities": "net_cash_from_operating_activities",
    "operating_cash_flow": "net_cash_from_operating_activities",
    "net_cash_from_investing_activities": "net_cash_from_investing_activities",
    "investing_cash_flow": "net_cash_from_investing_activities",
    "net_cash_from_financing_activities": "net_cash_from_financing_activities",
    "financing_cash_flow": "net_cash_from_financing_activities",
}
SUPPORTED_METRICS = tuple(sorted(set(METRIC_ALIASES.values())))
FLOW_METRICS = frozenset({"net_income", "revenue", *SEC_CASH_FLOW_METRICS})
CASH_FLOW_METRICS = frozenset(SEC_CASH_FLOW_METRICS)
POSITION_SCOPE = {
    "inventory_net": "Net inventory expected to be sold or consumed within one year or the operating cycle, if longer.",
    "long_term_debt_current": "Current portion of long-term debt, excluding lease obligations; not all current debt.",
    "long_term_debt_noncurrent": "Noncurrent portion of long-term debt, excluding lease obligations.",
    "short_term_borrowings": "Short-term borrowings with initial terms shorter than one year or the operating cycle, if longer; not current maturities of long-term debt.",
    **{metric: f"Reported net {activity} cash flow including discontinued operations; sign preserved; not a cash balance."
       for metric, activity in zip(SEC_CASH_FLOW_METRICS, ("operating", "investing", "financing"))},
}
METADATA_KEYS = {
    "currency": ("currency", "reporting_currency"),
    "unit": ("unit", "units"),
    "scale": ("scale",),
}
RATIO_SPECS = (
    ("liabilities_to_assets", "total_liabilities", "total_assets"),
    ("equity_to_assets", "total_equity", "total_assets"),
    ("debt_to_assets", "total_debt", "total_assets"),
    ("debt_to_equity", "total_debt", "total_equity"),
    ("net_margin", "net_income", "revenue"),
    ("return_on_assets", "net_income", "total_assets"),
    ("return_on_equity", "net_income", "total_equity"),
    ("cash_to_assets", "cash_and_equivalents", "total_assets"),
)
POSITIVE_WORDS = frozenset({
    "profit", "profits", "profitable", "gain", "gains", "growth", "growing",
    "strong", "stronger", "resilient", "stable", "improve", "improved",
    "improvement", "success", "successful", "robust", "efficient",
    "expansion", "exceeded", "outperform", "outperformed", "upgrade",
    "upgraded", "benefit", "benefits", "surplus", "recovery", "good",
})
NEGATIVE_WORDS = frozenset({
    "loss", "losses", "decline", "declined", "declining", "weak", "weaker",
    "bad", "worse", "poor", "failed", "failure", "default", "defaulted",
    "fraud", "fraudulent", "lawsuit", "litigation", "penalty", "penalties",
    "fined", "sanctions", "investigation", "breach", "violation", "warning",
    "downgrade", "downgraded", "bankruptcy", "insolvency", "crisis", "risk",
    "risks", "risky", "layoffs", "impairment", "scandal", "misconduct",
    "shortfall", "deficit", "plunge", "slump", "concern", "concerns",
})
NEGATORS = frozenset({
    "not", "no", "never", "neither", "nor", "without", "cannot", "can't",
    "won't", "don't", "doesn't", "didn't", "isn't", "aren't", "wasn't",
    "weren't", "hardly", "barely", "lack", "lacks", "lacking",
})
ENGLISH_WORDS = frozenset({
    "the", "a", "an", "this", "that", "these", "those", "is", "are", "was",
    "were", "be", "been", "has", "have", "had", "and", "or", "but", "with",
    "of", "for", "in", "on", "to", "from", "by", "it", "its", "we", "they",
    "our", "their", "very", "reported", "reports", "report", "bank", "banks",
    "company", "earnings", "revenue", "assets", "liabilities", "equity",
    "cash", "debt", "quarter", "year", "financial", "results", "outlook",
    "regulator", "regulators", "shareholder", "shareholders", "ownership",
    "criticism", "supervision", "meeting", "today", "announced", "despite",
    "however", "yet", "only", "all", "more", "than", "as", "at",
})
FOREIGN_MARKERS = frozenset({
    "le", "la", "les", "des", "une", "est", "avec", "mais", "pas", "und",
    "der", "die", "das", "ist", "nicht", "el", "los", "las", "una", "es",
    "con", "pero", "sin", "del", "un", "il", "della", "che", "non",
    "uma", "nao", "com", "het", "een", "van", "niet",
})
_TOKEN_RE = re.compile(r"[^\W_]+(?:'[^\W_]+)?|[.!?;,\n]", re.UNICODE)
_NUMBER_RE = re.compile(
    r"[+-]?(?:(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z"
)
SENTIMENT_LIMITATIONS = [
    "English lexicon heuristic only; language screening is conservative, not language identification.",
    "Score is (positive-negative)/(positive+negative) mentions, not a financial forecast.",
    "Confidence is lexicon coverage, not predictive accuracy or an outcome probability.",
    "Negation flips polarity within three tokens and stops at punctuation or contrast words; "
    "sarcasm, quotations, targets and complex scope are not understood.",
    "Counts are mention occurrences; word lists are unique and capped at 50 per category.",
    "Criticism, regulators, shareholders and ownership do not imply exploitation.",
]
CAVEATS = [
    "Generic accounting ratios do not establish bank capital adequacy or solvency.",
    "CET1, regulatory liquidity coverage and NPL/asset quality cannot be inferred from generic accounting data.",
    "Sentiment is limited to supplied text, not representative market sentiment.",
    "Relationships are recorded links, not evidence of exploitation, misconduct or control.",
    "No price targets, forecasts or recommendations are produced.",
]


class SentimentResult(TypedDict):
    method: str
    status: str
    reason: str | None
    language: str
    score: float | None
    direction: str | None
    confidence: float | None
    confidence_basis: str
    tokens: int
    counts: dict[str, int]
    words: dict[str, list[str]]
    negated: list[str]
    limitations: list[str]


class RecordedSentiment(TypedDict):
    recorded: bool
    entity_id: int
    kind: str
    method: str
    status: str
    source: str
    ref: str
    sentiment: SentimentResult
    score: float | None
    direction: str | None
    reason: NotRequired[str]
    rationale: NotRequired[str]


class EntityReport(TypedDict):
    method: str
    found: bool
    entity_id: int
    entity: dict[str, Any] | None
    fundamentals: dict[str, Any] | None
    sentiment: dict[str, Any] | None
    relationships: list[dict[str, Any]]
    warnings: list[str]
    caveats: list[str]
    provenance: dict[str, Any]


def _parse_number(raw):
    if isinstance(raw, bool):
        return None
    text = str(raw).strip()
    if not _NUMBER_RE.fullmatch(text):
        return None
    value = float(text.replace(",", ""))
    return value if math.isfinite(value) else None


def _metadata_value(raw, field):
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        return ""
    value = str(raw).strip()
    return value.upper() if field == "currency" else value.lower()


def _metadata(details, field):
    return {_metadata_value(details[k], field)
            for k in METADATA_KEYS[field] if k in details}


def _companion(key):
    for field, aliases in METADATA_KEYS.items():
        if key in aliases:
            return "", field
        for metric, canonical in METRIC_ALIASES.items():
            if any(key == metric + sep + alias
                   for sep in ("_", ".", ":") for alias in aliases):
                return canonical, field
    return None


def _alignment_error(left, right):
    for entry in (left, right):
        if entry.get("provenance_error"):
            return entry["provenance_error"]
    observations = [entry.get("observation") for entry in (left, right)]
    if observations == [None, None]:
        return ""
    if any(item is None for item in observations):
        return "partial observation provenance; cannot verify scope/duration"
    a, b = observations
    if any(a[field] != b[field] for field in ("end", "accession", "form")):
        return "observation end/accession/form differs; scope not aligned"
    if a["period_type"] == b["period_type"] == "duration" and a["start"] != b["start"]:
        return "duration start/end differs; quarter and YTD are not interchangeable"
    return ""


def _fundamental_group(period, source, rows, attributes):
    candidates: dict[str, list[dict[str, Any]]] = {}
    metadata: dict[tuple[str, str], set[str]] = {}
    unsupported, warnings = set(), []
    for field in METADATA_KEYS:
        metadata[("", field)] = _metadata(attributes, field)
    for row in rows:
        key = str(row.get("k") or "").strip().lower()
        companion = _companion(key)
        if companion:
            metadata.setdefault(companion, set()).add(
                _metadata_value(row.get("v"), companion[1])
            )
            continue
        canonical = METRIC_ALIASES.get(key)
        if canonical is None:
            unsupported.add(key)
            continue
        raw = row.get("v")
        details = dict(row)
        embedded = raw if isinstance(raw, dict) else {}
        if isinstance(raw, str) and raw.lstrip().startswith("{"):
            try:
                embedded = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
            except (ValueError, RecursionError):
                embedded = {"observation": None}
        if isinstance(embedded, dict) and embedded:
            for field in METADATA_KEYS:
                for alias in METADATA_KEYS[field]:
                    if alias in embedded and alias in details:
                        if embedded[alias] != details[alias]:
                            details[alias] = None
                    elif alias in embedded:
                        details[alias] = embedded[alias]
            raw = embedded.get("value")
        entry = {"stored_key": key, "value": _parse_number(raw),
                 "period": period, "source": source}
        if "observation" in embedded:
            entry["observation"] = embedded["observation"]
            try:
                validate_metric_envelope(embedded, period)
                observation = embedded["observation"]
                tag_key = "cash" if canonical == "cash_and_equivalents" else canonical
                if observation["tag"] not in SEC_METRIC_TAGS.get(tag_key, ()):
                    raise ValueError("tag does not match canonical metric")
                if observation.get("ambiguous"):
                    raise ValueError("ambiguous selected SEC observation")
                if observation.get("invalid"):
                    raise ValueError("invalid selected SEC observation")
                if not observation["accession"] or not observation["form"]:
                    raise ValueError("missing accession or form")
                if observation["period_type"] == "duration" and not observation["start"]:
                    raise ValueError("unknown duration start")
            except (ValueError, TypeError, OverflowError) as exc:
                entry["provenance_error"] = f"unusable observation provenance: {exc}; refetch SEC financials"
        elif source.strip().lower() == "sec":
            entry["provenance_error"] = "legacy SEC metric lacks observation evidence; refetch SEC financials"
        else:
            warnings.append("Legacy non-SEC inputs: reporting scope/duration not verified.")
        if entry.get("provenance_error"):
            warnings.append(f"{canonical}: {entry['provenance_error']}")
        entry.update({f: _metadata(details, f) for f in METADATA_KEYS})
        candidates.setdefault(canonical, []).append(entry)
    inputs, evidence, invalid = {}, {}, []
    for canonical, entries in sorted(candidates.items()):
        valid, normalized = True, []
        for entry in entries:
            for field in METADATA_KEYS:
                values = (entry[field] | metadata.get(("", field), set())
                          | metadata.get((canonical, field), set()))
                if len(values) > 1 or "" in values:
                    valid = False
                    warnings.append(f"conflicting or invalid {field} for {canonical}")
                entry[field] = next(iter(values)) if len(values) == 1 else None
            if entry["value"] is None:
                valid = False
                warnings.append(f"metric '{entry['stored_key']}' is nonnumeric or nonfinite")
            normalized.append((*tuple(entry[f] for f in ("value", "currency", "unit", "scale")),
                               json.dumps(entry.get("observation"), sort_keys=True),
                               entry.get("provenance_error")))
        if len(set(normalized)) > 1:
            valid = False
            warnings.append(f"conflicting aliases for {canonical}; no value selected")
        if not valid:
            invalid.append(canonical)
            continue
        entry = sorted(entries, key=lambda e: e["stored_key"])[0]
        inputs[canonical] = entry["value"]
        evidence[canonical] = entry
    indicators = []
    for name, numerator, denominator in RATIO_SPECS:
        needed = [numerator, denominator]
        missing = [m for m in needed if m not in inputs]
        note, value = "", None
        if missing:
            note = "missing or unusable inputs: " + ", ".join(missing)
        elif not period.strip() or not source.strip():
            note = "period and source must be recorded to establish comparability"
        elif alignment_error := _alignment_error(evidence[numerator], evidence[denominator]):
            note = alignment_error
            warnings.append(f"{name}: {alignment_error}")
        elif any(evidence[numerator][f] != evidence[denominator][f]
                 for f in METADATA_KEYS):
            note = "currency/unit/scale metadata differs or is only partly provided"
        elif inputs[denominator] <= 0:
            note = "nonpositive denominator; ratio not interpreted"
        else:
            ratio = inputs[numerator] / inputs[denominator]
            if math.isfinite(ratio):
                value = round(ratio, 6)
                if name in ("return_on_assets", "return_on_equity"):
                    note = "Uses period-ending stock end-balance, not average balance; nonannualized (not annualized)."
            else:
                note = "ratio exceeds finite numeric range"
        indicators.append({
            "name": name, "period": period, "source": source,
            "numerator": numerator, "denominator": denominator,
            "numerator_value": inputs.get(numerator),
            "denominator_value": inputs.get(denominator),
            "inputs": {m: dict(evidence[m]) for m in needed if m in evidence},
            "value": value, "status": "ok" if value is not None else "unknown",
            "missing_metrics": missing, "note": note,
        })
    if any(any(e[f] is None for f in METADATA_KEYS) for e in evidence.values()):
        warnings.append("Absent metadata on both inputs assumes a common reporting basis; "
                        "currency/unit/scale are not independently verified.")
    if not period.strip() or not source.strip():
        warnings.append("Missing period or source; no ratios calculated.")
    return {
        "period": period, "source": source,
        **{f: next(iter(metadata[("", f)]))
           if len(metadata.get(("", f), set())) == 1 else None for f in METADATA_KEYS},
        "inputs": inputs, "input_evidence": evidence, "indicators": indicators,
        "missing_metrics": sorted(set(SUPPORTED_METRICS) - set(candidates)),
        "invalid_metrics": invalid, "unsupported_metric_keys": sorted(unsupported),
        "warnings": sorted(set(warnings)),
    }


def _fundamentals(metric_rows, attributes):
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in metric_rows:
        key = (str(row.get("period") or ""), str(row.get("source") or ""))
        groups.setdefault(key, []).append(row)
    out = [_fundamental_group(p, s, groups[(p, s)], attributes) for p, s in sorted(groups)]
    return {
        "method": FUNDAMENTAL_METHOD,
        "supported_metrics": list(SUPPORTED_METRICS),
        "metric_aliases": dict(sorted(METRIC_ALIASES.items())),
        "metadata_conventions": [
            "Entity attributes and same-period/source currency, unit and scale rows "
            "constrain every input in a group.",
            "Metric companions use metric_currency, metric_unit or metric_scale "
            "(also dot or colon separators); JSON metric values accept value/currency/unit/scale.",
            "Metadata must agree after case/whitespace normalization; no FX or scale conversion.",
        ],
        "groups": out,
        "missing_metrics": sorted(set(SUPPORTED_METRICS) - {
            METRIC_ALIASES.get(str(r.get("k") or "").strip().lower()) for r in metric_rows
        }),
        "warnings": [] if metric_rows else ["no stored metrics for this entity"],
        "limitations": [
            "Only matched period/source accounting inputs are divided; missing values are never imputed.",
            "Returns use reported period-end assets/equity, not average balances, and are not annualized.",
            "Reporting scope, audit quality and stock/flow alignment are not independently verified.",
        ] + CAVEATS[:2],
    }


FINMAP_MAX_ISSUERS = 10
FINMAP_MAX_METRICS = 1000


def _position_group(period, source, rows, attributes):
    group = _fundamental_group(period, source, rows, attributes)
    positions, warnings = [], list(group["warnings"])
    for metric in SUPPORTED_METRICS:
        entry = group["input_evidence"].get(metric, {})
        observation = entry.get("observation")
        reason = ""
        if metric in group["invalid_metrics"]:
            reason = "conflicting or invalid metric inputs; inspect raw_metrics and warnings"
        elif not entry:
            reason = "missing metric for this period/source"
        elif entry.get("provenance_error"):
            reason = entry["provenance_error"]
        elif not isinstance(observation, dict):
            reason = "observation evidence required; legacy scope/duration is unknown"
        elif not period.strip() or not source.strip():
            reason = "period and source must be recorded"
        if reason:
            warnings.append(f"{metric}: {reason}")
        positions.append({
            "metric": metric, "kind": "flow" if metric in FLOW_METRICS else "stock",
            "flow_type": "cash_flow" if metric in CASH_FLOW_METRICS else
                "accounting_flow" if metric in FLOW_METRICS else None,
            "scope": POSITION_SCOPE.get(metric, "Reported accounting flow, not cash flow." if metric in FLOW_METRICS
                                         else "Reported stock balance; not a cash flow or inferred transfer."),
            "value": None if reason else entry["value"],
            "status": "unknown" if reason else "ok", "reason": reason,
            **{field: entry.get(field) for field in METADATA_KEYS},
            "observation": observation if isinstance(observation, dict) else None,
        })
    alignment = {}
    for label, metrics in (("stocks", set(SUPPORTED_METRICS) - FLOW_METRICS),
                           ("flows", {"net_income", "revenue"}),
                           ("cash_flows", CASH_FLOW_METRICS)):
        entries = [p for p in positions if p["metric"] in metrics]
        usable = [p for p in entries if p["status"] == "ok"]
        reasons = ["missing or unusable inputs: " + ", ".join(
            p["metric"] for p in entries if p["status"] != "ok"
        )] if len(usable) != len(entries) else []
        for entry in usable[1:]:
            error = _alignment_error(usable[0], entry)
            if error:
                reasons.append(error)
            if any(usable[0][field] != entry[field] for field in METADATA_KEYS):
                reasons.append("currency/unit/scale differs")
        alignment[label] = {
            "status": "unknown" if reasons else "ok",
            "reason": "; ".join(sorted(set(reasons))) if reasons else
                "Recorded end/filing/unit metadata align; reporting scope not independently verified.",
        }
    return {"period": period, "source": source, "positions": positions,
            "alignment": alignment, "raw_metrics": rows, "warnings": sorted(set(warnings))}


def finmap(conn, entity_ids):
    if (not isinstance(entity_ids, (list, tuple)) or not 1 <= len(entity_ids) <= FINMAP_MAX_ISSUERS
            or any(type(eid) is not int or not 1 <= eid <= 2**63 - 1 for eid in entity_ids)
            or len(set(entity_ids)) != len(entity_ids)):
        raise ValueError("finmap requires 1..10 unique positive SQLite entity IDs")
    issuers = []
    for eid in entity_ids:
        entity = registry.get_entity(conn, eid)
        if entity is None:
            raise ValueError(f"entity {eid} not found")
        rows = [dict(row) for row in conn.execute(
            "SELECT k,v,period,source FROM metrics WHERE entity_id=? "
            "ORDER BY period,source,k,v LIMIT ?", (eid, FINMAP_MAX_METRICS + 1),
        )]
        if len(rows) > FINMAP_MAX_METRICS:
            raise ValueError(f"entity {eid} exceeds finmap's 1000 metric row limit")
        attributes = dict(conn.execute("SELECT k,v FROM attributes WHERE entity_id=?", (eid,)))
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            identity = (str(row["period"] or ""), str(row["source"] or ""))
            groups.setdefault(identity, []).append(row)
        issuers.append({
            "entity": dict(entity),
            "groups": [_position_group(p, s, groups[(p, s)], attributes) for p, s in sorted(groups)],
            "warnings": [] if rows else ["no stored metrics; financial positions unknown"],
        })
    return {
        "method": "stored_financial_positions_v1", "issuers": issuers,
        "limitations": [
            "Bounded stored observations only; no network requests or complete statements.",
            "Stock balances are measured at an instant; income and revenue are duration flows, not cash flows.",
            "Each period/source remains separate; no totals, cross-issuer aggregation or inferred transfers.",
            "Cash is part of assets; liabilities and equity are not additional assets.",
            "Only validated F01 envelopes yield values; unknowns retain raw evidence, never imputed zeroes.",
            "Alignment checks recorded metadata, not audit quality, reporting scope or peer comparability.",
            "Stock, accounting-flow and cash-flow alignment are separate; no common filing across categories is implied.",
            "Debt components are reported separately, never added to a total; total_debt remains unknown for SEC envelopes.",
            "Net cash flows include discontinued operations; no continuing-only fallback or derived quarter, free cash flow or cash reconciliation.",
            "Provenance syntax is validated, not independently verified; selected snapshots are not all-history data.",
        ],
    }


def analyze_sentiment(text: str) -> SentimentResult:
    result: SentimentResult = {
        "method": SENTIMENT_METHOD, "status": "unknown", "reason": "empty_text",
        "language": "unknown", "score": None, "direction": None,
        "confidence": None, "confidence_basis": "matched_mentions / word_tokens",
        "tokens": 0, "counts": {"positive": 0, "negative": 0, "neutral": 0},
        "words": {"positive": [], "negative": [], "neutral": []}, "negated": [],
        "limitations": list(SENTIMENT_LIMITATIONS),
    }
    if not isinstance(text, str) or not text.strip():
        return result
    if len(text) > MAX_TEXT_LEN:
        result["reason"] = "text_too_long"
        return result
    tokens = _TOKEN_RE.findall(text.lower().replace("’", "'"))
    words = [t for t in tokens if any(c.isalpha() for c in t)]
    result["tokens"] = len(words)
    known = POSITIVE_WORDS | NEGATIVE_WORDS | NEGATORS | ENGLISH_WORDS
    if (not words or any(not w.isascii() for w in words)
            or any(w in FOREIGN_MARKERS for w in words)
            or sum(w in known for w in words) / len(words) < 0.6):
        result["reason"] = "unsupported_or_uncertain_language"
        return result
    result["language"] = "en-assumed"
    mentions: dict[str, list[str]] = {"positive": [], "negative": [], "neutral": []}
    history: list[str] = []
    negated: list[str] = []
    for token in tokens:
        if token in {".", "!", "?", ";", ",", "\n", "but", "however", "yet"}:
            history = []
        if not any(c.isalpha() for c in token):
            if token.isalnum():
                history.append(token)
            continue
        direction = "neutral"
        if token in POSITIVE_WORDS:
            direction = "positive"
        elif token in NEGATIVE_WORDS:
            direction = "negative"
        if direction != "neutral" and sum(w in NEGATORS for w in history[-NEGATION_WINDOW:]) % 2:
            direction = "negative" if direction == "positive" else "positive"
            negated.append(token)
        mentions[direction].append(token)
        history.append(token)
    result["counts"] = {k: len(v) for k, v in mentions.items()}
    result["words"] = {k: list(dict.fromkeys(v))[:WORD_CAP] for k, v in mentions.items()}
    result["negated"] = list(dict.fromkeys(negated))[:WORD_CAP]
    positive, negative = len(mentions["positive"]), len(mentions["negative"])
    total = positive + negative
    if not total:
        result.update({"status": "insufficient", "reason": "no_lexical_overlap"})
        return result
    score = round((positive - negative) / total, 6)
    result.update({
        "status": "ok", "reason": None, "score": score,
        "direction": "positive" if score > 0 else "negative" if score < 0 else "neutral",
        "confidence": round(total / len(words), 6),
    })
    return result


def _validate_eid(eid):
    if not isinstance(eid, int) or isinstance(eid, bool) or not 0 < eid <= 2**63 - 1:
        raise ValueError("entity id must be a positive SQLite integer")


def record_sentiment(conn, eid: int, text: str, source: str, ref: str = "") -> RecordedSentiment:
    _validate_eid(eid)
    if registry.get_entity(conn, eid) is None:
        raise ValueError(f"unknown entity id {eid}")
    for name, value, limit in (("text", text, MAX_TEXT_LEN),
                               ("source", source, MAX_SOURCE_LEN), ("ref", ref, MAX_REF_LEN)):
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(f"{name} must be a string of at most {limit} characters")
        if name != "ref" and not value.strip():
            raise ValueError(f"{name} must be nonempty")
    analysis = analyze_sentiment(text)
    result: RecordedSentiment = {
        "recorded": False, "entity_id": eid, "kind": SIGNAL_KIND,
        "method": SENTIMENT_METHOD, "status": analysis["status"],
        "source": source.strip(), "ref": ref.strip(), "sentiment": analysis,
        "score": analysis["score"], "direction": analysis["direction"],
    }
    if analysis["status"] != "ok":
        result["reason"] = "insufficient evidence; no signal recorded"
        return result
    evidence = {
        "method": SENTIMENT_METHOD, "source": result["source"], "ref": result["ref"],
        "text": text, "analysis": analysis,
    }
    rationale = json.dumps(evidence, sort_keys=True, ensure_ascii=True, allow_nan=False)
    assert analysis["score"] is not None
    assert analysis["direction"] is not None
    registry.add_signal(conn, eid, SIGNAL_KIND, analysis["score"], analysis["direction"],
                        rationale, result["source"], result["ref"])
    result.update({"recorded": True, "rationale": rationale})
    return result


def _sentiment_summary(signals):
    rows = sorted((s for s in signals if s.get("kind") == SIGNAL_KIND),
                  key=lambda s: tuple(str(s.get(k) or "") for k in
                                      ("ts", "source", "ref", "rationale", "score")), reverse=True)
    valid = []
    directions = {"positive": 0, "negative": 0, "neutral": 0}
    for row in rows:
        score = _parse_number(row.get("score"))
        try:
            evidence = json.loads(row.get("rationale") or "")
        except (ValueError, TypeError, RecursionError):
            continue
        if (not isinstance(evidence, dict) or evidence.get("method") != SENTIMENT_METHOD
                or score is None or not -1 <= score <= 1):
            continue
        direction = "positive" if score > 0 else "negative" if score < 0 else "neutral"
        if row.get("direction") != direction:
            continue
        valid.append((row, score))
        directions[direction] += 1
    return {
        "method": "mean_of_recorded_english_lexicon_scores",
        "analysis_method": SENTIMENT_METHOD,
        "status": "ok" if valid else "insufficient",
        "recorded_signals": len(rows), "scored_signals": len(valid),
        "excluded_signals": len(rows) - len(valid),
        "mean_score": round(math.fsum(s for _, s in valid) / len(valid), 6) if valid else None,
        "direction_counts": directions,
        "sources": sorted({str(r.get("source") or "") for r, _ in valid}),
        "latest": dict(valid[0][0]) if valid else None,
        "evidence": [dict(r) for r, _ in valid],
        "limitations": [
            "Input-limited summary, not representative market sentiment; repeated or biased inputs remain biased.",
            "Only recognized method markers and finite bounded scores with consistent directions are summarized.",
            "The payload includes at most 100 recent signals of all kinds, not necessarily all sentiment records; "
            "timestamp ties at the cutoff have no guaranteed membership.",
            "Missing signals mean insufficient data, not a neutral outlook; scores are not predictive.",
        ],
    }


def analyze_entity(conn, eid: int) -> EntityReport:
    _validate_eid(eid)
    payload = registry.entity_payload(conn, eid)
    provenance: dict[str, Any] = {
        "engine": ENGINE_NAME, "engine_version": ENGINE_VERSION,
        "input_api": "registry.entity_payload", "entity_id": eid,
        "inputs": ["registry.entities", "registry.attributes", "registry.edges",
                   "registry.metrics", "registry.signals", "registry.filings"],
    }
    report: EntityReport = {
        "method": "deterministic_stored_data_analysis_v1", "found": bool(payload),
        "entity_id": eid, "entity": None, "fundamentals": None, "sentiment": None,
        "relationships": [], "warnings": [], "caveats": list(CAVEATS),
        "provenance": provenance,
    }
    if not payload:
        report["warnings"].append(f"no entity with id {eid} exists in the registry")
        return report
    report["entity"] = {k: v for k, v in payload.items()
                        if k not in {"metrics", "signals", "relationships"}}
    fundamentals = _fundamentals(payload.get("metrics", []), payload.get("attributes", {}))
    sentiment = _sentiment_summary(payload.get("signals", []))
    report["fundamentals"] = fundamentals
    report["sentiment"] = sentiment
    report["relationships"] = sorted(payload.get("relationships", []),
                                     key=lambda r: (r["dir"], r["rel"], r["other_id"]))
    provenance["metric_groups"] = [
        {k: g[k] for k in ("period", "source", "currency", "unit", "scale")}
        for g in fundamentals["groups"]
    ]
    provenance["metrics"] = sorted(payload.get("metrics", []),
                                   key=lambda r: tuple(str(r.get(k) or "") for k in ("period", "source", "k", "v")))
    provenance["sentiment_signals_considered"] = sentiment["recorded_signals"]
    return report
