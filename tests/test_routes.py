"""
Route-level (HTTP) tests for the LiftTag MVP.

Covers active / revoked / unknown Tag IDs, the full check submission flow,
and the persistent test-environment banner.

Written with unittest (standard library, no extra install required). Also
discoverable and runnable by pytest, if installed, with no changes.
"""
import os
import tempfile
import unittest

import db as dbmod
from app import create_app


class RoutesTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.app = create_app(database_path=self.db_path)

        conn = dbmod.get_conn(self.db_path)
        dbmod.create_asset(conn, "ROUTE-001", "Test Sling")
        dbmod.create_tag(conn, "active-tag", "ROUTE-001", active=True)
        dbmod.create_tag(conn, "old-tag", "ROUTE-001", active=False)
        conn.commit()
        conn.close()

        self.client = self.app.test_client()

    def tearDown(self):
        os.remove(self.db_path)

    def test_active_tag_shows_asset_page(self):
        resp = self.client.get("/t/active-tag")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"ROUTE-001", resp.data)
        self.assertIn(b"Start Pre-Use Check", resp.data)

    def test_asset_page_shows_all_eight_required_fields(self):
        """The main asset screen must clearly show: Asset ID, Equipment Type,
        Current Tag ID, Periodic Inspection Status, Current Equipment Status,
        Last Pre-Use Check, Start Pre-Use Check, View Audit History."""
        resp = self.client.get("/t/active-tag")
        body = resp.data.decode("utf-8")
        self.assertIn("ROUTE-001", body)  # Asset ID
        self.assertIn("Equipment type", body)
        self.assertIn("Test Sling", body)  # equipment type value
        self.assertIn("Current Tag ID", body)
        self.assertIn("active-tag", body)
        self.assertIn("Periodic inspection status", body)
        self.assertIn("Current equipment status", body)
        self.assertIn("IN SERVICE", body)
        self.assertIn("Last pre-use check", body)
        self.assertIn("Start Pre-Use Check", body)
        self.assertIn("View Audit History", body)

    def test_quarantine_banner_is_prominent_on_asset_page(self):
        self.client.post(
            "/t/active-tag/check", data={"checked_by": "A", "lift_supervisor": "B", "result": "FAIL"}
        )
        resp = self.client.get("/t/active-tag")
        body = resp.data.decode("utf-8")
        self.assertIn('alert-danger">QUARANTINED — DO NOT USE', body)
        self.assertIn("Current equipment status", body)

    def test_unknown_tag_returns_fail_safe_page(self):
        resp = self.client.get("/t/does-not-exist")
        self.assertEqual(resp.status_code, 404)
        self.assertIn(b"nrecognised", resp.data)  # "Unrecognised"/"unrecognised"

    def test_revoked_tag_does_not_present_as_current(self):
        resp = self.client.get("/t/old-tag")
        self.assertEqual(resp.status_code, 200)
        body = resp.data.lower()
        self.assertIn(b"no longer active", body)
        self.assertNotIn(b"start pre-use check", body)

    def test_persistent_banner_present_on_every_page_type(self):
        banner = "LiftTag MVP — Test Environment — Fictitious Data Only — Not for Operational Use".encode("utf-8")
        for path in ("/", "/t/active-tag", "/t/old-tag", "/t/does-not-exist"):
            resp = self.client.get(path)
            self.assertIn(banner, resp.data, f"banner missing on {path}")

    def test_full_check_flow_fail_quarantines(self):
        resp = self.client.post(
            "/t/active-tag/check",
            data={"checked_by": "Alice", "lift_supervisor": "Bob", "result": "FAIL"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"QUARANTINED", resp.data)

    def test_pass_after_quarantine_keeps_quarantine_and_explains_no_release(self):
        self.client.post(
            "/t/active-tag/check", data={"checked_by": "A", "lift_supervisor": "B", "result": "FAIL"}
        )
        resp = self.client.post(
            "/t/active-tag/check",
            data={"checked_by": "C", "lift_supervisor": "D", "result": "PASS"},
            follow_redirects=True,
        )
        body = resp.data.lower()
        self.assertIn(b"quarantined", body)
        self.assertIn(b"does not release", body)

    def test_another_device_sees_latest_state(self):
        """Simulates requirement 8: a second 'device' (a fresh request with
        no shared client-side state) opening the same tag must see the new
        state."""
        self.client.post(
            "/t/active-tag/check", data={"checked_by": "A", "lift_supervisor": "B", "result": "FAIL"}
        )
        resp = self.client.get("/t/active-tag")
        self.assertIn(b"QUARANTINED", resp.data)

    def test_check_form_rejects_missing_fields(self):
        resp = self.client.post(
            "/t/active-tag/check",
            data={"checked_by": "", "lift_supervisor": "", "result": ""},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b"required", resp.data.lower())

    def test_audit_history_lists_events_and_is_read_only(self):
        self.client.post(
            "/t/active-tag/check", data={"checked_by": "A", "lift_supervisor": "B", "result": "FAIL"}
        )
        resp = self.client.get("/asset/ROUTE-001/audit")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"CHECK_FAIL", resp.data)
        self.assertIn(b"STATUS_CHANGE", resp.data)
        self.assertNotIn(b"<form", resp.data)  # no edit/delete controls


if __name__ == "__main__":
    unittest.main()
