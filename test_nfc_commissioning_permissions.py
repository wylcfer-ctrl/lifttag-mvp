"""
Tests for Supervisor NFC commissioning/replacement — added 2026-08-18.

Confirms: a Supervisor (via the route-level DEMO role gate) can perform
both first-time commissioning and replacement on an EXISTING asset, and
that every pre-existing safety guard in workflow.commission_tag() /
replace_tag() remains completely intact when exercised through an
authorised Supervisor — none of the guard logic itself was touched by
this increment (see workflow.py), only who is allowed to reach the route.
"""
import os
import tempfile
import unittest

import db as dbmod
from app import create_app
from workflow import record_pre_use_check, grant_supervisor
from models import STATUS_QUARANTINED
from seed_data import DEMO_TAG_IDS, DEMO_AP_NAME


class SupervisorNfcCommissioningTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.app = create_app(database_path=self.db_path)
        self.client = self.app.test_client()
        conn = dbmod.get_conn(self.db_path)
        grant_supervisor(conn, "NFC Sup", granted_by_name=DEMO_AP_NAME, actor=DEMO_AP_NAME)
        dbmod.create_asset(conn, "UNCOMMISSIONED-001", "Test Hook")  # no tag yet
        conn.commit()
        conn.close()
        with self.client.session_transaction() as sess:
            sess["demo_actor_name"] = "NFC Sup"

    def tearDown(self):
        os.remove(self.db_path)

    def test_supervisor_can_commission_first_time_tag_on_existing_asset(self):
        resp = self.client.post(
            "/admin/assets/UNCOMMISSIONED-001/assign-tag", data={"tag_id": "demo-uncommissioned-001"}
        )
        self.assertEqual(resp.status_code, 302)
        conn = dbmod.get_conn(self.db_path)
        tag = dbmod.get_active_tag(conn, "UNCOMMISSIONED-001")
        conn.close()
        self.assertIsNotNone(tag)
        self.assertEqual(tag.tag_id, "demo-uncommissioned-001")

    def test_supervisor_commissioning_does_not_create_a_second_asset(self):
        conn = dbmod.get_conn(self.db_path)
        count_before = len(dbmod.list_assets(conn))
        conn.close()
        self.client.post("/admin/assets/UNCOMMISSIONED-001/assign-tag", data={"tag_id": "demo-uncommissioned-001"})
        conn = dbmod.get_conn(self.db_path)
        count_after = len(dbmod.list_assets(conn))
        conn.close()
        self.assertEqual(count_before, count_after)

    def test_supervisor_can_replace_an_existing_tag(self):
        old_tag_id = DEMO_TAG_IDS["SLING-001"]
        resp = self.client.post("/admin/assets/SLING-001/replace-tag", data={"tag_id": "demo-sling-001-v2"})
        self.assertEqual(resp.status_code, 302)

        conn = dbmod.get_conn(self.db_path)
        old_tag = dbmod.get_tag(conn, old_tag_id)
        new_tag = dbmod.get_active_tag(conn, "SLING-001")
        conn.close()
        self.assertFalse(old_tag.active, "old tag must be revoked")
        self.assertEqual(new_tag.tag_id, "demo-sling-001-v2")

    def test_supervisor_replacement_preserves_asset_identity(self):
        self.client.post("/admin/assets/SLING-001/replace-tag", data={"tag_id": "demo-sling-001-v2"})
        conn = dbmod.get_conn(self.db_path)
        asset = dbmod.get_asset(conn, "SLING-001")
        conn.close()
        self.assertEqual(asset.asset_id, "SLING-001", "Asset ID must never change on tag replacement")

    def test_supervisor_replacement_preserves_check_and_audit_history(self):
        conn = dbmod.get_conn(self.db_path)
        asset = dbmod.get_asset(conn, "SLING-002")
        record_pre_use_check(conn, asset, DEMO_TAG_IDS["SLING-002"], "Alice", "Bob", "PASS")
        checks_before = conn.execute("SELECT id FROM checks WHERE asset_id = ?", ("SLING-002",)).fetchall()
        events_before = dbmod.list_audit_events(conn, "SLING-002")
        conn.close()

        self.client.post("/admin/assets/SLING-002/replace-tag", data={"tag_id": "demo-sling-002-v2"})

        conn = dbmod.get_conn(self.db_path)
        checks_after = conn.execute("SELECT id FROM checks WHERE asset_id = ?", ("SLING-002",)).fetchall()
        events_after = dbmod.list_audit_events(conn, "SLING-002")
        conn.close()

        self.assertEqual(len(checks_after), len(checks_before), "no check is created/altered/removed")
        # New TAG_REVOKED/TAG_ASSIGNED/TAG_REPLACED events are added, but
        # every original event is still present, unmodified.
        original_ids = {e.id for e in events_before}
        after_ids = {e.id for e in events_after}
        self.assertTrue(original_ids.issubset(after_ids), "original audit events must remain intact")

    def test_supervisor_replacement_never_releases_quarantine(self):
        conn = dbmod.get_conn(self.db_path)
        asset = dbmod.get_asset(conn, "CHAIN-001")
        record_pre_use_check(conn, asset, DEMO_TAG_IDS["CHAIN-001"], "Alice", "Bob", "FAIL", failure_reason="Damage")
        conn.close()

        conn = dbmod.get_conn(self.db_path)
        quarantined = dbmod.get_asset(conn, "CHAIN-001")
        self.assertEqual(quarantined.current_status, STATUS_QUARANTINED)
        conn.close()

        self.client.post("/admin/assets/CHAIN-001/replace-tag", data={"tag_id": "demo-chain-001-v2"})

        conn = dbmod.get_conn(self.db_path)
        after = dbmod.get_asset(conn, "CHAIN-001")
        conn.close()
        self.assertEqual(after.current_status, STATUS_QUARANTINED, "replacement via Supervisor must never release quarantine")

    def test_supervisor_commissioning_never_silently_overwrites_existing_active_tag(self):
        """SLING-001 already has an active tag — the assign-tag route must
        redirect to the replace flow instead of silently overwriting,
        exactly as it already did before Supervisor gating existed."""
        resp = self.client.get("/admin/assets/SLING-001/assign-tag")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/replace-tag", resp.headers["Location"])

    def test_field_user_still_cannot_commission_or_replace(self):
        with self.client.session_transaction() as sess:
            sess.pop("demo_actor_name", None)
        resp1 = self.client.get("/admin/assets/UNCOMMISSIONED-001/assign-tag")
        resp2 = self.client.get("/admin/assets/SLING-001/replace-tag")
        self.assertEqual(resp1.status_code, 403)
        self.assertEqual(resp2.status_code, 403)


if __name__ == "__main__":
    unittest.main()
