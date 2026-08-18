"""
Tests for standalone continuous checking ("SCAN NEXT TAG") outside any
Inspection Session — added 2026-08-18.

No new route was introduced for this: SCAN NEXT TAG on check_result.html
links to the existing Test Harness index ('/'), documented as this demo's
stand-in for a physical NFC tap (no real hardware exists yet — see
templates/index.html's own pre-existing note). This file proves the
underlying requirement — independent records, no artificial limit, no
forced trip through unnecessary intermediate pages — regardless of which
exact URL the continuation button points at.
"""
import os
import tempfile
import unittest

import db as dbmod
from app import create_app
from seed_data import DEMO_TAG_IDS

CHECKLIST_DATA = {
    "chk_visual": "on", "chk_tag_legible": "on", "chk_no_damage": "on",
    "chk_no_wear": "on", "chk_connections": "on",
}


class ContinuousIndividualCheckingTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.app = create_app(database_path=self.db_path)
        self.client = self.app.test_client()

    def tearDown(self):
        os.remove(self.db_path)

    def _checklist(self, **overrides):
        data = dict(CHECKLIST_DATA)
        data.update({"checked_by": "Alice", "lift_supervisor": "Bob", "result": "PASS"})
        data.update(overrides)
        return data

    def test_check_result_page_offers_scan_next_tag(self):
        resp = self.client.post(
            f"/t/{DEMO_TAG_IDS['SLING-001']}/check", data=self._checklist(), follow_redirects=True
        )
        self.assertIn(b"SCAN NEXT TAG", resp.data)

    def test_scan_next_tag_leads_to_a_page_that_can_reach_another_asset(self):
        resp = self.client.post(
            f"/t/{DEMO_TAG_IDS['SLING-001']}/check", data=self._checklist(), follow_redirects=True
        )
        # Extract the SCAN NEXT TAG href and follow it.
        self.assertIn(b'href="/"', resp.data)
        next_resp = self.client.get("/")
        self.assertEqual(next_resp.status_code, 200)
        for asset_id in DEMO_TAG_IDS:
            self.assertIn(asset_id.encode(), next_resp.data)

    def test_consecutive_checks_on_different_assets_are_fully_independent(self):
        self.client.post(
            f"/t/{DEMO_TAG_IDS['SLING-001']}/check",
            data=self._checklist(checked_by="Alice", result="FAIL", failure_reason="Damage"),
        )
        self.client.post(
            f"/t/{DEMO_TAG_IDS['CHAIN-001']}/check",
            data=self._checklist(checked_by="Carol", result="PASS"),
        )

        conn = dbmod.get_conn(self.db_path)
        sling = dbmod.get_asset(conn, "SLING-001")
        chain = dbmod.get_asset(conn, "CHAIN-001")
        sling_check = dbmod.get_last_check(conn, "SLING-001")
        chain_check = dbmod.get_last_check(conn, "CHAIN-001")
        conn.close()

        self.assertEqual(sling.current_status, "QUARANTINED — DO NOT USE")
        self.assertEqual(chain.current_status, "IN SERVICE")
        self.assertEqual(sling_check.checked_by, "Alice")
        self.assertEqual(chain_check.checked_by, "Carol")
        self.assertNotEqual(sling_check.id, chain_check.id)

    def test_no_artificial_maximum_on_consecutive_standalone_checks(self):
        """Loop through every demo asset performing an independent check —
        proves nothing in the standalone flow imposes a cap."""
        for i, (asset_id, tag_id) in enumerate(DEMO_TAG_IDS.items()):
            resp = self.client.post(
                f"/t/{tag_id}/check",
                data=self._checklist(checked_by=f"Checker-{i}", result="PASS"),
                follow_redirects=True,
            )
            self.assertEqual(resp.status_code, 200)
            self.assertIn(b"SCAN NEXT TAG", resp.data)

        conn = dbmod.get_conn(self.db_path)
        for asset_id in DEMO_TAG_IDS:
            check = dbmod.get_last_check(conn, asset_id)
            self.assertIsNotNone(check)
            self.assertEqual(check.result, "PASS")
        conn.close()

    def test_each_standalone_check_has_its_own_audit_trail(self):
        self.client.post(
            f"/t/{DEMO_TAG_IDS['SLING-001']}/check",
            data=self._checklist(result="FAIL", failure_reason="Tag issue"),
        )
        self.client.post(f"/t/{DEMO_TAG_IDS['CHAIN-001']}/check", data=self._checklist(result="PASS"))

        conn = dbmod.get_conn(self.db_path)
        sling_events = [e.event_type for e in dbmod.list_audit_events(conn, "SLING-001")]
        chain_events = [e.event_type for e in dbmod.list_audit_events(conn, "CHAIN-001")]
        conn.close()

        self.assertIn("CHECK_FAIL", sling_events)
        self.assertIn("STATUS_CHANGE", sling_events)
        self.assertNotIn("CHECK_PASS", sling_events)
        self.assertIn("CHECK_PASS", chain_events)
        self.assertNotIn("CHECK_FAIL", chain_events)


if __name__ == "__main__":
    unittest.main()
