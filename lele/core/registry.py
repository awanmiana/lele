"""SQLite-backed persistent registry for financial institutions and entities.

Tables:
- entities: id, kind, name, country, website, lei, notes
- attributes: entity_id, k, v
- edges: src_id, rel, dst_id  (governed_by / supervises / shareholder_of ...)
- metrics: entity_id, k, v, period, source
- signals: entity_id, ts, kind, score, direction, rationale, source, ref
- filings: entity_id, form, date, title, url, source
"""
import json
import re
import sqlite3
from datetime import datetime
from decimal import (Context, Decimal, InvalidOperation, ROUND_HALF_EVEN,
                   localcontext)

from ..core import clock, db
from ..core.schema import SCHEMA_VERSION as _SCHEMA_DDL_VERSION
from ..core.schema import _SCHEMA as _SCHEMA_DDL

_SCHEMA_VERSION = _SCHEMA_DDL_VERSION  # single source of truth in core/schema.py
KIND_DEFINITIONS = {
    "bank": {"definition": "US depository institution from the FDIC active/insured list, or an "
                           "OSFI domestic/foreign bank.",
             "produced_by": "fdic, osfi"},
    "bank_branch": {"definition": "Foreign bank branch (OSFI); a branch is not a separate "
                                  "incorporated entity.",
                    "produced_by": "osfi"},
    "representative_office": {"definition": "Foreign bank representative office (OSFI); carries no "
                                             "automatic supervision claim.",
                              "produced_by": "osfi"},
    "insurance_company": {"definition": "Life or property/casualty insurer (OSFI); not global "
                                        "insurance coverage.",
                          "produced_by": "osfi"},
    "trust_company": {"definition": "Federal trust company (OSFI).", "produced_by": "osfi"},
    "loan_company": {"definition": "Federal loan company (OSFI).", "produced_by": "osfi"},
    "fraternal_benefit_society": {"definition": "Canadian fraternal benefit society (OSFI).",
                                  "produced_by": "osfi"},
    "financial_institution": {"definition": "OSFI federally regulated institution whose group and "
                                            "industry are not recognized; classification is "
                                            "unknown, not a bank.",
                              "produced_by": "osfi"},
    "regulator": {"definition": "A financial regulator created as an edge target for sourced "
                                "supervision (for example OSFI).",
                  "produced_by": "osfi, local import"},
    "authority": {"definition": "A public authority supplied by a local import.",
                  "produced_by": "import"},
    "fund": {"definition": "A GLEIF legal entity whose category is FUND; not proof that it is a "
                           "fundraiser, VC firm or investment adviser.",
             "produced_by": "gleif"},
    "legal_entity": {"definition": "A GLEIF legal entity that is not categorized FUND; it may be a "
                                   "manager, parent, adviser or operating company.",
                     "produced_by": "gleif"},
    "company": {"definition": "An SEC EDGAR operating registrant.", "produced_by": "sec"},
    "issuer": {"definition": "An SEC EDGAR registrant whose entity type is not operating.",
               "produced_by": "sec"},
    "economy": {"definition": "A World Bank economy aggregate for a macro indicator series; not a "
                              "legal entity.",
                "produced_by": "worldbank"},
    "instrument": {"definition": "A tradable instrument bound to a recorded price series; not a "
                                 "legal entity.",
                   "produced_by": "local import, prices"},
}


def list_kinds() -> list[dict]:
    return [{"kind": kind, **KIND_DEFINITIONS[kind]} for kind in sorted(KIND_DEFINITIONS)]


RELATIONSHIPS = frozenset({
    "governed_by", "supervises", "supervised_by", "regulates", "regulated_by",
    "shareholder_of", "owned_by", "owns", "parent_of", "subsidiary_of",
    "ultimate_parent_of", "ultimate_subsidiary_of",
    "branch_of", "affiliate_of", "member_of", "invests_in", "invested_in",
    "funded_by", "funds", "managed_by", "manages", "custodian_of",
    "audited_by", "audits", "partner_of", "counterparty_of", "lends_to",
    "borrows_from", "sanctioned_by", "issuer_of", "guarantees", "enforcement_by",
})

_SCHEMA = _SCHEMA_DDL  # re-exported: tests and migrations read it from here

# The connection, migration, self-check and backup layer lives in core/db.py. These
# names stay here because the whole package, and its tests, open the registry
# through this module.
_require_runtime = db._require_runtime
_connect = db.connect
read_connect = db.read_connect
_initialize = db.initialize
init_db = db.init_db
get_conn = db.get_conn
get_read_conn = db.get_read_conn
health_check = db.health_check
backup_database = db.backup_database
schema_version = db.schema_version
object_inventory = db.object_inventory
RegistryError = db.RegistryError
RegistryMissing = db.RegistryMissing
RegistryMigrationError = db.RegistryMigrationError

# ---------------------------------------------------------------------------
# entity helpers
# ---------------------------------------------------------------------------

def normalize_name(name: str) -> str:
    return " ".join((name or "").split()).strip().lower()


_NAME_SUFFIXES = frozenset({
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "plc", "co", "company",
    "group", "holdings", "the", "sa", "ag", "gmbh", "nv", "bv", "kk", "pty",
})


def name_tokens(name: str) -> frozenset:
    return frozenset(word for word in re.split(r"[^0-9a-z]+", normalize_name(name))
                     if word and word not in _NAME_SUFFIXES)


def fuzzy_name_match(left: frozenset, right: frozenset) -> bool:
    if not left or not right:
        return False
    shared = left & right
    if not shared:
        return False
    return len(shared) / min(len(left), len(right)) >= 0.6

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
            "INSERT INTO edges(src_id, rel, dst_id, source_url, observed_at, evidence,"
            " first_seen_at, last_seen_at, seen_count, status)"
            " VALUES(?,?,?,?,?,?,?,?,1,'active') ON CONFLICT(src_id, rel, dst_id) DO UPDATE SET"
            " source_url=COALESCE(NULLIF(excluded.source_url, ''), edges.source_url),"
            " observed_at=COALESCE(NULLIF(excluded.observed_at, ''), edges.observed_at),"
            " evidence=COALESCE(NULLIF(excluded.evidence, ''), edges.evidence),"
            " first_seen_at=CASE WHEN edges.first_seen_at = ''"
            " THEN excluded.first_seen_at ELSE edges.first_seen_at END,"
            " last_seen_at=COALESCE(NULLIF(excluded.last_seen_at, ''), edges.last_seen_at),"
            " seen_count=edges.seen_count+1,"
            " status='active', retracted_at=''",
            (src_id, rel, dst_id, source_url, observed_at, evidence, observed_at, observed_at),
        )


def retract_edge(conn, src_id: int, rel: str, dst_id: int, at: str = "") -> int:
    cursor = conn.execute(
        "UPDATE edges SET status='retracted', retracted_at=?"
        " WHERE src_id=? AND rel=? AND dst_id=? AND status != 'retracted'",
        (at, src_id, rel, dst_id),
    )
    return cursor.rowcount


def record_edge_failure(conn, src_id: int, rel: str, at: str = "") -> None:
    conn.execute(
        "INSERT INTO edge_retry(src_id, rel, last_attempt_at, failures) VALUES(?,?,?,1)"
        " ON CONFLICT(src_id, rel) DO UPDATE SET"
        " last_attempt_at=excluded.last_attempt_at, failures=edge_retry.failures+1",
        (src_id, rel, at),
    )


def clear_edge_failure(conn, src_id: int, rel: str) -> None:
    conn.execute("DELETE FROM edge_retry WHERE src_id=? AND rel=?", (src_id, rel))

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
    legal_country: str | None = None,
    jurisdiction: str | None = None,
    headquarters_country: str | None = None,
    source: str | None = None,
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
    if legal_country:
        clauses.append("e.country = ?")
        params.append(legal_country)
    if jurisdiction:
        clauses.append("a.v = ?")
        params.append(jurisdiction)
    if headquarters_country:
        clauses.append(
            "EXISTS (SELECT 1 FROM attributes ah WHERE ah.entity_id = e.id"
            " AND ah.k = 'gleif.headquarters_country' AND ah.v = ?)"
        )
        params.append(headquarters_country)
    if source:
        clauses.append(
            "EXISTS (SELECT 1 FROM attributes asrc WHERE asrc.entity_id = e.id"
            " AND asrc.k = 'source' AND asrc.v = ?)"
        )
        params.append(source)
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

def resolve_entity_id(conn, eid: int) -> int:
    row = conn.execute("SELECT canonical_id FROM entity_aliases WHERE alias_id=?", (eid,)).fetchone()
    return row[0] if row is not None else eid


def list_aliases(conn, limit: int = 50) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer from 1 to 1000")
    rows = conn.execute(
        "SELECT a.alias_id, a.canonical_id, a.created_at, a.reason, a.source_url,"
        " ae.name AS alias_name, ce.name AS canonical_name FROM entity_aliases a"
        " JOIN entities ae ON ae.id=a.alias_id JOIN entities ce ON ce.id=a.canonical_id"
        " ORDER BY a.alias_id LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def merge_entity(conn, alias_id: int, canonical_id: int, reason: str = "",
                 source_url: str = "") -> dict:
    if alias_id == canonical_id:
        raise ValueError("alias and canonical ids must differ")
    for label, value in (("alias", alias_id), ("canonical", canonical_id)):
        if type(value) is not int or not 1 <= value <= 2**63 - 1:
            raise ValueError(f"{label} id must be a positive SQLite integer")
        if get_entity(conn, value) is None:
            raise ValueError(f"{label} entity {value} not found")
    if not isinstance(reason, str) or not isinstance(source_url, str):
        raise ValueError("reason and source_url must be strings")
    if resolve_entity_id(conn, alias_id) != alias_id:
        raise ValueError("entity is already an alias")
    if resolve_entity_id(conn, canonical_id) != canonical_id:
        raise ValueError("canonical entity is itself an alias")
    if conn.execute("SELECT 1 FROM entity_aliases WHERE canonical_id=?",
                    (alias_id,)).fetchone():
        raise ValueError("alias entity has its own aliases; unmerge them first")
    conn.execute(
        "INSERT INTO entity_aliases(alias_id, canonical_id, reason, source_url) VALUES(?,?,?,?)",
        (alias_id, canonical_id, reason, source_url))
    return {"alias_id": alias_id, "canonical_id": canonical_id, "reason": reason,
            "source_url": source_url, "review": "human_approved"}


def unmerge_entity(conn, alias_id: int) -> int:
    if type(alias_id) is not int or not 1 <= alias_id <= 2**63 - 1:
        raise ValueError("alias id must be a positive SQLite integer")
    return conn.execute("DELETE FROM entity_aliases WHERE alias_id=?", (alias_id,)).rowcount


def matching_candidates(conn, eid: int | None = None, limit: int = 50,
                        fuzzy: bool = False) -> list[dict]:
    if eid is not None and (type(eid) is not int or not 1 <= eid <= 2**63 - 1):
        raise ValueError("entity id must be a positive SQLite integer")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer from 1 to 1000")
    if type(fuzzy) is not bool:
        raise ValueError("fuzzy must be a boolean")
    if fuzzy and eid is None:
        raise ValueError("fuzzy matching requires a single entity id")
    merged = {row[0] for row in conn.execute("SELECT alias_id FROM entity_aliases")}
    pairs: dict[tuple, tuple] = {}

    def add(left, right, reason, strength):
        if left == right or left in merged or right in merged:
            return
        key = (min(left, right), max(left, right))
        if key in pairs and pairs[key][2] >= strength:
            return
        pairs[key] = (key[0], key[1], strength, reason)

    lei_groups: dict[str, list] = {}
    for row in conn.execute("SELECT id, lei FROM entities WHERE lei IS NOT NULL AND trim(lei)!=''"
                            " ORDER BY lei, id"):
        lei_groups.setdefault(row["lei"].strip().upper(), []).append(row["id"])
    for ids in lei_groups.values():
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                add(ids[i], ids[j], "same_lei", 3)
    name_groups: dict[str, list] = {}
    for row in conn.execute("SELECT id, name, country FROM entities ORDER BY name, id"):
        name_groups.setdefault(normalize_name(row["name"]), []).append((row["id"], row["country"]))
    for entries in name_groups.values():
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                left, left_country = entries[i]
                right, right_country = entries[j]
                if left_country and right_country and left_country == right_country:
                    add(left, right, "same_name_and_country", 2)
                else:
                    add(left, right, "same_name", 1)
    if fuzzy and eid is not None:
        target = name_tokens(get_entity(conn, eid)["name"])
        for row in conn.execute("SELECT id, name FROM entities ORDER BY id"):
            if row["id"] != eid and fuzzy_name_match(target, name_tokens(row["name"])):
                add(eid, row["id"], "fuzzy_name", 0)
    selected = [value for value in pairs.values() if eid is None or eid in (value[0], value[1])]
    selected.sort(key=lambda value: (-value[2], value[0], value[1]))
    out = []
    for left, right, strength, reason in selected[:limit]:
        left_entity, right_entity = get_entity(conn, left), get_entity(conn, right)
        out.append({"left_id": left, "left_name": left_entity["name"],
                    "left_kind": left_entity["kind"], "right_id": right,
                    "right_name": right_entity["name"], "right_kind": right_entity["kind"],
                    "reason": reason, "strength": strength,
                    "review": "unreviewed_candidate"})
    return out


_SANCTIONS_BASES = frozenset({"designation", "allegation", "finding", "unknown"})
_SANCTIONS_STATUSES = frozenset({"active", "delisted"})


def add_sanctions_listing(conn, source: str, listing_key: str, name: str, entity_type: str = "",
                          program: str = "", country: str = "", basis: str = "unknown",
                          status: str = "active", published_at: str = "", effective_at: str = "",
                          source_url: str = "", retrieved_at: str = "", evidence: str = "") -> int:
    for label, value in (("source", source), ("listing_key", listing_key), ("name", name)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be nonempty text")
    for label, value in (("entity_type", entity_type), ("program", program), ("country", country),
                         ("published_at", published_at), ("effective_at", effective_at),
                         ("source_url", source_url), ("retrieved_at", retrieved_at),
                         ("evidence", evidence)):
        if not isinstance(value, str):
            raise ValueError(f"{label} must be text")
    if basis not in _SANCTIONS_BASES:
        raise ValueError("basis must be designation, allegation, finding or unknown")
    if status not in _SANCTIONS_STATUSES:
        raise ValueError("status must be active or delisted")
    conn.execute(
        "INSERT INTO sanctions_listings(source, listing_key, name, entity_type, program, country,"
        " basis, status, published_at, effective_at, source_url, retrieved_at, evidence)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source, listing_key) DO UPDATE SET"
        " name=excluded.name, entity_type=excluded.entity_type, program=excluded.program,"
        " country=excluded.country, basis=excluded.basis, status=excluded.status,"
        " published_at=excluded.published_at, effective_at=excluded.effective_at,"
        " source_url=excluded.source_url, retrieved_at=excluded.retrieved_at,"
        " evidence=excluded.evidence",
        (source, listing_key, name, entity_type, program, country, basis, status, published_at,
         effective_at, source_url, retrieved_at, evidence))
    return int(conn.execute("SELECT id FROM sanctions_listings WHERE source=? AND listing_key=?",
                            (source, listing_key)).fetchone()[0])


def list_sanctions_listings(conn, limit: int = 50, active_only: bool = False) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer from 1 to 1000")
    if type(active_only) is not bool:
        raise ValueError("active_only must be a boolean")
    sql = "SELECT * FROM sanctions_listings"
    if active_only:
        sql += " WHERE status='active'"
    sql += " ORDER BY name, id LIMIT ?"
    return [dict(row) for row in conn.execute(sql, (limit,))]


def sanctions_candidates(conn, entity_id: int | None = None, limit: int = 50,
                         fuzzy: bool = False) -> list[dict]:
    if entity_id is not None and (type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1):
        raise ValueError("entity id must be a positive SQLite integer")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer from 1 to 1000")
    if type(fuzzy) is not bool:
        raise ValueError("fuzzy must be a boolean")
    if fuzzy and entity_id is None:
        raise ValueError("fuzzy matching requires a single entity id")
    if entity_id is not None and get_entity(conn, entity_id) is None:
        raise ValueError(f"entity {entity_id} not found")
    linked = {(row[0], row[1]) for row in conn.execute(
        "SELECT listing_id, entity_id FROM sanctions_links")}
    listings = conn.execute("SELECT * FROM sanctions_listings ORDER BY name, id").fetchall()
    entity_index: dict[str, list] = {}
    for row in conn.execute("SELECT id, name, kind, country FROM entities ORDER BY id"):
        entity_index.setdefault(normalize_name(row["name"]), []).append(row)

    def entry(listing, entity, reason):
        return {"listing_id": listing["id"], "listing_name": listing["name"],
                "basis": listing["basis"], "status": listing["status"],
                "program": listing["program"], "source": listing["source"],
                "source_url": listing["source_url"], "published_at": listing["published_at"],
                "entity_id": entity["id"], "entity_name": entity["name"],
                "entity_kind": entity["kind"], "reason": reason,
                "review": "unreviewed_candidate"}

    out = []
    seen = set()
    for listing in listings:
        for entity in entity_index.get(normalize_name(listing["name"]), []):
            if entity_id is not None and entity["id"] != entity_id:
                continue
            if (listing["id"], entity["id"]) in linked:
                continue
            out.append(entry(listing, entity, "same_name"))
            seen.add((listing["id"], entity["id"]))
            if len(out) >= limit:
                return out
    if fuzzy and entity_id is not None:
        target = get_entity(conn, entity_id)
        target_tokens = name_tokens(target["name"])
        for listing in listings:
            key = (listing["id"], entity_id)
            if key in linked or key in seen:
                continue
            if fuzzy_name_match(target_tokens, name_tokens(listing["name"])):
                out.append(entry(listing, target, "fuzzy_name"))
                if len(out) >= limit:
                    return out
    return out


_SANCTIONS_AUTHORITY_PREFIX = "authority:sanctions:"


def _sanctions_authority(conn, source: str) -> int:
    key = _SANCTIONS_AUTHORITY_PREFIX + source
    row = conn.execute("SELECT id FROM entities WHERE key=?", (key,)).fetchone()
    if row is not None:
        return row[0]
    return upsert_entity(conn, "authority", f"{source} sanctions authority", key=key,
                         notes="Created as the target of reviewed sanctions links; it does not "
                               "itself assert any finding against a linked entity.")


def link_sanctions(conn, listing_id: int, entity_id: int, reason: str = "") -> dict:
    if type(listing_id) is not int or not 1 <= listing_id <= 2**63 - 1:
        raise ValueError("listing id must be a positive SQLite integer")
    if type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1:
        raise ValueError("entity id must be a positive SQLite integer")
    if not isinstance(reason, str):
        raise ValueError("reason must be text")
    listing = conn.execute("SELECT * FROM sanctions_listings WHERE id=?", (listing_id,)).fetchone()
    if listing is None:
        raise ValueError(f"listing {listing_id} not found")
    if get_entity(conn, entity_id) is None:
        raise ValueError(f"entity {entity_id} not found")
    conn.execute(
        "INSERT INTO sanctions_links(listing_id, entity_id, reason) VALUES(?,?,?)"
        " ON CONFLICT(listing_id, entity_id) DO UPDATE SET reason=excluded.reason",
        (listing_id, entity_id, reason))
    authority_id = _sanctions_authority(conn, listing["source"])
    evidence = (f"Reviewed sanctions link to {listing['source']} listing {listing['listing_key']} "
                f"({listing['basis']}, {listing['status']}); reason: {reason or 'not stated'}. "
                "A name or listing match is not guilt and is human-approved only.")
    add_edge(conn, entity_id, "sanctioned_by", authority_id, source_url=listing["source_url"],
             observed_at=listing["retrieved_at"], evidence=evidence)
    return {"listing_id": listing_id, "entity_id": entity_id, "reason": reason,
            "basis": listing["basis"], "status": listing["status"],
            "source_url": listing["source_url"], "authority_id": authority_id,
            "edge": "sanctioned_by", "review": "human_approved"}


def unlink_sanctions(conn, listing_id: int, entity_id: int) -> int:
    listing = conn.execute("SELECT source FROM sanctions_listings WHERE id=?",
                           (listing_id,)).fetchone()
    removed = conn.execute("DELETE FROM sanctions_links WHERE listing_id=? AND entity_id=?",
                           (listing_id, entity_id)).rowcount
    if removed and listing is not None:
        remaining = conn.execute(
            "SELECT 1 FROM sanctions_links l JOIN sanctions_listings s ON s.id=l.listing_id"
            " WHERE l.entity_id=? AND s.source=? LIMIT 1",
            (entity_id, listing["source"])).fetchone()
        if remaining is None:
            authority = conn.execute("SELECT id FROM entities WHERE key=?",
                                     (_SANCTIONS_AUTHORITY_PREFIX + listing["source"],)).fetchone()
            if authority is not None:
                retract_edge(conn, entity_id, "sanctioned_by", authority["id"], "")
    return removed


def mark_delisted(conn, source: str, current_keys) -> int:
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source must be nonempty text")
    present = set(current_keys)
    delisted = 0
    rows = conn.execute("SELECT id, listing_key FROM sanctions_listings"
                        " WHERE source=? AND status='active'", (source,)).fetchall()
    for row in rows:
        if row["listing_key"] not in present:
            conn.execute("UPDATE sanctions_listings SET status='delisted' WHERE id=?", (row["id"],))
            delisted += 1
    return delisted


def sanctions_links(conn, limit: int = 50) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer from 1 to 1000")
    rows = conn.execute(
        "SELECT l.listing_id, l.entity_id, l.created_at, l.reason, s.name AS listing_name,"
        " s.basis, s.status, s.source, s.source_url, e.name AS entity_name FROM sanctions_links l"
        " JOIN sanctions_listings s ON s.id=l.listing_id JOIN entities e ON e.id=l.entity_id"
        " ORDER BY l.created_at, l.listing_id LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


_FLOW_TYPES = frozenset({
    "loan", "repayment", "investment", "divestment", "dividend", "grant", "purchase", "sale",
    "fee", "transfer", "guarantee", "other",
})
FLOW_TYPES = _FLOW_TYPES
_AMOUNT_RE = re.compile(r"[+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")


def entity_id_by_key(conn, key: str) -> int | None:
    if not isinstance(key, str) or not key.strip():
        raise ValueError("entity key must be nonempty text")
    row = conn.execute("SELECT id FROM entities WHERE key=?", (key,)).fetchone()
    return row[0] if row is not None else None


def add_money_flow(conn, src_id: int, dst_id: int, flow_type: str, amount: str, currency: str,
                   occurred_at: str = "", source_url: str = "", evidence: str = "") -> int:
    for label, value in (("src_id", src_id), ("dst_id", dst_id)):
        if type(value) is not int or not 1 <= value <= 2**63 - 1:
            raise ValueError(f"{label} must be a positive SQLite integer")
    if src_id == dst_id:
        raise ValueError("flow source and destination must differ")
    for label, value in (("source", src_id), ("destination", dst_id)):
        if get_entity(conn, value) is None:
            raise ValueError(f"{label} entity {value} not found")
    if flow_type not in _FLOW_TYPES:
        raise ValueError("flow_type is not a supported documented flow type")
    if not isinstance(amount, str) or not _AMOUNT_RE.fullmatch(amount.strip()):
        raise ValueError("amount must be a positive decimal string")
    try:
        number = Decimal(amount.strip())
    except InvalidOperation as exc:
        raise ValueError("amount must be a positive decimal string") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError("amount must be a positive decimal string")
    if not isinstance(currency, str) or not currency.strip():
        raise ValueError("currency must be nonempty text")
    for label, item in (("occurred_at", occurred_at), ("source_url", source_url),
                        ("evidence", evidence)):
        if not isinstance(item, str):
            raise ValueError(f"{label} must be text")
    cursor = conn.execute(
        "INSERT INTO money_flows(src_id, dst_id, flow_type, amount, currency, occurred_at,"
        " source_url, evidence) VALUES(?,?,?,?,?,?,?,?)",
        (src_id, dst_id, flow_type, str(number), currency.strip(), occurred_at, source_url,
         evidence))
    return int(cursor.lastrowid)


def list_money_flows(conn, entity_id: int | None = None, limit: int = 50) -> list[dict]:
    if entity_id is not None:
        if type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1:
            raise ValueError("entity id must be a positive SQLite integer")
        if get_entity(conn, entity_id) is None:
            raise ValueError(f"entity {entity_id} not found")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer from 1 to 1000")
    sql = ("SELECT f.id, f.flow_type, f.amount, f.currency, f.occurred_at, f.source_url,"
           " f.evidence, f.src_id, se.name AS src_name, se.key AS src_key, f.dst_id,"
           " de.name AS dst_name, de.key AS dst_key FROM money_flows f"
           " JOIN entities se ON se.id=f.src_id JOIN entities de ON de.id=f.dst_id")
    params: tuple = (limit,)
    if entity_id is not None:
        sql += " WHERE f.src_id=? OR f.dst_id=?"
        params = (entity_id, entity_id, limit)
    sql += " ORDER BY f.occurred_at, f.id LIMIT ?"
    return [dict(row) for row in conn.execute(sql, params)]


_LINK_TYPES = frozenset({"website", "social", "registry", "other"})


def set_entity_link(conn, entity_id: int, link_type: str, url: str, label: str = "",
                    source_url: str = "", observed_at: str = "", evidence: str = "") -> dict:
    if type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1:
        raise ValueError("entity id must be a positive SQLite integer")
    if get_entity(conn, entity_id) is None:
        raise ValueError(f"entity {entity_id} not found")
    if link_type not in _LINK_TYPES:
        raise ValueError("link_type must be website, social, registry or other")
    for name, value in (("url", url), ("source_url", source_url)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be nonempty text")
    for name, value in (("label", label), ("observed_at", observed_at), ("evidence", evidence)):
        if not isinstance(value, str):
            raise ValueError(f"{name} must be text")
    conn.execute(
        "INSERT INTO entity_links(entity_id, link_type, url, label, source_url, observed_at,"
        " evidence) VALUES(?,?,?,?,?,?,?) ON CONFLICT(entity_id, link_type, url) DO UPDATE SET"
        " label=excluded.label, source_url=excluded.source_url, observed_at=excluded.observed_at,"
        " evidence=excluded.evidence",
        (entity_id, link_type, url.strip(), label, source_url.strip(), observed_at, evidence))
    return {"entity_id": entity_id, "link_type": link_type, "url": url.strip(), "label": label,
            "source_url": source_url.strip(), "observed_at": observed_at, "evidence": evidence,
            "review": "verified_link_recorded"}


def list_entity_links(conn, entity_id: int | None = None, limit: int = 50) -> list[dict]:
    if entity_id is not None:
        if type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1:
            raise ValueError("entity id must be a positive SQLite integer")
        if get_entity(conn, entity_id) is None:
            raise ValueError(f"entity {entity_id} not found")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer from 1 to 1000")
    sql = "SELECT * FROM entity_links"
    params: tuple = ()
    if entity_id is not None:
        sql += " WHERE entity_id=?"
        params = (entity_id,)
    sql += " ORDER BY link_type, url LIMIT ?"
    return [dict(row) for row in conn.execute(sql, (*params, limit))]


def remove_entity_link(conn, entity_id: int, url: str) -> int:
    if type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1:
        raise ValueError("entity id must be a positive SQLite integer")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be nonempty text")
    return conn.execute("DELETE FROM entity_links WHERE entity_id=? AND url=?",
                        (entity_id, url.strip())).rowcount


OBSERVATION_REASON_BASES = ("unknown", "stated_reason", "documented_mandate", "analyst_hypothesis")
OBSERVATION_BASES = ("observed", "estimated")
_OBSERVATION_AMOUNT_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")


def _observation_text(value, label, limit=4096, required=False) -> str:
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise ValueError(f"{label} must be text of at most {limit} characters without NUL")
    if required and not value.strip():
        raise ValueError(f"{label} must be nonempty")
    return value


def add_observation(conn, *, source: str, external_id: str, kind: str, observed_at: str,
                    available_at: str, description: str = "", actor_key: str = "",
                    counterparty_key: str = "", instrument_key: str = "", action: str = "",
                    reason: str = "", reason_basis: str = "unknown", amount: str | None = None,
                    unit: str = "", currency: str = "", basis: str = "observed",
                    occurred_at: str = "", source_url: str = "", evidence: str = "") -> int:
    _observation_text(source, "source", 256, True)
    _observation_text(external_id, "external_id", 512, True)
    _observation_text(kind, "kind", 128, True)
    _observation_text(description, "description", 4096)
    for label, value in (("actor_key", actor_key), ("counterparty_key", counterparty_key),
                         ("instrument_key", instrument_key), ("action", action),
                         ("reason", reason), ("unit", unit), ("currency", currency),
                         ("occurred_at", occurred_at), ("source_url", source_url),
                         ("evidence", evidence)):
        _observation_text(value, label, 4096 if label == "source_url" else 1024)
    _observation_text(observed_at, "observed_at", 64, True)
    _observation_text(available_at, "available_at", 64, True)
    if reason_basis not in OBSERVATION_REASON_BASES:
        raise ValueError("reason_basis must be unknown, stated_reason, documented_mandate or "
                         "analyst_hypothesis")
    if reason_basis == "unknown":
        if reason.strip():
            raise ValueError("an unknown reason basis must not carry reason text")
    elif not reason.strip():
        raise ValueError("an attributed reason basis requires reason text")
    if basis not in OBSERVATION_BASES:
        raise ValueError("basis must be observed or estimated")
    if amount is not None:
        _observation_text(amount, "amount", 64, True)
        if not _OBSERVATION_AMOUNT_RE.fullmatch(amount.strip()):
            raise ValueError("amount must be a finite decimal string")
        try:
            number = Decimal(amount.strip())
        except InvalidOperation as exc:
            raise ValueError("amount must be a finite decimal string") from exc
        if not number.is_finite() or not -12 <= number.adjusted() <= 12:
            raise ValueError("amount outside numeric bounds")
        if not unit.strip():
            raise ValueError("a measured observation requires a unit")
        amount = str(number)
    row = conn.execute(
        "INSERT INTO observations(source, external_id, kind, description, actor_key,"
        " counterparty_key, instrument_key, action, reason, reason_basis, amount, unit, currency,"
        " basis, occurred_at, observed_at, available_at, source_url, evidence)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(source, external_id) DO UPDATE SET kind=excluded.kind,"
        " description=excluded.description, actor_key=excluded.actor_key,"
        " counterparty_key=excluded.counterparty_key, instrument_key=excluded.instrument_key,"
        " action=excluded.action, reason=excluded.reason, reason_basis=excluded.reason_basis,"
        " amount=excluded.amount, unit=excluded.unit, currency=excluded.currency,"
        " basis=excluded.basis, occurred_at=excluded.occurred_at, observed_at=excluded.observed_at,"
        " available_at=excluded.available_at, source_url=excluded.source_url,"
        " evidence=excluded.evidence RETURNING id",
        (source, external_id, kind, description, actor_key, counterparty_key, instrument_key,
         action, reason, reason_basis, amount, unit, currency, basis, occurred_at, observed_at,
         available_at, source_url, evidence),
    ).fetchone()
    return int(row[0])


def list_observations(conn, entity_id: int | None = None, limit: int = 50) -> list[dict]:
    if entity_id is not None:
        if type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1:
            raise ValueError("entity id must be a positive SQLite integer")
        entity = get_entity(conn, entity_id)
        if entity is None:
            raise ValueError(f"entity {entity_id} not found")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer from 1 to 1000")
    columns = ("id, source, external_id, kind, description, actor_key, counterparty_key,"
               " instrument_key, action, reason, reason_basis, amount, unit, currency, basis,"
               " occurred_at, observed_at, available_at, source_url, evidence, created_at")
    if entity_id is None:
        sql = f"SELECT {columns} FROM observations ORDER BY observed_at, id LIMIT ?"
        params: tuple = (limit,)
    else:
        key = entity["key"]
        sql = (f"SELECT {columns} FROM observations WHERE instrument_key=? OR actor_key=?"
               " OR counterparty_key=? ORDER BY observed_at, id LIMIT ?")
        params = (key, key, key, limit)
    return [dict(row) for row in conn.execute(sql, params)]


def observations_for_instrument(conn, entity_id: int, limit: int = 10_000) -> list[dict]:
    if type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1:
        raise ValueError("entity id must be a positive SQLite integer")
    entity = get_entity(conn, entity_id)
    if entity is None:
        raise ValueError(f"entity {entity_id} not found")
    if type(limit) is not int or not 1 <= limit <= 100_000:
        raise ValueError("limit must be an integer from 1 to 100000")
    columns = ("id, source, external_id, kind, description, actor_key, counterparty_key,"
               " instrument_key, action, reason, reason_basis, amount, unit, currency, basis,"
               " occurred_at, observed_at, available_at, source_url, evidence")
    rows = conn.execute(
        f"SELECT {columns} FROM observations WHERE instrument_key=? ORDER BY observed_at, id LIMIT ?",
        (entity["key"], limit)).fetchall()
    return [dict(row) for row in rows]


MAX_FLOWS_SUMMARY = 100_000


def money_flow_summary(conn, entity_id: int, limit: int = 50) -> dict:
    if type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1:
        raise ValueError("entity id must be a positive SQLite integer")
    if get_entity(conn, entity_id) is None:
        raise ValueError(f"entity {entity_id} not found")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer from 1 to 1000")
    rows = conn.execute(
        "SELECT f.amount, f.currency, f.src_id, f.dst_id, se.name AS src_name,"
        " de.name AS dst_name FROM money_flows f JOIN entities se ON se.id=f.src_id"
        " JOIN entities de ON de.id=f.dst_id WHERE f.src_id=? OR f.dst_id=?",
        (entity_id, entity_id)).fetchall()
    truncated = len(rows) > MAX_FLOWS_SUMMARY
    rows = rows[:MAX_FLOWS_SUMMARY]
    totals: dict[str, dict] = {}
    partners: dict[str, dict] = {}
    with localcontext(Context(prec=128, rounding=ROUND_HALF_EVEN)):
        for row in rows:
            amount = Decimal(row["amount"])
            currency = row["currency"]
            bucket = totals.setdefault(currency, {"inflow": Decimal(0), "outflow": Decimal(0),
                                                  "inflow_count": 0, "outflow_count": 0})
            partner_map = partners.setdefault(currency, {})
            if row["dst_id"] == entity_id:
                bucket["inflow"] += amount
                bucket["inflow_count"] += 1
                other_id, other_name, key = row["src_id"], row["src_name"], "inflow"
            else:
                bucket["outflow"] += amount
                bucket["outflow_count"] += 1
                other_id, other_name, key = row["dst_id"], row["dst_name"], "outflow"
            entry = partner_map.setdefault(other_id, {"entity_id": other_id, "name": other_name,
                                                      "inflow": Decimal(0), "outflow": Decimal(0)})
            entry[key] += amount
        currencies = []
        for currency in sorted(totals):
            bucket = totals[currency]
            counterparty_list = [{
                "entity_id": entry["entity_id"], "name": entry["name"],
                "inflow": str(entry["inflow"]), "outflow": str(entry["outflow"]),
                "total": str(entry["inflow"] + entry["outflow"])}
                for entry in partners.get(currency, {}).values()]
            counterparty_list.sort(key=lambda item: (Decimal(item["total"]), item["entity_id"]),
                                   reverse=True)
            currencies.append({
                "currency": currency,
                "inflow": str(bucket["inflow"]), "outflow": str(bucket["outflow"]),
                "net": str(bucket["inflow"] - bucket["outflow"]),
                "inflow_count": bucket["inflow_count"], "outflow_count": bucket["outflow_count"],
                "counterparties": counterparty_list[:limit],
                "counterparties_truncated": len(counterparty_list) > limit,
            })
    return {
        "method": "documented_money_flows_v1",
        "entity_id": entity_id,
        "currencies": currencies,
        "coverage": {"flows": len(rows), "truncated": truncated,
                     "cross_currency_netting": False},
        "limitations": [
            "Only explicitly documented flows between registry entities are summed; a missing flow "
            "is unknown coverage, not evidence of no transfer.",
            "Amounts are summed only within the same currency; no cross-currency conversion or "
            "netting is performed.",
            "Net figures are per-currency sums of documented flows, not balances, positions, "
            "exposures or account statements, and they imply nothing about control.",
        ],
    }


# --- volatility instances ---

def add_volatility_instance(conn, *, instrument_key: str, as_of: str, target_time: str,
                            start_price: str, end_price: str, magnitude_percent: str,
                            direction: str, episode_type: str, regime: str,
                            threshold_percent: int, start_index: int, end_index: int,
                            features_hash: str = "", observed_at: str = "",
                            available_at: str = "", source_url: str = "",
                            evidence: str = "") -> int:
    for label, value in (("instrument_key", instrument_key), ("as_of", as_of),
                          ("target_time", target_time), ("start_price", start_price),
                          ("end_price", end_price), ("magnitude_percent", magnitude_percent),
                          ("direction", direction), ("episode_type", episode_type),
                          ("observed_at", observed_at), ("available_at", available_at)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be nonempty text")
    if direction not in ("up", "down", "flat"):
        raise ValueError("direction must be up, down, or flat")
    if episode_type not in ("pump", "dump"):
        raise ValueError("episode_type must be pump or dump")
    if regime and regime not in ("bull", "bear", "range", "unknown"):
        raise ValueError("regime must be bull, bear, range, or unknown")
    if type(threshold_percent) is not int or threshold_percent < 1:
        raise ValueError("threshold_percent must be a positive integer")
    if type(start_index) is not int or start_index < 0:
        raise ValueError("start_index must be a non-negative integer")
    if type(end_index) is not int or end_index < start_index:
        raise ValueError("end_index must be >= start_index")
    row = conn.execute(
        """INSERT INTO volatility_instances(
            instrument_key, as_of, target_time, start_price, end_price,
            magnitude_percent, direction, episode_type, regime, threshold_percent,
            start_index, end_index, features_hash, observed_at, available_at,
            source_url, evidence)
         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
         RETURNING id""",
        (instrument_key, as_of, target_time, start_price, end_price,
         magnitude_percent, direction, episode_type, regime or "unknown", threshold_percent,
         start_index, end_index, features_hash, observed_at, available_at,
         source_url, evidence),
    ).fetchone()
    return int(row[0])


def list_volatility_instances(conn, instrument_key: str | None = None,
                              limit: int = 100) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer from 1 to 1000")
    sql = "SELECT * FROM volatility_instances"
    params: list = []
    if instrument_key:
        sql += " WHERE instrument_key=?"
        params.append(instrument_key)
    sql += " ORDER BY as_of DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in conn.execute(sql, params)]


def get_volatility_instance(conn, volatility_id: int) -> dict | None:
    if type(volatility_id) is not int or not 1 <= volatility_id <= 2**63 - 1:
        raise ValueError("volatility_id must be a positive SQLite integer")
    row = conn.execute("SELECT * FROM volatility_instances WHERE id=?", (volatility_id,)).fetchone()
    return dict(row) if row else None


# --- event-volatility links ---

def add_event_volatility_link(conn, *, event_id: int, volatility_id: int,
                              confidence: str = "0.0", time_delta_seconds: int = 0,
                              evidence_basis: str = "temporal_proximity") -> None:
    if type(event_id) is not int or not 1 <= event_id <= 2**63 - 1:
        raise ValueError("event_id must be a positive SQLite integer")
    if type(volatility_id) is not int or not 1 <= volatility_id <= 2**63 - 1:
        raise ValueError("volatility_id must be a positive SQLite integer")
    if type(time_delta_seconds) is not int:
        raise ValueError("time_delta_seconds must be an integer")
    conn.execute(
        """INSERT OR IGNORE INTO event_volatility_links(
            event_id, volatility_id, confidence, time_delta_seconds, evidence_basis)
         VALUES(?,?,?,?,?)""",
        (event_id, volatility_id, confidence, time_delta_seconds, evidence_basis),
    )


def list_event_volatility_links(conn, event_id: int | None = None,
                                volatility_id: int | None = None,
                                limit: int = 100) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer from 1 to 1000")
    sql = "SELECT * FROM event_volatility_links"
    params: list = []
    conditions = []
    if event_id is not None:
        if type(event_id) is not int or not 1 <= event_id <= 2**63 - 1:
            raise ValueError("event_id must be a positive SQLite integer")
        conditions.append("event_id=?")
        params.append(event_id)
    if volatility_id is not None:
        if type(volatility_id) is not int or not 1 <= volatility_id <= 2**63 - 1:
            raise ValueError("volatility_id must be a positive SQLite integer")
        conditions.append("volatility_id=?")
        params.append(volatility_id)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY time_delta_seconds LIMIT ?"
    params.append(limit)
    return [dict(row) for row in conn.execute(sql, params)]


# --- price bar history ---

def _decimal_text(value, label, required=True):
    _observation_text(value, label, 64, required)
    if not value.strip():
        return ""
    if not _OBSERVATION_AMOUNT_RE.fullmatch(value.strip()):
        raise ValueError(f"{label} must be a finite decimal string")
    try:
        number = Decimal(value.strip())
    except InvalidOperation as exc:
        raise ValueError(f"{label} must be a finite decimal string") from exc
    if not number.is_finite() or number.adjusted() < -18 or number.adjusted() > 18:
        raise ValueError(f"{label} is outside the supported numeric range")
    return str(number)


# --- instruments ---

ADJUSTMENT_BASES = ("adjusted", "unadjusted", "unknown")


def add_instrument(conn, *, entity_id: int, symbol: str, asset_class: str, venue: str = "",
                   quote_currency: str = "", contract_multiplier: str = "",
                   expiry: str = "", adjustment_basis: str = "unknown",
                   rights_basis: str = "unknown", rights_verified: bool = False,
                   first_seen_at: str = "", last_seen_at: str = "", notes: str = "") -> int:
    """Record what a tradable thing *is*, so price rows can be compared across venues.

    A symbol is not an identity: `GC=F` is a continuous futures series and not a
    fixed-expiry contract, `BTCUSDT` on two venues is two instruments, and a
    back-adjusted series is not the unadjusted one. That distinction was previously
    carried only inside an `evidence` blob, which nothing could join or filter on.
    """
    if type(entity_id) is not int or entity_id <= 0:
        raise ValueError("entity_id must be a positive integer")
    if not conn.execute("SELECT 1 FROM entities WHERE id=?", (entity_id,)).fetchone():
        raise ValueError(f"entity {entity_id} not found")
    _observation_text(symbol, "symbol", 128, True)
    _observation_text(asset_class, "asset_class", 64, True)
    _observation_text(venue, "venue", 256)
    _observation_text(quote_currency, "quote_currency", 32)
    _observation_text(contract_multiplier, "contract_multiplier", 64)
    _observation_text(expiry, "expiry", 64)
    _observation_text(rights_basis, "rights_basis", 256)
    _observation_text(first_seen_at, "first_seen_at", 64)
    _observation_text(last_seen_at, "last_seen_at", 64)
    _observation_text(notes, "notes")
    if adjustment_basis not in ADJUSTMENT_BASES:
        raise ValueError(f"adjustment_basis must be one of {', '.join(ADJUSTMENT_BASES)}")
    if type(rights_verified) is not bool:
        raise ValueError("rights_verified must be a boolean")
    row = conn.execute(
        """INSERT INTO instruments(entity_id, symbol, venue, asset_class, quote_currency,
             contract_multiplier, expiry, adjustment_basis, rights_basis, rights_verified,
             first_seen_at, last_seen_at, notes)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(symbol, venue, asset_class) DO UPDATE SET
             entity_id=excluded.entity_id, quote_currency=excluded.quote_currency,
             contract_multiplier=excluded.contract_multiplier, expiry=excluded.expiry,
             adjustment_basis=excluded.adjustment_basis, rights_basis=excluded.rights_basis,
             rights_verified=excluded.rights_verified, last_seen_at=excluded.last_seen_at,
             notes=excluded.notes
           RETURNING id""",
        (entity_id, symbol.strip(), venue.strip(), asset_class.strip(), quote_currency.strip(),
         contract_multiplier.strip(), expiry.strip(), adjustment_basis, rights_basis,
         1 if rights_verified else 0, first_seen_at, last_seen_at, notes),
    ).fetchone()
    return int(row[0])


def list_instruments(conn, *, entity_id: int | None = None, asset_class: str = "",
                     symbol: str = "", limit: int = 1000) -> list[dict]:
    if entity_id is not None and (type(entity_id) is not int or entity_id <= 0):
        raise ValueError("entity_id must be a positive integer or None")
    _observation_text(asset_class, "asset_class", 64)
    _observation_text(symbol, "symbol", 128)
    if type(limit) is not int or not 1 <= limit <= 5000:
        raise ValueError("limit must be an integer from 1 to 5000")
    sql = ("SELECT i.*, e.key AS entity_key FROM instruments i"
           " JOIN entities e ON e.id = i.entity_id")
    params: list = []
    if entity_id is not None:
        sql += " WHERE i.entity_id=?"
        params.append(entity_id)
    if asset_class:
        sql += (" AND" if entity_id is not None else " WHERE") + " i.asset_class=?"
        params.append(asset_class)
    if symbol:
        sql += (" AND" if (entity_id is not None or asset_class) else " WHERE") + " i.symbol=?"
        params.append(symbol)
    sql += " ORDER BY i.asset_class, i.symbol, i.venue LIMIT ?"
    params.append(limit)
    return [dict(row) for row in conn.execute(sql, params)]


def get_instrument(conn, entity_key: str, *, venue: str = "", asset_class: str = "") -> dict | None:
    """The instrument row for an entity, or None.

    `venue` and `asset_class` disambiguate only when they are given. Guessing a
    default venue would silently pick one of several series for the same entity.
    """
    _observation_text(entity_key, "entity_key", 512, True)
    _observation_text(venue, "venue", 256)
    _observation_text(asset_class, "asset_class", 64)
    sql = ("SELECT i.*, e.key AS entity_key FROM instruments i"
           " JOIN entities e ON e.id = i.entity_id WHERE e.key=?")
    params: list = [entity_key]
    if venue:
        sql += " AND i.venue=?"
        params.append(venue)
    if asset_class:
        sql += " AND i.asset_class=?"
        params.append(asset_class)
    sql += " ORDER BY i.id LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row is not None else None


# --- volatility estimates ---

def add_volatility_estimate(conn, *, instrument_key: str, interval_seconds: int, estimator: str,
                            window_bars: int, as_of: str, window_start: str,
                            volatility_percent: str, annualized_percent: str, variance: str,
                            basis: str = "per_bar", annualization: str = "", ddof: int = 0,
                            convention: str = "{}", baseline_bars: int = 0,
                            observed_at: str, available_at: str, source_url: str = "",
                            evidence: str = "") -> int:
    """Store one volatility estimate together with the convention that produced it.

    The estimator name alone is not reproducible: close-to-close differs by drift
    handling and ddof, Yang-Zhang differs by its weight `k`, ATR by its seed, and
    Bollinger by using the population rather than sample deviation. So the
    convention is stored as data rather than left in a docstring.
    """
    _observation_text(instrument_key, "instrument_key", 512, True)
    if type(interval_seconds) is not int or interval_seconds < 60:
        raise ValueError("interval_seconds must be an integer of at least 60")
    _observation_text(estimator, "estimator", 64, True)
    if type(window_bars) is not int or window_bars < 2:
        raise ValueError("window_bars must be an integer of at least 2")
    if type(ddof) is not int or ddof < 0:
        raise ValueError("ddof must be a nonnegative integer")
    if type(baseline_bars) is not int or baseline_bars < 0:
        raise ValueError("baseline_bars must be a nonnegative integer")
    if basis not in ("per_bar", "window", "per_year"):
        raise ValueError("basis must be per_bar, window or per_year")
    for label, value in (("as_of", as_of), ("window_start", window_start),
                         ("observed_at", observed_at), ("available_at", available_at)):
        _observation_text(value, label, 64, True)
    _observation_text(annualization, "annualization", 64)
    _observation_text(source_url, "source_url", 4096)
    _observation_text(evidence, "evidence")
    _observation_text(convention, "convention", 4096)
    if convention.strip() and not convention.strip().startswith("{"):
        raise ValueError("convention must be a JSON object when given")
    numbers = {name: _decimal_text(value, name) for name, value in
               (("volatility_percent", volatility_percent),
                ("annualized_percent", annualized_percent), ("variance", variance))}
    row = conn.execute(
        """INSERT INTO volatility_estimates(instrument_key, interval_seconds, estimator,
             window_bars, as_of, window_start, volatility_percent, annualized_percent,
             variance, basis, annualization, ddof, convention, baseline_bars,
             observed_at, available_at, source_url, evidence)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(instrument_key, interval_seconds, estimator, window_bars, as_of)
           DO UPDATE SET
             window_start=excluded.window_start,
             volatility_percent=excluded.volatility_percent,
             annualized_percent=excluded.annualized_percent, variance=excluded.variance,
             basis=excluded.basis, annualization=excluded.annualization,
             ddof=excluded.ddof, convention=excluded.convention,
             baseline_bars=excluded.baseline_bars, observed_at=excluded.observed_at,
             available_at=excluded.available_at, source_url=excluded.source_url,
             evidence=excluded.evidence
           RETURNING id""",
        (instrument_key, interval_seconds, estimator, window_bars, as_of, window_start,
         numbers["volatility_percent"], numbers["annualized_percent"], numbers["variance"],
         basis, annualization, ddof, convention, baseline_bars, observed_at, available_at,
         source_url, evidence),
    ).fetchone()
    return int(row[0])


def list_volatility_estimates(conn, instrument_key: str, interval_seconds: int, *,
                              estimator: str = "", start: str = "", end: str = "",
                              limit: int = 1000, order: str = "desc") -> list[dict]:
    _observation_text(instrument_key, "instrument_key", 512, True)
    if type(interval_seconds) is not int or interval_seconds < 60:
        raise ValueError("interval_seconds must be an integer of at least 60")
    _observation_text(estimator, "estimator", 64)
    if type(limit) is not int or not 1 <= limit <= 20000:
        raise ValueError("limit must be an integer from 1 to 20000")
    if order not in ("asc", "desc"):
        raise ValueError("order must be asc or desc")
    sql = ("SELECT * FROM volatility_estimates WHERE instrument_key=? AND interval_seconds=?")
    params: list = [instrument_key, interval_seconds]
    if estimator:
        _observation_text(estimator, "estimator", 64, True)
        sql += " AND estimator=?"
        params.append(estimator)
    if start:
        _observation_text(start, "start", 64, True)
        sql += " AND as_of>=?"
        params.append(start)
    if end:
        _observation_text(end, "end", 64, True)
        sql += " AND as_of<=?"
        params.append(end)
    sql += f" ORDER BY as_of {order.upper()}, id {order.upper()} LIMIT ?"
    params.append(limit)
    return [dict(row) for row in conn.execute(sql, params)]


# --- context measures ---

CONTEXT_MEASURES = ("change", "zscore")


def add_context_measure(conn, *, source_kind: str, measure: str, value: str,
                        observed_at: str, available_at: str, instrument_key: str = "",
                        baseline_observations: int = 0, unit: str = "",
                        prior_observed_at: str = "", cadence_seconds: int = 0,
                        baseline_mean: str = "", baseline_deviation: str = "",
                        ddof: int = 0, source: str = "", source_url: str = "",
                        evidence: str = "") -> int:
    """Store one stationary quantity derived from a stored context series.

    The measure name is not reproducible on its own, for the same reason the
    volatility estimator name is not: a difference needs the interval it spans,
    and a z-score needs the baseline it was taken against and the degrees of
    freedom that produced its spread. All of that is stored as data, and
    `baseline_observations` is part of the uniqueness key so a score against a
    longer baseline cannot silently overwrite one against a shorter one.

    These rows are deliberately not `observations`. A derived quantity is not a
    second reading of the world, and putting it in that table would let a
    difference and a level be listed side by side as if both had been observed.
    """
    _observation_text(source_kind, "source_kind", 128, True)
    _observation_text(instrument_key, "instrument_key", 512)
    _observation_text(measure, "measure", 64, True)
    if measure not in CONTEXT_MEASURES:
        raise ValueError(f"measure must be one of {', '.join(CONTEXT_MEASURES)}")
    if type(baseline_observations) is not int or baseline_observations < 0:
        raise ValueError("baseline_observations must be a nonnegative integer")
    if type(cadence_seconds) is not int or cadence_seconds < 0:
        raise ValueError("cadence_seconds must be a nonnegative integer")
    if type(ddof) is not int or ddof < 0:
        raise ValueError("ddof must be a nonnegative integer")
    if measure == "change" and baseline_observations:
        raise ValueError("a difference is taken between two points and has no baseline length")
    if measure == "zscore" and baseline_observations < 2:
        raise ValueError("a z-score needs at least two baseline observations")
    _observation_text(unit, "unit", 128)
    _observation_text(prior_observed_at, "prior_observed_at", 64)
    _observation_text(source, "source", 256)
    _observation_text(source_url, "source_url", 4096)
    _observation_text(evidence, "evidence")
    for label, moment in (("observed_at", observed_at), ("available_at", available_at)):
        _observation_text(moment, label, 64, True)
    if available_at < observed_at:
        raise ValueError("availability cannot precede observation")
    numbers = {"value": _decimal_text(value, "value"),
               "baseline_mean": _decimal_text(baseline_mean, "baseline_mean", required=False),
               "baseline_deviation": _decimal_text(baseline_deviation, "baseline_deviation",
                                                   required=False)}
    row = conn.execute(
        """INSERT INTO context_measures(source_kind, instrument_key, measure,
             baseline_observations, value, unit, prior_observed_at, cadence_seconds,
             baseline_mean, baseline_deviation, ddof, source, observed_at, available_at,
             source_url, evidence)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(source_kind, instrument_key, measure, baseline_observations, observed_at)
           DO UPDATE SET
             value=excluded.value, unit=excluded.unit,
             prior_observed_at=excluded.prior_observed_at,
             cadence_seconds=excluded.cadence_seconds,
             baseline_mean=excluded.baseline_mean,
             baseline_deviation=excluded.baseline_deviation, ddof=excluded.ddof,
             source=excluded.source, available_at=excluded.available_at,
             source_url=excluded.source_url, evidence=excluded.evidence
           RETURNING id""",
        (source_kind, instrument_key, measure, baseline_observations, numbers["value"],
         unit, prior_observed_at, cadence_seconds, numbers["baseline_mean"],
         numbers["baseline_deviation"], ddof, source, observed_at, available_at,
         source_url, evidence),
    ).fetchone()
    return int(row[0])


def context_measure_series(conn) -> list[dict]:
    """Every stored measure series, with the span and count it actually covers."""
    rows = conn.execute(
        """SELECT source_kind, instrument_key, measure, baseline_observations, ddof,
                  COUNT(*) AS observations, MIN(observed_at) AS first, MAX(available_at) AS last,
                  MIN(unit) AS unit, MIN(source) AS source
           FROM context_measures
           GROUP BY source_kind, instrument_key, measure, baseline_observations, ddof
           ORDER BY source_kind, instrument_key, measure, baseline_observations"""
    ).fetchall()
    return [dict(row) for row in rows]


def list_context_measures(conn, source_kind: str, *, instrument_key: str = "",
                          measure: str = "", baseline_observations: int | None = None,
                          start: str = "", end: str = "", limit: int = 1000,
                          order: str = "desc") -> list[dict]:
    _observation_text(source_kind, "source_kind", 128, True)
    _observation_text(instrument_key, "instrument_key", 512)
    _observation_text(measure, "measure", 64)
    if measure and measure not in CONTEXT_MEASURES:
        raise ValueError(f"measure must be one of {', '.join(CONTEXT_MEASURES)}")
    if baseline_observations is not None and (
            type(baseline_observations) is not int or baseline_observations < 0):
        raise ValueError("baseline_observations must be a nonnegative integer")
    if type(limit) is not int or not 1 <= limit <= 20000:
        raise ValueError("limit must be an integer from 1 to 20000")
    if order not in ("asc", "desc"):
        raise ValueError("order must be asc or desc")
    sql = "SELECT * FROM context_measures WHERE source_kind=?"
    params: list = [source_kind]
    if instrument_key:
        sql += " AND instrument_key=?"
        params.append(instrument_key)
    if measure:
        sql += " AND measure=?"
        params.append(measure)
    if baseline_observations is not None:
        sql += " AND baseline_observations=?"
        params.append(baseline_observations)
    if start:
        _observation_text(start, "start", 64, True)
        sql += " AND observed_at>=?"
        params.append(start)
    if end:
        _observation_text(end, "end", 64, True)
        sql += " AND observed_at<=?"
        params.append(end)
    sql += f" ORDER BY observed_at {order.upper()}, id {order.upper()} LIMIT ?"
    params.append(limit)
    return [dict(row) for row in conn.execute(sql, params)]


# `open`, `high`, `low` and `close` are the provider and schema field names, so
# this published signature keeps its spelling; the builtin-shadowing rule exists
# to catch accidents, not to rename an interface every caller uses.
def add_price_bar(conn, *, instrument_key: str, interval_seconds: int, open_time: str,
                  close_time: str, open: str, high: str,  # noqa: A002
                  low: str, close: str, volume: str, source: str, retrieved_at: str,
                  quote_volume: str = "",
                  open_interest: str = "",
                  trades: int = 0, source_url: str = "", evidence: str = "") -> int:
    # `open`, `high`, `low` and `close` are the OHLC field names used by every
    # provider and by the schema, so they keep their spelling; the rule exists
    # to catch accidents, not to rename a published interface.
    _observation_text(instrument_key, "instrument_key", 512, True)
    if type(interval_seconds) is not int or interval_seconds < 60:
        raise ValueError("interval_seconds must be an integer of at least 60")
    for label, value in (("open_time", open_time), ("close_time", close_time),
                         ("source", source), ("retrieved_at", retrieved_at)):
        _observation_text(value, label, 64, True)
    _observation_text(source_url, "source_url", 4096)
    _observation_text(evidence, "evidence")
    if type(trades) is not int or not 0 <= trades <= 2**63 - 1:
        raise ValueError("trades must be a nonnegative SQLite integer")
    numbers = {name: _decimal_text(value, name) for name, value in
               (("open", open), ("high", high), ("low", low), ("close", close),
                ("volume", volume))}
    numbers["quote_volume"] = _decimal_text(quote_volume, "quote_volume", required=False)
    numbers["open_interest"] = _decimal_text(open_interest, "open_interest", required=False)
    with localcontext(Context(prec=60, rounding=ROUND_HALF_EVEN)):
        if min(Decimal(numbers["low"]), Decimal(numbers["close"]),
               Decimal(numbers["open"]), Decimal(numbers["high"])) <= 0:
            raise ValueError("open, high, low and close must be positive")
        if Decimal(numbers["high"]) < Decimal(numbers["low"]):
            raise ValueError("high must not be below low")
        if not (Decimal(numbers["low"]) <= Decimal(numbers["open"]) <= Decimal(numbers["high"])):
            raise ValueError("open must lie within the low..high range")
        if not (Decimal(numbers["low"]) <= Decimal(numbers["close"]) <= Decimal(numbers["high"])):
            raise ValueError("close must lie within the low..high range")
        if Decimal(numbers["volume"]) < 0 or Decimal(numbers["quote_volume"] or 0) < 0:
            raise ValueError("volume must not be negative")
        if numbers["open_interest"] and Decimal(numbers["open_interest"]) < 0:
            raise ValueError("open interest must not be negative")
    row = conn.execute(
        """INSERT INTO price_bars(instrument_key, interval_seconds, open_time, close_time,
             open, high, low, close, volume, quote_volume, open_interest, trades, source,
             source_url, retrieved_at, evidence)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(instrument_key, interval_seconds, open_time) DO UPDATE SET
             close_time=excluded.close_time, open=excluded.open, high=excluded.high,
             low=excluded.low, close=excluded.close, volume=excluded.volume,
             quote_volume=excluded.quote_volume, open_interest=excluded.open_interest,
             trades=excluded.trades, source=excluded.source,
             source_url=excluded.source_url,
             retrieved_at=excluded.retrieved_at, evidence=excluded.evidence
           RETURNING id""",
        (instrument_key, interval_seconds, open_time, close_time, numbers["open"],
         numbers["high"], numbers["low"], numbers["close"], numbers["volume"],
         numbers["quote_volume"], numbers["open_interest"], trades, source, source_url,
         retrieved_at, evidence),
    ).fetchone()
    return int(row[0])


def list_price_bars(conn, instrument_key: str, interval_seconds: int, *, start: str = "",
                    end: str = "", limit: int = 10000, order: str = "asc") -> list[dict]:
    _observation_text(instrument_key, "instrument_key", 512, True)
    if type(interval_seconds) is not int or interval_seconds < 60:
        raise ValueError("interval_seconds must be an integer of at least 60")
    if type(limit) is not int or not 1 <= limit <= 20000:
        raise ValueError("limit must be an integer from 1 to 20000")
    if order not in ("asc", "desc"):
        raise ValueError("order must be asc or desc")
    sql = ("SELECT * FROM price_bars WHERE instrument_key=? AND interval_seconds=?")
    params: list = [instrument_key, interval_seconds]
    if start:
        _observation_text(start, "start", 64, True)
        sql += " AND open_time>=?"
        params.append(start)
    if end:
        _observation_text(end, "end", 64, True)
        sql += " AND open_time<=?"
        params.append(end)
    sql += f" ORDER BY open_time {order.upper()} LIMIT ?"
    params.append(limit)
    return [dict(row) for row in conn.execute(sql, params)]


# --- move events and causes ---

MOVE_TIER_RE = re.compile(r"p[1-9][0-9]{0,3}")


def move_tier(threshold_percent) -> str:
    """Percent label for a tier, so one move can be recorded at every level."""
    if type(threshold_percent) is not int or not 1 <= threshold_percent <= 1000:
        raise ValueError("threshold percent must be an integer from 1 to 1000")
    return f"p{threshold_percent}"


def move_tier_percent(tier) -> int | None:
    if not isinstance(tier, str) or not MOVE_TIER_RE.fullmatch(tier):
        return None
    return int(tier[1:])


def add_move_event(conn, *, instrument_key: str, interval_seconds: int, move_hours: int,
                   tier: str, threshold_percent: str, direction: str, start_time: str,
                   end_time: str, start_price: str, end_price: str, change_percent: str,
                   terminal_bar_range_percent: str, baseline_mean_percent: str,
                   baseline_std_percent: str, z_score: str, detected_at: str,
                   available_at: str, baseline_percentile: str | None = None,
                   baseline_bars: int = 0, source_url: str = "", evidence: str = "") -> int:
    _observation_text(instrument_key, "instrument_key", 512, True)
    if type(interval_seconds) is not int or interval_seconds < 60:
        raise ValueError("interval_seconds must be an integer of at least 60")
    if type(move_hours) is not int or not 1 <= move_hours <= 24 * 365:
        raise ValueError("move_hours must be an integer from 1 to 8760")
    if move_tier_percent(tier) is None:
        raise ValueError("tier must be a percent label such as p3, p5, p7 or p11")
    if direction not in ("up", "down"):
        raise ValueError("direction must be up or down")
    if type(baseline_bars) is not int or not 0 <= baseline_bars <= 2**31 - 1:
        raise ValueError("baseline_bars must be a nonnegative integer")
    for label, value in (("start_time", start_time), ("end_time", end_time),
                         ("detected_at", detected_at), ("available_at", available_at)):
        _observation_text(value, label, 64, True)
    _observation_text(source_url, "source_url", 4096)
    _observation_text(evidence, "evidence")
    numbers = {name: _decimal_text(value, name) for name, value in (
        ("threshold_percent", threshold_percent), ("start_price", start_price),
        ("end_price", end_price), ("change_percent", change_percent),
        ("terminal_bar_range_percent", terminal_bar_range_percent),
        ("baseline_mean_percent", baseline_mean_percent),
        ("baseline_std_percent", baseline_std_percent), ("z_score", z_score))}
    percentile = None
    if baseline_percentile is not None:
        _observation_text(baseline_percentile, "baseline_percentile", 64, True)
        value = _decimal_text(baseline_percentile, "baseline_percentile")
        if not Decimal(0) <= Decimal(value) <= Decimal(100):
            raise ValueError("baseline_percentile must lie between 0 and 100")
        percentile = value
    row = conn.execute(
        """INSERT INTO move_events(instrument_key, interval_seconds, move_hours, tier,
             threshold_percent, direction, start_time, end_time, start_price, end_price,
             change_percent, terminal_bar_range_percent, baseline_mean_percent,
             baseline_std_percent, z_score, baseline_percentile, baseline_bars, detected_at,
             available_at, source_url, evidence)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(instrument_key, interval_seconds, move_hours, tier, start_time, end_time)
           DO UPDATE SET change_percent=excluded.change_percent,
             terminal_bar_range_percent=excluded.terminal_bar_range_percent,
             baseline_mean_percent=excluded.baseline_mean_percent,
             baseline_std_percent=excluded.baseline_std_percent, z_score=excluded.z_score,
             baseline_percentile=excluded.baseline_percentile,
             baseline_bars=excluded.baseline_bars, detected_at=excluded.detected_at,
             available_at=excluded.available_at, source_url=excluded.source_url,
             evidence=excluded.evidence
           RETURNING id""",
        (instrument_key, interval_seconds, move_hours, tier, numbers["threshold_percent"],
         direction, start_time, end_time, numbers["start_price"], numbers["end_price"],
         numbers["change_percent"], numbers["terminal_bar_range_percent"],
         numbers["baseline_mean_percent"], numbers["baseline_std_percent"], numbers["z_score"],
         percentile, baseline_bars, detected_at, available_at, source_url, evidence),
    ).fetchone()
    return int(row[0])


def list_move_events(conn, instrument_key: str, interval_seconds: int, move_hours: int, *,
                     tier: str | None = None, direction: str | None = None,
                     start: str = "", end: str = "", limit: int = 1000) -> list[dict]:
    _observation_text(instrument_key, "instrument_key", 512, True)
    if type(interval_seconds) is not int or interval_seconds < 60:
        raise ValueError("interval_seconds must be an integer of at least 60")
    if type(move_hours) is not int or not 1 <= move_hours <= 24 * 365:
        raise ValueError("move_hours must be an integer from 1 to 8760")
    if type(limit) is not int or not 1 <= limit <= 20000:
        raise ValueError("limit must be an integer from 1 to 20000")
    if tier is not None and move_tier_percent(tier) is None:
        raise ValueError("tier must be a percent label such as p3, p5, p7 or p11")
    if direction is not None and direction not in ("up", "down"):
        raise ValueError("direction must be up or down")
    sql = ("SELECT * FROM move_events WHERE instrument_key=? AND interval_seconds=?"
           " AND move_hours=?")
    params: list = [instrument_key, interval_seconds, move_hours]
    if tier:
        sql += " AND tier=?"
        params.append(tier)
    if direction:
        sql += " AND direction=?"
        params.append(direction)
    if start:
        _observation_text(start, "start", 64, True)
        sql += " AND end_time>=?"
        params.append(start)
    if end:
        _observation_text(end, "end", 64, True)
        sql += " AND end_time<=?"
        params.append(end)
    sql += " ORDER BY end_time LIMIT ?"
    params.append(limit)
    return [dict(row) for row in conn.execute(sql, params)]


CAUSE_ROLES = ("move", "control")


def add_move_cause(conn, *, role: str, category: str, article_id: str, observed_at: str,
                   headline: str, source: str, retrieved_at: str, move_event_id: int | None = None,
                   control_key: str = "", domain: str = "", source_url: str = "",
                   matched_terms: str = "[]") -> int:
    if role not in CAUSE_ROLES:
        raise ValueError("role must be move or control")
    if role == "move":
        if type(move_event_id) is not int or not 1 <= move_event_id <= 2**63 - 1:
            raise ValueError("a move cause requires a positive move_event_id")
    elif move_event_id is not None or not control_key.strip():
        raise ValueError("a control cause requires a nonempty control_key and no move_event_id")
    else:
        move_event_id = None
    _observation_text(control_key, "control_key", 512)
    _observation_text(category, "category", 128, True)
    _observation_text(article_id, "article_id", 512, True)
    for label, value in (("observed_at", observed_at), ("source", source),
                         ("retrieved_at", retrieved_at)):
        _observation_text(value, label, 64, True)
    _observation_text(headline, "headline", 4096)
    _observation_text(domain, "domain", 256)
    _observation_text(source_url, "source_url", 4096)
    _observation_text(matched_terms, "matched_terms", 4096)
    row = conn.execute(
        """INSERT OR IGNORE INTO move_causes(move_event_id, control_key, role, category,
             article_id, observed_at, headline, domain, source_url, matched_terms, source,
             retrieved_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id""",
        (move_event_id, control_key, role, category, article_id, observed_at, headline,
         domain, source_url, matched_terms, source, retrieved_at),
    ).fetchone()
    return int(row[0]) if row else -1


def list_move_causes(conn, move_event_id: int | None = None, role: str | None = None, *,
                     control_key: str = "") -> list[dict]:
    if role is not None and role not in CAUSE_ROLES:
        raise ValueError("role must be move or control")
    _observation_text(control_key, "control_key", 512)
    sql = "SELECT * FROM move_causes WHERE 1=1"
    params: list = []
    if move_event_id is not None:
        if type(move_event_id) is not int or not 1 <= move_event_id <= 2**63 - 1:
            raise ValueError("move_event_id must be a positive SQLite integer")
        sql += " AND move_event_id=?"
        params.append(move_event_id)
    if role:
        sql += " AND role=?"
        params.append(role)
    if control_key.strip():
        sql += " AND control_key=?"
        params.append(control_key)
    sql += " ORDER BY observed_at, article_id, category"
    return [dict(row) for row in conn.execute(sql, params)]


def list_cause_windows(conn, role: str, instrument_key: str | None = None) -> list[dict]:
    """Group stored causes back into the windows they were collected for."""
    if role not in CAUSE_ROLES:
        raise ValueError("role must be move or control")
    if instrument_key:
        _observation_text(instrument_key, "instrument_key", 512, True)
    if role == "move":
        sql = ("SELECT m.id AS window_id, '' AS control_key, m.instrument_key, m.tier,"
               " m.direction, m.start_time, m.end_time, m.change_percent, c.category,"
               " c.article_id FROM move_causes c JOIN move_events m ON m.id = c.move_event_id")
        params: list = []
        if instrument_key:
            sql += " WHERE m.instrument_key=?"
            params.append(instrument_key)
    else:
        sql = ("SELECT NULL AS window_id, c.control_key, '' AS instrument_key, '' AS tier,"
               " '' AS direction, '' AS start_time, '' AS end_time, '' AS change_percent,"
               " c.category, c.article_id FROM move_causes c WHERE c.role='control'")
        params = []
    sql += " ORDER BY window_id, control_key, category, article_id"
    prefix = f"{instrument_key}|" if instrument_key else None
    grouped: dict[str, dict] = {}
    for row in conn.execute(sql, params):
        key = row["control_key"] if role == "control" else str(row["window_id"])
        if prefix and role == "control" and not key.startswith(prefix):
            continue
        entry = grouped.setdefault(key, {
            "key": key, "window_id": row["window_id"], "control_key": row["control_key"],
            "instrument_key": row["instrument_key"], "tier": row["tier"],
            "direction": row["direction"], "start_time": row["start_time"],
            "end_time": row["end_time"], "change_percent": row["change_percent"],
            "articles": set(), "categories": set()})
        entry["articles"].add(row["article_id"])
        entry["categories"].add(row["category"])
    result = []
    for key in sorted(grouped, key=lambda item: (grouped[item]["window_id"] is None, item)):
        entry = grouped[key]
        entry["articles"] = sorted(entry["articles"])
        entry["categories"] = sorted(entry["categories"])
        result.append(entry)
    return result


# --- time-window reads -------------------------------------------------------
#
# The question the tool kept not being able to answer is "what is stored between
# these two instants", so the window is a first-class query rather than something
# a caller filters after pulling a bounded prefix. The `limit` here is a ceiling
# on how much is returned, never a substitute for the filter: a truncated
# window is reported as truncated rather than quietly presented as complete.

OBSERVATION_COLUMNS = (
    "id, source, external_id, kind, description, actor_key, counterparty_key,"
    " instrument_key, action, reason, reason_basis, amount, unit, currency, basis,"
    " occurred_at, observed_at, available_at, source_url, evidence, created_at"
)

WINDOW_MAX_ROWS = 200_000
WINDOW_DEFAULT_LIMIT = 5_000


def _window_bounds(start: str, end: str) -> tuple[str, str]:
    for label, value in (("start", start), ("end", end)):
        _observation_text(value, label, 64, True)
    if start >= end:
        raise ValueError("start must be before end")
    return start, end


def observations_in_window(conn, *, start: str, end: str, instrument_key: str = "",
                           kinds: tuple[str, ...] = (), include_market_wide: bool = True,
                           limit: int = WINDOW_DEFAULT_LIMIT) -> dict:
    """Every observation whose occurrence falls in `[start, end)`.

    `occurred_at` is the event's own time. `available_at` is reported per row so
    a caller can see which records could have been known at the window's start
    and which only became visible later; a retrospective publication must not be
    presented as if it were available beforehand.
    """
    start, end = _window_bounds(start, end)
    if type(limit) is not int or not 1 <= limit <= WINDOW_MAX_ROWS:
        raise ValueError(f"limit must be an integer from 1 to {WINDOW_MAX_ROWS}")
    clauses = ["occurred_at >= ?", "occurred_at < ?"]
    params: list = [start, end]
    scopes = []
    if instrument_key:
        scopes.append("instrument_key = ?")
    if include_market_wide:
        scopes.append("instrument_key = ''")
    if scopes:
        clauses.append("(" + " OR ".join(scopes) + ")")
        if instrument_key:
            params.append(instrument_key)
    if kinds:
        clauses.append("kind IN (" + ",".join("?" for _ in kinds) + ")")
        params.extend(kinds)
    where = " AND ".join(clauses)
    total = conn.execute(f"SELECT count(*) FROM observations WHERE {where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT {OBSERVATION_COLUMNS} FROM observations WHERE {where}"
        " ORDER BY occurred_at, id LIMIT ?", (*params, limit)).fetchall()
    return {"rows": [dict(row) for row in rows], "total": total,
            "truncated": total > len(rows), "start": start, "end": end}


def observation_coverage(conn, *, start: str, end: str, instrument_key: str = "",
                         include_market_wide: bool = True) -> list[dict]:
    """What is stored per kind in a window, whether bound or market-wide.

    A kind that is absent is the important half of this answer: the caller needs
    to distinguish "nothing happened" from "nothing was recorded", and only the
    second can be established from the registry.
    """
    start, end = _window_bounds(start, end)
    clauses = ["occurred_at >= ?", "occurred_at < ?"]
    params: list = [start, end]
    scopes = []
    if instrument_key:
        scopes.append("instrument_key = ?")
    if include_market_wide:
        scopes.append("instrument_key = ''")
    if scopes:
        clauses.append("(" + " OR ".join(scopes) + ")")
        if instrument_key:
            params.append(instrument_key)
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"SELECT kind, count(*) AS rows, count(DISTINCT source) AS sources,"
        f" min(occurred_at) AS first, max(occurred_at) AS last,"
        " sum(CASE WHEN amount IS NOT NULL AND amount != '' THEN 1 ELSE 0 END) AS measured"
        f" FROM observations WHERE {where} GROUP BY kind ORDER BY kind", params).fetchall()
    return [dict(row) for row in rows]


def observation_kinds_ever(conn) -> dict[str, dict]:
    """Every kind this registry has ever recorded, with its span and row count."""
    rows = conn.execute(
        "SELECT kind, count(*) AS rows, count(DISTINCT source) AS sources,"
        " min(occurred_at) AS first, max(occurred_at) AS last"
        " FROM observations GROUP BY kind ORDER BY kind").fetchall()
    return {row["kind"]: dict(row) for row in rows}


def anomalies_in_window(conn, instrument_key: str, start: str, end: str,
                        limit: int = WINDOW_DEFAULT_LIMIT) -> list[dict]:
    """Recorded price anomalies for one instrument inside a window."""
    start, end = _window_bounds(start, end)
    if type(limit) is not int or not 1 <= limit <= WINDOW_MAX_ROWS:
        raise ValueError("limit must be a positive integer")
    rows = conn.execute(
        "SELECT * FROM price_anomalies WHERE instrument_key=?"
        " AND observed_at >= ? AND observed_at < ? ORDER BY observed_at, id LIMIT ?",
        (instrument_key, start, end, limit)).fetchall()
    return [dict(row) for row in rows]


def volatility_in_window(conn, instrument_key: str, start: str, end: str,
                         limit: int = WINDOW_DEFAULT_LIMIT) -> list[dict]:
    """Stored volatility instances overlapping a window, by start time."""
    start, end = _window_bounds(start, end)
    if type(limit) is not int or not 1 <= limit <= WINDOW_MAX_ROWS:
        raise ValueError("limit must be a positive integer")
    rows = conn.execute(
        "SELECT * FROM volatility_instances WHERE instrument_key=?"
        " AND as_of >= ? AND as_of < ? ORDER BY as_of, id LIMIT ?",
        (instrument_key, start, end, limit)).fetchall()
    return [dict(row) for row in rows]


def stored_bar_span(conn, instrument_key: str, interval_seconds: int) -> dict:
    """What bar history exists for an instrument, and whether it has holes.

    Answers "can this window be measured at all" before anything is measured, so
    a caller is not handed a percentage computed across a gap.
    """
    row = conn.execute(
        "SELECT count(*) AS bars, min(open_time) AS first, max(open_time) AS last"
        " FROM price_bars WHERE instrument_key=? AND interval_seconds=?",
        (instrument_key, interval_seconds)).fetchone()
    bars = int(row["bars"] or 0)
    cut = latest_prune_cut(conn, instrument_key, interval_seconds)
    removed = {"history_removed_before": cut["cut"] if cut else None,
               "history_removed_run_id": cut["id"] if cut else None,
               "history_removed_reason": cut["reason"] if cut else None}
    if not bars:
        return {"bars": 0, "first": None, "last": None, "gaps": None,
                "contiguous_fraction": None, "usable": False,
                "note": "no stored bars; run `lele fetch-history` first", **removed}
    times = [row["open_time"] for row in conn.execute(
        "SELECT open_time FROM price_bars WHERE instrument_key=? AND interval_seconds=?"
        " ORDER BY open_time", (instrument_key, interval_seconds)).fetchall()]
    # Imported here, not at module scope: the analysis package imports the
    # registry, so a top-level import would be circular. The cadence and gap
    # rules live with move detection because they are what a measured window has
    # to satisfy; this is the only place the registry needs them.
    from ..analysis.moves import _cadence, _gaps
    parsed = [datetime.fromisoformat(item) for item in times]
    cadence = _cadence(parsed, interval_seconds)
    gaps = _gaps(parsed, cadence)
    largest = max((int((parsed[index] - parsed[index - 1]).total_seconds())
                   for index in gaps), default=0)
    return {"bars": bars, "first": times[0], "last": times[-1], "cadence_seconds": cadence,
            "gaps": len(gaps), "largest_gap_seconds": largest,
            "contiguous_fraction": round(1 - len(gaps) / max(1, len(times) - 1), 6),
            "usable": not gaps, "completeness": "unknown",
            "completeness_note": "a bounded fetch cannot prove it reached the provider's "
                                 "first bar, so no frequency claim may be made from this",
            **removed}


def add_cause_scan(conn, *, instrument_key: str, interval_seconds: int, move_hours: int,
                   as_of: str, change_percent: str, observed_at: str, available_at: str,
                   tier: str | None = None, direction: str | None = None,
                   historical_moves: int = 0, current_categories: str = "[]",
                   matched_categories: str = "[]", news_articles: int = 0,
                   score: str = "0", source_url: str = "", evidence: str = "") -> int:
    _observation_text(instrument_key, "instrument_key", 512, True)
    if type(interval_seconds) is not int or interval_seconds < 60:
        raise ValueError("interval_seconds must be an integer of at least 60")
    if type(move_hours) is not int or not 1 <= move_hours <= 24 * 365:
        raise ValueError("move_hours must be an integer from 1 to 8760")
    if tier is not None and move_tier_percent(tier) is None:
        raise ValueError("tier must be a percent label such as p3, p5, p7 or p11")
    if direction is not None and direction not in ("up", "down"):
        raise ValueError("direction must be up or down")
    if type(historical_moves) is not int or not 0 <= historical_moves <= 2**31 - 1:
        raise ValueError("historical_moves must be a nonnegative integer")
    if type(news_articles) is not int or not 0 <= news_articles <= 2**31 - 1:
        raise ValueError("news_articles must be a nonnegative integer")
    for label, value in (("as_of", as_of), ("observed_at", observed_at),
                         ("available_at", available_at)):
        _observation_text(value, label, 64, True)
    _observation_text(source_url, "source_url", 4096)
    _observation_text(evidence, "evidence")
    numbers = {"change_percent": _decimal_text(change_percent, "change_percent"),
               "score": _decimal_text(score, "score")}
    row = conn.execute(
        """INSERT INTO cause_scans(instrument_key, interval_seconds, move_hours, tier,
             direction, as_of, change_percent, historical_moves, current_categories,
             matched_categories, news_articles, score, observed_at, available_at,
             source_url, evidence)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(instrument_key, interval_seconds, move_hours, as_of) DO UPDATE SET
             tier=excluded.tier, direction=excluded.direction,
             change_percent=excluded.change_percent,
             historical_moves=excluded.historical_moves,
             current_categories=excluded.current_categories,
             matched_categories=excluded.matched_categories,
             news_articles=excluded.news_articles, score=excluded.score,
             observed_at=excluded.observed_at, available_at=excluded.available_at,
             source_url=excluded.source_url, evidence=excluded.evidence
           RETURNING id""",
        (instrument_key, interval_seconds, move_hours, tier, direction, as_of,
         numbers["change_percent"], historical_moves, current_categories, matched_categories,
         news_articles, numbers["score"], observed_at, available_at, source_url, evidence),
    ).fetchone()
    return int(row[0])


def list_cause_scans(conn, instrument_key: str, *, limit: int = 100) -> list[dict]:
    _observation_text(instrument_key, "instrument_key", 512, True)
    if type(limit) is not int or not 1 <= limit <= 20000:
        raise ValueError("limit must be an integer from 1 to 20000")
    rows = conn.execute("SELECT * FROM cause_scans WHERE instrument_key=? ORDER BY as_of LIMIT ?",
                        (instrument_key, limit))
    return [dict(row) for row in rows]


# --- retention cuts ---


def record_prune_run(conn, *, cut: str, reason: str, keep_bars: int, applied_at: str,
                     instrument_key: str = "", interval_seconds: int = 0,
                     deleted=None, spans_cut=None, series=None) -> int:
    """Record that stored history before an instant was deliberately removed.

    The record exists so that "no rows in 2021" can be read as *removed* rather
    than as *never fetched*, which are different claims and only the first of
    them is knowable from a registry afterwards. It states the cut, the reason
    the operator gave, how many rows went from each table, and what remains; it
    cannot state what the rows contained, so a reader is told the loss is
    described and not restorable from here.
    """
    _observation_text(cut, "cut", 64, True)
    _observation_text(reason, "reason", 2048, True)
    _observation_text(applied_at, "applied_at", 64, True)
    _observation_text(instrument_key, "instrument_key", 512)
    if type(keep_bars) is not int or keep_bars < 1:
        raise ValueError("keep_bars must be a positive integer")
    if type(interval_seconds) is not int or interval_seconds < 0:
        raise ValueError("interval_seconds must be a nonnegative integer")
    try:
        parsed = datetime.fromisoformat(cut)
    except (TypeError, ValueError) as exc:
        raise ValueError("cut must be an ISO instant") from exc
    if parsed.tzinfo is None:
        raise ValueError("cut must carry a UTC offset; a local time is not an instant")
    payloads = {}
    for label, value, empty in (("deleted", deleted, {}), ("spans_cut", spans_cut, {}),
                                ("series", series, [])):
        text = json.dumps(value if value is not None else empty, ensure_ascii=True,
                          sort_keys=True)
        if len(text) > 65536:
            raise ValueError(f"{label} exceeds the recordable size for one prune run")
        payloads[label] = text
    cursor = conn.execute(
        "INSERT INTO prune_runs(cut, reason, keep_bars, instrument_key, interval_seconds,"
        " applied_at, deleted, spans_cut, series) VALUES(?,?,?,?,?,?,?,?,?)",
        (cut, reason, keep_bars, instrument_key, interval_seconds, applied_at,
         payloads["deleted"], payloads["spans_cut"], payloads["series"]))
    return int(cursor.lastrowid)


def _has_prune_runs(conn: sqlite3.Connection) -> bool:
    """Whether this registry records retention cuts at all.

    A database written before the table existed has recorded none, and a read-only
    open never migrates one into place, so a reader that asked would otherwise fail
    with "no such table" on a registry that is perfectly sound. Nothing was cut
    there, because the command did not exist, which is the truth to report.
    """
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='prune_runs'"
                             ).fetchone())


def list_prune_runs(conn, limit: int = 50) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer from 1 to 1000")
    if not _has_prune_runs(conn):
        return []
    rows = conn.execute("SELECT * FROM prune_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    decoded = []
    for row in rows:
        item = dict(row)
        for field, empty in (("deleted", {}), ("spans_cut", {}), ("series", [])):
            try:
                item[field] = json.loads(item.get(field) or json.dumps(empty))
            except (TypeError, ValueError):
                item[field] = empty
        decoded.append(item)
    return decoded


def latest_prune_cut(conn, instrument_key: str, interval_seconds: int) -> dict | None:
    """The most recent recorded cut that removed history for this series.

    A run scoped to one series applies to that series; a run with no series
    applies to every series, so it is the later of the two that counts. A window
    entirely before the returned instant is one this registry has recorded
    removing, which is a different statement from having nothing there.
    """
    _observation_text(instrument_key, "instrument_key", 512, True)
    if type(interval_seconds) is not int or interval_seconds < 0:
        raise ValueError("interval_seconds must be a nonnegative integer")
    if not _has_prune_runs(conn):
        return None
    row = conn.execute(
        "SELECT id, cut, reason, applied_at, keep_bars FROM prune_runs"
        " WHERE (instrument_key=? AND interval_seconds IN (0, ?)) OR instrument_key=''"
        " ORDER BY id DESC LIMIT 1",
        (instrument_key, interval_seconds)).fetchone()
    return dict(row) if row is not None else None


# --- semantic embeddings ---

def add_semantic_embedding(conn, *, content_hash: str, embedding_blob: bytes,
                           source_kind: str, entity_key: str = "", as_of: str,
                           content_text: str = "", observed_at: str = "") -> int:
    for label, value in (("content_hash", content_hash), ("source_kind", source_kind),
                          ("as_of", as_of), ("observed_at", observed_at)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be nonempty text")
    if not isinstance(embedding_blob, (bytes, bytearray)):
        raise ValueError("embedding_blob must be bytes")
    if len(embedding_blob) == 0:
        raise ValueError("embedding_blob must not be empty")
    row = conn.execute(
        """INSERT OR IGNORE INTO semantic_embeddings(
            content_hash, embedding_blob, source_kind, entity_key, as_of,
            content_text, observed_at)
         VALUES(?,?,?,?,?,?,?)
         RETURNING id""",
        (content_hash, embedding_blob, source_kind, entity_key, as_of,
         content_text, observed_at),
    ).fetchone()
    return int(row[0]) if row else -1


def list_semantic_embeddings(conn, source_kind: str | None = None,
                             entity_key: str | None = None,
                             limit: int = 100) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer from 1 to 1000")
    sql = "SELECT * FROM semantic_embeddings"
    params: list = []
    conditions = []
    if source_kind:
        conditions.append("source_kind=?")
        params.append(source_kind)
    if entity_key:
        conditions.append("entity_key=?")
        params.append(entity_key)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY as_of DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in conn.execute(sql, params)]


def get_semantic_embedding(conn, content_hash: str) -> dict | None:
    if not isinstance(content_hash, str) or not content_hash.strip():
        raise ValueError("content_hash must be nonempty text")
    row = conn.execute("SELECT * FROM semantic_embeddings WHERE content_hash=?", (content_hash,)).fetchone()
    return dict(row) if row else None


# --- pattern matches ---

def add_pattern_match(conn, *, current_volatility_id: int, historical_volatility_id: int,
                      similarity_score: str, matched_features: str, matched_kinds: str,
                      as_of: str, analysis_method: str = "cosine_similarity") -> int:
    if type(current_volatility_id) is not int or not 1 <= current_volatility_id <= 2**63 - 1:
        raise ValueError("current_volatility_id must be a positive SQLite integer")
    if type(historical_volatility_id) is not int or not 1 <= historical_volatility_id <= 2**63 - 1:
        raise ValueError("historical_volatility_id must be a positive SQLite integer")
    if current_volatility_id == historical_volatility_id:
        raise ValueError("current and historical volatility IDs must differ")
    if not isinstance(as_of, str) or not as_of.strip():
        raise ValueError("as_of must be nonempty text")
    row = conn.execute(
        """INSERT INTO pattern_matches(
            current_volatility_id, historical_volatility_id, similarity_score,
            matched_features, matched_kinds, as_of, analysis_method)
         VALUES(?,?,?,?,?,?,?)
         RETURNING id""",
        (current_volatility_id, historical_volatility_id, similarity_score,
         matched_features, matched_kinds, as_of, analysis_method),
    ).fetchone()
    return int(row[0])


def list_pattern_matches(conn, current_volatility_id: int | None = None,
                         limit: int = 100) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer from 1 to 1000")
    sql = "SELECT * FROM pattern_matches"
    params: list = []
    if current_volatility_id is not None:
        if type(current_volatility_id) is not int or not 1 <= current_volatility_id <= 2**63 - 1:
            raise ValueError("current_volatility_id must be a positive SQLite integer")
        sql += " WHERE current_volatility_id=?"
        params.append(current_volatility_id)
    sql += " ORDER BY similarity_score DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in conn.execute(sql, params)]


def find_pattern_matches(conn, current_volatility_id: int,
                         min_similarity: float = 0.5,
                         limit: int = 10) -> list[dict]:
    if type(current_volatility_id) is not int or not 1 <= current_volatility_id <= 2**63 - 1:
        raise ValueError("current_volatility_id must be a positive SQLite integer")
    if not isinstance(min_similarity, (int, float)) or not 0 <= min_similarity <= 1:
        raise ValueError("min_similarity must be a float between 0 and 1")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 to 100")
    rows = conn.execute(
        """SELECT * FROM pattern_matches
         WHERE current_volatility_id=? AND CAST(similarity_score AS REAL) >= ?
         ORDER BY CAST(similarity_score AS REAL) DESC LIMIT ?""",
        (current_volatility_id, min_similarity, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def _write_ingest_run(conn, source: str, started_at: str, finished_at: str, *, query: str = "",
                      country: str = "", indicator: str = "", category: str = "",
                      fetched: int = 0, stored: int = 0, skipped: int = 0, missing: int = 0,
                      pages: int = 0, total: int | None = None, truncated: bool = False,
                      request_sha256: str = "", records_sha256: str = "", warnings=None,
                      coverage: str = "", status: str = "completed", resumable: bool = False,
                      next_offset: int = 0, next_page: int = 0, pages_detail=None) -> int:
    cursor = conn.execute(
        "INSERT INTO ingest_runs(source, query, country, indicator, category, started_at,"
        " finished_at, status, fetched, stored, skipped, missing, pages, total, truncated,"
        " request_sha256, records_sha256, warnings, coverage, resumable, next_offset, next_page,"
        " pages_detail)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (source, query, country, indicator, category, started_at, finished_at, status, fetched,
         stored, skipped, missing, pages, total, 1 if truncated else 0, request_sha256,
         records_sha256, json.dumps(list(warnings or []), ensure_ascii=True, sort_keys=True),
         coverage, 1 if resumable else 0, next_offset, next_page,
         json.dumps(list(pages_detail or []), ensure_ascii=True, sort_keys=True)),
    )
    return int(cursor.lastrowid)


def record_ingest_run(conn, source: str, started_at: str, finished_at: str, **fields) -> int:
    return _write_ingest_run(conn, source, started_at, finished_at, **fields)


#: How a failure is named in a run row. The exception's own message is never
#: recorded or echoed: it can carry a token, a password or a response body, and the
#: rule that a classified message replaces it applies here as much as it does on the
#: console. The type name is kept because "which class of failure" is diagnostic and
#: "what it said" is not safe.
FAILURE_REASONS = {
    "source_request": "the source request failed or was refused",
    "database": "the registry rejected or could not complete the work",
    "file": "a file or directory could not be read or written",
    "data": "the data did not pass validation",
    "cancelled": "the operation was cancelled",
    "unexpected": "a defect in this program rather than a bad input",
}


def classify_failure(error: BaseException) -> tuple[str, str]:
    """The reason a failure is recorded under, and the exception's class name.

    Two things come back and neither is the message. The reason is drawn from a
    closed vocabulary so a run row says the same thing about the same kind of
    failure every time; the class name says which one it was without saying what it
    contained. A test asserts a secret planted in the message reaches neither.
    """
    import sqlite3 as _sqlite3
    from urllib.error import URLError as _URLError
    name = type(error).__name__
    if isinstance(error, (KeyboardInterrupt, EOFError)):
        return "cancelled", name
    if isinstance(error, _sqlite3.Error):
        return "database", name
    if isinstance(error, (OSError, _URLError)):
        return "file" if isinstance(error, OSError) else "source_request", name
    if isinstance(error, (ValueError, UnicodeError, OverflowError, RecursionError)):
        return "data", name
    try:
        from ..fetchers.http import SourceError
        if isinstance(error, SourceError):
            return "source_request", name
    except ImportError:  # pragma: no cover - the fetchers always import cleanly
        pass
    return "unexpected", name


def record_failed_run(conn, *, error: BaseException, source: str, started_at: str,
                      query: str = "", country: str = "", indicator: str = "",
                      category: str = "", request_sha256: str = "", finished_at: str = "",
                      now=None) -> dict:
    """Write the one row that says a fetch failed.

    The reason comes from `FAILURE_REASONS` and the exception's class name is kept.
    Its **message never is**: it can carry a token, a password or a response body,
    and the rule that a classified message replaces the raw one applies here as much
    as it does on the console.

    It commits nothing. `get_conn` owns every commit, and it calls this *after* it
    has rolled the failed session back, precisely so this row is not discarded along
    with the rows the fetch wrote. Returns what happened, because a caller has to be
    able to report that the failure was not recorded.
    """
    reason, name = classify_failure(error)
    finished = finished_at or (now if now is not None else clock.now()).isoformat()
    run_id = _write_ingest_run(
        conn, source, started_at, finished, query=query, country=country,
        indicator=indicator, category=category, request_sha256=request_sha256,
        status="failed", coverage=f"{reason}: {name}", warnings=[f"{reason}: {name}"])
    return {"recorded": True, "run_id": run_id, "reason": reason, "error_class": name}


def failed_run(*, source: str, started_at: str, **fields):
    """A recorder for `get_conn`: one argument, the connection, after the rollback.

    Built with `partial`-style closure rather than an exception argument so a caller
    registers a small named thing rather than a lambda, and so the exception always
    comes from the session that failed.
    """
    def recorder(conn) -> None:
        record_failed_run(conn, error=getattr(conn, "lele_failure", None) or
                          RuntimeError("an unnamed failure"), source=source,
                          started_at=started_at, **fields)
    return recorder


def set_failure_recorder(conn, recorder) -> bool:
    """Register what to record if this session fails. See `get_conn`.

    Returns whether it was registered, because a plain `sqlite3.Connection` has no
    instance dictionary and cannot carry it — a test or an embedder may hold one, and
    the fetch must work on it. A caller that cannot register gets told so in its
    result rather than losing the capability silently.
    """
    try:
        conn.lele_failure_recorder = recorder
    except AttributeError:
        return False
    return True


def _decode_ingest_run(row) -> dict:
    item = dict(row)
    for field in ("warnings", "pages_detail"):
        try:
            item[field] = json.loads(item.get(field) or "[]")
        except (TypeError, ValueError):
            item[field] = []
    item["truncated"] = bool(item.get("truncated"))
    item["resumable"] = bool(item.get("resumable"))
    return item


def list_ingest_runs(conn, limit: int = 50) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer from 1 to 1000")
    rows = conn.execute("SELECT * FROM ingest_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_decode_ingest_run(row) for row in rows]


def latest_resumable_run(conn, source: str, query: str = "", country: str = "",
                         indicator: str = "", category: str = "") -> dict | None:
    row = conn.execute(
        "SELECT * FROM ingest_runs WHERE source=? AND query=? AND country=? AND indicator=?"
        " AND category=? AND resumable=1 AND truncated=1 AND status='completed'"
        " ORDER BY id DESC LIMIT 1",
        (source, query, country, indicator, category),
    ).fetchone()
    return _decode_ingest_run(row) if row is not None else None


_CONSOLIDATION_RELATIONS = ("subsidiary_of", "ultimate_subsidiary_of")


def _tree_entry(conn, eid, level, direction, rel, source_url, observed_at, seen_count):
    entity = get_entity(conn, eid)
    attrs = {r["k"]: r["v"] for r in conn.execute(
        "SELECT k, v FROM attributes WHERE entity_id=?", (eid,))}
    exceptions = {key: attrs[key] for key in
                  ("gleif.direct_parent_exception", "gleif.ultimate_parent_exception")
                  if key in attrs}
    recorded = conn.execute(
        "SELECT 1 FROM edges WHERE src_id=? AND rel IN (?, ?) AND status='active' LIMIT 1",
        (eid, *_CONSOLIDATION_RELATIONS)).fetchone() is not None
    parent_status = "recorded" if recorded else ("exception" if exceptions else "unknown")
    return {
        "id": eid, "key": entity["key"], "name": entity["name"], "kind": entity["kind"],
        "country": entity["country"], "level": level, "direction": direction,
        "via_rel": rel or None, "edge_source_url": source_url or None,
        "edge_observed_at": observed_at or None, "edge_seen_count": seen_count,
        "parent_status": parent_status, "parent_exceptions": exceptions,
    }


def entity_tree(conn, eid: int, depth: int = 3, max_nodes: int = 1000,
                direction: str = "both") -> dict:
    if type(eid) is not int or not 1 <= eid <= 2**63 - 1:
        raise ValueError("entity id must be a positive SQLite integer")
    if type(depth) is not int or not 1 <= depth <= 20:
        raise ValueError("depth must be an integer from 1 to 20")
    if type(max_nodes) is not int or not 1 <= max_nodes <= 10000:
        raise ValueError("max_nodes must be an integer from 1 to 10000")
    if direction not in ("both", "parents", "subsidiaries"):
        raise ValueError("direction must be both, parents or subsidiaries")
    if get_entity(conn, eid) is None:
        raise ValueError(f"entity {eid} not found")
    want_parents = direction in ("both", "parents")
    want_subsidiaries = direction in ("both", "subsidiaries")
    nodes = []
    seen = {eid}
    cycles = 0
    queue = [(eid, 0, "self", "", "", None, None)]
    index = 0
    truncated = False
    while index < len(queue):
        node_id, level, node_direction, rel, source_url, observed_at, seen_count = queue[index]
        index += 1
        nodes.append(_tree_entry(conn, node_id, level, node_direction, rel, source_url,
                                 observed_at, seen_count))
        if len(nodes) >= max_nodes:
            truncated = index < len(queue) or level < depth
            break
        if level >= depth:
            continue
        expand_parents = want_parents and node_direction in ("self", "ancestor")
        expand_subsidiaries = want_subsidiaries and node_direction in ("self", "descendant")
        if expand_parents:
            rows = conn.execute(
                "SELECT dst_id AS other, rel, source_url, observed_at, seen_count FROM edges"
                " WHERE src_id=? AND rel IN (?, ?) AND status='active' ORDER BY dst_id",
                (node_id, *_CONSOLIDATION_RELATIONS)).fetchall()
            for row in rows:
                if row["other"] in seen:
                    cycles += 1
                    continue
                seen.add(row["other"])
                queue.append((row["other"], level + 1, "ancestor", row["rel"],
                              row["source_url"], row["observed_at"], row["seen_count"]))
        if expand_subsidiaries:
            rows = conn.execute(
                "SELECT src_id AS other, rel, source_url, observed_at, seen_count FROM edges"
                " WHERE dst_id=? AND rel IN (?, ?) AND status='active' ORDER BY src_id",
                (node_id, *_CONSOLIDATION_RELATIONS)).fetchall()
            for row in rows:
                if row["other"] in seen:
                    cycles += 1
                    continue
                seen.add(row["other"])
                queue.append((row["other"], level + 1, "descendant", row["rel"],
                              row["source_url"], row["observed_at"], row["seen_count"]))
    unknown = [node["id"] for node in nodes if node["parent_status"] == "unknown"]
    return {
        "method": "gleif_consolidation_tree_v1",
        "root_id": eid,
        "parameters": {"depth": depth, "max_nodes": max_nodes, "direction": direction,
                       "edge_relations": list(_CONSOLIDATION_RELATIONS)},
        "nodes": nodes,
        "coverage": {"nodes": len(nodes), "truncated": truncated, "cycle_edges_skipped": cycles,
                     "nodes_without_recorded_or_exception_parent": len(unknown)},
        "limitations": [
            "Only stored, active subsidiary_of/ultimate_subsidiary_of edges are followed; a "
            "missing edge is unknown coverage, not evidence that no parent exists.",
            "A recorded GLEIF consolidation relationship is not proof of control, ownership or "
            "beneficial ownership; reported exceptions are preserved as recorded.",
            "Traversal is bounded by depth and a node cap; cross-holdings, cycles and overlapping "
            "structures are not resolved and parent_status=unknown is not 'no parent'.",
        ],
    }


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
