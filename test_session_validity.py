"""
Tests for the corrected new-session-vs-same-session validity rules —
added 2026-08-18, per the explicit correction overriding the original
Phase 2 proposal:

  "A previous PASS from another Inspection Session must NOT automatically
   satisfy the pre-use requirement of a newly-created Inspection Session."
  "Only a completed real PASS check starts/restarts the 24-hour validity
   period... Creating a session or adding an asset by itself must NEVER
   renew validity."

Also covers the same-screen continuous multi-accessory scanning UX
(session_add.html/session_add_picker showing the working list alongside
the scan/add picker).

IMPORTANT: add_asset_to_session() itself was NOT modified by this
increment (see workflow.py) — it already unconditionally created a
PENDING item for a genuinely new (session, asset) pair, which already
*is* "new session requires a new real check." These tests are therefore
primarily REGRESSION tests confirming that pre-existing, unmodified
behaviour satisfies the corrected specification, plus a couple of new
same-session "recognises its own completed check" and same-screen UX
tests.
"""
import os
import tempfile
import unittest

import db as dbmod
from app import create_app
from workflow import record_pre_use_check
from seed_data import DEMO_TAG_IDS

CHECKLIST_DATA = {
    "chk_visual": "on", "chk_tag_legible": "on", "chk_no_damage": "on",
    "chk_no_wear": "on", "chk_connections": "on",
}


class NewSessionValidityTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.app = create_app(database_path=self.db_path)
        self.client = self.app.test_client()

    def tearDown(self):
        os.remove(self.db_path)

    def _new_session(self):
        conn = dbmod.get_conn(self.db_path)
        session_id = dbmod.create_session(conn, "Sup", "Slinger")
        conn.commit()
        conn.close()
        return session_id

    def test_previous_standalone_pass_does_not_satisfy_a_new_session(self):
        """A real, recent, still-valid standalone PASS must NOT let a new
        session skip the checklist for that asset."""
        conn = dbmod.get_conn(self.db_path)
        asset = dbmod.get_asset(conn, "SLING-001")
        record_pre_use_check(conn, asset, DEMO_TAG_IDS["SLING-001"], "Alice", "Bob", "PASS")
        conn.close()

        session_id = self._new_session()
        self.client.get(f"/session/{session_id}/add/{DEMO_TAG_IDS['SLING-001']}")

        conn = dbmod.get_conn(self.db_path)
        items = dbmod.list_session_items(conn, session_id, include_removed=False)
        conn.close()
        self.assertEqual(items[0].check_result, "PENDING", "new session must require its own checklist")

    def test_previous_session_pass_does_not_satisfy_a_different_new_session(self):
        """Session A's real completed PASS for an asset must not carry
        over to Session B — the explicit corrected example."""
        session_a = self._new_session()
        self.client.get(f"/session/{session_a}/add/{DEMO_TAG_IDS['SLING-001']}")
        conn = dbmod.get_conn(self.db_path)
        item_a = dbmod.list_session_items(conn, session_a, include_removed=False)[0]
        conn.close()
        self.client.post(
            f"/session/{session_a}/item/{item_a.id}/check",
            data={**CHECKLIST_DATA, "result": "PASS"},
        )
        conn = dbmod.get_conn(self.db_path)
        item_a_after = dbmod.get_session_item(conn, item_a.id)
        conn.close()
        self.assertEqual(item_a_after.check_result, "PASS")

        # Now a brand NEW session adds the same asset.
        session_b = self._new_session()
        self.client.get(f"/session/{session_b}/add/{DEMO_TAG_IDS['SLING-001']}")
        conn = dbmod.get_conn(self.db_path)
        item_b = dbmod.list_session_items(conn, session_b, include_removed=False)[0]
        conn.close()
        self.assertEqual(item_b.check_result, "PENDING",
                          "Session B must require its own real check despite Session A's valid PASS")

    def test_new_pass_in_new_session_restarts_the_24h_period(self):
        """Since validity is derived from the single most recent Check row
        (whatever its origin), a fresh PASS recorded inside Session B
        becomes the new 'most recent PASS' the moment it's committed —
        nothing needs to be separately 'restarted'."""
        session_b = self._new_session()
        self.client.get(f"/session/{session_b}/add/{DEMO_TAG_IDS['CHAIN-001']}")
        conn = dbmod.get_conn(self.db_path)
        item_b = dbmod.list_session_items(conn, session_b, include_removed=False)[0]
        conn.close()
        self.client.post(
            f"/session/{session_b}/item/{item_b.id}/check",
            data={**CHECKLIST_DATA, "result": "PASS"},
        )
        # Standalone view of the same asset must now show VALID CHECK,
        # sourced from the check just recorded inside Session B.
        resp = self.client.get(f"/t/{DEMO_TAG_IDS['CHAIN-001']}")
        self.assertIn(b"PRE-USE CHECK VALID", resp.data)

    def test_merely_adding_an_asset_to_a_session_does_not_create_a_check(self):
        conn = dbmod.get_conn(self.db_path)
        checks_before = conn.execute(
            "SELECT COUNT(*) AS n FROM checks WHERE asset_id = ?", ("SLING-002",)
        ).fetchone()["n"]
        conn.close()

        session_id = self._new_session()
        self.client.get(f"/session/{session_id}/add/{DEMO_TAG_IDS['SLING-002']}")

        conn = dbmod.get_conn(self.db_path)
        checks_after = conn.execute(
            "SELECT COUNT(*) AS n FROM checks WHERE asset_id = ?", ("SLING-002",)
        ).fetchone()["n"]
        conn.close()
        self.assertEqual(checks_before, checks_after, "adding to a session must never create a fake check")


class SameSessionRecognisesOwnCheckTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.app = create_app(database_path=self.db_path)
        self.client = self.app.test_client()

    def tearDown(self):
        os.remove(self.db_path)

    def test_same_session_does_not_force_a_duplicate_check_on_re_add(self):
        """Duplicate-add protection (pre-existing, unmodified) already
        returns the existing item untouched — this is 'the same session
        recognises an already-completed check' in practice."""
        conn = dbmod.get_conn(self.db_path)
        session_id = dbmod.create_session(conn, "Sup", "Slinger")
        conn.commit()
        conn.close()

        self.client.get(f"/session/{session_id}/add/{DEMO_TAG_IDS['BEAM-001']}")
        conn = dbmod.get_conn(self.db_path)
        item = dbmod.list_session_items(conn, session_id, include_removed=False)[0]
        conn.close()
        self.client.post(f"/session/{session_id}/item/{item.id}/check", data={**CHECKLIST_DATA, "result": "PASS"})

        # Re-add (e.g. an accidental repeat scan) the same asset to the
        # same session.
        self.client.get(f"/session/{session_id}/add/{DEMO_TAG_IDS['BEAM-001']}")

        conn = dbmod.get_conn(self.db_path)
        items = dbmod.list_session_items(conn, session_id, include_removed=False)
        conn.close()
        self.assertEqual(len(items), 1, "re-adding must not create a second item")
        self.assertEqual(items[0].check_result, "PASS", "the existing PASS must be recognised, not overwritten")


class SameScreenContinuousScanningTestCase(unittest.TestCase):
    """Same-screen multi-accessory scanning UX — session_add.html now
    shows the working list alongside the scan/add picker."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.app = create_app(database_path=self.db_path)
        self.client = self.app.test_client()

    def tearDown(self):
        os.remove(self.db_path)

    def test_add_screen_shows_working_list_without_leaving_the_page(self):
        conn = dbmod.get_conn(self.db_path)
        session_id = dbmod.create_session(conn, "Sup", "Slinger")
        conn.commit()
        conn.close()

        self.client.get(f"/session/{session_id}/add/{DEMO_TAG_IDS['BEAM-001']}")
        resp = self.client.get(f"/session/{session_id}/add")
        self.assertIn(b"Current Working List", resp.data)
        self.assertIn(b"BEAM-001", resp.data)

    def test_same_session_id_persists_across_many_consecutive_adds(self):
        conn = dbmod.get_conn(self.db_path)
        session_id = dbmod.create_session(conn, "Sup", "Slinger")
        conn.commit()
        conn.close()

        for asset_id, tag_id in DEMO_TAG_IDS.items():
            resp = self.client.get(f"/session/{session_id}/add/{tag_id}", follow_redirects=True)
            self.assertEqual(resp.status_code, 200)
            self.assertIn(f"/session/{session_id}/add".encode(), resp.request.path.encode() if hasattr(resp, "request") else b"")

        conn = dbmod.get_conn(self.db_path)
        items = dbmod.list_session_items(conn, session_id, include_removed=False)
        conn.close()
        # No artificial maximum — every one of the seven demo assets landed
        # in the SAME session.
        self.assertEqual(len(items), len(DEMO_TAG_IDS))
        self.assertTrue(all(i.session_id == session_id for i in items))

    def test_no_forced_navigation_to_home_between_scans(self):
        """The add-tap route must redirect back to the add/scan picker
        itself, never to '/' (home/Test Harness) — this is what makes
        repeated scanning stay on one screen."""
        conn = dbmod.get_conn(self.db_path)
        session_id = dbmod.create_session(conn, "Sup", "Slinger")
        conn.commit()
        conn.close()

        resp = self.client.get(f"/session/{session_id}/add/{DEMO_TAG_IDS['SLING-001']}")
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f"/session/{session_id}/add", resp.headers["Location"])
        self.assertNotEqual(resp.headers["Location"], "/")


if __name__ == "__main__":
    unittest.main()
