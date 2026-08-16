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

RESULT_PASS = "PASS"
RESULT_FAIL = "FAIL"


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
