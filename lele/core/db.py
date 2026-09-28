"""Opening, migrating, inspecting and backing up the registry database.

Three properties matter here and all three used to be wrong.

1. *A read must not write.* Every command used to replay the whole schema in a
   `BEGIN IMMEDIATE` transaction on open, so `lele list` created a database,
   took a write lock and grew the file. An up-to-date database is now recognised
   from its committed version and opened without a single write, and read-only
   commands open the file through SQLite's `mode=ro` so a read-only filesystem
   or a locked registry still answers questions.

2. *One owner per transaction.* `get_conn` is the only place that commits or
   rolls back. Analysis and fetchers write rows and return; they never commit,
   so a failure anywhere leaves the database exactly as it was instead of
   leaving half a run behind.

3. *A migration must be explainable.* Version detection is a number, and a
   committed version means the whole migration committed, so the fast path is
   sound. The slow path checks the object inventory, refuses to run a rebuild
   that would fail on orphaned rows, and says exactly which rows are at fault
   instead of dying with an opaque IntegrityError that bricks every command.
"""
import hashlib
import os
import platform
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .constants import (APP_NAME, APP_VERSION, DB_FILENAME, DB_PATH, DB_PATH_SOURCE,
                        HOME_DIR, LEGACY_HOME_DIR, PREVIOUS_APP_NAME)
from .schema import (EXPECTED_INDEXES, EXPECTED_TABLES, SCHEMA_VERSION, inventory, missing,
                     statements, table_definition)

__all__ = [
    "SCHEMA_VERSION",
    "RegistryError",
    "RegistryMigrationError",
    "RegistryMissing",
    "backup_database",
    "connect",
    "get_conn",
    "get_read_conn",
    "health_check",
    "init_db",
    "initialize",
    "inventory",
    "object_inventory",
    "read_connect",
    "schema_version",
]

BUSY_TIMEOUT_MS = 30000


class Connection(sqlite3.Connection):
    """A connection that can carry its own path and read-only flag.

    `sqlite3.Connection` has no instance dictionary, so the session facts that
    reporting needs have to live on the type.
    """

    lele_path: str = ""
    lele_readonly: bool = False
    lele_readonly_reason: str = ""


class RegistryError(ValueError):
    """A registry problem the operator has to resolve, reported as such.

    A `ValueError` because that is what a caller already catches, and because
    "this database is not usable" is a statement about the value on disk.
    """


class RegistryMissing(RegistryError):
    """The registry file does not exist and the command does not create it."""


class RegistryMigrationError(RegistryError):
    """The stored schema cannot be brought to the current version."""


def _require_runtime() -> None:
    if sys.version_info < (3, 11):
        raise RuntimeError("lele requires Python 3.11 or newer")
    if sqlite3.sqlite_version_info < (3, 35, 0):
        raise RuntimeError(f"lele requires SQLite 3.35 or newer (found {sqlite3.sqlite_version})")


IN_MEMORY = ":memory:"


def _path(db_path: str | None) -> str:
    """Resolve a registry path, keeping `:memory:` as the name it is.

    `:memory:` is sqlite3's spelling for a database that never touches disk.
    Running it through `abspath` turns it into a real file called `:memory:` in
    the working directory, so an in-memory test or a dry run silently leaves a
    370 KB file behind and every assertion about "no database was created" is
    false.
    """
    candidate = db_path or DB_PATH
    if candidate == IN_MEMORY or candidate == "":
        return IN_MEMORY
    return os.path.abspath(os.path.expanduser(candidate))


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")


def _ensure_wal(conn: sqlite3.Connection) -> None:
    """Switch the file to write-ahead logging once, if it is not already.

    WAL is a persistent property of the database file, so this is a no-op
    after the first time. It cannot run inside a transaction, which is why it
    lives here rather than in the migration.
    """
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()
        if mode and str(mode[0]).lower() != "wal":
            conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        # A network or read-only filesystem may refuse WAL. The database still
        # works with its rollback journal; correctness does not depend on it.
        pass


def connect(db_path: str | None = None, *, create: bool = True) -> Connection:
    """Open the registry read-write, creating the file when allowed."""
    _require_runtime()
    path = _path(db_path)
    if not create and not os.path.isfile(path):
        raise RegistryMissing(f"registry not found at {path}; run `lele init` first")
    if create and path != IN_MEMORY:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, factory=Connection)
    try:
        conn.row_factory = sqlite3.Row
        _apply_pragmas(conn)
        conn.lele_path = path
        return conn
    except BaseException:
        conn.close()
        raise


def read_connect(db_path: str | None = None) -> Connection:
    """Open the registry read-only, falling back to a writable handle if needed.

    A read-only handle is the point: it cannot be made to write by a bug, and it
    works on a read-only mount. SQLite cannot open every database read-only (a
    WAL database with no shared-memory file, for example), so a failure falls
    back to a normal handle and says so through `lele_readonly`.
    """
    _require_runtime()
    path = _path(db_path)
    if path == IN_MEMORY:
        raise ValueError("an in-memory registry has no separate read-only handle; "
                         "the same connection is already usable for reading")
    if not os.path.isfile(path):
        raise RegistryMissing(f"registry not found at {path}; run `lele init` first")
    uri = Path(path).as_uri() + "?mode=ro"
    conn: Connection | None = None
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000, factory=Connection)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.Error as error:
        # SQLite cannot open every database read-only: a WAL database whose
        # shared-memory file is absent or unwritable needs to create it, which a
        # read-only handle may not do. Answering the question with a writable
        # handle beats refusing, so the open is retried and the flag records
        # that the read-only guarantee was not available this time.
        if conn is not None:
            conn.close()
        conn = connect(path, create=False)
        assert conn is not None
        conn.lele_readonly = False
        conn.lele_readonly_reason = f"{type(error).__name__}: {error}"
    else:
        conn.lele_readonly = True
    assert conn is not None
    try:
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.lele_path = path
        return conn
    except BaseException:
        conn.close()
        raise


def _state(conn: sqlite3.Connection) -> dict:
    """Version and object inventory from a single schema read.

    Touching `sqlite_master` makes SQLite parse the whole schema, which is the
    dominant cost of opening a registry, so the version and the inventory are
    taken together rather than in two round trips.
    """
    rows = conn.execute("SELECT type, name FROM sqlite_master").fetchall()
    tables = {name for kind, name in rows if kind == "table"}
    indexes = {name for kind, name in rows if kind == "index"}
    version = 0
    if "meta" in tables:
        row = conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
        if row is not None:
            try:
                version = int(row[0])
            except (TypeError, ValueError) as exc:
                raise RegistryError(
                    f"registry schema version is not an integer ({row[0]!r}); restore the "
                    f"database from a backup or delete it and run `lele init`") from exc
    return {"version": version,
            "missing_tables": sorted(EXPECTED_TABLES - tables),
            "missing_indexes": sorted(EXPECTED_INDEXES - indexes)}


def schema_version(conn: sqlite3.Connection) -> int:
    """The committed schema version, or 0 for a database with no meta table."""
    return _state(conn)["version"]


def object_inventory(conn: sqlite3.Connection) -> dict:
    """What the database actually holds against what the schema promises."""
    present = missing(conn)
    return {"schema_version": schema_version(conn),
            "expected_schema_version": SCHEMA_VERSION,
            "tables": len(EXPECTED_TABLES), "indexes": len(EXPECTED_INDEXES),
            "missing_tables": present["tables"],
            "missing_indexes": present["indexes"]}


def _orphan_report(conn: sqlite3.Connection) -> list[dict]:
    """Tables whose rows point at a parent row that is not there.

    Checked before any rebuild: adding a foreign key to a table that already
    holds orphans fails the rebuild, and because the whole migration is one
    transaction that failure leaves the registry unopenable by every command.
    """
    reports = []
    for table in ("attributes", "edges", "edge_retry", "metrics", "signals", "filings",
                  "entity_links", "entity_aliases", "sanctions_links", "money_flows"):
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?"
                            " AND sql LIKE '%REFERENCES%'", (table,)).fetchone():
            continue
        violations = conn.execute(f"PRAGMA foreign_key_check({table})").fetchall()
        if violations:
            reports.append({"table": table, "violations": len(violations),
                            "sample": [dict(row) for row in violations[:5]]})
    return reports


def _migrate_move_causes(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(move_causes)")}
    if not columns or "control_key" in columns:
        return
    temporary = "_registry_migrate_move_causes"
    conn.execute(table_definition("move_causes").replace(
        "move_causes(", f"{temporary}(", 1))
    conn.execute(
        f"""INSERT INTO {temporary}(move_event_id, control_key, role, category, article_id,
             observed_at, headline, domain, source_url, matched_terms, source, retrieved_at)
           SELECT move_event_id, '', role, category, article_id, observed_at, headline,
             domain, source_url, matched_terms, source, retrieved_at
           FROM move_causes WHERE role='move'""")
    conn.execute("DROP TABLE move_causes")
    conn.execute(f"ALTER TABLE {temporary} RENAME TO move_causes")


def _rebuild_with_foreign_keys(conn: sqlite3.Connection, table: str) -> None:
    temporary = f"_registry_migrate_{table}"
    conn.execute(table_definition(table).replace(f"{table}(", f"{temporary}(", 1))
    names = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    fields = ", ".join('"' + name.replace('"', '""') + '"' for name in names)
    conn.execute(f"INSERT INTO {temporary}(rowid, {fields}) SELECT rowid, {fields} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {temporary} RENAME TO {table}")


def _move_tier(threshold_percent: str | int) -> str:
    from .registry import move_tier
    return move_tier(threshold_percent)


def initialize(conn: sqlite3.Connection) -> bool:
    """Bring `conn`'s database to the current schema. Returns True if it wrote.

    The fast path is the important part: a database whose committed version is
    current is left alone. No transaction, no write lock, no file change. That
    is what makes a read command a read.
    """
    state = _state(conn)
    version = state["version"]
    if not 0 <= version <= SCHEMA_VERSION:
        raise RegistryMigrationError(
            f"unsupported registry schema version {version}; this build expects "
            f"{SCHEMA_VERSION}. A newer database cannot be opened by an older lele, "
            f"and a corrupted version row cannot be guessed at")
    if version == SCHEMA_VERSION:
        absent = state["missing_tables"] + state["missing_indexes"]
        if not absent:
            return False
        raise RegistryMigrationError(
            f"registry claims schema {SCHEMA_VERSION} but is missing {len(absent)} object(s): "
            f"{', '.join(absent[:8])}. Restore the database from a backup, or delete it and run "
            f"`lele init` to start a new one")
    if conn.in_transaction:
        raise RegistryError("cannot migrate inside an open transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        orphans = _orphan_report(conn)
        if orphans:
            summary = "; ".join(f"{item['table']}: {item['violations']} row(s)" for item in orphans)
            raise RegistryMigrationError(
                f"registry rows reference entities that no longer exist ({summary}). Adding "
                f"foreign keys to these tables would fail and would leave the registry "
                f"unopenable. Inspect with `PRAGMA foreign_key_check(<table>)`, then delete or "
                f"restore the orphaned rows by hand")
        preserved_events, preserved_causes = [], []
        legacy = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='move_events'"
        ).fetchone()
        if legacy and "tier IN ('medium', 'big')" in (legacy[0] or ""):
            preserved_events = [dict(row) for row in conn.execute("SELECT * FROM move_events")]
            preserved_causes = [dict(row) for row in conn.execute("SELECT * FROM move_causes")]
            conn.execute("DROP TABLE move_causes")
            conn.execute("DROP TABLE move_events")
        _migrate_move_causes(conn)
        for statement in statements():
            conn.execute(statement)
        for values in preserved_events:
            values["tier"] = _move_tier(int(values["threshold_percent"]))
            conn.execute(
                """INSERT INTO move_events(id, instrument_key, interval_seconds, move_hours,
                     tier, threshold_percent, direction, start_time, end_time, start_price,
                     end_price, change_percent, realized_volatility_percent,
                     baseline_mean_percent, baseline_std_percent, z_score, baseline_percentile,
                     baseline_bars, detected_at, available_at, source_url, evidence)
                   VALUES(:id, :instrument_key, :interval_seconds, :move_hours, :tier,
                     :threshold_percent, :direction, :start_time, :end_time, :start_price,
                     :end_price, :change_percent, :realized_volatility_percent,
                     :baseline_mean_percent, :baseline_std_percent, :z_score,
                     :baseline_percentile, :baseline_bars, :detected_at, :available_at,
                     :source_url, :evidence)""", values)
        for row in preserved_causes:
            conn.execute(
                """INSERT OR IGNORE INTO move_causes(move_event_id, control_key, role, category,
                     article_id, observed_at, headline, domain, source_url, matched_terms,
                     source, retrieved_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row["move_event_id"], row.get("control_key", ""), row["role"], row["category"],
                 row["article_id"], row["observed_at"], row["headline"], row.get("domain", ""),
                 row.get("source_url"), row.get("matched_terms", "[]"), row["source"],
                 row["retrieved_at"]))
        edge_columns = {row[1] for row in conn.execute("PRAGMA table_info(edges)")}
        for column, definition in (
            ("source_url", "TEXT NOT NULL DEFAULT ''"),
            ("observed_at", "TEXT NOT NULL DEFAULT ''"),
            ("evidence", "TEXT NOT NULL DEFAULT ''"),
            ("first_seen_at", "TEXT NOT NULL DEFAULT ''"),
            ("last_seen_at", "TEXT NOT NULL DEFAULT ''"),
            ("seen_count", "INTEGER NOT NULL DEFAULT 0"),
            ("status", "TEXT NOT NULL DEFAULT 'active'"),
            ("retracted_at", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in edge_columns:
                conn.execute(f"ALTER TABLE edges ADD COLUMN {column} {definition}")
        run_columns = {row[1] for row in conn.execute("PRAGMA table_info(ingest_runs)")}
        for column, definition in (
            ("resumable", "INTEGER NOT NULL DEFAULT 0"),
            ("next_offset", "INTEGER NOT NULL DEFAULT 0"),
            ("next_page", "INTEGER NOT NULL DEFAULT 0"),
            ("pages_detail", "TEXT NOT NULL DEFAULT '[]'"),
        ):
            if column not in run_columns:
                conn.execute(f"ALTER TABLE ingest_runs ADD COLUMN {column} {definition}")
        for table in ("attributes", "edges", "metrics", "signals", "filings"):
            if not conn.execute(f"PRAGMA foreign_key_list({table})").fetchall():
                _rebuild_with_foreign_keys(conn, table)
        for name, table, fields in (
            ("idx_entities_kind_name", "entities", "kind, name"),
            ("idx_entities_country", "entities", "country"),
            ("idx_entities_name", "entities", "name"),
            ("idx_edges_dst", "edges", "dst_id, rel"),
            ("idx_edges_rel", "edges", "rel"),
            ("idx_signals_ent", "signals", "entity_id"),
            ("idx_event_store_type", "event_store", "event_type, occurred_at"),
            ("idx_event_store_actor", "event_store", "actor_key, occurred_at"),
            ("idx_event_store_instrument", "event_store", "instrument_key, occurred_at"),
            ("idx_event_rel_source", "event_relationships", "source_event_id"),
            ("idx_event_rel_target", "event_relationships", "target_event_id"),
            ("idx_price_anomaly_instrument", "price_anomalies", "instrument_key, observed_at"),
            ("idx_money_attrib_event", "money_flow_attribution", "event_id"),
            ("idx_money_attrib_flow", "money_flow_attribution", "flow_id"),
            ("idx_volatility_instrument", "volatility_instances", "instrument_key, as_of"),
            ("idx_volatility_magnitude", "volatility_instances", "magnitude_percent"),
            ("idx_event_vol_event", "event_volatility_links", "event_id"),
            ("idx_event_vol_volatility", "event_volatility_links", "volatility_id"),
            ("idx_embeddings_kind", "semantic_embeddings", "source_kind, as_of"),
            ("idx_embeddings_entity", "semantic_embeddings", "entity_key, as_of"),
            ("idx_pattern_current", "pattern_matches", "current_volatility_id"),
            ("idx_pattern_historical", "pattern_matches", "historical_volatility_id"),
            ("idx_price_bars_series", "price_bars", "instrument_key, interval_seconds, open_time"),
            ("idx_move_events_series", "move_events",
             "instrument_key, interval_seconds, move_hours, end_time"),
            ("idx_move_events_tier", "move_events", "tier, direction, end_time"),
            ("idx_move_causes_category", "move_causes", "category, observed_at"),
            ("idx_move_causes_window", "move_causes", "role, observed_at"),
            ("idx_move_causes_control", "move_causes", "control_key"),
            ("idx_cause_scans_series", "cause_scans", "instrument_key, as_of"),
        ):
            conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({fields})")
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            raise sqlite3.IntegrityError("Registry contains orphaned records")
        conn.execute(
            "INSERT INTO meta(k, v) VALUES('schema_version', ?)"
            " ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (str(SCHEMA_VERSION),))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    _ensure_wal(conn)
    return True


def init_db(db_path: str | None = None) -> None:
    conn = connect(db_path)
    try:
        initialize(conn)
    finally:
        conn.close()


@contextmanager
def get_conn(db_path: str | None = None) -> Iterator[Connection]:
    """Read-write session. The single owner of commit and rollback."""
    conn = connect(db_path)
    try:
        initialize(conn)
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_read_conn(db_path: str | None = None) -> Iterator[Connection]:
    """Read-only session. Never migrates and never writes."""
    conn = read_connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def _path_note(source: str) -> str:
    """Why this path, in words, so a surprising default is explainable."""
    if source == "environment":
        return "set by LELE_DB or the previous FINWORLD_DB"
    if source == "previous_name":
        return (f"an existing registry is at the previous location "
                f"({LEGACY_HOME_DIR}); nothing was moved. Set LELE_DB, or move the file to "
                f"{os.path.join(HOME_DIR, DB_FILENAME)} to use the current location")
    return f"the default location for {APP_NAME}"


def health_check(db_path: str) -> dict:
    """Runtime and registry self-check that never creates or repairs anything."""
    python_supported = sys.version_info >= (3, 11)
    sqlite_supported = sqlite3.sqlite_version_info >= (3, 35, 0)
    path = _path(db_path)
    report: dict = {
        "python": {"version": ".".join(str(part) for part in sys.version_info[:3]),
                   "implementation": platform.python_implementation(),
                   "supported": python_supported},
        "sqlite": {"version": sqlite3.sqlite_version, "supported": sqlite_supported},
        "platform": {"system": sys.platform, "machine": platform.machine()},
        "expected_schema_version": SCHEMA_VERSION,
        "application": {"name": APP_NAME, "version": APP_VERSION,
                        "previous_name": PREVIOUS_APP_NAME},
        "registry": {"path": path, "present": os.path.isfile(path),
                     "path_source": DB_PATH_SOURCE,
                     "path_note": _path_note(DB_PATH_SOURCE)},
        "status": "ok",
    }
    if not python_supported or not sqlite_supported:
        report["status"] = "unsupported_runtime"
    if not report["registry"]["present"]:
        return report
    try:
        conn = read_connect(path)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
            state = _state(conn)
            version = state["version"]
            entities = conn.execute("SELECT count(*) FROM entities").fetchone()[0]
            edges = conn.execute("SELECT count(*) FROM edges").fetchone()[0]
            journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
            absent = state["missing_tables"] + state["missing_indexes"]
        finally:
            conn.close()
    except RegistryError as exc:
        report["registry"]["error"] = str(exc)
        report["status"] = "issues"
        return report
    except (sqlite3.Error, OSError) as exc:
        report["registry"]["error"] = str(exc)
        report["status"] = "unreadable"
        return report
    report["registry"].update({
        "integrity_check": integrity, "foreign_key_violations": violations,
        "schema_version": version, "entities": entities, "edges": edges,
        "journal_mode": journal,
        "missing_objects": absent})
    if integrity != "ok" or violations or version < 0 or version > SCHEMA_VERSION:
        report["status"] = "issues"
    elif absent:
        report["status"] = "incomplete"
    elif version < SCHEMA_VERSION:
        report["status"] = "migration_pending"
    return report


def backup_database(source_path: str, target_path: str, force: bool = False) -> dict:
    """Write a consistent copy through SQLite's online backup, then verify it."""
    if type(force) is not bool:
        raise ValueError("force must be a boolean")
    source = _path(source_path)
    target = _path(target_path)
    if not os.path.isfile(source):
        raise ValueError("registry source file does not exist")
    if os.path.isdir(target):
        raise ValueError("backup target must be a file")
    if target in {source, source + "-wal", source + "-shm", source + "-journal"}:
        raise ValueError("backup target must not be the registry or its journal files")
    if os.path.lexists(target) and not force:
        raise ValueError("backup target already exists; use force to replace it")
    parent = os.path.dirname(target) or "."
    if not os.path.isdir(parent):
        raise ValueError("backup parent directory must already exist")
    descriptor, temporary = tempfile.mkstemp(dir=parent, prefix=".lele-backup-")
    os.close(descriptor)
    try:
        source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True,
                                      timeout=BUSY_TIMEOUT_MS / 1000)
        try:
            destination = sqlite3.connect(temporary)
            try:
                source_conn.backup(destination)
            finally:
                destination.close()
        finally:
            source_conn.close()
        check = sqlite3.connect(temporary)
        try:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("backup integrity check failed")
            entities = check.execute("SELECT count(*) FROM entities").fetchone()[0]
            row = check.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
        finally:
            check.close()
        if force:
            os.replace(temporary, target)
        else:
            if not hasattr(os, "link"):
                raise ValueError("atomic no-overwrite backup is unavailable on this runtime")
            try:
                os.link(temporary, target)
            except FileExistsError:
                raise ValueError("backup target already exists; use force to replace it") from None
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    hasher = hashlib.sha256()
    size = 0
    with open(target, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            hasher.update(chunk)
            size += len(chunk)
    return {"source": source, "target": target, "bytes": size, "sha256": hasher.hexdigest(),
            "entities": entities, "schema_version": row[0] if row is not None else None}
