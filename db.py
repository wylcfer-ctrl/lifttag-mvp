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

from models import (
    Asset,
    Tag,
    Check,
    AuditEvent,
    InspectionSession,
    SessionItem,
    DemoUser,
    iso,
    parse_iso,
    now,
    STATUS_IN_SERVICE,
    SESSION_STATUS_OPEN,
    ITEM_STATUS_ACTIVE,
    ITEM_STATUS_REMOVED,
    CHECK_RESULT_PENDING,
)

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

-- --- Inspection Session v1 (added 2026-08-17) ------------------------------
-- New tables only — nothing above this line was changed. See db.migrate()
-- below for the (also non-destructive) column additions to the two tables
-- above this comment.

CREATE TABLE IF NOT EXISTS inspection_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    inspection_date TEXT NOT NULL,
    lift_supervisor TEXT NOT NULL,
    slinger_signaller TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS session_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES inspection_sessions(id),
    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
    tag_id_used TEXT,
    item_status TEXT NOT NULL DEFAULT 'ACTIVE',
    check_result TEXT NOT NULL DEFAULT 'PENDING',
    check_id INTEGER REFERENCES checks(id),
    added_at TEXT NOT NULL,
    removed_at TEXT
);

-- Append-only, same convention as audit_events: no UPDATE/DELETE anywhere in
-- this module. Holds session-level events that have no single asset_id to
-- attach to in audit_events (e.g. SESSION_CREATED, SESSION_READY,
-- SESSION_COMPLETED). Per-asset session events (ASSET_ADDED_TO_SESSION,
-- ASSET_REMOVED_FROM_ACTIVE_SESSION) go in audit_events instead, tagged with
-- the new audit_events.session_id column — see migrate() below. This keeps
-- one audit system, split only where the data model genuinely requires it
-- (a session-level event has no single asset), per project instructions not
-- to create a competing second audit system unless technically necessary.
CREATE TABLE IF NOT EXISTS session_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES inspection_sessions(id),
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    previous_status TEXT,
    new_status TEXT,
    timestamp TEXT NOT NULL,
    reference TEXT
);

-- --- Rolling 24h Validity + Demo Role Architecture (added 2026-08-18) ------
-- New tables only. No column was added to any table above for the 24-hour
-- validity feature — it is DERIVED from checks.timestamp on every read (see
-- workflow.get_operational_state()), per explicit instruction: "Do NOT
-- automatically add checks.valid_until merely because it was proposed
-- during Phase 2 ... prefer deriving." No stored value would ever be
-- inconsistent with the real check history, and no migration was needed
-- for it at all.

-- DEMO ONLY — see models.DemoUser. Not a real user/authentication table:
-- no password hash, no login mechanism. Exists only so AP-grants-Supervisor
-- can be demoed as a real, auditable registry rather than unchecked free
-- text (see the explicit instruction against free-text "enter your name
-- and role").
CREATE TABLE IF NOT EXISTS demo_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL,
    granted_by TEXT,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

-- Append-only, same convention as audit_events/session_events: no
-- UPDATE/DELETE anywhere in this module. Holds AP grant/revoke events and
-- other demo-role-related events, which (like session_events) have no
-- single asset_id to attach to in audit_events.
CREATE TABLE IF NOT EXISTS demo_auth_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    target_name TEXT,
    previous_role TEXT,
    new_role TEXT,
    timestamp TEXT NOT NULL,
    reference TEXT
);
"""


def _ensure_column(conn, table, column, coldef):
    """
    Add `column` to `table` if it doesn't already exist. SQLite's
    CREATE TABLE IF NOT EXISTS (used for SCHEMA above) does not add new
    columns to a table that already exists from a prior deploy — this is
    the lightweight migration step for that case. Always adds a NULLABLE
    column with no default that would rewrite existing rows, so this never
    destroys or alters any existing data (see migrate() below for exactly
    which columns this adds and why).
    """
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")


def migrate(conn):
    """
    Lightweight migration for Inspection Session v1 (2026-08-17).

    Adds four NULLABLE columns to two pre-existing tables so the new
    multi-asset session workflow can (a) tag a check as belonging to a
    session item and carry a FAIL reason, and (b) tag an audit event as
    having happened under a session. Every existing row gets NULL for these
    new columns — nothing is rewritten, nothing is dropped, no existing
    table is recreated. Safe to run on every startup (checks first, only
    alters if missing) and safe to run against a fresh database (the
    columns are simply already present from SCHEMA in that case, so this
    is a no-op).

        checks.session_item_id     -- which session_items row this check was
                                       recorded for; NULL for a standalone
                                       single-asset check (the original,
                                       untouched /t/<tag_id>/check flow).
        checks.failure_reason      -- required by the app (not the database)
                                       when a session pre-use check result is
                                       FAIL; NULL otherwise.
        checks.checklist_confirmed -- 1 if the operator confirmed all five
                                       generic checklist points before
                                       submitting a session pre-use check;
                                       NULL for the original single-asset
                                       flow, which has no checklist.
        audit_events.session_id    -- which Inspection Session (if any) an
                                       asset-level audit event happened
                                       under.

    Also adds eight NULLABLE Asset Registry columns to `assets`
    (2026-08-17) — see the Asset Registry / Tag Commissioning comment
    further down in this function for exactly which ones and why.
    """
    _ensure_column(conn, "checks", "session_item_id", "INTEGER")
    _ensure_column(conn, "checks", "failure_reason", "TEXT")
    _ensure_column(conn, "checks", "checklist_confirmed", "INTEGER")
    _ensure_column(conn, "audit_events", "session_id", "INTEGER")

    # --- Asset Registry / Tag Commissioning (added 2026-08-17) -------------
    # Eight new NULLABLE columns on the pre-existing `assets` table, so a
    # company equipment register (which may not populate all of these) can
    # be imported without any row being rejected for a missing field, and
    # so every existing asset row (including the demo assets, and anything
    # already deployed) keeps working unchanged — these are simply NULL
    # until an admin or an import sets them. Nothing above this comment
    # was touched, no existing table was recreated.
    _ensure_column(conn, "assets", "serial_number", "TEXT")
    _ensure_column(conn, "assets", "description", "TEXT")
    _ensure_column(conn, "assets", "manufacturer", "TEXT")
    _ensure_column(conn, "assets", "model", "TEXT")
    _ensure_column(conn, "assets", "wll", "TEXT")
    _ensure_column(conn, "assets", "company", "TEXT")
    _ensure_column(conn, "assets", "periodic_inspection_due", "TEXT")
    _ensure_column(conn, "assets", "notes", "TEXT")
    conn.commit()


def get_conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path):
    conn = get_conn(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    migrate(conn)
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
        serial_number=_row_get(row, "serial_number"),
        description=_row_get(row, "description"),
        manufacturer=_row_get(row, "manufacturer"),
        model=_row_get(row, "model"),
        wll=_row_get(row, "wll"),
        company=_row_get(row, "company"),
        periodic_inspection_due=_row_get(row, "periodic_inspection_due"),
        notes=_row_get(row, "notes"),
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


def _row_get(row, key, default=None):
    """sqlite3.Row has no .get() — this tolerates a column being absent
    (shouldn't happen after migrate(), but keeps these converters robust)."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


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
        failure_reason=_row_get(row, "failure_reason"),
        session_item_id=_row_get(row, "session_item_id"),
        checklist_confirmed=(
            bool(_row_get(row, "checklist_confirmed"))
            if _row_get(row, "checklist_confirmed") is not None
            else None
        ),
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
        session_id=_row_get(row, "session_id"),
    )


def _session(row):
    if row is None:
        return None
    return InspectionSession(
        id=row["id"],
        created_at=parse_iso(row["created_at"]),
        inspection_date=parse_iso(row["inspection_date"]),
        lift_supervisor=row["lift_supervisor"],
        slinger_signaller=row["slinger_signaller"],
        status=row["status"],
        completed_at=parse_iso(row["completed_at"]),
    )


def _session_item(row):
    if row is None:
        return None
    return SessionItem(
        id=row["id"],
        session_id=row["session_id"],
        asset_id=row["asset_id"],
        tag_id_used=row["tag_id_used"],
        item_status=row["item_status"],
        check_result=row["check_result"],
        check_id=row["check_id"],
        added_at=parse_iso(row["added_at"]),
        removed_at=parse_iso(row["removed_at"]),
    )


# --- assets -------------------------------------------------------------

def get_asset(conn, asset_id):
    row = conn.execute("SELECT * FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
    return _asset(row)


def list_assets(conn):
    rows = conn.execute("SELECT * FROM assets ORDER BY asset_id").fetchall()
    return [_asset(r) for r in rows]


def create_asset(conn, asset_id, equipment_type, periodic_inspection_status="VALID",
                  current_status=STATUS_IN_SERVICE, serial_number=None, description=None,
                  manufacturer=None, model=None, wll=None, company=None,
                  periodic_inspection_due=None, notes=None):
    """
    Registers a new asset. The eight registry fields (serial_number onward)
    are all optional/nullable — a company register may not populate all of
    them (requirement 3: "keep fields nullable where the source company
    register may not contain them") — and none of them are invented here;
    every value comes from the caller (demo seed data or a CSV import row).
    """
    conn.execute(
        "INSERT INTO assets (asset_id, equipment_type, periodic_inspection_status, "
        "current_status, created_at, serial_number, description, manufacturer, model, wll, "
        "company, periodic_inspection_due, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            asset_id, equipment_type, periodic_inspection_status, current_status, iso(now()),
            serial_number, description, manufacturer, model, wll, company,
            periodic_inspection_due, notes,
        ),
    )


def search_assets(conn, query):
    """
    Asset Registry search (requirement 2): matches Asset ID or Serial
    Number, case-insensitively, as a substring — the two fields an admin
    is told to search by at minimum. An empty/blank query returns every
    registered asset (used for the plain "browse the registry" view).
    """
    query = (query or "").strip()
    if not query:
        return list_assets(conn)
    like = f"%{query}%"
    rows = conn.execute(
        "SELECT * FROM assets WHERE asset_id LIKE ? COLLATE NOCASE "
        "OR (serial_number IS NOT NULL AND serial_number LIKE ? COLLATE NOCASE) "
        "ORDER BY asset_id",
        (like, like),
    ).fetchall()
    return [_asset(r) for r in rows]


def list_tags_for_asset(conn, asset_id):
    """Every tag ever assigned to this asset, oldest first — the 'historical
    NFC Tag assignments' required alongside the permanent asset record."""
    rows = conn.execute(
        "SELECT * FROM tags WHERE asset_id = ? ORDER BY created_at ASC, tag_id ASC", (asset_id,)
    ).fetchall()
    return [_tag(r) for r in rows]


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

def insert_check(conn, asset_id, tag_id_used, checked_by, lift_supervisor, result,
                  failure_reason=None, session_item_id=None, checklist_confirmed=None):
    cur = conn.execute(
        "INSERT INTO checks (asset_id, tag_id_used, checked_by, lift_supervisor, result, timestamp, "
        "failure_reason, session_item_id, checklist_confirmed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            asset_id, tag_id_used, checked_by, lift_supervisor, result, iso(now()),
            failure_reason, session_item_id,
            None if checklist_confirmed is None else (1 if checklist_confirmed else 0),
        ),
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

def insert_audit_event(conn, asset_id, event_type, actor, previous_state, new_state, reference=None,
                        session_id=None):
    conn.execute(
        "INSERT INTO audit_events (asset_id, event_type, actor, previous_state, new_state, "
        "timestamp, reference, session_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (asset_id, event_type, actor, previous_state, new_state, iso(now()), reference, session_id),
    )


def list_audit_events(conn, asset_id):
    rows = conn.execute(
        "SELECT * FROM audit_events WHERE asset_id = ? ORDER BY timestamp ASC, id ASC",
        (asset_id,),
    ).fetchall()
    return [_audit(r) for r in rows]


def list_audit_events_for_session(conn, session_id):
    """Asset-level audit events recorded under a given session (e.g.
    ASSET_ADDED_TO_SESSION, CHECK_PASS/CHECK_FAIL, STATUS_CHANGE,
    ASSET_REMOVED_FROM_ACTIVE_SESSION) — does not include session-level
    events, see list_session_events for those."""
    rows = conn.execute(
        "SELECT * FROM audit_events WHERE session_id = ? ORDER BY timestamp ASC, id ASC",
        (session_id,),
    ).fetchall()
    return [_audit(r) for r in rows]


# --- Inspection Session v1 (added 2026-08-17) ------------------------------

def create_session(conn, lift_supervisor, slinger_signaller):
    """Inspection date/time is always server-generated here (now()) — never
    accepted from the caller — per requirement 2: 'Do not trust a
    user-supplied timestamp as the authoritative audit timestamp.'"""
    timestamp = iso(now())
    cur = conn.execute(
        "INSERT INTO inspection_sessions (created_at, inspection_date, lift_supervisor, "
        "slinger_signaller, status, completed_at) VALUES (?, ?, ?, ?, ?, NULL)",
        (timestamp, timestamp, lift_supervisor, slinger_signaller, SESSION_STATUS_OPEN),
    )
    return cur.lastrowid


def get_session(conn, session_id):
    row = conn.execute("SELECT * FROM inspection_sessions WHERE id = ?", (session_id,)).fetchone()
    return _session(row)


def list_sessions(conn):
    rows = conn.execute("SELECT * FROM inspection_sessions ORDER BY id DESC").fetchall()
    return [_session(r) for r in rows]


def list_open_sessions(conn):
    rows = conn.execute(
        "SELECT * FROM inspection_sessions WHERE status != ? ORDER BY id DESC",
        ("COMPLETED",),
    ).fetchall()
    return [_session(r) for r in rows]


def set_session_status(conn, session_id, new_status):
    conn.execute("UPDATE inspection_sessions SET status = ? WHERE id = ?", (new_status, session_id))


def complete_session_row(conn, session_id):
    conn.execute(
        "UPDATE inspection_sessions SET status = 'COMPLETED', completed_at = ? WHERE id = ?",
        (iso(now()), session_id),
    )


def create_session_item(conn, session_id, asset_id, tag_id_used):
    cur = conn.execute(
        "INSERT INTO session_items (session_id, asset_id, tag_id_used, item_status, check_result, "
        "check_id, added_at, removed_at) VALUES (?, ?, ?, ?, ?, NULL, ?, NULL)",
        (session_id, asset_id, tag_id_used, ITEM_STATUS_ACTIVE, CHECK_RESULT_PENDING, iso(now())),
    )
    return cur.lastrowid


def get_session_item(conn, item_id):
    row = conn.execute("SELECT * FROM session_items WHERE id = ?", (item_id,)).fetchone()
    return _session_item(row)


def list_session_items(conn, session_id, include_removed=True):
    if include_removed:
        rows = conn.execute(
            "SELECT * FROM session_items WHERE session_id = ? ORDER BY id ASC", (session_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM session_items WHERE session_id = ? AND item_status = ? ORDER BY id ASC",
            (session_id, ITEM_STATUS_ACTIVE),
        ).fetchall()
    return [_session_item(r) for r in rows]


def get_active_session_item_for_asset(conn, session_id, asset_id):
    row = conn.execute(
        "SELECT * FROM session_items WHERE session_id = ? AND asset_id = ? AND item_status = ?",
        (session_id, asset_id, ITEM_STATUS_ACTIVE),
    ).fetchone()
    return _session_item(row)


def remove_session_item(conn, item_id):
    """Marks the item REMOVED (from the *active* set) — never deletes the
    row. The check(s) already recorded against it, and every audit event,
    remain exactly as they were (requirement 11)."""
    conn.execute(
        "UPDATE session_items SET item_status = ?, removed_at = ? WHERE id = ?",
        (ITEM_STATUS_REMOVED, iso(now()), item_id),
    )


def set_session_item_check(conn, item_id, check_id, check_result):
    conn.execute(
        "UPDATE session_items SET check_id = ?, check_result = ? WHERE id = ?",
        (check_id, check_result, item_id),
    )


def insert_session_event(conn, session_id, event_type, actor, previous_status=None, new_status=None,
                          reference=None):
    conn.execute(
        "INSERT INTO session_events (session_id, event_type, actor, previous_status, new_status, "
        "timestamp, reference) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (session_id, event_type, actor, previous_status, new_status, iso(now()), reference),
    )


def list_session_events(conn, session_id):
    rows = conn.execute(
        "SELECT * FROM session_events WHERE session_id = ? ORDER BY timestamp ASC, id ASC",
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# --- Demo Role Architecture (added 2026-08-18) ------------------------------
# DEMO ONLY — see models.DemoUser docstring. Not real authentication.

def _demo_user(row):
    if row is None:
        return None
    return DemoUser(
        id=row["id"],
        name=row["name"],
        role=row["role"],
        granted_by=row["granted_by"],
        created_at=parse_iso(row["created_at"]),
        revoked_at=parse_iso(row["revoked_at"]),
    )


def create_demo_user(conn, name, role, granted_by=None):
    cur = conn.execute(
        "INSERT INTO demo_users (name, role, granted_by, created_at, revoked_at) "
        "VALUES (?, ?, ?, ?, NULL)",
        (name, role, granted_by, iso(now())),
    )
    return cur.lastrowid


def get_demo_user(conn, user_id):
    row = conn.execute("SELECT * FROM demo_users WHERE id = ?", (user_id,)).fetchone()
    return _demo_user(row)


def get_demo_user_by_name(conn, name):
    row = conn.execute("SELECT * FROM demo_users WHERE name = ?", (name,)).fetchone()
    return _demo_user(row)


def get_active_demo_user_by_name(conn, name):
    row = conn.execute(
        "SELECT * FROM demo_users WHERE name = ? AND revoked_at IS NULL", (name,)
    ).fetchone()
    return _demo_user(row)


def list_active_demo_users(conn):
    rows = conn.execute(
        "SELECT * FROM demo_users WHERE revoked_at IS NULL ORDER BY role, name"
    ).fetchall()
    return [_demo_user(r) for r in rows]


def list_all_demo_users(conn):
    rows = conn.execute("SELECT * FROM demo_users ORDER BY role, name").fetchall()
    return [_demo_user(r) for r in rows]


def revoke_demo_user(conn, user_id):
    conn.execute("UPDATE demo_users SET revoked_at = ? WHERE id = ?", (iso(now()), user_id))


def insert_demo_auth_event(conn, event_type, actor, target_name=None, previous_role=None,
                            new_role=None, reference=None):
    conn.execute(
        "INSERT INTO demo_auth_events (event_type, actor, target_name, previous_role, "
        "new_role, timestamp, reference) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (event_type, actor, target_name, previous_role, new_role, iso(now()), reference),
    )


def list_demo_auth_events(conn):
    rows = conn.execute("SELECT * FROM demo_auth_events ORDER BY timestamp ASC, id ASC").fetchall()
    return [dict(r) for r in rows]
