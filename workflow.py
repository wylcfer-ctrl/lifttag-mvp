"""
LiftTag MVP — safety-critical workflow logic.

Kept separate from Flask routing so the state-transition logic can be
unit-tested directly (see tests/test_workflow.py) without an HTTP cycle.

Correction 1 (mandatory, approved 2026-08-16): a PASS pre-use check must
NEVER release an asset from quarantine. There is no release-from-quarantine
workflow in this MVP, and none is added here.
"""
import db as dbmod
from models import (
    STATUS_QUARANTINED,
    STATUS_IN_SERVICE,
    PERIODIC_INSPECTION_VALID,
    RESULT_PASS,
    RESULT_FAIL,
    SESSION_STATUS_OPEN,
    SESSION_STATUS_READY,
    SESSION_STATUS_BLOCKED,
    SESSION_STATUS_COMPLETED,
    new_tag_id,
)


class UnknownTagError(Exception):
    """Raised when a Tag ID does not exist. Fail-safe: no asset is resolved."""

    def __init__(self, tag_id):
        self.tag_id = tag_id
        super().__init__(f"Tag ID '{tag_id}' is not recognised")


class RevokedTagError(Exception):
    """
    Raised when a Tag ID exists but is no longer active.

    Fail-safe: the caller must NOT treat this as resolving to a currently
    valid asset. The old tag remains historically traceable (the caller can
    show which asset it used to point to) but must never present itself as
    current.
    """

    def __init__(self, tag):
        self.tag = tag
        super().__init__(f"Tag ID '{tag.tag_id}' is revoked (no longer active)")


def resolve_tag(conn, tag_id):
    """
    Resolve a Tag ID to its (Tag, Asset) pair.

    Raises UnknownTagError or RevokedTagError rather than ever returning a
    stale or guessed result — the fail-safe boundary described in the
    Design Proposal §6.
    """
    tag = dbmod.get_tag(conn, tag_id)
    if tag is None:
        raise UnknownTagError(tag_id)
    if not tag.active:
        raise RevokedTagError(tag)
    asset = dbmod.get_asset(conn, tag.asset_id)
    return tag, asset


def record_pre_use_check(conn, asset, tag_id_used, checked_by, lift_supervisor, result,
                          failure_reason=None, session_item_id=None, checklist_confirmed=None,
                          session_id=None):
    """
    Record a pre-use check and apply the approved state-transition table
    (Design Proposal §8, Correction 1):

        IN SERVICE  + PASS -> stays IN SERVICE       (CHECK_PASS)
        IN SERVICE  + FAIL -> becomes QUARANTINED     (CHECK_FAIL, STATUS_CHANGE)
        QUARANTINED + PASS -> stays QUARANTINED       (CHECK_PASS)  -- never releases
        QUARANTINED + FAIL -> stays QUARANTINED       (CHECK_FAIL only, no redundant STATUS_CHANGE)

    There is no code path here, or anywhere else in this application, that
    sets current_status back to IN SERVICE. Quarantine release is out of
    scope for this MVP. This state table, and the CHECK_PASS / CHECK_FAIL /
    STATUS_CHANGE event names, are UNCHANGED by Inspection Session v1
    (2026-08-17) — the session pre-use check (requirement 8) calls this
    exact function rather than reimplementing the safety logic, so a check
    recorded from inside a session is governed by the identical, already
    -approved rules as the original single-asset flow.

    The last four parameters are all optional and default to None, so every
    existing caller (and every existing test) is unaffected:
      - failure_reason: an operator-supplied defect note. The database
        layer never requires this; the *session* pre-use check route
        enforces "no empty reason on FAIL" (requirement 8) before calling
        this function. The original single-asset check form still does not
        collect one, exactly as before.
      - session_item_id / session_id: recorded on the check / audit events
        so a check performed inside an Inspection Session is traceable back
        to it. Both stay NULL for the original single-asset flow.

    Commits the transaction (check + audit event(s) + optional status
    change) as a single unit on the given connection.
    """
    if result not in (RESULT_PASS, RESULT_FAIL):
        raise ValueError("result must be 'PASS' or 'FAIL'")

    actor = f"{checked_by} (Checked By) / {lift_supervisor} (Lift Supervisor)"
    previous_status = asset.current_status

    check_id = dbmod.insert_check(
        conn, asset.asset_id, tag_id_used, checked_by, lift_supervisor, result,
        failure_reason=failure_reason, session_item_id=session_item_id,
        checklist_confirmed=checklist_confirmed,
    )
    reference = f"check:{check_id}"

    if result == RESULT_PASS:
        # Deliberately NO status mutation on PASS, regardless of current_status.
        dbmod.insert_audit_event(
            conn, asset.asset_id, "CHECK_PASS", actor, previous_status, previous_status, reference,
            session_id=session_id,
        )
    else:  # RESULT_FAIL
        dbmod.insert_audit_event(
            conn, asset.asset_id, "CHECK_FAIL", actor, previous_status, previous_status, reference,
            session_id=session_id,
        )
        if previous_status != STATUS_QUARANTINED:
            dbmod.set_asset_status(conn, asset.asset_id, STATUS_QUARANTINED)
            dbmod.insert_audit_event(
                conn, asset.asset_id, "STATUS_CHANGE", actor, previous_status, STATUS_QUARANTINED, reference,
                session_id=session_id,
            )
            asset.current_status = STATUS_QUARANTINED  # keep in-memory object consistent for the caller
        # else: already quarantined -> stays quarantined, no redundant STATUS_CHANGE event.

    conn.commit()
    return dbmod.get_check(conn, check_id)


def assign_tag(conn, asset, actor="test-harness"):
    """
    Create a new active Tag for an asset, revoking any previously active
    tag. Test-harness / seeding helper only — this is NOT a production
    tag-issuance or release workflow.
    """
    previous = dbmod.get_active_tag(conn, asset.asset_id)
    if previous is not None:
        dbmod.revoke_tag(conn, previous.tag_id)
        dbmod.insert_audit_event(
            conn, asset.asset_id, "TAG_REVOKED", actor, previous.tag_id, None, reference=f"tag:{previous.tag_id}"
        )

    tag_id = new_tag_id()
    dbmod.create_tag(conn, tag_id, asset.asset_id, active=True)
    dbmod.insert_audit_event(conn, asset.asset_id, "TAG_ASSIGNED", actor, None, tag_id, reference=f"tag:{tag_id}")
    conn.commit()
    return dbmod.get_tag(conn, tag_id)


# --- Inspection Session v1 (added 2026-08-17) -------------------------------
#
# Multi-asset session workflow. Layered on top of the functions above rather
# than reimplementing them: adding an asset to a session still goes through
# resolve_tag() (identical fail-safe behaviour for unknown/revoked tags as
# the original single-asset tap flow), and every pre-use check recorded
# inside a session still goes through record_pre_use_check() above (identical
# Correction 1 safety-state logic, unmodified).


class SessionNotReadyError(Exception):
    """Raised when COMPLETE INSPECTION is attempted on a session that is not
    currently READY. Fail-safe: a session can never be completed with a
    pending, failed, quarantined, or otherwise invalid active item."""

    def __init__(self, session):
        self.session = session
        super().__init__(
            f"Session {session.id} is {session.status}, not READY — it cannot be completed yet"
        )


class SessionCompletedError(Exception):
    """Raised by any attempt to mutate a COMPLETED session (add/remove an
    item, record a check, or complete it again). Completed sessions are
    immutable (requirement 13)."""

    def __init__(self, session):
        self.session = session
        super().__init__(f"Session {session.id} is COMPLETED and cannot be modified")


def _require_open_for_mutation(session):
    if session.status == SESSION_STATUS_COMPLETED:
        raise SessionCompletedError(session)


def recompute_session_status(conn, session_id, actor="system"):
    """
    Derive a session's status from its *active* items, per requirement 10/12:

        no active items                                        -> OPEN
        any active item FAILed, or its live asset is currently
          QUARANTINED, or its periodic inspection is not VALID  -> BLOCKED
        every active item PASSed and none of the above applies  -> READY
        otherwise (e.g. some items still PENDING, nothing
          actively wrong yet)                                   -> OPEN

    A COMPLETED session is immutable and is never recomputed (requirement 13
    — "prevent accidental modification of the completed session").

    Persists the new status only if it actually changed, and logs a
    SESSION_<STATUS> session_event for that transition (requirement 14) —
    this is the mechanism behind the SESSION_READY / SESSION_BLOCKED /
    SESSION_OPEN events. Always commits. Returns the (possibly updated)
    InspectionSession.
    """
    session = dbmod.get_session(conn, session_id)
    if session.status == SESSION_STATUS_COMPLETED:
        return session

    items = dbmod.list_session_items(conn, session_id, include_removed=False)
    if not items:
        new_status = SESSION_STATUS_OPEN
    else:
        blocked = False
        all_pass = True
        for item in items:
            asset = dbmod.get_asset(conn, item.asset_id)
            if item.check_result == RESULT_FAIL:
                blocked = True
            if asset.current_status != STATUS_IN_SERVICE:
                blocked = True
            if asset.periodic_inspection_status != PERIODIC_INSPECTION_VALID:
                blocked = True
            if item.check_result != RESULT_PASS:
                all_pass = False
        if blocked:
            new_status = SESSION_STATUS_BLOCKED
        elif all_pass:
            new_status = SESSION_STATUS_READY
        else:
            new_status = SESSION_STATUS_OPEN

    if new_status != session.status:
        dbmod.set_session_status(conn, session_id, new_status)
        dbmod.insert_session_event(
            conn, session_id, f"SESSION_{new_status}", actor, session.status, new_status
        )
        conn.commit()
        session = dbmod.get_session(conn, session_id)
    return session


def add_asset_to_session(conn, session, tag_id, actor):
    """
    Simulated-NFC "add equipment" (requirement 5). Deliberately reuses
    resolve_tag() so an unknown or revoked tag fails safe in exactly the
    same way, and with exactly the same two exceptions, as the original
    single-asset /t/<tag_id> tap flow — there is only one fail-safe tag
    -resolution code path in this application. Raises UnknownTagError /
    RevokedTagError; callers already handle both.

    Does NOT reject a QUARANTINED / non-VALID asset (requirement 7 —
    "DO NOT silently treat it as usable," not "silently refuse to add it").
    The asset is added and its unsafe state is surfaced clearly by the
    caller/template and by recompute_session_status(), which will not let
    the session reach READY while it remains active.

    Duplicate protection (requirement 6): if this asset already has an
    ACTIVE item in this session, nothing is created — the existing item is
    returned with created=False so the caller can show
    "<asset> is already part of this inspection."

    Returns (session_item, created, asset).
    """
    _require_open_for_mutation(session)
    tag, asset = resolve_tag(conn, tag_id)

    existing = dbmod.get_active_session_item_for_asset(conn, session.id, asset.asset_id)
    if existing is not None:
        return existing, False, asset

    item_id = dbmod.create_session_item(conn, session.id, asset.asset_id, tag.tag_id)
    dbmod.insert_audit_event(
        conn, asset.asset_id, "ASSET_ADDED_TO_SESSION", actor,
        previous_state=None, new_state=None, reference=f"session:{session.id}", session_id=session.id,
    )
    conn.commit()
    recompute_session_status(conn, session.id, actor=actor)
    return dbmod.get_session_item(conn, item_id), True, asset


def remove_item_from_active_session(conn, session, item, actor):
    """
    Removes an item from the *active* set only (requirement 11). Never
    deletes the session_items row, never touches its check(s), never
    touches any audit event, never touches the asset itself — the item's
    full history remains exactly as it was, just excluded from the active
    count/readiness computation from now on.
    """
    _require_open_for_mutation(session)
    dbmod.remove_session_item(conn, item.id)
    dbmod.insert_audit_event(
        conn, item.asset_id, "ASSET_REMOVED_FROM_ACTIVE_SESSION", actor,
        previous_state="ACTIVE", new_state="REMOVED", reference=f"session:{session.id}", session_id=session.id,
    )
    conn.commit()
    return recompute_session_status(conn, session.id, actor=actor)


def record_session_item_check(conn, session, item, asset, checked_by, lift_supervisor, result,
                               failure_reason, checklist_confirmed, actor):
    """
    Pre-Use Check V2 (requirement 8): records a result for exactly one
    session item, via the same record_pre_use_check() used everywhere else
    in this app — so FAIL-quarantines-only-this-asset and
    PASS-never-releases-quarantine (Correction 1) apply identically here.
    Does not assume any other item in the session is affected.
    """
    _require_open_for_mutation(session)
    check = record_pre_use_check(
        conn, asset, tag_id_used=item.tag_id_used, checked_by=checked_by, lift_supervisor=lift_supervisor,
        result=result, failure_reason=failure_reason, session_item_id=item.id,
        checklist_confirmed=checklist_confirmed, session_id=session.id,
    )
    dbmod.set_session_item_check(conn, item.id, check.id, result)
    conn.commit()
    recompute_session_status(conn, session.id, actor=actor)
    return check


def complete_session(conn, session, actor):
    """
    COMPLETE INSPECTION (requirement 13). Only allowed when the session is
    READY (requirement 12 is the gate for requirement 13) — recomputes
    first so this is judged on current, not stale, state. Once completed, a
    session is immutable: every mutating function above refuses to touch it
    (SessionCompletedError).
    """
    _require_open_for_mutation(session)
    session = recompute_session_status(conn, session.id, actor=actor)
    if session.status != SESSION_STATUS_READY:
        raise SessionNotReadyError(session)

    dbmod.complete_session_row(conn, session.id)
    dbmod.insert_session_event(
        conn, session.id, "SESSION_COMPLETED", actor, SESSION_STATUS_READY, SESSION_STATUS_COMPLETED
    )
    conn.commit()
    return dbmod.get_session(conn, session.id)


# --- Asset Registry / Tag Commissioning (added 2026-08-17) ------------------
#
# The core model — a permanent Company Asset, structurally separate from its
# replaceable NFC Tag — already existed from day one of this MVP (Correction
# 2: the Tag ID is a routing token, never the equipment's identity), and
# workflow.assign_tag() already demonstrated "revoke old, issue new, asset
# unchanged." What was missing was the ADMIN-FACING commissioning workflow
# that enforces requirement 5 ("do not silently overwrite an existing active
# assignment; require an explicit replacement workflow") — assign_tag()
# above is explicitly documented as an internal test/seed helper, not that
# workflow, so it is deliberately left untouched here (and every one of the
# 66 existing tests that rely on its exact behaviour keeps passing). These
# three functions are the real, stricter admin primitives; the app.py admin
# routes call these, never assign_tag().


class AssetAlreadyRegisteredError(Exception):
    """Raised when an import/registration attempt uses an Asset ID that
    already exists. Fail-safe / requirement 5&9: never silently overwrite
    an existing Asset ID or its history."""

    def __init__(self, asset_id):
        self.asset_id = asset_id
        super().__init__(f"Asset ID '{asset_id}' is already registered")


class AssetAlreadyHasActiveTagError(Exception):
    """Raised by commission_tag() when the asset already has a current
    active tag — requirement 5: an initial assignment must never silently
    overwrite an existing active assignment. Use replace_tag() instead,
    which is its own explicit, auditable action."""

    def __init__(self, asset, existing_tag):
        self.asset = asset
        self.existing_tag = existing_tag
        super().__init__(
            f"Asset {asset.asset_id} already has an active tag ({existing_tag.tag_id}) — "
            "use the explicit replace-tag workflow instead"
        )


class NoActiveTagToReplaceError(Exception):
    """Raised by replace_tag() when the asset has no active tag to replace
    — that asset needs an initial commission_tag(), not a replacement."""

    def __init__(self, asset):
        self.asset = asset
        super().__init__(f"Asset {asset.asset_id} has no active tag to replace")


class TagIdAlreadyExistsError(Exception):
    """Raised when a chosen/simulated-read Tag ID collides with any tag
    that already exists (active or historical) for any asset — a real NFC
    tag's ID is unique, and this MVP simulation preserves that."""

    def __init__(self, tag_id):
        self.tag_id = tag_id
        super().__init__(f"Tag ID '{tag_id}' already exists and cannot be reused")


def register_asset(conn, asset_id, equipment_type, actor, serial_number=None, description=None,
                    manufacturer=None, model=None, wll=None, company=None,
                    periodic_inspection_status="VALID", periodic_inspection_due=None, notes=None):
    """
    Adds a new permanent asset to the registry (requirement 2/9) — e.g. from
    a CSV import row. Never overwrites an existing Asset ID (requirement 5
    &9's "never silently overwrite"): raises AssetAlreadyRegisteredError
    instead, so the caller can report it rather than lose or corrupt the
    existing record.

    Logs ASSET_REGISTERED (requirement 12). Deliberately NOT used by
    seed_data.seed_demo_data() — that startup path predates this function,
    is exercised by tests that assert an exact, already-approved audit
    -event sequence for the demo assets (e.g.
    tests/test_persistence.py::test_quarantined_status_survives_restart),
    and per this increment's explicit instruction ("all 66 currently
    passing tests must continue to pass"), is left completely unchanged.
    The demo assets are still fictitious pre-existing company assets
    conceptually (see README "Asset Registry"); they just weren't run
    through this newer, stricter code path retroactively.
    """
    if dbmod.get_asset(conn, asset_id) is not None:
        raise AssetAlreadyRegisteredError(asset_id)
    dbmod.create_asset(
        conn, asset_id, equipment_type, periodic_inspection_status=periodic_inspection_status,
        serial_number=serial_number, description=description, manufacturer=manufacturer,
        model=model, wll=wll, company=company, periodic_inspection_due=periodic_inspection_due,
        notes=notes,
    )
    conn.commit()
    dbmod.insert_audit_event(conn, asset_id, "ASSET_REGISTERED", actor, None, None, reference=None)
    conn.commit()
    return dbmod.get_asset(conn, asset_id)


def commission_tag(conn, asset, tag_id=None, actor="admin"):
    """
    Initial NFC tag commissioning for an existing registry asset
    (requirement 4/5): Asset Registry -> search/select -> ASSIGN NFC TAG.

    Refuses (AssetAlreadyHasActiveTagError) if the asset already has an
    active tag — that is exactly the "do not silently overwrite; require
    an explicit replacement workflow" rule; use replace_tag() for that
    case. tag_id is optional: pass None to simulate "read a fresh blank
    tag" (auto-generates via models.new_tag_id()), or pass an explicit
    value to simulate reading a specific physical tag — either way it must
    not collide with any tag that already exists (TagIdAlreadyExistsError).
    """
    existing_active = dbmod.get_active_tag(conn, asset.asset_id)
    if existing_active is not None:
        raise AssetAlreadyHasActiveTagError(asset, existing_active)

    tag_id = tag_id or new_tag_id()
    if dbmod.get_tag(conn, tag_id) is not None:
        raise TagIdAlreadyExistsError(tag_id)

    dbmod.create_tag(conn, tag_id, asset.asset_id, active=True)
    dbmod.insert_audit_event(conn, asset.asset_id, "TAG_ASSIGNED", actor, None, tag_id, reference=f"tag:{tag_id}")
    conn.commit()
    return dbmod.get_tag(conn, tag_id)


def replace_tag(conn, asset, new_tag_id_value=None, actor="admin"):
    """
    Explicit tag replacement (requirement 6): Asset -> existing tag ->
    REPLACE TAG -> revoke old -> assign new. Requires an existing active
    tag (NoActiveTagToReplaceError otherwise — use commission_tag() for a
    first-time assignment).

    Revokes the old tag, assigns the new one, and logs TAG_REVOKED,
    TAG_ASSIGNED, and TAG_REPLACED (requirement 12) — all as ordinary
    INSERTs into the same append-only audit_events table. Deliberately does
    NOT touch asset.current_status, checks, audit history, or session
    membership: the Asset ID, serial number, inspection history, quarantine
    state, and session history are all untouched by construction, because
    nothing here ever writes to the `assets`, `checks`, or `session_items`
    tables — only `tags` and `audit_events` (requirement 6/13: replacing a
    tag must never release quarantine or reset status).
    """
    existing_active = dbmod.get_active_tag(conn, asset.asset_id)
    if existing_active is None:
        raise NoActiveTagToReplaceError(asset)

    new_tag_id_value = new_tag_id_value or new_tag_id()
    if dbmod.get_tag(conn, new_tag_id_value) is not None:
        raise TagIdAlreadyExistsError(new_tag_id_value)

    old_tag_id = existing_active.tag_id
    dbmod.revoke_tag(conn, old_tag_id)
    dbmod.insert_audit_event(
        conn, asset.asset_id, "TAG_REVOKED", actor, old_tag_id, None, reference=f"tag:{old_tag_id}"
    )
    dbmod.create_tag(conn, new_tag_id_value, asset.asset_id, active=True)
    dbmod.insert_audit_event(
        conn, asset.asset_id, "TAG_ASSIGNED", actor, None, new_tag_id_value, reference=f"tag:{new_tag_id_value}"
    )
    dbmod.insert_audit_event(
        conn, asset.asset_id, "TAG_REPLACED", actor, old_tag_id, new_tag_id_value,
        reference=f"tag:{old_tag_id}->{new_tag_id_value}",
    )
    conn.commit()
    return dbmod.get_tag(conn, new_tag_id_value)
