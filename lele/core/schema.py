"""The registry DDL and the derived facts the runtime needs to check it.

The schema lives here as one string so a fresh database and a migrated one are
produced by the same statements, and so the expected table and index inventory
can be derived from the same source instead of being restated by hand and
drifting away from it.
"""

import re
import sqlite3

SCHEMA_VERSION = 19

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
  k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS entities(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL UNIQUE CHECK(length(trim(key)) > 0),
  kind TEXT NOT NULL CHECK(length(trim(kind)) > 0),
  name TEXT NOT NULL CHECK(length(trim(name)) > 0),
  country TEXT,
  website TEXT,
  lei TEXT,
  notes TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS attributes(
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  k TEXT,
  v TEXT,
  UNIQUE(entity_id, k));
CREATE TABLE IF NOT EXISTS edges(
  src_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  rel TEXT NOT NULL CHECK(length(trim(rel)) > 0),
  dst_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  source_url TEXT NOT NULL DEFAULT '',
  observed_at TEXT NOT NULL DEFAULT '',
  evidence TEXT NOT NULL DEFAULT '',
  first_seen_at TEXT NOT NULL DEFAULT '',
  last_seen_at TEXT NOT NULL DEFAULT '',
  seen_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  retracted_at TEXT NOT NULL DEFAULT '',
  UNIQUE(src_id, rel, dst_id));
CREATE TABLE IF NOT EXISTS edge_retry(
  src_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  rel TEXT NOT NULL CHECK(length(trim(rel)) > 0),
  last_attempt_at TEXT NOT NULL DEFAULT '',
  failures INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(src_id, rel));
CREATE TABLE IF NOT EXISTS metrics(
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  k TEXT,
  v TEXT,
  period TEXT,
  source TEXT,
  UNIQUE(entity_id, k, period, source));
CREATE TABLE IF NOT EXISTS signals(
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  ts TEXT DEFAULT CURRENT_TIMESTAMP,
  kind TEXT,
  score REAL,
  direction TEXT,
  rationale TEXT,
  source TEXT,
  ref TEXT);
CREATE INDEX IF NOT EXISTS idx_signals_ent ON signals(entity_id);
CREATE TABLE IF NOT EXISTS filings(
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  form TEXT,
  date TEXT,
  title TEXT,
  url TEXT,
  source TEXT,
  UNIQUE(entity_id, form, date, title, url));
CREATE TABLE IF NOT EXISTS ingest_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  query TEXT NOT NULL DEFAULT '',
  country TEXT NOT NULL DEFAULT '',
  indicator TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL DEFAULT '',
  finished_at TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'completed',
  fetched INTEGER NOT NULL DEFAULT 0,
  stored INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0,
  missing INTEGER NOT NULL DEFAULT 0,
  pages INTEGER NOT NULL DEFAULT 0,
  total INTEGER,
  truncated INTEGER NOT NULL DEFAULT 0,
  request_sha256 TEXT NOT NULL DEFAULT '',
  records_sha256 TEXT NOT NULL DEFAULT '',
  warnings TEXT NOT NULL DEFAULT '[]',
  coverage TEXT NOT NULL DEFAULT '',
  resumable INTEGER NOT NULL DEFAULT 0,
  next_offset INTEGER NOT NULL DEFAULT 0,
  next_page INTEGER NOT NULL DEFAULT 0,
  pages_detail TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS entity_aliases(
  alias_id INTEGER PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
  canonical_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  reason TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  CHECK(alias_id != canonical_id));
CREATE INDEX IF NOT EXISTS idx_aliases_canonical ON entity_aliases(canonical_id);
CREATE TABLE IF NOT EXISTS sanctions_listings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  listing_key TEXT NOT NULL,
  name TEXT NOT NULL,
  entity_type TEXT NOT NULL DEFAULT '',
  program TEXT NOT NULL DEFAULT '',
  country TEXT NOT NULL DEFAULT '',
  basis TEXT NOT NULL DEFAULT 'unknown',
  status TEXT NOT NULL DEFAULT 'active',
  published_at TEXT NOT NULL DEFAULT '',
  effective_at TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  retrieved_at TEXT NOT NULL DEFAULT '',
  evidence TEXT NOT NULL DEFAULT '',
  UNIQUE(source, listing_key));
CREATE TABLE IF NOT EXISTS sanctions_links(
  listing_id INTEGER NOT NULL REFERENCES sanctions_listings(id) ON DELETE CASCADE,
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  reason TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(listing_id, entity_id));
CREATE TABLE IF NOT EXISTS money_flows(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  src_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  dst_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  flow_type TEXT NOT NULL,
  amount TEXT NOT NULL,
  currency TEXT NOT NULL,
  occurred_at TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  evidence TEXT NOT NULL DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  CHECK(src_id != dst_id));
CREATE INDEX IF NOT EXISTS idx_flows_src ON money_flows(src_id);
CREATE INDEX IF NOT EXISTS idx_flows_dst ON money_flows(dst_id);
CREATE TABLE IF NOT EXISTS entity_links(
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  link_type TEXT NOT NULL,
  url TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  observed_at TEXT NOT NULL DEFAULT '',
  evidence TEXT NOT NULL DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(entity_id, link_type, url));
CREATE INDEX IF NOT EXISTS idx_links_entity ON entity_links(entity_id);
CREATE TABLE IF NOT EXISTS observations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL CHECK(length(trim(source)) > 0),
  external_id TEXT NOT NULL CHECK(length(trim(external_id)) > 0),
  kind TEXT NOT NULL CHECK(length(trim(kind)) > 0),
  description TEXT NOT NULL DEFAULT '',
  actor_key TEXT NOT NULL DEFAULT '',
  counterparty_key TEXT NOT NULL DEFAULT '',
  instrument_key TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  reason_basis TEXT NOT NULL DEFAULT 'unknown',
  amount TEXT,
  unit TEXT NOT NULL DEFAULT '',
  currency TEXT NOT NULL DEFAULT '',
  basis TEXT NOT NULL DEFAULT 'observed',
  occurred_at TEXT NOT NULL DEFAULT '',
  observed_at TEXT NOT NULL DEFAULT '',
  available_at TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  evidence TEXT NOT NULL DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(source, external_id));
CREATE INDEX IF NOT EXISTS idx_observations_instrument
  ON observations(instrument_key, observed_at);
CREATE INDEX IF NOT EXISTS idx_observations_actor ON observations(actor_key, observed_at);
CREATE TABLE IF NOT EXISTS event_store(
   id INTEGER PRIMARY KEY AUTOINCREMENT,
   source TEXT NOT NULL,
   event_type TEXT NOT NULL,
   event_subtype TEXT NOT NULL DEFAULT '',
   external_id TEXT NOT NULL DEFAULT '',
   title TEXT NOT NULL DEFAULT '',
   description TEXT NOT NULL DEFAULT '',
   actor_key TEXT NOT NULL DEFAULT '',
   actor_name TEXT NOT NULL DEFAULT '',
   counterparty_key TEXT NOT NULL DEFAULT '',
   counterparty_name TEXT NOT NULL DEFAULT '',
   instrument_key TEXT NOT NULL DEFAULT '',
   occurred_at TEXT NOT NULL DEFAULT '',
   observed_at TEXT NOT NULL DEFAULT '',
   severity TEXT NOT NULL DEFAULT 'unknown',
   confidence REAL NOT NULL DEFAULT 0.0,
   source_url TEXT NOT NULL DEFAULT '',
   evidence TEXT NOT NULL DEFAULT '',
   metadata TEXT NOT NULL DEFAULT '{}',
   created_at TEXT DEFAULT CURRENT_TIMESTAMP,
   UNIQUE(source, event_type, external_id));
CREATE INDEX IF NOT EXISTS idx_event_store_type ON event_store(event_type, occurred_at);
CREATE INDEX IF NOT EXISTS idx_event_store_actor ON event_store(actor_key, occurred_at);
CREATE INDEX IF NOT EXISTS idx_event_store_instrument ON event_store(instrument_key, occurred_at);
CREATE TABLE IF NOT EXISTS event_relationships(
   id INTEGER PRIMARY KEY AUTOINCREMENT,
   source_event_id INTEGER NOT NULL REFERENCES event_store(id) ON DELETE CASCADE,
   target_event_id INTEGER NOT NULL REFERENCES event_store(id) ON DELETE CASCADE,
   relationship TEXT NOT NULL CHECK(length(trim(relationship)) > 0),
   strength REAL NOT NULL DEFAULT 0.0,
   direction TEXT NOT NULL DEFAULT 'causal',
   source_url TEXT NOT NULL DEFAULT '',
   evidence TEXT NOT NULL DEFAULT '',
   created_at TEXT DEFAULT CURRENT_TIMESTAMP,
   UNIQUE(source_event_id, target_event_id, relationship));
CREATE INDEX IF NOT EXISTS idx_event_rel_source ON event_relationships(source_event_id);
CREATE INDEX IF NOT EXISTS idx_event_rel_target ON event_relationships(target_event_id);
CREATE TABLE IF NOT EXISTS price_anomalies(
   id INTEGER PRIMARY KEY AUTOINCREMENT,
   instrument_key TEXT NOT NULL DEFAULT '',
   anomaly_type TEXT NOT NULL,
   observed_at TEXT NOT NULL DEFAULT '',
   occurred_at TEXT NOT NULL DEFAULT '',
   severity TEXT NOT NULL DEFAULT 'unknown',
   score REAL NOT NULL DEFAULT 0.0,
   description TEXT NOT NULL DEFAULT '',
   evidence TEXT NOT NULL DEFAULT '',
   source_url TEXT NOT NULL DEFAULT '',
   metadata TEXT NOT NULL DEFAULT '{}',
   created_at TEXT DEFAULT CURRENT_TIMESTAMP,
   UNIQUE(instrument_key, anomaly_type, observed_at));
CREATE INDEX IF NOT EXISTS idx_price_anomaly_instrument ON price_anomalies(instrument_key, observed_at);
CREATE TABLE IF NOT EXISTS money_flow_attribution(
   id INTEGER PRIMARY KEY AUTOINCREMENT,
   event_id INTEGER NOT NULL REFERENCES event_store(id) ON DELETE CASCADE,
   flow_id INTEGER NOT NULL,
   src_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
   dst_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
   attribution_type TEXT NOT NULL DEFAULT 'direct',
   attribution_score REAL NOT NULL DEFAULT 0.0,
   rationale TEXT NOT NULL DEFAULT '',
   created_at TEXT DEFAULT CURRENT_TIMESTAMP,
   UNIQUE(event_id, flow_id, attribution_type));
CREATE INDEX IF NOT EXISTS idx_money_attrib_event ON money_flow_attribution(event_id);
CREATE INDEX IF NOT EXISTS idx_money_attrib_flow ON money_flow_attribution(flow_id);
CREATE TABLE IF NOT EXISTS volatility_instances(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_key TEXT NOT NULL,
    as_of TEXT NOT NULL,
    target_time TEXT NOT NULL,
    start_price TEXT NOT NULL,
    end_price TEXT NOT NULL,
    magnitude_percent TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('up', 'down', 'flat')),
    episode_type TEXT NOT NULL CHECK(episode_type IN ('pump', 'dump')),
    regime TEXT CHECK(regime IN ('bull', 'bear', 'range', 'unknown')),
    threshold_percent INTEGER NOT NULL,
    start_index INTEGER NOT NULL,
    end_index INTEGER NOT NULL,
    features_hash TEXT,
    observed_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    source_url TEXT,
    evidence TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_volatility_instrument ON volatility_instances(instrument_key, as_of);
CREATE INDEX IF NOT EXISTS idx_volatility_magnitude ON volatility_instances(magnitude_percent);
CREATE TABLE IF NOT EXISTS event_volatility_links(
    event_id INTEGER NOT NULL REFERENCES event_store(id) ON DELETE CASCADE,
    volatility_id INTEGER NOT NULL REFERENCES volatility_instances(id) ON DELETE CASCADE,
    confidence TEXT NOT NULL DEFAULT '0.0',
    time_delta_seconds INTEGER NOT NULL,
    evidence_basis TEXT NOT NULL DEFAULT 'temporal_proximity',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (event_id, volatility_id));
CREATE INDEX IF NOT EXISTS idx_event_vol_event ON event_volatility_links(event_id);
CREATE INDEX IF NOT EXISTS idx_event_vol_volatility ON event_volatility_links(volatility_id);
CREATE TABLE IF NOT EXISTS semantic_embeddings(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash TEXT UNIQUE NOT NULL,
    embedding_blob BLOB NOT NULL,
    source_kind TEXT NOT NULL,
    entity_key TEXT,
    as_of TEXT NOT NULL,
    content_text TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_embeddings_kind ON semantic_embeddings(source_kind, as_of);
CREATE INDEX IF NOT EXISTS idx_embeddings_entity ON semantic_embeddings(entity_key, as_of);
CREATE TABLE IF NOT EXISTS pattern_matches(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    current_volatility_id INTEGER NOT NULL REFERENCES volatility_instances(id) ON DELETE CASCADE,
    historical_volatility_id INTEGER NOT NULL REFERENCES volatility_instances(id) ON DELETE CASCADE,
    similarity_score TEXT NOT NULL DEFAULT '0.0',
    matched_features TEXT NOT NULL DEFAULT '{}',
    matched_kinds TEXT NOT NULL DEFAULT '[]',
    as_of TEXT NOT NULL,
    analysis_method TEXT NOT NULL DEFAULT 'cosine_similarity',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (current_volatility_id) REFERENCES volatility_instances(id),
    FOREIGN KEY (historical_volatility_id) REFERENCES volatility_instances(id));
CREATE INDEX IF NOT EXISTS idx_pattern_current ON pattern_matches(current_volatility_id);
CREATE INDEX IF NOT EXISTS idx_pattern_historical ON pattern_matches(historical_volatility_id);
CREATE TABLE IF NOT EXISTS instruments(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL CHECK(length(trim(symbol)) > 0),
    venue TEXT NOT NULL DEFAULT '',
    asset_class TEXT NOT NULL CHECK(length(trim(asset_class)) > 0),
    quote_currency TEXT NOT NULL DEFAULT '',
    contract_multiplier TEXT,
    expiry TEXT,
    adjustment_basis TEXT NOT NULL DEFAULT 'unknown'
        CHECK(adjustment_basis IN ('adjusted', 'unadjusted', 'unknown')),
    rights_basis TEXT NOT NULL DEFAULT 'unknown',
    rights_verified INTEGER NOT NULL DEFAULT 0 CHECK(rights_verified IN (0, 1)),
    first_seen_at TEXT NOT NULL DEFAULT '',
    last_seen_at TEXT NOT NULL DEFAULT '',
    notes TEXT,
    UNIQUE(symbol, venue, asset_class));
CREATE INDEX IF NOT EXISTS idx_instruments_entity ON instruments(entity_id);
CREATE INDEX IF NOT EXISTS idx_instruments_class ON instruments(asset_class);
CREATE TABLE IF NOT EXISTS price_bars(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_key TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL CHECK(interval_seconds >= 60),
    open_time TEXT NOT NULL,
    close_time TEXT NOT NULL,
    open TEXT NOT NULL,
    high TEXT NOT NULL,
    low TEXT NOT NULL,
    close TEXT NOT NULL,
    volume TEXT NOT NULL,
    quote_volume TEXT NOT NULL DEFAULT '',
    open_interest TEXT NOT NULL DEFAULT '',
    trades INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    source_url TEXT,
    retrieved_at TEXT NOT NULL,
    evidence TEXT,
    UNIQUE(instrument_key, interval_seconds, open_time));
CREATE INDEX IF NOT EXISTS idx_price_bars_series ON price_bars(instrument_key, interval_seconds, open_time);
CREATE TABLE IF NOT EXISTS volatility_estimates(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_key TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL CHECK(interval_seconds >= 60),
    estimator TEXT NOT NULL,
    window_bars INTEGER NOT NULL CHECK(window_bars >= 2),
    as_of TEXT NOT NULL,
    window_start TEXT NOT NULL,
    volatility_percent TEXT NOT NULL,
    annualized_percent TEXT NOT NULL,
    variance TEXT NOT NULL,
    basis TEXT NOT NULL DEFAULT 'per_bar',
    annualization TEXT NOT NULL DEFAULT '',
    ddof INTEGER NOT NULL DEFAULT 0,
    convention TEXT NOT NULL DEFAULT '{}',
    baseline_bars INTEGER NOT NULL DEFAULT 0,
    observed_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    source_url TEXT,
    evidence TEXT,
    UNIQUE(instrument_key, interval_seconds, estimator, window_bars, as_of));
CREATE INDEX IF NOT EXISTS idx_volatility_estimates_series
    ON volatility_estimates(instrument_key, interval_seconds, estimator, as_of);
CREATE TABLE IF NOT EXISTS context_measures(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_kind TEXT NOT NULL CHECK(length(trim(source_kind)) > 0),
    instrument_key TEXT NOT NULL DEFAULT '',
    measure TEXT NOT NULL CHECK(length(trim(measure)) > 0),
    baseline_observations INTEGER NOT NULL DEFAULT 0,
    value TEXT NOT NULL CHECK(length(trim(value)) > 0),
    unit TEXT NOT NULL DEFAULT '',
    prior_observed_at TEXT NOT NULL DEFAULT '',
    cadence_seconds INTEGER NOT NULL DEFAULT 0,
    baseline_mean TEXT NOT NULL DEFAULT '',
    baseline_deviation TEXT NOT NULL DEFAULT '',
    ddof INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    source_url TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    UNIQUE(source_kind, instrument_key, measure, baseline_observations, observed_at));
CREATE INDEX IF NOT EXISTS idx_context_measures_series
    ON context_measures(source_kind, instrument_key, measure, observed_at);
CREATE TABLE IF NOT EXISTS move_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_key TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL CHECK(interval_seconds >= 60),
    move_hours INTEGER NOT NULL,
    tier TEXT NOT NULL CHECK(tier LIKE 'p%'),
    threshold_percent TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('up', 'down')),
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    start_price TEXT NOT NULL,
    end_price TEXT NOT NULL,
    change_percent TEXT NOT NULL,
    terminal_bar_range_percent TEXT NOT NULL,
    baseline_mean_percent TEXT NOT NULL,
    baseline_std_percent TEXT NOT NULL,
    z_score TEXT NOT NULL,
    baseline_percentile TEXT,
    baseline_bars INTEGER NOT NULL,
    detected_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    source_url TEXT,
    evidence TEXT,
    UNIQUE(instrument_key, interval_seconds, move_hours, tier, start_time, end_time));
CREATE INDEX IF NOT EXISTS idx_move_events_series ON move_events(instrument_key, interval_seconds, move_hours, end_time);
CREATE INDEX IF NOT EXISTS idx_move_events_tier ON move_events(tier, direction, end_time);
CREATE TABLE IF NOT EXISTS move_causes(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    move_event_id INTEGER REFERENCES move_events(id) ON DELETE CASCADE,
    control_key TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL CHECK(role IN ('move', 'control')),
    category TEXT NOT NULL,
    article_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    headline TEXT NOT NULL,
    domain TEXT NOT NULL DEFAULT '',
    source_url TEXT,
    matched_terms TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    CHECK((role = 'move' AND move_event_id IS NOT NULL AND control_key = '')
       OR (role = 'control' AND move_event_id IS NULL AND control_key != '')),
    UNIQUE(move_event_id, control_key, role, article_id, category));
CREATE INDEX IF NOT EXISTS idx_move_causes_category ON move_causes(category, observed_at);
CREATE INDEX IF NOT EXISTS idx_move_causes_window ON move_causes(role, observed_at);
CREATE INDEX IF NOT EXISTS idx_move_causes_control ON move_causes(control_key);
CREATE TABLE IF NOT EXISTS cause_scans(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_key TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL,
    move_hours INTEGER NOT NULL,
    tier TEXT,
    direction TEXT,
    as_of TEXT NOT NULL,
    change_percent TEXT NOT NULL,
    historical_moves INTEGER NOT NULL DEFAULT 0,
    current_categories TEXT NOT NULL DEFAULT '[]',
    matched_categories TEXT NOT NULL DEFAULT '[]',
    news_articles INTEGER NOT NULL DEFAULT 0,
    score TEXT NOT NULL DEFAULT '0',
    observed_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    source_url TEXT,
    evidence TEXT,
    UNIQUE(instrument_key, interval_seconds, move_hours, as_of));
CREATE INDEX IF NOT EXISTS idx_cause_scans_series ON cause_scans(instrument_key, as_of);
CREATE TABLE IF NOT EXISTS prune_runs(
   id INTEGER PRIMARY KEY AUTOINCREMENT,
   cut TEXT NOT NULL,
   reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
   keep_bars INTEGER NOT NULL CHECK(keep_bars >= 1),
   instrument_key TEXT NOT NULL DEFAULT '',
   interval_seconds INTEGER NOT NULL DEFAULT 0,
   applied_at TEXT NOT NULL,
   deleted TEXT NOT NULL DEFAULT '{}',
   spans_cut TEXT NOT NULL DEFAULT '{}',
   series TEXT NOT NULL DEFAULT '[]');
CREATE INDEX IF NOT EXISTS idx_prune_runs_series ON prune_runs(instrument_key, cut);
"""


def statements() -> list[str]:
    """Every DDL statement, in order, with blanks removed."""
    return [part.strip() for part in _SCHEMA.split(";") if part.strip()]


def table_definition(name: str) -> str:
    """The CREATE statement for one table, used by the migration rebuilds."""
    prefix = f"CREATE TABLE IF NOT EXISTS {name}("
    for part in statements():
        if part.startswith(prefix):
            return part
    raise KeyError(name)


#: Every table and index the DDL above declares, read out of it rather than
#: restated. This was a hand-maintained pair of frozensets, which is how the
#: module docstring's claim -- that the inventory comes from the same source as
#: the DDL -- stopped being true: schema v18 added `context_measures` and
#: `idx_context_measures_series` to the DDL and to neither list, so a v18
#: database missing that table reported every table present, opened without
#: complaint, and failed later with "no such table" from the one command that
#: reads it. `tests/test_db_guarantees.py` now drops each declared object in turn
#: and requires the registry to name it.
_DECLARED = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?(TABLE|INDEX)\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


def declared_objects() -> tuple[frozenset, frozenset]:
    """The table and index names the DDL declares.

    A statement this pattern does not recognise is a statement that is executed
    but invisible to the inventory, which is the drift above in the making, so it
    raises rather than being skipped.
    """
    tables: set[str] = set()
    indexes: set[str] = set()
    unparsed: list[str] = []
    for statement in statements():
        match = _DECLARED.match(statement)
        if match is None:
            unparsed.append(statement.split("(")[0].strip())
            continue
        target = tables if match.group(1).upper() == "TABLE" else indexes
        target.add(match.group(2))
    if unparsed:
        raise ValueError("schema statements this module cannot inventory: "
                         + ", ".join(unparsed))
    return frozenset(tables), frozenset(indexes)


EXPECTED_TABLES, EXPECTED_INDEXES = declared_objects()


def inventory(conn: sqlite3.Connection) -> tuple[set, set]:
    """The tables and indexes actually present in `conn`."""
    rows = conn.execute("SELECT type, name FROM sqlite_master").fetchall()
    return ({name for kind, name in rows if kind == "table"},
            {name for kind, name in rows if kind == "index"})


def missing(conn: sqlite3.Connection) -> dict:
    """Expected objects absent from `conn`.

    A committed schema version is only trustworthy if the objects it promised
    are present, so a hand-edited or partially restored database is reported
    here instead of failing later with "no such table".
    """
    tables, indexes = inventory(conn)
    return {"tables": sorted(EXPECTED_TABLES - tables),
            "indexes": sorted(EXPECTED_INDEXES - indexes)}
