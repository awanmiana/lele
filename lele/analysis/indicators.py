"""Constructed indicator specifications and computation from wired observations.

Phase 4 (P09): Define each indicator spec (inputs, window, normalization, direction
convention, expected sign) BEFORE outcomes, freeze as a method, compute per-instrument
features, and score direction/tolerance through prospective with non-event controls.

Indicator families from wired sources:
- SEC (P06): insider net-buy/cluster, ownership-delta/activist flags, offering/buyback, private-capital
- Gov money (P07): awarded-money momentum, lobbying-award divergence, political money flow
- Real economy (P08): trade-shift, supply/logistics, labor-stress
"""
import json
from dataclasses import dataclass, asdict
from ..core import clock
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from collections.abc import Callable

IndicatorComputer = Callable[..., dict]

# ruff: noqa: E402
from ..core import registry
from ..analysis import events


@dataclass(frozen=True)
class IndicatorSpec:
    """Frozen indicator specification - defined BEFORE outcomes."""
    name: str
    version: str
    description: str
    observation_kinds: list[str]
    window_hours: int
    normalization: str
    direction_convention: str
    expected_sign: str
    inputs: dict
    created_at: str

    def to_dict(self):
        return asdict(self)


INDICATOR_SPECS = {
    "insider_net_buy_v1": IndicatorSpec(
        name="insider_net_buy_v1",
        version="1.0",
        description="Net insider buying pressure: sum of signed shares (acquired positive, disposed negative) over window. Positive = net buying pressure.",
        observation_kinds=["insider_trade"],
        window_hours=72,
        normalization="signed_share_sum",
        direction_convention="positive_means_upward_pressure",
        expected_sign="positive",
        inputs={"lookback_hours": 72, "min_transactions": 1},
        created_at="2026-09-20T00:00:00+00:00",
    ),
    "insider_cluster_v1": IndicatorSpec(
        name="insider_cluster_v1",
        version="1.0",
        description="Insider transaction cluster detection: count of unique reporting owners trading in same direction within window. >=3 = cluster signal.",
        observation_kinds=["insider_trade"],
        window_hours=72,
        normalization="unique_owner_count",
        direction_convention="positive_means_upward_pressure",
        expected_sign="positive",
        inputs={"lookback_hours": 72, "cluster_threshold": 3},
        created_at="2026-09-20T00:00:00+00:00",
    ),
    "filing_event_momentum_v1": IndicatorSpec(
        name="filing_event_momentum_v1",
        version="1.0",
        description="Material filing event momentum: count of 8-K/S-1/424B/DEF14A filings over window. More filings = more corporate action.",
        observation_kinds=["filing_event"],
        window_hours=72,
        normalization="event_count",
        direction_convention="positive_means_more_activity",
        expected_sign="positive",
        inputs={"lookback_hours": 72, "form_filter": ["8-K", "8-K/A", "S-1", "S-1/A", "424B*", "DEF 14A", "DEFA14A"]},
        created_at="2026-09-20T00:00:00+00:00",
    ),
    "macro_release_momentum_v1": IndicatorSpec(
        name="macro_release_momentum_v1",
        version="1.0",
        description="Treasury operating cash balance momentum: direction of change in TGA balance over window. Positive = increasing liquidity.",
        observation_kinds=["macro_release"],
        window_hours=168,
        normalization="balance_change_direction",
        direction_convention="positive_means_increasing_liquidity",
        expected_sign="positive",
        inputs={"lookback_hours": 168, "source_filter": "treasury-fiscal-data"},
        created_at="2026-09-20T00:00:00+00:00",
    ),
    "labor_stress_v1": IndicatorSpec(
        name="labor_stress_v1",
        version="1.0",
        description="Labor market stress indicator: rising unemployment rate (LNS14000000) or declining job openings (JTS00000000JOL) over window.",
        observation_kinds=["labor_metric"],
        window_hours=720,
        normalization="trend_direction",
        direction_convention="positive_means_worsening_labor",
        expected_sign="positive",
        inputs={"lookback_hours": 720, "series_filter": ["LNS14000000", "JTS00000000JOL"]},
        created_at="2026-09-20T00:00:00+00:00",
    ),
    "flight_activity_v1": IndicatorSpec(
        name="flight_activity_v1",
        version="1.0",
        description="Air traffic activity proxy: count of airborne flights over window. Higher = more economic activity.",
        observation_kinds=["flight_event"],
        window_hours=24,
        normalization="airborne_count",
        direction_convention="positive_means_more_activity",
        expected_sign="positive",
        inputs={"lookback_hours": 24, "filter_airborne_only": True},
        created_at="2026-09-20T00:00:00+00:00",
    ),
}


def _validate_spec(spec: IndicatorSpec):
    for kind in spec.observation_kinds:
        if kind not in events.KINDS:
            raise ValueError(f"Unknown observation kind: {kind}")
    if spec.window_hours < 1 or spec.window_hours > 8760:
        raise ValueError("window_hours must be 1..8760")
    if spec.direction_convention not in ("positive_means_upward_pressure", "positive_means_more_activity", "positive_means_increasing_liquidity", "positive_means_worsening_labor"):
        raise ValueError(f"Unknown direction convention: {spec.direction_convention}")
    if spec.expected_sign not in ("positive", "negative", "neutral"):
        raise ValueError(f"Unknown expected sign: {spec.expected_sign}")


def get_indicator_spec(name: str) -> IndicatorSpec:
    """Get a frozen indicator specification by name."""
    if name not in INDICATOR_SPECS:
        raise ValueError(f"Unknown indicator: {name}. Available: {list(INDICATOR_SPECS.keys())}")
    return INDICATOR_SPECS[name]


def list_indicator_specs() -> list[dict]:
    """List all frozen indicator specifications."""
    return [spec.to_dict() for spec in INDICATOR_SPECS.values()]


def _fetch_observations(conn, entity_id: int, kinds: list[str], window_hours: int):
    """Fetch observations for an entity within the lookback window."""
    cutoff = (clock.now() - timedelta(hours=window_hours)).isoformat()
    entity = registry.get_entity(conn, entity_id)
    if not entity:
        return []
    entity_key = entity["key"]
    placeholders = ",".join("?" for _ in kinds)
    query = f"""
        SELECT kind, actor_key, counterparty_key, instrument_key, action, reason,
               reason_basis, amount, unit, currency, basis, occurred_at, observed_at,
               available_at, source_url, evidence
        FROM observations
        WHERE instrument_key = ? AND kind IN ({placeholders}) AND occurred_at >= ?
        ORDER BY occurred_at
    """
    params = [entity_key, *kinds, cutoff]
    return conn.execute(query, params).fetchall()


def _parse_amount(row):
    """Parse amount from observation row."""
    amount = row["amount"]
    if amount is None or amount == "":
        return None
    try:
        return Decimal(str(amount))
    except (InvalidOperation, ValueError):
        return None


def compute_insider_net_buy(conn, entity_id: int, window_hours: int = 72, **kwargs) -> dict:
    """Compute insider net-buy indicator: sum of signed shares over window."""
    rows = _fetch_observations(conn, entity_id, ["insider_trade"], window_hours)
    if not rows:
        return {"value": 0, "transactions": 0, "net_shares": 0, "direction": "neutral"}

    net_shares = 0
    for row in rows:
        evidence = json.loads(row["evidence"]) if row["evidence"] else {}
        shares = evidence.get("shares")
        code = evidence.get("code", "")
        if shares is not None:
            try:
                val = int(shares)
                if code == "D":
                    val = -val
                net_shares += val
            except (ValueError, TypeError):
                continue

    direction = "positive" if net_shares > 0 else "negative" if net_shares < 0 else "neutral"
    return {
        "value": net_shares,
        "transactions": len(rows),
        "net_shares": net_shares,
        "direction": direction,
        "window_hours": window_hours,
    }


def compute_insider_cluster(conn, entity_id: int, window_hours: int = 72, threshold: int = 3, **kwargs) -> dict:
    """Compute insider cluster indicator: unique owners trading same direction."""
    rows = _fetch_observations(conn, entity_id, ["insider_trade"], window_hours)
    if not rows:
        return {"cluster": False, "unique_owners": 0, "direction": "neutral"}

    buy_owners = set()
    sell_owners = set()
    for row in rows:
        evidence = json.loads(row["evidence"]) if row["evidence"] else {}
        owner = row["actor_key"]
        code = evidence.get("code", "")
        if code == "A":
            buy_owners.add(owner)
        elif code == "D":
            sell_owners.add(owner)

    max_cluster = max(len(buy_owners), len(sell_owners))
    cluster_dir = "positive" if len(buy_owners) >= len(sell_owners) else "negative"
    return {
        "cluster": max_cluster >= threshold,
        "unique_owners": max_cluster,
        "buy_owners": len(buy_owners),
        "sell_owners": len(sell_owners),
        "direction": cluster_dir if max_cluster >= threshold else "neutral",
        "threshold": threshold,
        "window_hours": window_hours,
    }


def compute_filing_event_momentum(conn, entity_id: int, window_hours: int = 72, **kwargs) -> dict:
    """Compute filing event momentum: count of material filings."""
    rows = _fetch_observations(conn, entity_id, ["filing_event"], window_hours)
    if not rows:
        return {"count": 0, "direction": "neutral"}

    form_counts: dict[str, int] = {}
    for row in rows:
        evidence = json.loads(row["evidence"]) if row["evidence"] else {}
        form = evidence.get("form", "")
        form_counts[form] = form_counts.get(form, 0) + 1

    total = len(rows)
    return {
        "count": total,
        "by_form": form_counts,
        "direction": "positive" if total > 0 else "neutral",
        "window_hours": window_hours,
    }


def compute_macro_release_momentum(conn, window_hours: int = 168, **kwargs) -> dict:
    """Compute Treasury cash balance momentum."""
    cutoff = (clock.now() - timedelta(hours=window_hours)).isoformat()
    rows = conn.execute(
        """SELECT amount, occurred_at FROM observations
           WHERE kind = 'macro_release' AND source = 'treasury-fiscal-data'
           AND occurred_at >= ?
           ORDER BY occurred_at""",
        (cutoff,),
    ).fetchall()

    if len(rows) < 2:
        return {"direction": "neutral", "change": 0, "points": len(rows)}

    balances = []
    for row in rows:
        try:
            balances.append((row["occurred_at"], Decimal(str(row["amount"]))))
        except (InvalidOperation, ValueError):
            continue

    if len(balances) < 2:
        return {"direction": "neutral", "change": 0, "points": len(balances)}

    first = balances[0][1]
    last = balances[-1][1]
    change = last - first
    direction = "positive" if change > 0 else "negative" if change < 0 else "neutral"
    return {
        "direction": direction,
        "change": str(change),
        "first_balance": str(first),
        "last_balance": str(last),
        "points": len(balances),
        "window_hours": window_hours,
    }


def compute_labor_stress(conn, window_hours: int = 720, **kwargs) -> dict:
    """Compute labor stress from BLS series."""
    cutoff = (clock.now() - timedelta(hours=window_hours)).isoformat()
    rows = conn.execute(
        """SELECT evidence, occurred_at FROM observations
           WHERE kind = 'labor_metric' AND occurred_at >= ?
           ORDER BY occurred_at""",
        (cutoff,),
    ).fetchall()

    series_data: dict[str, list[tuple[str, float]]] = {}
    for row in rows:
        try:
            evidence = json.loads(row["evidence"]) if row["evidence"] else {}
            series_id = evidence.get("series_id", "")
            value = evidence.get("value", "")
            if series_id and value:
                if series_id not in series_data:
                    series_data[series_id] = []
                series_data[series_id].append((row["occurred_at"], float(value)))
        except (json.JSONDecodeError, ValueError):
            continue

    results = {}
    for series_id, points in series_data.items():
        if len(points) < 2:
            continue
        first_val = points[0][1]
        last_val = points[-1][1]
        change = last_val - first_val
        if series_id == "LNS14000000":
            direction = "positive" if change > 0 else "negative"
        elif series_id == "JTS00000000JOL":
            direction = "negative" if change > 0 else "positive"
        else:
            direction = "neutral"
        results[series_id] = {
            "direction": direction,
            "change": change,
            "first": first_val,
            "last": last_val,
            "points": len(points),
        }

    overall = "neutral"
    if results:
        pos = sum(1 for v in results.values() if v["direction"] == "positive")
        neg = sum(1 for v in results.values() if v["direction"] == "negative")
        if pos > neg:
            overall = "positive"
        elif neg > pos:
            overall = "negative"

    return {
        "direction": overall,
        "series": results,
        "window_hours": window_hours,
    }


def compute_flight_activity(conn, window_hours: int = 24, airborne_only: bool = True, **kwargs) -> dict:
    """Compute flight activity from OpenSky data."""
    cutoff = (clock.now() - timedelta(hours=window_hours)).isoformat()
    rows = conn.execute(
        """SELECT evidence, occurred_at FROM observations
           WHERE kind = 'flight_event' AND occurred_at >= ?
           ORDER BY occurred_at""",
        (cutoff,),
    ).fetchall()

    if not rows:
        return {"count": 0, "airborne": 0, "direction": "neutral"}

    total = 0
    airborne = 0
    for row in rows:
        total += 1
        try:
            evidence = json.loads(row["evidence"]) if row["evidence"] else {}
            if evidence.get("on_ground") is False:
                airborne += 1
        except (json.JSONDecodeError, KeyError):
            continue

    return {
        "count": total,
        "airborne": airborne,
        "direction": "positive" if airborne > 0 else "neutral",
        "window_hours": window_hours,
    }


INDICATOR_COMPUTERS: dict[str, IndicatorComputer] = {
    "insider_net_buy_v1": compute_insider_net_buy,
    "insider_cluster_v1": compute_insider_cluster,
    "filing_event_momentum_v1": compute_filing_event_momentum,
    "macro_release_momentum_v1": compute_macro_release_momentum,
    "labor_stress_v1": compute_labor_stress,
    "flight_activity_v1": compute_flight_activity,
}


def compute_indicator(conn, entity_id: int, indicator_name: str) -> dict:
    """Compute a frozen indicator for an entity."""
    spec = get_indicator_spec(indicator_name)
    computer = INDICATOR_COMPUTERS.get(indicator_name)
    if not computer:
        raise ValueError(f"No computer for indicator: {indicator_name}")

    if spec.observation_kinds == ["macro_release"] or spec.observation_kinds == ["labor_metric"] or spec.observation_kinds == ["flight_event"]:
        result = computer(conn, window_hours=spec.window_hours, **spec.inputs)
    else:
        result = computer(conn, entity_id, window_hours=spec.window_hours, **spec.inputs)

    return {
        "indicator": indicator_name,
        "spec_version": spec.version,
        "entity_id": entity_id,
        "computed_at": clock.now().isoformat(),
        "spec": spec.to_dict(),
        "result": result,
    }


def compute_all_indicators(conn, entity_id: int) -> list[dict]:
    """Compute all frozen indicators for an entity."""
    results = []
    for name in INDICATOR_SPECS:
        try:
            result = compute_indicator(conn, entity_id, name)
            results.append(result)
        except Exception as e:
            results.append({
                "indicator": name,
                "error": str(e),
                "entity_id": entity_id,
            })
    return results


def project_indicators_to_worldstate(conn, entity_id: int) -> dict:
    """Project computed indicators into world-state evidence format for prospective."""
    indicators = compute_all_indicators(conn, entity_id)
    evidence_rows = []

    for ind in indicators:
        if "error" in ind:
            continue
        result = ind["result"]
        direction = result.get("direction", "neutral")
        if direction == "neutral":
            continue

        evidence_rows.append({
            "source": "constructed_indicator",
            "external_id": f"{ind['indicator']}-{entity_id}-{int(clock.now().timestamp())}",
            "kind": "decision",
            "description": f"Constructed indicator {ind['indicator']}: {direction}",
            "actor": f"indicator:{ind['indicator']}",
            "counterparty": "",
            "instrument": "",
            "action": "indicator_signal",
            "reason": json.dumps(result),
            "reason_basis": "analyst_hypothesis",
            "amount": "1",
            "unit": "signal",
            "currency": "",
            "basis": "estimated",
            "occurred_at": clock.now().isoformat(),
            "observed_at": clock.now().isoformat(),
            "available_at": clock.now().isoformat(),
            "source_url": "constructed",
            "evidence": json.dumps({
                "indicator": ind["indicator"],
                "spec_version": ind["spec_version"],
                "result": result,
            }),
        })

    return {"observations": evidence_rows}
