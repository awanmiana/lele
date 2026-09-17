"""SQLite-backed persistent registry for financial institutions and entities.

Tables:
- entities: id, kind, name, country, website, lei, notes
- attributes: entity_id, k, v
- edges: src_id, rel, dst_id  (governed_by / supervises / shareholder_of ...)
- metrics: entity_id, k, v, period, source
- signals: entity_id, ts, kind, score, direction, rationale, source, ref
- filings: entity_id, form, date, title, url, source
"""
import os
import sqlite3
from contextlib import contextmanager

from ..core.constants import DB_PATH

_SCHEMA_VERSION = 3
RELATIONSHIPS = frozenset({
    "governed_by", "supervises", "supervised_by", "regulates", "regulated_by",
    "shareholder_of", "owned_by", "owns", "parent_of", "subsidiary_of",
    "branch_of", "affiliate_of", "member_of", "invests_in", "invested_in",
    "funded_by", "funds", "managed_by", "manages", "custodian_of",
    "audited_by", "audits", "partner_of", "counterparty_of", "lends_to",
    "borrows_from", "sanctioned_by", "issuer_of", "guarantees", "enforcement_by",
})

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
  UNIQUE(src_id, rel, dst_id));
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
"""

def _connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    except BaseException:
        conn.close()
        raise

def _initialize(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        has_meta = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        row = conn.execute(
            "SELECT v FROM meta WHERE k='schema_version'"
        ).fetchone() if has_meta else None
        version = 0
        if row is not None:
            try:
                version = int(row[0])
            except (TypeError, ValueError) as exc:
                raise ValueError("Invalid registry schema version") from exc
        if not 0 <= version <= _SCHEMA_VERSION:
            raise ValueError(f"Unsupported registry schema version: {version}")
        for statement in _SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(edges)")}
        for column in ("source_url", "observed_at", "evidence"):
            if column not in columns:
                conn.execute(f"ALTER TABLE edges ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        for table in ("attributes", "edges", "metrics", "signals", "filings"):
            if not conn.execute(f"PRAGMA foreign_key_list({table})").fetchall():
                definition = next(s.strip() for s in _SCHEMA.split(";")
                                  if s.strip().startswith(f"CREATE TABLE IF NOT EXISTS {table}("))
                temporary = f"_registry_migrate_{table}"
                conn.execute(definition.replace(f"{table}(", f"{temporary}(", 1))
                names = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
                fields = ", ".join('"' + name.replace('"', '""') + '"' for name in names)
                conn.execute(f"INSERT INTO {temporary}(rowid, {fields}) SELECT rowid, {fields} FROM {table}")
                conn.execute(f"DROP TABLE {table}")
                conn.execute(f"ALTER TABLE {temporary} RENAME TO {table}")
        for name, table, fields in (
            ("idx_entities_kind_name", "entities", "kind, name"),
            ("idx_entities_country", "entities", "country"),
            ("idx_entities_name", "entities", "name"),
            ("idx_edges_dst", "edges", "dst_id, rel"),
            ("idx_edges_rel", "edges", "rel"),
            ("idx_signals_ent", "signals", "entity_id"),
        ):
            conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({fields})")
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            raise sqlite3.IntegrityError("Registry contains orphaned records")
        conn.execute(
            "INSERT INTO meta(k, v) VALUES('schema_version', ?)"
            " ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (str(_SCHEMA_VERSION),),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    conn.execute("PRAGMA journal_mode=WAL")


def init_db(db_path: str | None = None) -> None:
    conn = _connect(db_path)
    try:
        _initialize(conn)
    finally:
        conn.close()

@contextmanager
def get_conn(db_path: str | None = None):
    conn = _connect(db_path)
    try:
        _initialize(conn)
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# entity helpers
# ---------------------------------------------------------------------------

def normalize_name(name: str) -> str:
    return " ".join((name or "").split()).strip().lower()

def upsert_entity(
    conn: sqlite3.Connection,
    kind: str,
    name: str,
    country: str | None = None,
    website: str | None = None,
    lei: str | None = None,
    notes: str | None = None,
    key: str | None = None,
) -> int:
    """Insert or update an entity; returns its internal id."""
    for field, value in (("kind", kind), ("name", name)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be nonempty text")
    if key is None:
        key = f"{kind}:{normalize_name(name)}"
    if not isinstance(key, str) or not key.strip():
        raise ValueError("key must be nonempty text")
    row = conn.execute(
        "INSERT INTO entities(key, kind, name, country, website, lei, notes)"
        " VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET"
        " name=excluded.name, country=COALESCE(excluded.country, entities.country),"
        " website=COALESCE(excluded.website, entities.website),"
        " lei=COALESCE(excluded.lei, entities.lei), notes=COALESCE(excluded.notes, entities.notes)"
        " WHERE entities.kind=excluded.kind RETURNING id",
        (key, kind, name, country, website, lei, notes),
    ).fetchone()
    if row is None:
        raise ValueError("Entity key already belongs to a different kind")
    return row[0]

def set_attr(conn, eid: int, k: str, v) -> None:
    if v is None:
        return
    conn.execute(
        "INSERT OR REPLACE INTO attributes(entity_id, k, v) VALUES(?,?,?)",
        (eid, k, str(v)),
    )

def add_edge(conn, src_id: int, rel: str, dst_id: int, source_url: str = "",
             observed_at: str = "", evidence: str = "") -> None:
    if not isinstance(rel, str) or not rel.strip():
        raise ValueError("rel must be nonempty text")
    if src_id and dst_id and src_id != dst_id:
        conn.execute(
            "INSERT INTO edges(src_id, rel, dst_id, source_url, observed_at, evidence)"
            " VALUES(?,?,?,?,?,?) ON CONFLICT(src_id, rel, dst_id) DO UPDATE SET"
            " source_url=COALESCE(NULLIF(excluded.source_url, ''), edges.source_url),"
            " observed_at=COALESCE(NULLIF(excluded.observed_at, ''), edges.observed_at),"
            " evidence=COALESCE(NULLIF(excluded.evidence, ''), edges.evidence)",
            (src_id, rel, dst_id, source_url, observed_at, evidence),
        )

def add_metric(conn, eid: int, k: str, v, period: str = "", source: str = "") -> None:
    if v is None:
        return
    conn.execute(
        "INSERT OR REPLACE INTO metrics(entity_id, k, v, period, source) VALUES(?,?,?,?,?)",
        (eid, k, str(v), period, source),
    )

def add_signal(
    conn,
    eid: int,
    kind: str,
    score: float,
    direction: str,
    rationale: str = "",
    source: str = "",
    ref: str = "",
) -> None:
    conn.execute(
        "INSERT INTO signals(entity_id, kind, score, direction, rationale, source, ref)"
        " VALUES(?,?,?,?,?,?,?)",
        (eid, kind, float(score), direction, rationale, source, ref),
    )

def add_filing(
    conn, eid: int, form: str, date: str, title: str, url: str, source: str
) -> None:
    if not title and not url:
        return
    conn.execute(
        "INSERT OR IGNORE INTO filings(entity_id, form, date, title, url, source)"
        " VALUES(?,?,?,?,?,?)",
        (eid, form, date, title, url, source),
    )

# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------

def find_entities(
    conn,
    q: str | None = None,
    kind: str | None = None,
    country: str | None = None,
    with_attribute: str | None = None,
    limit: int = 50,
):
    sql = (
        "SELECT e.* FROM entities e"
        " LEFT JOIN attributes a ON a.entity_id = e.id AND a.k = 'iso_jurisdiction'"
    )
    clauses: list[str] = []
    params: list[str | int] = []
    if q:
        clauses.append("(e.name LIKE ? OR e.key LIKE ? OR e.lei LIKE ? OR e.notes LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like, like]
    if kind:
        clauses.append("e.kind = ?")
        params.append(kind)
    if country:
        clauses.append("(e.country LIKE ? OR a.v LIKE ?)")
        params += [f"%{country}%", f"%{country}%"]
    if with_attribute:
        clauses.append(
            "EXISTS (SELECT 1 FROM attributes af WHERE af.entity_id = e.id AND af.k = ? AND af.v != '')"
        )
        params.append(with_attribute)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY e.name LIMIT ?"
    params.append(int(limit))
    return conn.execute(sql, params).fetchall()

def get_entity(conn, eid: int):
    return conn.execute("SELECT * FROM entities WHERE id=?", (eid,)).fetchone()

def entity_payload(conn, eid: int) -> dict:
    e = get_entity(conn, eid)
    if not e:
        return {}
    attrs = {
        r["k"]: r["v"]
        for r in conn.execute(
            "SELECT k, v FROM attributes WHERE entity_id=?", (eid,)
        )
    }
    rels = []
    for r in conn.execute(
        "SELECT 'out' AS dir, rel, dst_id AS other, source_url, observed_at, evidence"
        " FROM edges WHERE src_id=? UNION ALL"
        " SELECT 'in' AS dir, rel, src_id AS other, source_url, observed_at, evidence"
        " FROM edges WHERE dst_id=?",
        (eid, eid),
    ):
        other = get_entity(conn, r["other"])
        rels.append(
            {"dir": r["dir"], "rel": r["rel"], "other_id": r["other"],
             "other_name": other["name"] if other else f"#{r['other']}",
             "other_kind": other["kind"] if other else "",
             "source_url": r["source_url"], "observed_at": r["observed_at"],
             "evidence": r["evidence"]}
        )
    metrics = [dict(r) for r in conn.execute(
        "SELECT k, v, period, source FROM metrics WHERE entity_id=? ORDER BY k, period", (eid,))]
    sigs = [dict(r) for r in conn.execute(
        "SELECT ts, kind, score, direction, rationale, source, ref FROM signals"
        " WHERE entity_id=? ORDER BY ts DESC LIMIT 100", (eid,))]
    fills = [dict(r) for r in conn.execute(
        "SELECT form, date, title, url, source FROM filings"
        " WHERE entity_id=? ORDER BY date DESC LIMIT 50", (eid,))]
    d = dict(e)
    d["attributes"] = attrs
    d["relationships"] = rels
    d["metrics"] = metrics
    d["signals"] = sigs
    d["filings"] = fills
    return d

def counts(conn) -> dict:
    out = {}
    for r in conn.execute(
        "SELECT kind, COUNT(*) AS c FROM entities GROUP BY kind ORDER BY kind"
    ):
        out[r["kind"]] = r["c"]
    out["_edges"] = conn.execute("SELECT COUNT(*) AS c FROM edges").fetchone()["c"]
    out["_signals"] = conn.execute("SELECT COUNT(*) AS c FROM signals").fetchone()["c"]
    out["_filings"] = conn.execute("SELECT COUNT(*) AS c FROM filings").fetchone()["c"]
    out["_metrics"] = conn.execute("SELECT COUNT(*) AS c FROM metrics").fetchone()["c"]
    return out

def list_countries(conn) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT COALESCE(country,'?') AS c FROM entities ORDER BY c"
    ).fetchall()
    return [r["c"] for r in rows]

def stats(conn) -> dict:
    c = counts(conn)
    return {
        "entities": sum(v for k, v in c.items() if not k.startswith("_")),
        "edges": c.get("_edges", 0),
        "signals": c.get("_signals", 0),
        "filings": c.get("_filings", 0),
        "metrics": c.get("_metrics", 0),
        "by_kind": {k: v for k, v in c.items() if not k.startswith("_")},
    }
