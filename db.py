"""
LiftTag MVP — data access layer (plain sqlite3, no ORM).

Every function takes an explicit `db_path` or an already-open `conn`, never
a module-level global — this keeps multiple app instances (e.g. one per
test) fully isolated from each other in the same process.

Callers are responsible for conn.commit() and conn.close() (see app.py and
workflow.py) so related writes (e.g. a check plus its audit events) can be
grouped into one transaction.
"""
import sqlite3

from models import Asset, Tag, Check, AuditEvent, iso, parse_iso, now, STATUS_IN_SERVICE

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    asset_id TEXT PRIMARY KEY,
    equipment_type TEXT NOT NULL,
    periodic_inspection_status TEXT NOT NULL DEFAULT 'VALID',
    current_status TEXT NOT NULL DEFAULT 'IN SERVICE',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tags (
    tag_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
    tag_id_used TEXT,
    checked_by TEXT NOT NULL,
    lift_supervisor TEXT NOT NULL,
    result TEXT NOT NULL,
    timestamp TEXT NOT NULL
);

-- Audit events are only ever INSERTed by this application. No function in
-- this module updates or deletes a row in this table.
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    previous_state TEXT,
    new_state TEXT,
    timestamp TEXT NOT NULL,
    reference TEXT
);
"""


def get_conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path):
    conn = get_conn(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


# --- row -> dataclass -------------------------------------------------

def _asset(row):
    if row is None:
        return None
    return Asset(
        asset_id=row["asset_id"],
        equipment_type=row["equipment_type"],
        periodic_inspection_status=row["periodic_inspection_status"],
        current_status=row["current_status"],
        created_at=parse_iso(row["created_at"]),
    )


def _tag(row):
    if row is None:
        return None
    return Tag(
        tag_id=row["tag_id"],
        asset_id=row["asset_id"],
        active=bool(row["active"]),
        created_at=parse_iso(row["created_at"]),
        revoked_at=parse_iso(row["revoked_at"]),
    )


def _check(row):
    if row is None:
        return None
    return Check(
        id=row["id"],
        asset_id=row["asset_id"],
        tag_id_used=row["tag_id_used"],
        checked_by=row["checked_by"],
        lift_supervisor=row["lift_supervisor"],
        result=row["result"],
        timestamp=parse_iso(row["timestamp"]),
    )


def _audit(row):
    return AuditEvent(
        id=row["id"],
        asset_id=row["asset_id"],
        event_type=row["event_type"],
        actor=row["actor"],
        previous_state=row["previous_state"],
        new_state=row["new_state"],
        timestamp=parse_iso(row["timestamp"]),
        reference=row["reference"],
    )


# --- assets -------------------------------------------------------------

def get_asset(conn, asset_id):
    row = conn.execute("SELECT * FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
    return _asset(row)


def list_assets(conn):
    rows = conn.execute("SELECT * FROM assets ORDER BY asset_id").fetchall()
    return [_asset(r) for r in rows]


def create_asset(conn, asset_id, equipment_type, periodic_inspection_status="VALID",
                  current_status=STATUS_IN_SERVICE):
    conn.execute(
        "INSERT INTO assets (asset_id, equipment_type, periodic_inspection_status, "
        "current_status, created_at) VALUES (?, ?, ?, ?, ?)",
        (asset_id, equipment_type, periodic_inspection_status, current_status, iso(now())),
    )


def set_asset_status(conn, asset_id, new_status):
    conn.execute("UPDATE assets SET current_status = ? WHERE asset_id = ?", (new_status, asset_id))


# --- tags -----------------------------------------------------------------

def get_tag(conn, tag_id):
    row = conn.execute("SELECT * FROM tags WHERE tag_id = ?", (tag_id,)).fetchone()
    return _tag(row)


def get_active_tag(conn, asset_id):
    row = conn.execute(
        "SELECT * FROM tags WHERE asset_id = ? AND active = 1", (asset_id,)
    ).fetchone()
    return _tag(row)


def create_tag(conn, tag_id, asset_id, active=True):
    conn.execute(
        "INSERT INTO tags (tag_id, asset_id, active, created_at, revoked_at) VALUES (?, ?, ?, ?, NULL)",
        (tag_id, asset_id, 1 if active else 0, iso(now())),
    )


def revoke_tag(conn, tag_id):
    conn.execute("UPDATE tags SET active = 0, revoked_at = ? WHERE tag_id = ?", (iso(now()), tag_id))


# --- checks -----------------------------------------------------------------

def insert_check(conn, asset_id, tag_id_used, checked_by, lift_supervisor, result):
    cur = conn.execute(
        "INSERT INTO checks (asset_id, tag_id_used, checked_by, lift_supervisor, result, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (asset_id, tag_id_used, checked_by, lift_supervisor, result, iso(now())),
    )
    return cur.lastrowid


def get_check(conn, check_id):
    row = conn.execute("SELECT * FROM checks WHERE id = ?", (check_id,)).fetchone()
    return _check(row)


def get_last_check(conn, asset_id):
    row = conn.execute(
        "SELECT * FROM checks WHERE asset_id = ? ORDER BY timestamp DESC, id DESC LIMIT 1",
        (asset_id,),
    ).fetchone()
    return _check(row)


# --- audit events (append-only: no update_*/delete_* functions exist) -----

def insert_audit_event(conn, asset_id, event_type, actor, previous_state, new_state, reference=None):
    conn.execute(
        "INSERT INTO audit_events (asset_id, event_type, actor, previous_state, new_state, "
        "timestamp, reference) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (asset_id, event_type, actor, previous_state, new_state, iso(now()), reference),
    )


def list_audit_events(conn, asset_id):
    rows = conn.execute(
        "SELECT * FROM audit_events WHERE asset_id = ? ORDER BY timestamp ASC, id ASC",
        (asset_id,),
    ).fetchall()
    return [_audit(r) for r in rows]
