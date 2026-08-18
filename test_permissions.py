"""
Tests for the DEMO ONLY AP / Supervisor / Field User permission
architecture — added 2026-08-18.

DEMO ACCESS — NOT AUTHENTICATED throughout: there is no password anywhere
here. These tests confirm the ROLE ARCHITECTURE works as specified (a
real registry, real route-level enforcement, a real audit trail) and that
it is impossible to grant/act-as via free text (only a currently
registered identity can be selected) — NOT that it is secure against
impersonation, which is explicitly out of scope for this increment.
"""
import os
import tempfile
import unittest

import db as dbmod
from app import create_app
from workflow import (
    grant_supervisor,
    revoke_supervisor,
    get_role,
    NotAuthorizedError,
    RoleAlreadyGrantedError,
    NoActiveGrantError,
)
from models import ROLE_AP, ROLE_SUPERVISOR
from seed_data import DEMO_AP_NAME, DEMO_TAG_IDS


class DemoRoleWorkflowTestCase(unittest.TestCase):
    """Direct unit tests of the workflow-layer grant/revoke functions."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.app = create_app(database_path=self.db_path)  # bootstraps Demo AP

    def tearDown(self):
        os.remove(self.db_path)

    def test_bootstrap_ap_exists_after_startup(self):
        conn = dbmod.get_conn(self.db_path)
        role = get_role(conn, DEMO_AP_NAME)
        conn.close()
        self.assertEqual(role, ROLE_AP)

    def test_ap_may_grant_supervisor(self):
        conn = dbmod.get_conn(self.db_path)
        grant_supervisor(conn, "Sup One", granted_by_name=DEMO_AP_NAME, actor=DEMO_AP_NAME)
        role = get_role(conn, "Sup One")
        conn.close()
        self.assertEqual(role, ROLE_SUPERVISOR)

    def test_ap_may_revoke_supervisor(self):
        conn = dbmod.get_conn(self.db_path)
        grant_supervisor(conn, "Sup Two", granted_by_name=DEMO_AP_NAME, actor=DEMO_AP_NAME)
        revoke_supervisor(conn, "Sup Two", revoked_by_name=DEMO_AP_NAME, actor=DEMO_AP_NAME)
        role = get_role(conn, "Sup Two")
        conn.close()
        self.assertIsNone(role, "a revoked Supervisor must have no active role")

    def test_supervisor_cannot_grant_supervisor(self):
        conn = dbmod.get_conn(self.db_path)
        grant_supervisor(conn, "Sup Three", granted_by_name=DEMO_AP_NAME, actor=DEMO_AP_NAME)
        with self.assertRaises(NotAuthorizedError):
            grant_supervisor(conn, "Sup Four", granted_by_name="Sup Three", actor="Sup Three")
        conn.close()

    def test_supervisor_cannot_revoke_another_supervisor(self):
        conn = dbmod.get_conn(self.db_path)
        grant_supervisor(conn, "Sup Five", granted_by_name=DEMO_AP_NAME, actor=DEMO_AP_NAME)
        grant_supervisor(conn, "Sup Six", granted_by_name=DEMO_AP_NAME, actor=DEMO_AP_NAME)
        with self.assertRaises(NotAuthorizedError):
            revoke_supervisor(conn, "Sup Six", revoked_by_name="Sup Five", actor="Sup Five")
        conn.close()

    def test_double_grant_is_rejected(self):
        conn = dbmod.get_conn(self.db_path)
        grant_supervisor(conn, "Sup Seven", granted_by_name=DEMO_AP_NAME, actor=DEMO_AP_NAME)
        with self.assertRaises(RoleAlreadyGrantedError):
            grant_supervisor(conn, "Sup Seven", granted_by_name=DEMO_AP_NAME, actor=DEMO_AP_NAME)
        conn.close()

    def test_revoke_with_no_active_grant_is_rejected(self):
        conn = dbmod.get_conn(self.db_path)
        with self.assertRaises(NoActiveGrantError):
            revoke_supervisor(conn, "Nobody", revoked_by_name=DEMO_AP_NAME, actor=DEMO_AP_NAME)
        conn.close()

    def test_grant_creates_an_auditable_event(self):
        conn = dbmod.get_conn(self.db_path)
        grant_supervisor(conn, "Sup Eight", granted_by_name=DEMO_AP_NAME, actor=DEMO_AP_NAME)
        events = dbmod.list_demo_auth_events(conn)
        conn.close()
        self.assertTrue(any(e["event_type"] == "SUPERVISOR_GRANTED" and e["target_name"] == "Sup Eight" for e in events))

    def test_revoke_creates_an_auditable_event(self):
        conn = dbmod.get_conn(self.db_path)
        grant_supervisor(conn, "Sup Nine", granted_by_name=DEMO_AP_NAME, actor=DEMO_AP_NAME)
        revoke_supervisor(conn, "Sup Nine", revoked_by_name=DEMO_AP_NAME, actor=DEMO_AP_NAME)
        events = dbmod.list_demo_auth_events(conn)
        conn.close()
        self.assertTrue(any(e["event_type"] == "SUPERVISOR_REVOKED" and e["target_name"] == "Sup Nine" for e in events))


class RouteAuthorizationTestCase(unittest.TestCase):
    """HTTP-level tests: /admin/* routes gated, pre-use checking ungated."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.app = create_app(database_path=self.db_path)
        self.client = self.app.test_client()
        conn = dbmod.get_conn(self.db_path)
        grant_supervisor(conn, "Field Sup", granted_by_name=DEMO_AP_NAME, actor=DEMO_AP_NAME)
        conn.close()

    def tearDown(self):
        os.remove(self.db_path)

    def _act_as(self, name):
        with self.client.session_transaction() as sess:
            if name is None:
                sess.pop("demo_actor_name", None)
            else:
                sess["demo_actor_name"] = name

    def test_unregistered_field_user_cannot_access_asset_registry(self):
        self._act_as(None)
        resp = self.client.get("/admin/assets")
        self.assertEqual(resp.status_code, 403)

    def test_unregistered_field_user_cannot_access_admin_users(self):
        self._act_as(None)
        resp = self.client.get("/admin/users")
        self.assertEqual(resp.status_code, 403)

    def test_ap_can_access_asset_registry(self):
        self._act_as(DEMO_AP_NAME)
        resp = self.client.get("/admin/assets")
        self.assertEqual(resp.status_code, 200)

    def test_supervisor_can_access_asset_registry(self):
        self._act_as("Field Sup")
        resp = self.client.get("/admin/assets")
        self.assertEqual(resp.status_code, 200)

    def test_supervisor_cannot_access_admin_users(self):
        """/admin/users is AP-only — a Supervisor may operate NFC/registry
        functions but may not manage other users' access."""
        self._act_as("Field Sup")
        resp = self.client.get("/admin/users")
        self.assertEqual(resp.status_code, 403)

    def test_ap_can_access_admin_users(self):
        self._act_as(DEMO_AP_NAME)
        resp = self.client.get("/admin/users")
        self.assertEqual(resp.status_code, 200)

    def test_supervisor_cannot_grant_via_route(self):
        self._act_as("Field Sup")
        resp = self.client.post("/admin/users/grant", data={"name": "Sneaky"})
        self.assertEqual(resp.status_code, 403)
        conn = dbmod.get_conn(self.db_path)
        role = get_role(conn, "Sneaky")
        conn.close()
        self.assertIsNone(role)

    def test_ap_can_grant_via_route(self):
        self._act_as(DEMO_AP_NAME)
        resp = self.client.post("/admin/users/grant", data={"name": "New Sup"}, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        conn = dbmod.get_conn(self.db_path)
        role = get_role(conn, "New Sup")
        conn.close()
        self.assertEqual(role, ROLE_SUPERVISOR)

    # --- pre-use checking remains ungated, per requirement 6 ---------------

    def test_field_user_can_perform_pre_use_check_without_any_role(self):
        self._act_as(None)
        resp = self.client.post(
            f"/t/{DEMO_TAG_IDS['SLING-001']}/check",
            data={
                "checked_by": "Any Field User", "lift_supervisor": "Bob", "result": "PASS",
                "chk_visual": "on", "chk_tag_legible": "on", "chk_no_damage": "on",
                "chk_no_wear": "on", "chk_connections": "on",
            },
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)

    def test_field_user_cannot_perform_admin_nfc_operations(self):
        self._act_as(None)
        resp = self.client.get("/admin/assets/SLING-001/replace-tag")
        self.assertEqual(resp.status_code, 403)

    def test_act_as_rejects_a_free_text_unregistered_name(self):
        """The act-as picker only accepts a currently-registered identity —
        this is the concrete proof that 'enter your name and role' free
        text is impossible, per the explicit instruction against it."""
        resp = self.client.post("/demo/act-as", data={"name": "I Just Typed This In"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b"not a currently-registered", resp.data)

    def test_every_role_boundary_page_is_labelled_demo_only(self):
        """Every place the role architecture is surfaced must say DEMO —
        never presented as real authentication."""
        self._act_as(DEMO_AP_NAME)
        for path in ("/demo/act-as", "/admin/users"):
            resp = self.client.get(path)
            self.assertIn(b"DEMO", resp.data)
            self.assertIn(b"NOT AUTHENTICATED", resp.data)


if __name__ == "__main__":
    unittest.main()
