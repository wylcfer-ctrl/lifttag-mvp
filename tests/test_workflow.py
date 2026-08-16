"""
Unit tests for the safety-critical state-transition logic in workflow.py.

These tests exist specifically to prove Correction 1 (approved 2026-08-16):
a PASS pre-use check must NEVER release an asset from quarantine.

Written with unittest (standard library, no extra install required). Also
discoverable and runnable by pytest, if installed, with no changes.
"""
import os
import tempfile
import unittest

import db as dbmod
from models import STATUS_IN_SERVICE, STATUS_QUARANTINED
from workflow import record_pre_use_check, resolve_tag, UnknownTagError, RevokedTagError


class WorkflowTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        dbmod.init_db(self.db_path)
        self.conn = dbmod.get_conn(self.db_path)

        dbmod.create_asset(self.conn, "TEST-001", "Test Sling", "VALID", STATUS_IN_SERVICE)
        self.conn.commit()
        self.asset = dbmod.get_asset(self.conn, "TEST-001")

        dbmod.create_tag(self.conn, "tag-active-1", "TEST-001", active=True)
        dbmod.create_tag(self.conn, "tag-revoked-1", "TEST-001", active=False)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        os.remove(self.db_path)

    # --- Correction 1: the four required state-transition cases -----------

    def test_in_service_pass_stays_in_service(self):
        record_pre_use_check(self.conn, self.asset, None, "Alice", "Bob", "PASS")
        self.assertEqual(self.asset.current_status, STATUS_IN_SERVICE)

    def test_in_service_fail_quarantines(self):
        record_pre_use_check(self.conn, self.asset, None, "Alice", "Bob", "FAIL")
        self.assertEqual(self.asset.current_status, STATUS_QUARANTINED)

    def test_quarantined_pass_does_not_release(self):
        record_pre_use_check(self.conn, self.asset, None, "Alice", "Bob", "FAIL")
        self.assertEqual(self.asset.current_status, STATUS_QUARANTINED)

        record_pre_use_check(self.conn, self.asset, None, "Carol", "Dave", "PASS")
        self.assertEqual(
            self.asset.current_status, STATUS_QUARANTINED, "PASS must never release quarantine"
        )

    def test_quarantined_fail_stays_quarantined(self):
        record_pre_use_check(self.conn, self.asset, None, "Alice", "Bob", "FAIL")
        record_pre_use_check(self.conn, self.asset, None, "Carol", "Dave", "FAIL")
        self.assertEqual(self.asset.current_status, STATUS_QUARANTINED)

    def test_repeated_pass_never_releases_quarantine(self):
        """Belt-and-braces: no number of subsequent PASS checks should ever
        release a quarantined asset."""
        record_pre_use_check(self.conn, self.asset, None, "Alice", "Bob", "FAIL")
        for _ in range(10):
            record_pre_use_check(self.conn, self.asset, None, "Someone", "Else", "PASS")
            self.assertEqual(self.asset.current_status, STATUS_QUARANTINED)

    def test_repeated_fail_while_quarantined_stays_quarantined(self):
        """Explicit requirement: repeated FAIL checks on an already-quarantined
        asset must leave it quarantined, and must not add a redundant
        STATUS_CHANGE event on every repeat (only the first FAIL that actually
        changes status logs one)."""
        record_pre_use_check(self.conn, self.asset, None, "Alice", "Bob", "FAIL")  # -> quarantines
        for _ in range(10):
            record_pre_use_check(self.conn, self.asset, None, "Someone", "Else", "FAIL")
            self.assertEqual(self.asset.current_status, STATUS_QUARANTINED)

        events = dbmod.list_audit_events(self.conn, self.asset.asset_id)
        status_changes = [e for e in events if e.event_type == "STATUS_CHANGE"]
        check_fails = [e for e in events if e.event_type == "CHECK_FAIL"]
        self.assertEqual(len(status_changes), 1, "only the initial IN SERVICE -> QUARANTINED transition should log a STATUS_CHANGE")
        self.assertEqual(len(check_fails), 11, "every FAIL check, including repeats, must still log CHECK_FAIL")

    # --- Audit event correctness --------------------------------------------

    def test_pass_creates_check_pass_event_only(self):
        record_pre_use_check(self.conn, self.asset, None, "Alice", "Bob", "PASS")
        events = dbmod.list_audit_events(self.conn, self.asset.asset_id)
        self.assertEqual([e.event_type for e in events], ["CHECK_PASS"])

    def test_fail_from_in_service_creates_check_fail_and_status_change(self):
        record_pre_use_check(self.conn, self.asset, None, "Alice", "Bob", "FAIL")
        events = dbmod.list_audit_events(self.conn, self.asset.asset_id)
        self.assertEqual([e.event_type for e in events], ["CHECK_FAIL", "STATUS_CHANGE"])
        status_change = events[1]
        self.assertEqual(status_change.previous_state, STATUS_IN_SERVICE)
        self.assertEqual(status_change.new_state, STATUS_QUARANTINED)

    def test_pass_while_quarantined_creates_check_pass_event_only(self):
        record_pre_use_check(self.conn, self.asset, None, "Alice", "Bob", "FAIL")  # quarantines
        record_pre_use_check(self.conn, self.asset, None, "Carol", "Dave", "PASS")
        events = dbmod.list_audit_events(self.conn, self.asset.asset_id)
        self.assertEqual(
            [e.event_type for e in events], ["CHECK_FAIL", "STATUS_CHANGE", "CHECK_PASS"]
        )

    def test_fail_when_already_quarantined_does_not_duplicate_status_change(self):
        record_pre_use_check(self.conn, self.asset, None, "Alice", "Bob", "FAIL")   # -> quarantines
        record_pre_use_check(self.conn, self.asset, None, "Carol", "Dave", "FAIL")  # already quarantined
        events = dbmod.list_audit_events(self.conn, self.asset.asset_id)
        self.assertEqual(
            [e.event_type for e in events],
            ["CHECK_FAIL", "STATUS_CHANGE", "CHECK_FAIL"],
            "a second FAIL while already quarantined must not add a redundant STATUS_CHANGE",
        )

    def test_no_code_path_mutates_or_deletes_audit_events(self):
        """Guards the append-only property described in the Design Proposal
        §7: db.py must never UPDATE/DELETE an audit_events row, and every
        write path must go through an INSERT."""
        import inspect

        source = inspect.getsource(dbmod)
        audit_section = source[source.index("# --- audit events"):]
        self.assertNotIn("UPDATE audit_events", audit_section)
        self.assertNotIn("DELETE FROM audit_events", audit_section)
        self.assertIn("INSERT INTO audit_events", audit_section)

    # --- Correction 2 / Tag ID resolution --------------------------------

    def test_active_tag_resolves_to_asset(self):
        tag, resolved_asset = resolve_tag(self.conn, "tag-active-1")
        self.assertEqual(resolved_asset.asset_id, self.asset.asset_id)
        self.assertTrue(tag.active)

    def test_unknown_tag_raises_and_does_not_resolve(self):
        with self.assertRaises(UnknownTagError):
            resolve_tag(self.conn, "this-tag-id-does-not-exist")

    def test_revoked_tag_raises_and_is_not_treated_as_current(self):
        with self.assertRaises(RevokedTagError) as ctx:
            resolve_tag(self.conn, "tag-revoked-1")
        self.assertEqual(ctx.exception.tag.tag_id, "tag-revoked-1")
        self.assertFalse(ctx.exception.tag.active)


if __name__ == "__main__":
    unittest.main()
