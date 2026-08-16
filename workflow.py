"""
LiftTag MVP — safety-critical workflow logic.

Kept separate from Flask routing so the state-transition logic can be
unit-tested directly (see tests/test_workflow.py) without an HTTP cycle.

Correction 1 (mandatory, approved 2026-08-16): a PASS pre-use check must
NEVER release an asset from quarantine. There is no release-from-quarantine
workflow in this MVP, and none is added here.
"""
import db as dbmod
from models import STATUS_QUARANTINED, RESULT_PASS, RESULT_FAIL, new_tag_id


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


def record_pre_use_check(conn, asset, tag_id_used, checked_by, lift_supervisor, result):
    """
    Record a pre-use check and apply the approved state-transition table
    (Design Proposal §8, Correction 1):

        IN SERVICE  + PASS -> stays IN SERVICE       (CHECK_PASS)
        IN SERVICE  + FAIL -> becomes QUARANTINED     (CHECK_FAIL, STATUS_CHANGE)
        QUARANTINED + PASS -> stays QUARANTINED       (CHECK_PASS)  -- never releases
        QUARANTINED + FAIL -> stays QUARANTINED       (CHECK_FAIL only, no redundant STATUS_CHANGE)

    There is no code path here, or anywhere else in this application, that
    sets current_status back to IN SERVICE. Quarantine release is out of
    scope for this MVP.

    Commits the transaction (check + audit event(s) + optional status
    change) as a single unit on the given connection.
    """
    if result not in (RESULT_PASS, RESULT_FAIL):
        raise ValueError("result must be 'PASS' or 'FAIL'")

    actor = f"{checked_by} (Checked By) / {lift_supervisor} (Lift Supervisor)"
    previous_status = asset.current_status

    check_id = dbmod.insert_check(conn, asset.asset_id, tag_id_used, checked_by, lift_supervisor, result)
    reference = f"check:{check_id}"

    if result == RESULT_PASS:
        # Deliberately NO status mutation on PASS, regardless of current_status.
        dbmod.insert_audit_event(conn, asset.asset_id, "CHECK_PASS", actor, previous_status, previous_status, reference)
    else:  # RESULT_FAIL
        dbmod.insert_audit_event(conn, asset.asset_id, "CHECK_FAIL", actor, previous_status, previous_status, reference)
        if previous_status != STATUS_QUARANTINED:
            dbmod.set_asset_status(conn, asset.asset_id, STATUS_QUARANTINED)
            dbmod.insert_audit_event(
                conn, asset.asset_id, "STATUS_CHANGE", actor, previous_status, STATUS_QUARANTINED, reference
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
