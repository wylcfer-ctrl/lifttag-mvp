"""
Tests for Inspection Session v1 + multi-asset workflow — added 2026-08-17.

HTTP-level tests via the Flask test client (mirroring tests/test_routes.py),
mixed with direct db.py/workflow.py access where that is the clearest way
to set up or verify state (mirroring tests/test_workflow.py).

These tests exist to prove the requirements of the "Inspection Session v1 +
multi-asset/multi-accessory workflow" increment, while confirming nothing
about the previously-approved single-asset safety logic changed:

  - create a session; add one asset; add many assets (no artificial limit);
  - duplicate-asset protection;
  - adding a quarantined / revoked / unknown tag is surfaced, never hidden;
  - each item gets its own independent PASS/FAIL result;
  - FAIL requires a non-empty reason;
  - FAIL quarantines only the failed asset — other active items are
    unaffected;
  - the session's derived status (OPEN/BLOCKED/READY) matches its items;
  - removing an item from the active set preserves all of its history;
  - a replacement asset can be added afterwards;
  - a session can only be completed from READY, and is immutable afterward;
  - audit events (session-level and asset-level) are recorded.

Written with unittest (standard library, no extra install required). Also
discoverable and runnable by pytest, if installed, with no changes.
"""
import os
import tempfile
import unittest

import db as dbmod
from app import create_app
from workflow import (
    record_pre_use_check,
    assign_tag,
    recompute_session_status,
    complete_session,
    SessionNotReadyError,
    SessionCompletedError,
)
from models import (
    STATUS_IN_SERVICE,
    STATUS_QUARANTINED,
    SESSION_STATUS_OPEN,
    SESSION_STATUS_READY,
    SESSION_STATUS_BLOCKED,
    SESSION_STATUS_COMPLETED,
)
from seed_data import DEMO_TAG_IDS


def _full_checklist():
    return {
        "chk_visual": "on",
        "chk_tag_legible": "on",
        "chk_no_damage": "on",
        "chk_no_wear": "on",
        "chk_connections": "on",
    }


class SessionsTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.app = create_app(database_path=self.db_path)  # seeds the demo assets automatically
        self.client = self.app.test_client()

    def tearDown(self):
        os.remove(self.db_path)

    # --- helpers -------------------------------------------------------

    def _start_session(self, lift_supervisor="John Smith", slinger_signaller="Mark Jones"):
        resp = self.client.post(
            "/session/new",
            data={"lift_supervisor": lift_supervisor, "slinger_signaller": slinger_signaller},
        )
        self.assertEqual(resp.status_code, 302)
        session_id = int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1])
        return session_id

    def _add(self, session_id, tag_id):
        return self.client.get(f"/session/{session_id}/add/{tag_id}")

    def _active_items(self, session_id):
        conn = dbmod.get_conn(self.db_path)
        items = dbmod.list_session_items(conn, session_id, include_removed=False)
        conn.close()
        return items

    def _check(self, session_id, item_id, result, failure_reason=None, checklist=True):
        data = {"result": result}
        if checklist:
            data.update(_full_checklist())
        if failure_reason is not None:
            data["failure_reason"] = failure_reason
        return self.client.post(f"/session/{session_id}/item/{item_id}/check", data=data)

    # --- 1. create inspection session ------------------------------------

    def test_create_inspection_session(self):
        session_id = self._start_session("Alice Supervisor", "Bob Slinger")
        conn = dbmod.get_conn(self.db_path)
        session = dbmod.get_session(conn, session_id)
        conn.close()

        self.assertEqual(session.status, SESSION_STATUS_OPEN)
        self.assertEqual(session.lift_supervisor, "Alice Supervisor")
        self.assertEqual(session.slinger_signaller, "Bob Slinger")
        self.assertIsNotNone(session.created_at)
        self.assertIsNotNone(session.inspection_date)
        self.assertIsNone(session.completed_at)

    def test_session_creation_requires_supervisor_and_slinger(self):
        resp = self.client.post("/session/new", data={"lift_supervisor": "", "slinger_signaller": ""})
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b"required", resp.data.lower())

    def test_inspection_date_is_server_generated_not_client_supplied(self):
        """The session form has no date/time field at all — the server
        always stamps its own now(). This test proves that by confirming
        the created session's inspection_date is close to 'now', with no
        way for a client-supplied value to have been used (there is no
        such field to submit)."""
        from datetime import datetime, timezone
        before = datetime.now(timezone.utc)
        session_id = self._start_session()
        after = datetime.now(timezone.utc)

        conn = dbmod.get_conn(self.db_path)
        session = dbmod.get_session(conn, session_id)
        conn.close()
        self.assertTrue(before <= session.inspection_date <= after)

    # --- 2/4. add one asset / add multiple assets, no artificial limit ---

    def test_add_one_asset(self):
        session_id = self._start_session()
        self._add(session_id, DEMO_TAG_IDS["BIN-001"])
        items = self._active_items(session_id)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].asset_id, "BIN-001")

    def test_add_many_assets_no_artificial_limit(self):
        session_id = self._start_session()
        # All seven demo assets — deliberately more than the five in the
        # illustrative example, to prove there is no hardcoded cap.
        for asset_id, tag_id in DEMO_TAG_IDS.items():
            self._add(session_id, tag_id)
        items = self._active_items(session_id)
        self.assertEqual(len(items), len(DEMO_TAG_IDS))

    # --- 6. duplicate protection ------------------------------------------

    def test_duplicate_asset_is_not_added_twice(self):
        session_id = self._start_session()
        self._add(session_id, DEMO_TAG_IDS["BIN-001"])
        resp = self._add(session_id, DEMO_TAG_IDS["BIN-001"])

        self.assertEqual(resp.status_code, 302)
        self.assertIn("duplicate=BIN-001", resp.headers["Location"])
        items = self._active_items(session_id)
        self.assertEqual(len(items), 1, "BIN-001 must not be added twice")

        picker = self.client.get(f"/session/{session_id}/add?duplicate=BIN-001")
        self.assertIn(b"BIN-001 is already part of this inspection", picker.data)

    # --- 7. safety validation when adding an asset ------------------------

    def test_adding_a_quarantined_asset_is_surfaced_not_hidden(self):
        conn = dbmod.get_conn(self.db_path)
        asset = dbmod.get_asset(conn, "SHACKLE-001")
        record_pre_use_check(conn, asset, DEMO_TAG_IDS["SHACKLE-001"], "Checker", "Supervisor", "FAIL")
        conn.close()

        session_id = self._start_session()
        resp = self._add(session_id, DEMO_TAG_IDS["SHACKLE-001"])
        self.assertIn("added=SHACKLE-001", resp.headers["Location"])

        items = self._active_items(session_id)
        self.assertEqual(len(items), 1, "a quarantined asset is still added, not silently refused")

        picker = self.client.get(f"/session/{session_id}/add?added=SHACKLE-001")
        self.assertIn(b"QUARANTINED", picker.data)  # safety state shown clearly, not hidden

        conn = dbmod.get_conn(self.db_path)
        session = recompute_session_status(conn, session_id)
        conn.close()
        self.assertEqual(session.status, SESSION_STATUS_BLOCKED)

    def test_adding_a_revoked_tag_does_not_add_anything(self):
        conn = dbmod.get_conn(self.db_path)
        asset = dbmod.get_asset(conn, "SLING-001")
        assign_tag(conn, asset, actor="test-harness")  # revokes demo-sling-001, issues a new random tag
        conn.close()

        session_id = self._start_session()
        resp = self._add(session_id, DEMO_TAG_IDS["SLING-001"])
        self.assertIn("revoked=", resp.headers["Location"])
        self.assertEqual(len(self._active_items(session_id)), 0)

    def test_adding_an_unknown_tag_does_not_add_anything(self):
        session_id = self._start_session()
        resp = self._add(session_id, "does-not-exist")
        self.assertIn("unknown=does-not-exist", resp.headers["Location"])
        self.assertEqual(len(self._active_items(session_id)), 0)

    # --- 8/9. individual PASS / FAIL, independent per item -----------------

    def test_individual_pass(self):
        session_id = self._start_session()
        self._add(session_id, DEMO_TAG_IDS["BIN-001"])
        item = self._active_items(session_id)[0]

        resp = self._check(session_id, item.id, "PASS")
        self.assertEqual(resp.status_code, 302)

        conn = dbmod.get_conn(self.db_path)
        refreshed = dbmod.get_session_item(conn, item.id)
        asset = dbmod.get_asset(conn, "BIN-001")
        conn.close()
        self.assertEqual(refreshed.check_result, "PASS")
        self.assertEqual(asset.current_status, STATUS_IN_SERVICE)

    def test_individual_fail_quarantines_only_that_asset(self):
        session_id = self._start_session()
        self._add(session_id, DEMO_TAG_IDS["BIN-001"])
        self._add(session_id, DEMO_TAG_IDS["SHACKLE-001"])
        self._add(session_id, DEMO_TAG_IDS["SLING-001"])
        items = {i.asset_id: i for i in self._active_items(session_id)}

        self._check(session_id, items["BIN-001"].id, "PASS")
        self._check(session_id, items["SHACKLE-001"].id, "FAIL", failure_reason="Visible crack in body")
        self._check(session_id, items["SLING-001"].id, "PASS")

        conn = dbmod.get_conn(self.db_path)
        bin_asset = dbmod.get_asset(conn, "BIN-001")
        shackle_asset = dbmod.get_asset(conn, "SHACKLE-001")
        sling_asset = dbmod.get_asset(conn, "SLING-001")
        conn.close()

        self.assertEqual(shackle_asset.current_status, STATUS_QUARANTINED)
        self.assertEqual(bin_asset.current_status, STATUS_IN_SERVICE, "PASS-ing item must not be affected by a different item's FAIL")
        self.assertEqual(sling_asset.current_status, STATUS_IN_SERVICE, "other passing assets remain IN SERVICE")

    # --- 10. FAIL requires a non-empty reason ------------------------------

    def test_fail_without_reason_is_rejected(self):
        session_id = self._start_session()
        self._add(session_id, DEMO_TAG_IDS["BIN-001"])
        item = self._active_items(session_id)[0]

        resp = self._check(session_id, item.id, "FAIL")  # no failure_reason
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b"failure reason", resp.data.lower())

        refreshed = dbmod.get_session_item(dbmod.get_conn(self.db_path), item.id)
        self.assertEqual(refreshed.check_result, "PENDING", "a rejected submission must not record a result")

    def test_checklist_must_be_fully_confirmed(self):
        session_id = self._start_session()
        self._add(session_id, DEMO_TAG_IDS["BIN-001"])
        item = self._active_items(session_id)[0]

        resp = self._check(session_id, item.id, "PASS", checklist=False)
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b"checklist", resp.data.lower())

    # --- 13. session BLOCKED while a failed active item exists --------------

    def test_session_blocked_while_failed_item_is_active(self):
        session_id = self._start_session()
        self._add(session_id, DEMO_TAG_IDS["BIN-001"])
        self._add(session_id, DEMO_TAG_IDS["SHACKLE-001"])
        items = {i.asset_id: i for i in self._active_items(session_id)}
        self._check(session_id, items["BIN-001"].id, "PASS")
        self._check(session_id, items["SHACKLE-001"].id, "FAIL", failure_reason="Bent pin")

        conn = dbmod.get_conn(self.db_path)
        session = dbmod.get_session(conn, session_id)
        conn.close()
        self.assertEqual(session.status, SESSION_STATUS_BLOCKED)

    # --- 11. remove a failed item from the active set, history preserved ---

    def test_remove_failed_item_preserves_history(self):
        session_id = self._start_session()
        self._add(session_id, DEMO_TAG_IDS["SHACKLE-001"])
        item = self._active_items(session_id)[0]
        self._check(session_id, item.id, "FAIL", failure_reason="Deformed body")

        resp = self.client.post(f"/session/{session_id}/item/{item.id}/remove")
        self.assertEqual(resp.status_code, 302)

        self.assertEqual(len(self._active_items(session_id)), 0, "removed item must not count as active")

        conn = dbmod.get_conn(self.db_path)
        all_items = dbmod.list_session_items(conn, session_id, include_removed=True)
        preserved = next(i for i in all_items if i.id == item.id)
        checks = conn.execute("SELECT * FROM checks WHERE session_item_id = ?", (item.id,)).fetchall()
        events = dbmod.list_audit_events(conn, "SHACKLE-001")
        asset = dbmod.get_asset(conn, "SHACKLE-001")
        conn.close()

        self.assertEqual(preserved.item_status, "REMOVED")
        self.assertEqual(preserved.check_result, "FAIL", "the historical result is not erased")
        self.assertEqual(len(checks), 1, "the FAIL check itself must still exist")
        self.assertTrue(any(e.event_type == "CHECK_FAIL" for e in events), "audit history preserved")
        self.assertTrue(any(e.event_type == "STATUS_CHANGE" for e in events), "audit history preserved")
        self.assertEqual(asset.current_status, STATUS_QUARANTINED, "the asset itself is still quarantined — removal from a session is not a release")

    # --- 15. a replacement asset can be added afterwards --------------------

    def test_replacement_asset_can_be_added_after_removal(self):
        session_id = self._start_session()
        self._add(session_id, DEMO_TAG_IDS["SHACKLE-001"])
        failed_item = self._active_items(session_id)[0]
        self._check(session_id, failed_item.id, "FAIL", failure_reason="Cracked")
        self.client.post(f"/session/{session_id}/item/{failed_item.id}/remove")

        self._add(session_id, DEMO_TAG_IDS["SHACKLE-002"])
        active = self._active_items(session_id)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].asset_id, "SHACKLE-002")

    # --- 16/17/18. READY gating ----------------------------------------------

    def test_session_not_ready_while_item_pending(self):
        session_id = self._start_session()
        self._add(session_id, DEMO_TAG_IDS["BIN-001"])
        conn = dbmod.get_conn(self.db_path)
        session = recompute_session_status(conn, session_id)
        conn.close()
        self.assertNotEqual(session.status, SESSION_STATUS_READY)

    def test_session_not_ready_while_failed_item_active(self):
        session_id = self._start_session()
        self._add(session_id, DEMO_TAG_IDS["BIN-001"])
        item = self._active_items(session_id)[0]
        self._check(session_id, item.id, "FAIL", failure_reason="Damaged")

        conn = dbmod.get_conn(self.db_path)
        session = recompute_session_status(conn, session_id)
        conn.close()
        self.assertEqual(session.status, SESSION_STATUS_BLOCKED)
        self.assertNotEqual(session.status, SESSION_STATUS_READY)

    def test_session_ready_when_all_active_items_pass(self):
        session_id = self._start_session()
        self._add(session_id, DEMO_TAG_IDS["BIN-001"])
        self._add(session_id, DEMO_TAG_IDS["SLING-001"])
        for item in self._active_items(session_id):
            self._check(session_id, item.id, "PASS")

        conn = dbmod.get_conn(self.db_path)
        session = recompute_session_status(conn, session_id)
        conn.close()
        self.assertEqual(session.status, SESSION_STATUS_READY)

    # --- 19/20. complete session; completed session is immutable -----------

    def test_complete_session(self):
        session_id = self._start_session()
        self._add(session_id, DEMO_TAG_IDS["BIN-001"])
        item = self._active_items(session_id)[0]
        self._check(session_id, item.id, "PASS")

        resp = self.client.post(f"/session/{session_id}/complete")
        self.assertEqual(resp.status_code, 302)

        conn = dbmod.get_conn(self.db_path)
        session = dbmod.get_session(conn, session_id)
        conn.close()
        self.assertEqual(session.status, SESSION_STATUS_COMPLETED)
        self.assertIsNotNone(session.completed_at)

    def test_cannot_complete_a_session_that_is_not_ready(self):
        session_id = self._start_session()
        self._add(session_id, DEMO_TAG_IDS["BIN-001"])  # still PENDING
        resp = self.client.post(f"/session/{session_id}/complete")
        self.assertEqual(resp.status_code, 400)

        conn = dbmod.get_conn(self.db_path)
        session = dbmod.get_session(conn, session_id)
        conn.close()
        self.assertNotEqual(session.status, SESSION_STATUS_COMPLETED)

    def test_completed_session_cannot_be_modified(self):
        session_id = self._start_session()
        self._add(session_id, DEMO_TAG_IDS["BIN-001"])
        item = self._active_items(session_id)[0]
        self._check(session_id, item.id, "PASS")
        self.client.post(f"/session/{session_id}/complete")

        # Adding another asset must be refused (redirected away, no item created).
        resp = self._add(session_id, DEMO_TAG_IDS["SLING-001"])
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(self._active_items(session_id)), 1, "no new item may be added to a completed session")

        # Re-checking the existing item must be refused.
        resp = self._check(session_id, item.id, "FAIL", failure_reason="trying to sneak this in")
        self.assertEqual(resp.status_code, 302)
        conn = dbmod.get_conn(self.db_path)
        refreshed = dbmod.get_session_item(conn, item.id)
        conn.close()
        self.assertEqual(refreshed.check_result, "PASS", "a completed session's recorded results must not change")

        # Removing an item must be refused.
        resp = self.client.post(f"/session/{session_id}/item/{item.id}/remove")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(self._active_items(session_id)), 1, "a completed session's membership must not change")

        # Direct workflow-layer calls must also refuse (defence in depth,
        # not just a route-level guard) — the route handlers above rely on
        # exactly these exceptions to decide to redirect instead of mutate.
        from workflow import add_asset_to_session, remove_item_from_active_session, record_session_item_check

        conn = dbmod.get_conn(self.db_path)
        session = dbmod.get_session(conn, session_id)
        asset = dbmod.get_asset(conn, "BIN-001")
        session_item = dbmod.get_session_item(conn, item.id)

        with self.assertRaises(SessionCompletedError):
            complete_session(conn, session, actor="test")
        with self.assertRaises(SessionCompletedError):
            add_asset_to_session(conn, session, DEMO_TAG_IDS["SLING-001"], actor="test")
        with self.assertRaises(SessionCompletedError):
            remove_item_from_active_session(conn, session, session_item, actor="test")
        with self.assertRaises(SessionCompletedError):
            record_session_item_check(
                conn, session, session_item, asset, checked_by="A", lift_supervisor="B",
                result="FAIL", failure_reason="x", checklist_confirmed=True, actor="test",
            )
        conn.close()

    # --- 21. audit events are recorded --------------------------------------

    def test_audit_events_are_recorded_for_session_activity(self):
        session_id = self._start_session()
        self._add(session_id, DEMO_TAG_IDS["BIN-001"])
        item = self._active_items(session_id)[0]
        self._check(session_id, item.id, "PASS")

        conn = dbmod.get_conn(self.db_path)
        session_events = dbmod.list_session_events(conn, session_id)
        asset_events = dbmod.list_audit_events_for_session(conn, session_id)
        conn.close()

        session_event_types = {e["event_type"] for e in session_events}
        self.assertIn("SESSION_CREATED", session_event_types)
        self.assertIn("SESSION_READY", session_event_types)

        asset_event_types = {e.event_type for e in asset_events}
        self.assertIn("ASSET_ADDED_TO_SESSION", asset_event_types)
        self.assertIn("CHECK_PASS", asset_event_types)

    def test_session_audit_page_renders(self):
        session_id = self._start_session()
        self._add(session_id, DEMO_TAG_IDS["BIN-001"])
        resp = self.client.get(f"/session/{session_id}/audit")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"SESSION_CREATED", resp.data)
        self.assertIn(b"ASSET_ADDED_TO_SESSION", resp.data)

    # --- 22/24. existing routes / startup seeding are unaffected ------------

    def test_existing_single_asset_route_still_works(self):
        resp = self.client.get(f"/t/{DEMO_TAG_IDS['SLING-001']}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"SLING-001", resp.data)
        self.assertIn(b"Start Pre-Use Check", resp.data)

    def test_startup_seeding_now_covers_seven_demo_assets_idempotently(self):
        conn = dbmod.get_conn(self.db_path)
        for asset_id in DEMO_TAG_IDS:
            self.assertIsNotNone(dbmod.get_asset(conn, asset_id))
        asset_count_before = len(dbmod.list_assets(conn))
        conn.close()

        create_app(database_path=self.db_path)  # simulate another startup against the same db
        create_app(database_path=self.db_path)

        conn = dbmod.get_conn(self.db_path)
        asset_count_after = len(dbmod.list_assets(conn))
        conn.close()
        self.assertEqual(asset_count_after, asset_count_before, "repeated startup must not duplicate any demo asset")
        self.assertEqual(asset_count_before, len(DEMO_TAG_IDS))


if __name__ == "__main__":
    unittest.main()
