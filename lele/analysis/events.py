import csv
import hashlib
import io
import json
from datetime import datetime, timedelta, UTC
from decimal import Context, Decimal, InvalidOperation, localcontext

from ..core import importer, registry
from . import projection

KINDS = frozenset({
    "fund_flow", "exchange_transfer", "liquidation", "liquidation_level",
    "market_context", "capitulation_indicator", "decision",
    "onchain_transfer", "etf_flow", "stablecoin_mint_burn",
    "open_interest", "funding_rate", "short_interest", "margin_debt",
    "options_positioning", "order_book_imbalance",
    "long_short_account_ratio", "top_long_short_position_ratio",
    "cot_positioning", "cot_open_interest",
    "trade_print", "block_trade", "order_flow", "insider_trade", "holding",
    "news_event", "macro_release", "calendar_event", "filing_event",
    "social_sentiment", "stablecoin_supply", "market_activity",
    "political_event", "geopolitical_event",
    "regulatory_action", "market_shock",
    "trade_flow", "energy_flow", "labor_metric", "flight_event",
    "investment_adviser",
})
ROUTED_KINDS = frozenset({
    "fund_flow", "exchange_transfer", "liquidation", "onchain_transfer",
    "etf_flow", "stablecoin_mint_burn",
})
MEASUREMENT_OPTIONAL_KINDS = frozenset({
    "decision", "news_event", "macro_release", "calendar_event", "filing_event", "market_context",
    "political_event", "geopolitical_event", "regulatory_action", "market_shock",
})
FIELDS = {"id", "entity_key", "observed_at", "available_at", "kind", "source_url",
          "description", "platform", "industry", "location", "measurement", "mapping"}


def _time(value):
    importer._text(value, "evidence timestamp", 64, True)
    if not projection._TIMESTAMP_RE.fullmatch(value):
        raise ValueError("evidence requires aware whole-second timestamps")
    return datetime.fromisoformat(value).astimezone(UTC)


MAPPING_FIELDS = {"from", "to", "actor", "action", "reason", "reason_basis", "related_ids", "source_locator"}
CSV_FIELDS = (FIELDS - {"measurement", "mapping"} | MAPPING_FIELDS
              | {"value", "unit", "currency", "basis"})
REASON_BASES = ("unknown", "stated_reason", "documented_mandate", "analyst_hypothesis")


def _mapping(row):
    mapping = row.get("mapping")
    if mapping is None:
        return
    projection._exact_object(mapping, MAPPING_FIELDS, "mapping")
    for field in MAPPING_FIELDS - {"related_ids"}:
        if mapping[field] is not None:
            importer._text(mapping[field], "mapping." + field, 1024, True)
    related = importer._array(mapping["related_ids"], 50, "related_ids")
    for reference in related:
        importer._text(reference, "related ID", 1024, True)
    if len(set(related)) != len(related) or row["id"] in related:
        raise ValueError("duplicate or self-referencing related IDs")
    if mapping["reason_basis"] not in (None, *REASON_BASES):
        raise ValueError("invalid reason basis")
    if ((mapping["reason_basis"] == "unknown" and mapping["reason"] is not None)
            or (mapping["reason_basis"] in REASON_BASES[1:] and mapping["reason"] is None)):
        raise ValueError("reason text must match the declared reason basis")
    if row["kind"] == "decision":
        if mapping["from"] is not None or mapping["to"] is not None:
            raise ValueError("decisions link to transfers by related_ids, not implied routes")
        if mapping["action"] is None:
            raise ValueError("decision requires an action")
        if mapping["reason_basis"] in ("stated_reason", "documented_mandate") and (
                not mapping["actor"] or not mapping["source_locator"]):
            raise ValueError("attributed reasons require actor and source locator")
    elif (any(mapping[f] is not None for f in ("actor", "action", "reason"))
            or mapping["reason_basis"] not in (None, "unknown")):
        raise ValueError("decision claims require a separate decision observation")
    if any(mapping[f] is not None for f in ("from", "to")):
        if row["kind"] not in ROUTED_KINDS:
            raise ValueError("routes require a flow, transfer or liquidation observation")
        if mapping["from"] is None or mapping["to"] is None:
            raise ValueError("directed routes require both an origin and a destination")
        measurement = row["measurement"] if isinstance(row["measurement"], dict) else None
        if measurement is None:
            raise ValueError("directed route amount must be positive; do not infer direction from signed net flows")
        try:
            amount = Decimal(measurement["value"])
        except (InvalidOperation, TypeError):
            amount = None
        if amount is None or amount <= 0:
            raise ValueError("directed route amount must be positive; do not infer direction from signed net flows")
        if not measurement.get("currency"):
            raise ValueError("directed routes require currency or asset denomination")


def _csv_payload(text):
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames) or set(reader.fieldnames) != CSV_FIELDS:
        raise ValueError("CSV requires the documented unique evidence/mapping columns")
    rows: list[dict] = []
    for line in reader:
        if len(rows) >= 1000 or None in line or any(v is None for v in line.values()):
            raise ValueError("CSV row limit or column count mismatch")
        row: dict = {f: line[f] for f in FIELDS - {"measurement", "mapping"}}
        for field in ("platform", "industry", "location"):
            row[field] = line[field] or None
        row["measurement"] = None if row["kind"] == "decision" and not any(
            line[f] for f in ("value", "unit", "currency", "basis")) else {
                f: line[f] or None for f in ("value", "unit", "currency", "basis")}
        mapping = {f: line[f] or None for f in MAPPING_FIELDS - {"related_ids"}}
        mapping["related_ids"] = line["related_ids"].split("|") if line["related_ids"] else []
        row["mapping"] = None if (not any(mapping.values()) or all(v is None for v in mapping.values())
                                  ) and not mapping["related_ids"] else mapping
        rows.append(row)
    return {"observations": rows}


def _evidence(path, entity_key):
    if path is None:
        return [], None
    with open(path, "rb") as stream:
        raw = stream.read(projection.MAX_FILE_BYTES + 1)
    if len(raw) > projection.MAX_FILE_BYTES:
        raise ValueError("evidence exceeds 2 MiB")
    text = raw.decode("utf-8-sig")
    payload = _csv_payload(text) if str(path).lower().endswith(".csv") else json.loads(
        text, object_pairs_hook=importer._pairs, parse_constant=importer._constant)
    projection._exact_object(payload, {"observations"}, "evidence")
    rows = importer._array(payload["observations"], 1000, "evidence observations")
    out, ids = [], set()
    for row in rows:
        if (not isinstance(row, dict) or row.keys() - FIELDS
                or not FIELDS - {"measurement", "mapping"} <= row.keys()):
            raise ValueError("evidence observation requires exactly the documented fields")
        row["measurement"], row["mapping"] = row.get("measurement"), row.get("mapping")
        for field in FIELDS - {"measurement", "mapping", "platform", "industry", "location"}:
            importer._text(row[field], field, 4096 if field == "source_url" else 1024, True)
        for field in ("platform", "industry", "location"):
            if row[field] is not None:
                importer._text(row[field], field, 256, True)
        if row["id"] in ids or row["entity_key"] != entity_key:
            raise ValueError("duplicate evidence ID or mismatched instrument")
        ids.add(row["id"])
        _mapping(row)
        if row["kind"] not in KINDS:
            raise ValueError("unsupported evidence kind")
        importer._provenance(row["source_url"], "evidence source")
        observed, available = _time(row["observed_at"]), _time(row["available_at"])
        if available < observed:
            raise ValueError("availability cannot precede observation")
        measurement = row["measurement"]
        if row["kind"] in MEASUREMENT_OPTIONAL_KINDS and measurement is None:
            out.append((observed, available, row))
            continue
        projection._exact_object(measurement, {"value", "unit", "currency", "basis"}, "measurement")
        importer._text(measurement["unit"], "measurement unit", 128, True)
        if measurement["currency"] is not None:
            importer._text(measurement["currency"], "currency", 128, True)
        value = measurement["value"]
        importer._text(value, "measurement value", 64, True)
        unsigned = value[1:] if value.startswith("-") else value
        if not projection._PRICE_RE.fullmatch(unsigned):
            raise ValueError("invalid measurement decimal")
        try:
            number = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("invalid measurement decimal") from exc
        if not number.is_finite() or not -12 <= number.adjusted() <= 12:
            raise ValueError("measurement outside numeric bounds")
        if measurement["basis"] not in ("observed", "estimated"):
            raise ValueError("measurement basis must be observed or estimated")
        if row["kind"] == "liquidation_level" and (measurement["basis"] != "estimated" or number <= 0):
            raise ValueError("liquidation levels require a positive estimate")
        out.append((observed, available, row))
    known = {row["id"] for _, _, row in out}
    for _, _, row in out:
        mapping = row.get("mapping")
        if mapping and any(reference not in known for reference in mapping["related_ids"]):
            raise ValueError("related IDs must reference evidence in the same file")
    return sorted(out, key=lambda r: (r[0], r[2]["id"])), hashlib.sha256(raw).hexdigest()


def study(conn, entity_id, price_path, evidence_path=None, move_hours=24,
          thresholds=(3, 5, 7, 11), limit=100):
    if type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1:
        raise ValueError("invalid entity ID")
    if type(move_hours) is not int or not 1 <= move_hours <= 72:
        raise ValueError("move_hours must be 1..72")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("event limit must be 1..100")
    if (not isinstance(thresholds, (tuple, list)) or not 1 <= len(thresholds) <= 20
            or any(type(t) is not int or not 1 <= t <= 1000 for t in thresholds)
            or len(set(thresholds)) != len(thresholds)):
        raise ValueError("thresholds require 1..20 distinct integer percentages, 1..1000")
    thresholds = sorted(thresholds)
    instrument, _, points, _, raw = projection._load(price_path)
    entity = registry.get_entity(conn, entity_id)
    if entity is None or entity["key"] != instrument["entity_key"]:
        raise ValueError("missing entity or instrument key mismatch")
    evidence, evidence_hash = _evidence(evidence_path, instrument["entity_key"])
    prices = dict(points)
    matches, eligible, missing, warmup = [], 0, 0, 0
    with localcontext(Context(prec=128)):
        for end, end_price in points:
            try:
                start = end - timedelta(hours=move_hours)
            except OverflowError:
                warmup += 1
                continue
            if start < points[0][0]:
                warmup += 1
                continue
            if start not in prices:
                missing += 1
                continue
            eligible += 1
            initial = prices[start]
            change = (end_price - initial) * 100
            exceeded = [t for t in thresholds if abs(change) > t * initial]
            if exceeded:
                matches.append({"start": start.isoformat(), "end": end.isoformat(),
                                "start_price": str(initial), "end_price": str(end_price),
                                "change_percent": str(change / initial),
                                "direction": "up" if change > 0 else "down",
                                "exceeded_thresholds": exceeded})
    events = matches[-limit:]
    selected, late_pairs = {}, set()
    for event in events:
        start = datetime.fromisoformat(event["start"])
        windows = []
        for hours in (24, 48, 72):
            try:
                lower = start - timedelta(hours=hours)
            except OverflowError:
                lower = datetime.min.replace(tzinfo=UTC)
            included = []
            for observed, available, record in evidence:
                if lower <= observed < start:
                    if available >= start:
                        late_pairs.add((event["end"], record["id"]))
                    else:
                        included.append(record)
                        selected[record["id"]] = record
            windows.append({
                "hours": hours, "start": lower.isoformat(), "end_exclusive": start.isoformat(),
                "observation_ids": [r["id"] for r in included],
                "evidence_coverage": "unknown", "flow_attribution": "not_established",
                "liquidation_levels_status": "estimates_only" if any(
                    r["kind"] == "liquidation_level" for r in included) else "unknown",
                "capitulation_status": "not_established",
            })
        event["precursor_windows"] = windows
    return {
        "method": "historical_event_windows_v1", "entity": dict(entity), "instrument": instrument,
        "move_hours": move_hours, "thresholds_percent": thresholds,
        "comparison": "strictly greater than absolute percentage threshold",
        "coverage": {"points": len(points), "start": points[0][0].isoformat(),
                     "end": points[-1][0].isoformat(), "eligible_intervals": eligible,
                     "missing_start_points": missing, "warmup_points": warmup,
                     "all_history": False, "all_assets": False},
        "total_matches": len(matches), "truncated": len(matches) > limit, "events": events,
        "evidence_coverage": "unknown", "evidence": selected,
        "evidence_records_supplied": len(evidence),
        "late_publication_event_pairs": len(late_pairs),
        "provenance": {"prices_sha256": hashlib.sha256(raw).hexdigest(),
                       "evidence_sha256": evidence_hash, "truth_verified": False},
        "limitations": [
            "Only supplied instrument history is scanned, not every historical market event.",
            "Rolling fixed-horizon endpoint returns are not intraperiod extremes or rally/crash onset detection.",
            "Exact endpoints are required; interior gaps are not filled or interpreted as no trading.",
            "Prices need reviewed split, distribution and futures-roll treatment; unadjusted jumps may be artifacts.",
            "Precursor windows are cumulative and strictly before move start; later-publication evidence is excluded.",
            "Missing evidence does not mean no flows, liquidations or market activity.",
            "Gross volume, market-cap changes and exchange transfers do not establish net investment or who caused a move.",
            "No flows are summed across units or platforms; reported observations are not verified causal attribution.",
            "Liquidation levels are supplied estimates, not known future orders; actual liquidation records are separate.",
            "Capitulation indicators remain observations, not a confirmed bottom or reversal signal.",
            "Overlapping event windows are not independent; event-only patterns need non-event controls and held-out testing.",
            "No projection is changed and no predictive accuracy is established by this event study.",
        ],
    }
