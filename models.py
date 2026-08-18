"""
LiftTag MVP — plain data types and constants.

Deliberately small: no ORM. Rows are read from SQLite (see db.py) into
these lightweight dataclasses.
"""
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

STATUS_IN_SERVICE = "IN SERVICE"
STATUS_QUARANTINED = "QUARANTINED — DO NOT USE"

PERIODIC_INSPECTION_VALID = "VALID"

RESULT_PASS = "PASS"
RESULT_FAIL = "FAIL"

# --- Inspection Session v1 (added 2026-08-17) -------------------------------
#
# Session status. "Use terminology that fits the existing architecture if
# necessary, but document any deviation" (project instructions) — these four
# names are exactly the ones requested; no deviation was needed here.
SESSION_STATUS_OPEN = "OPEN"
SESSION_STATUS_READY = "READY"
SESSION_STATUS_BLOCKED = "BLOCKED"
SESSION_STATUS_COMPLETED = "COMPLETED"

# Session item (an asset's membership in one session) status. An item is
# ACTIVE unless explicitly removed from the active set (see requirement 11
# — "removed" is a status flag, never a delete).
ITEM_STATUS_ACTIVE = "ACTIVE"
ITEM_STATUS_REMOVED = "REMOVED"

# Per-item pre-use check result, before a PASS/FAIL has been recorded.
CHECK_RESULT_PENDING = "PENDING"

# --- Rolling 24h Pre-Use Validity + Demo Role Architecture (added 2026-08-18)
#
# These are DERIVED DISPLAY STATES ONLY — see workflow.get_operational_state().
# They are never written to checks.result. checks.result remains exactly
# RESULT_PASS / RESULT_FAIL, always. Per explicit correction: "Do NOT
# introduce VALID as though it were another inspection result equivalent to
# PASS."
OPERATIONAL_STATE_VALID = "VALID CHECK"
OPERATIONAL_STATE_CHECK_REQUIRED = "CHECK REQUIRED"
OPERATIONAL_STATE_QUARANTINED = "QUARANTINED"

# Rolling window, not a calendar-day rule. See workflow.get_operational_state().
PRE_USE_VALIDITY_HOURS = 24

# Demo role architecture (explicitly NOT real authentication — see
# workflow.py / app.py "DEMO ACCESS — NOT AUTHENTICATED" comments and every
# template that surfaces this). AP is the primary authority; Supervisor is
# granted by AP; anyone with no registered role is treated as an ordinary
# Field User for pre-use-check purposes only (checking remains open to
# everyone, unchanged from the original MVP — only /admin/* routes are
# gated by these roles).
ROLE_AP = "AP"
ROLE_SUPERVISOR = "SUPERVISOR"


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat()


def parse_iso(value):
    return datetime.fromisoformat(value) if value else None


def new_tag_id():
    """
    Generate a short, random, non-sequential opaque token for use in
    /t/<tag_id> URLs.

    IMPORTANT: this token is a ROUTING identifier only. It is NOT
    authentication or authorisation — see README.md "Security note".
    Anyone who has the URL can open it. That is acceptable only because
    this environment holds fictitious data and is explicitly labelled
    as not for operational use.
    """
    return secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:10]


@dataclass
class Asset:
    asset_id: str
    equipment_type: str
    periodic_inspection_status: str
    current_status: str
    created_at: datetime
    # Asset Registry fields (added 2026-08-17). All optional/nullable — see
    # requirement 3: a company's existing equipment register may not
    # populate every one of these. None of these values are ever invented;
    # they come from demo seed data or an imported CSV row.
    serial_number: Optional[str] = None
    description: Optional[str] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    wll: Optional[str] = None
    company: Optional[str] = None
    periodic_inspection_due: Optional[str] = None
    notes: Optional[str] = None


@dataclass
class Tag:
    tag_id: str
    asset_id: str
    active: bool
    created_at: datetime
    revoked_at: Optional[datetime]


@dataclass
class Check:
    id: int
    asset_id: str
    tag_id_used: Optional[str]
    checked_by: str
    lift_supervisor: str
    result: str
    timestamp: datetime
    # Added 2026-08-17 for Inspection Session v1. Both optional/default None
    # so existing rows (and existing code that builds/expects a Check without
    # these) are unaffected — see db.SCHEMA migration notes.
    failure_reason: Optional[str] = None
    session_item_id: Optional[int] = None
    checklist_confirmed: Optional[bool] = None


@dataclass
class AuditEvent:
    id: int
    asset_id: str
    event_type: str
    actor: str
    previous_state: Optional[str]
    new_state: Optional[str]
    timestamp: datetime
    reference: Optional[str]
    # Added 2026-08-17: which Inspection Session (if any) this asset-level
    # event happened under. None for every event predating this increment,
    # and for any event not associated with a session.
    session_id: Optional[int] = None


@dataclass
class InspectionSession:
    id: int
    created_at: datetime
    inspection_date: datetime
    lift_supervisor: str
    slinger_signaller: str
    status: str
    completed_at: Optional[datetime]

    @property
    def session_ref(self):
        """Human-friendly display reference, e.g. 'INSP-000142'. Derived from
        the integer id rather than stored, so it can never drift out of sync."""
        return f"INSP-{self.id:06d}"


@dataclass
class SessionItem:
    id: int
    session_id: int
    asset_id: str
    tag_id_used: Optional[str]
    item_status: str
    check_result: str
    check_id: Optional[int]
    added_at: datetime
    removed_at: Optional[datetime]


@dataclass
class DemoUser:
    """
    A registered DEMO identity (added 2026-08-18) — NOT a real user account.
    No password, no login, no verified identity. See workflow.py /
    app.py / templates for the explicit "DEMO ACCESS — NOT AUTHENTICATED"
    labelling required everywhere this is surfaced.
    """
    id: int
    name: str
    role: str  # ROLE_AP or ROLE_SUPERVISOR
    granted_by: Optional[str]
    created_at: datetime
    revoked_at: Optional[datetime]

    @property
    def active(self):
        return self.revoked_at is None
