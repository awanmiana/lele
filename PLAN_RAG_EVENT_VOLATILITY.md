# Price-Volatility-Event-Money-Flow System — Implementation Plan

## Requirement Summary

Build a system that:
1. **Finds all price volatility instances** (3%, 5%, 7%, 11%) across any valuable asset
2. **Stores all data in a relational vector RAG-style database**
3. **Detects events that led to each volatility instance**
4. **Tracks all money/value movements and the decisions/reasons behind them**
5. **Classifies sources into two types**:
   - **Structured**: Direct numerical data (e.g., "1 million transferred")
   - **Unstructured**: Intent/emotion/sentiment (where value is not defined)
6. **Maps events → money flow → price volatility** relationally
7. **Builds a price volatility anomaly indicator** that detects current patterns similar to past ones

## What Already Exists

### Already built and directly reusable:

| Component | File | What it does |
|-----------|------|--------------|
| Episode/ZigZag detection | `analysis/episodes.py` | Finds pump/dump legs at configurable thresholds (3%, 5%, 7%, 11%) with 72h precursor windows |
| Evidence taxonomy | `analysis/events.py` | KINDS frozenset with 29 kinds including `fund_flow`, `insider_trade`, `news_event`, `macro_release`, `social_sentiment`, `decision` |
| Observation bridge | `analysis/observations.py` | Normalized decision/flow observations with `reason`, `reason_basis`, `action`, `actor_key`, `counterparty_key` |
| Money flow import | `analysis/flows.py` | Imports structured JSON flows (src, dst, type, amount, currency, source_url, evidence) |
| World-state features | `analysis/worldstate.py` | `build_features()`, `weighted_direction()`, `observation_indicator()` — point-in-time feature vectors from evidence records |
| Prospective ledger | `analysis/prospective.py` | Pre-registered, hash-chained forecast ledger with frozen methods and scoring |
| Decision kind | `analysis/events.py` | `decision` kind with `action`, `reason`, `reason_basis` fields for tracking intent |
| GDELT news fetcher | `fetchers/news.py` | Keyword-based GDELT news event extraction as `news_event` observations |
| Sentiment recording | `cli/main.py` → `engine.record_sentiment()` | Records supplied-text sentiment with provenance |
| SQLite schema v12 | `core/registry.py` | `observations`, `money_flows`, `entities`, `edges`, `filings`, `ingest_runs`, `signals` tables |
| HTTP client | `fetchers/http.py` | Allowlisted HTTPS, bounded, paced, cached |
| CLI infrastructure | `cli/main.py` | All commands: `fetch-*`, `episodes`, `events`, `worldstate`, `prospective`, `sentiment`, `flows`, `observations` |

### What is NOT built (gaps):

| Gap | Details |
|-----|---------|
| No vector/RAG/semantic search | Zero vector DB, embeddings, or semantic retrieval anywhere |
| No political/geopolitical event kinds | KINDS has no `political_event`, `geopolitical_event`, `regulatory_action`, `market_shock` |
| No political/social sentiment sources | Only GDELT keyword-based news; no Twitter/X, Reddit, political news APIs |
| No event correlation engine | No cross-event pattern matching or causal inference |
| No entity types for political actors | KIND_DEFINITIONS covers banks/funds/companies/economies/instruments — no government bodies, political entities |
| No intent tracking beyond `decision` | `decision` kind exists but requires separate decision observations |
| No event → volatility mapping table | No table linking specific events to specific volatility instances |
| No semantic pattern matching | No text similarity, embeddings, or semantic search |

## Architecture Design

### Source Classification

**Type A — Structured sources** (direct numerical data):
- USAspending federal awards (`fetch-awards`)
- Senate LDA lobbying (`fetch-lobbying`)
- Treasury Fiscal Data (`fetch-treasury`)
- SEC Form 4 insider trades (`fetch-form4`)
- SEC 13F holdings (`fetch-13f`)
- Binance order flow, open interest, funding (`fetch-evidence`)
- CFTC COT positioning (`fetch-cot`)
- FINRA short interest (`fetch-short`)
- Money flow imports (`flows import`)
- All produce `fund_flow`, `insider_trade`, `holding`, `macro_release` observations with explicit amounts

**Type B — Unstructured sources** (intent/emotion/sentiment):
- GDELT news events (`fetch-news`) → `news_event`
- Social sentiment → `social_sentiment` (partially exists)
- Political/geopolitical events → NEW `political_event`, `geopolitical_event`
- Regulatory actions → NEW `regulatory_action`
- Intent/motive → NEW `intent`/`decision` observations
- All produce observations with text descriptions, no fixed numerical amount

### Database Design (Relational Vector RAG)

The existing SQLite schema (v12) will be extended with new tables:

1. **`volatility_instances`** — Stores each price volatility event found by ZigZag
   - `id`, `instrument_key`, `as_of`, `target_time`, `magnitude_percent`, `direction` (up/down), `start_index`, `end_index`, `episode_type` (pump/dump), `regime` (bull/bear/range)

2. **`event_instances`** — Stores detected events that may have caused volatility
   - `id`, `source`, `kind` (`political_event`, `geopolitical_event`, `regulatory_action`, `news_event`, etc.), `description`, `actor_key`, `observed_at`, `available_at`, `source_url`, `evidence`, `severity`

3. **`event_money_flows`** — Links event instances to money movements
   - `event_id`, `money_flow_id`, `relationship_type` (caused/contributed/coincided)

4. **`event_volatility_links`** — Maps events to volatility instances (the RAG relational core)
   - `event_id`, `volatility_id`, `confidence`, `time_delta_seconds`, `evidence_basis`

5. **`semantic_embeddings`** — Vector store for semantic pattern matching
   - `id`, `content_hash`, `embedding_vector`, `source_kind`, `entity_key`, `as_of`
   - (Note: vector storage will be in SQLite BLOB for simplicity; a separate vector DB can be plugged in later)

6. **`pattern_matches`** — Stores detected similarity between current and past volatility patterns
   - `id`, `current_volatility_id`, `historical_volatility_id`, `similarity_score`, `matched_features`, `as_of`

### Indicator Design

**Price Volatility Anomaly Indicator** (`volatility_anomaly_v1`):
- Inputs: current evidence records (all kinds), current price series
- Process:
  1. Run ZigZag to detect current volatility instances
  2. Build world-state features for the current window
  3. Compute semantic similarity between current features and historical volatility instances
  4. Score based on how similar the current pattern is to past volatility instances that preceded large moves
- Output: anomaly score + top matching historical patterns
- Direction convention: positive evidence = upward pressure

## Implementation Phases

### Phase A: New Observation Kinds and Sources

1. Add `political_event`, `geopolitical_event`, `regulatory_action`, `market_shock` to KINDS in `events.py`
2. Add `political_event`, `geopolitical_event`, `regulatory_action` to `MEASUREMENT_OPTIONAL_KINDS`
3. Add `political_event`, `geopolitical_event` to `EVENT_KINDS` in `worldstate.py`
4. Add new observation kinds to `DIRECTION_WEIGHTS` (political_event: 2, geopolitical_event: 2, regulatory_action: 1)
5. Create `fetchers/political.py` — fetches political/geopolitical events from free sources (Federal Register, UN Peacekeeping, government gazettes)
6. Create `fetchers/geopolitical.py` — fetches geopolitical events
7. Register in `SOURCES` constant and CLI

### Phase B: RAG Database Layer

1. Extend `registry._SCHEMA` with new tables (volatility_instances, event_instances, event_volatility_links, semantic_embeddings)
2. Add `registry.add_volatility_instance()`, `registry.add_event_instance()`, `registry.link_event_to_volatility()`, `registry.add_semantic_embedding()`
3. Add `registry.list_volatility_instances()`, `registry.search_events()`, `registry.find_pattern_matches()`
4. Implement `analysis/rag.py` — RAG retrieval engine using semantic similarity

### Phase C: Event Correlation and Attribution

1. Create `analysis/event_correlation.py` — links events to volatility instances
   - For each ZigZag leg, find events within a configurable time window (default 24h before)
   - Score attribution based on time proximity, kind relevance, actor overlap
2. Create `analysis/money_flow_attribution.py` — traces money movements to events
   - For each event, find money flows in the same time window involving related entities
   - Classify flow direction (inflow/outflow) relative to the event

### Phase D: Price Volatility Anomaly Indicator

1. Create `analysis/volatility_anomaly.py` — the anomaly detector
   - Step 1: Run ZigZag on current price series
   - Step 2: Build evidence features for current window
   - Step 3: Query RAG for similar historical patterns
   - Step 4: Score anomaly based on similarity to past volatility instances
   - Step 5: Return top matches and anomaly score
2. Register as frozen method in `prospective.py`
3. Add CLI command `volatility-analyze`
4. Add pre-registration support

### Phase E: Tests and Documentation

1. Unit tests for all new modules
2. Integration tests for the full pipeline
3. Update WORKLOG, DEVELOPMENT_PLAN, README

## Detailed Implementation Specifications

### New Observation Kinds (Phase A)

**`political_event`**: Political decisions, elections, policy changes, government actions
- Measurement: optional (text description, can carry a numerical score if quantified)
- Directional: yes (weight 2) — political events can push markets
- Actor: government entity key
- Reason basis: `stated_reason` or `documented_mandate`

**`geopolitical_event`**: Wars, sanctions, territorial disputes, international agreements
- Measurement: optional
- Directional: yes (weight 2)
- Actor: country/entity key
- Reason basis: `stated_reason` or `documented_mandate`

**`regulatory_action`**: Regulatory decisions, enforcement actions, rule changes
- Measurement: optional
- Directional: yes (weight 1)
- Actor: regulatory body key
- Reason basis: `stated_reason` or `documented_mandate`

**`market_shock`**: Sudden market events (flash crashes, circuit breakers, exchange outages)
- Measurement: optional
- Directional: yes (weight 2)
- Actor: exchange/market key
- Reason basis: `stated_reason`

### Source Fetchers (Phase A)

**`fetchers/political.py`** — Political events from free sources:
- Federal Register API (US federal regulations and notices)
- Government gazette feeds
- Election result feeds
- Each record becomes a `political_event` observation with:
  - `description`: event description
  - `actor_key`: government entity
  - `action`: policy/decision type
  - `reason`: stated rationale
  - `reason_basis`: `documented_mandate`
  - `source_url`: official publication URL
  - `observed_at`, `available_at`: publication times

**`fetchers/geopolitical.py`** — Geopolitical events from free sources:
- UN Peacekeeping Mission updates
- US State Department updates
- OpenSanctions (already available via sanctions infrastructure)
- Each record becomes a `geopolitical_event` observation

### RAG Database (Phase B)

All stored in the existing SQLite database (no external vector DB dependency for the initial implementation):

```sql
CREATE TABLE volatility_instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_key TEXT NOT NULL,
    as_of TEXT NOT NULL,
    target_time TEXT NOT NULL,
    start_price TEXT NOT NULL,
    end_price TEXT NOT NULL,
    magnitude_percent TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('up', 'down', 'flat')),
    episode_type TEXT NOT NULL CHECK(episode_type IN ('pump', 'dump')),
    regime TEXT CHECK(regime IN ('bull', 'bear', 'range')),
    threshold_percent INTEGER NOT NULL,
    start_index INTEGER NOT NULL,
    end_index INTEGER NOT NULL,
    features_hash TEXT,
    observed_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    source_url TEXT,
    evidence TEXT
);

CREATE TABLE event_instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('political_event', 'geopolitical_event', 'regulatory_action', 'news_event', 'macro_release', 'market_shock')),
    description TEXT NOT NULL,
    actor_key TEXT,
    action TEXT,
    reason TEXT,
    reason_basis TEXT,
    amount TEXT,
    currency TEXT,
    observed_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    source_url TEXT,
    evidence TEXT,
    severity INTEGER DEFAULT 0
);

CREATE TABLE event_volatility_links (
    event_id INTEGER NOT NULL REFERENCES event_instances(id),
    volatility_id INTEGER NOT NULL REFERENCES volatility_instances(id),
    confidence TEXT NOT NULL DEFAULT '0.0',
    time_delta_seconds INTEGER NOT NULL,
    evidence_basis TEXT NOT NULL DEFAULT 'temporal_proximity',
    PRIMARY KEY (event_id, volatility_id)
);

CREATE TABLE semantic_embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash TEXT UNIQUE NOT NULL,
    embedding_blob BLOB NOT NULL,
    source_kind TEXT NOT NULL,
    entity_key TEXT,
    as_of TEXT NOT NULL,
    content_text TEXT NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE TABLE pattern_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    current_volatility_id INTEGER NOT NULL REFERENCES volatility_instances(id),
    historical_volatility_id INTEGER NOT NULL REFERENCES volatility_instances(id),
    similarity_score TEXT NOT NULL DEFAULT '0.0',
    matched_features TEXT NOT NULL,
    matched_kinds TEXT NOT NULL,
    as_of TEXT NOT NULL,
    analysis_method TEXT NOT NULL DEFAULT 'cosine_similarity',
    FOREIGN KEY (current_volatility_id) REFERENCES volatility_instances(id),
    FOREIGN KEY (historical_volatility_id) REFERENCES volatility_instances(id)
);
```

### Event Correlation Engine (Phase C)

For each ZigZag leg (volatility instance):
1. Find all `event_instances` for the same `instrument_key` within ±24h of the leg start
2. Score each event's attribution:
   - Time proximity: `1 / (1 + time_delta_hours)`
   - Kind relevance: `DIRECTION_WEIGHTS.get(kind, 0) / max_weight`
   - Actor overlap: whether the event actor appears in the entity's registry
3. Store links in `event_volatility_links`

### Money Flow Attribution (Phase C)

For each event:
1. Find all `money_flows` involving the event's actor/entities within ±48h
2. Classify flow direction (inflow/outflow to the affected instrument's entities)
3. Store in `event_money_flows` with `relationship_type`

### Price Volatility Anomaly Indicator (Phase D)

**Algorithm**:
1. Input: current price series + current evidence records
2. Run ZigZag to detect current volatility instances
3. For each instance:
   a. Build world-state features (`worldstate.build_features()`)
   b. Query `pattern_matches` for similar historical instances
   c. Compute cosine similarity between current and historical feature vectors
   d. Score anomaly = `max_similarity * direction_agreement`
4. Output: list of anomaly scores with top matching historical patterns
5. If score > threshold → potential volatility anomaly detected

**Pre-registration**: `volatility_anomaly_v1` will be registered as a frozen method in the prospective ledger with `target_metric=direction`, `tolerance_bps=100`, `target_rate=0.98`.

## Verification Against Codebase

All of the following are already built and can be directly reused:
- ✅ ZigZag episode detection (`episodes.py`) with 3%, 5%, 7%, 11% thresholds
- ✅ Evidence taxonomy with extensible KINDS (`events.py`)
- ✅ Observation bridge with `reason`, `reason_basis`, `action`, `actor_key`, `counterparty_key` (`observations.py`)
- ✅ Money flow import and tracking (`flows.py`, `money_flows` table)
- ✅ World-state feature building (`worldstate.py`)
- ✅ Prospective ledger with pre-registration (`prospective.py`)
- ✅ SQLite schema v12 with extensible tables (`registry.py`)
- ✅ HTTP client with allowlist (`http.py`)
- ✅ GDELT news fetcher (`news.py`)
- ✅ Sentiment recording (`engine.record_sentiment()`)
- ✅ CLI infrastructure with command dispatch (`main.py`)
- ✅ `decision` kind for tracking intent (`events.py`)
- ✅ `social_sentiment` kind (`events.py`)
- ✅ Point-in-time evidence validation (`events._evidence()`)
- ✅ Appended-only hash chain for provenance (`prospective.py`)

## Source Strategy

**Free/Public sources for political/geopolitical data**:
1. **Federal Register API** (`https://www.federalregister.gov/api/v1/documents.json`) — US federal regulations and notices
2. **UN Peacekeeping API** — Mission updates
3. **OpenSanctions** — Already available via sanctions infrastructure
4. **GDELT** — Already implemented (can extend to event codes and tone)
5. **GovTrack** — Congressional legislation tracking
6. **OSINT sources** — Public government feeds

**Structured vs Unstructured classification**:
- Structured: Federal Register (numbers, dates, codes), USAspending, Senate LDA, Treasury, SEC
- Unstructured: GDELT headlines, social media sentiment, political commentary

## Cross-Cutting Constraints

1. **Never infer motives** — only record stated reasons from source documents
2. **Point-in-time availability** — evidence must be observed before the volatility window
3. **No fabricated data** — every source must be verified before integration
4. **Provenance preserved** — every record has `source_url`, `observed_at`, `available_at`, `evidence`
5. **No trading advice** — this is research infrastructure, not investment guidance
6. **Free vs licensed** — all sources must be free or properly licensed; never bypass paywalls

## CLI Commands to Add

1. `volatility-analyze ID PRICES_JSON --evidence EVIDENCE_JSON [--threshold-percent N] [--horizon-hours N]` — Detect price volatility instances and link to events
2. `event-correlate ID PRICES_JSON --evidence EVIDENCE_JSON` — Correlate events with volatility instances
3. `rag-search QUERY` — Semantic search through stored events and patterns
4. `pattern-match ID PRICES_JSON --evidence EVIDENCE_JSON` — Find similar historical patterns
5. `event-stats ID` — Show event statistics and money flow attribution
6. `volatility-report ID PRICES_JSON [--threshold-percent 3,5,7,11]` — Generate full volatility report with event links

## Test Plan

1. **Unit tests** for each new module
2. **Integration tests** for the full pipeline (volatility detection → event correlation → anomaly scoring)
3. **Offline tests** with fixture data
4. **Live smoke tests** with free public APIs
5. **Prospective pre-registration** tests
6. **Edge case** tests (no events, no money flows, no volatility, etc.)