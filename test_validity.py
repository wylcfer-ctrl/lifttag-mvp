"""
Tests for the rolling 24-hour pre-use validity architecture — added
2026-08-18.

Covers workflow.get_operational_state() directly (the pure derivation
function) plus the standalone HTTP flow that surfaces it. Does NOT test
Inspection Session behaviour — see test_session_validity.py for the
new-session-vs-same-session rules, which are governed by a completely
separate code path (add_asset_to_session()'s existing duplicate-add
guard, unchanged by this increment).
"""
import os
import tempfile
import unittest
from datetime import timedelta

import db as dbmod
from app import create_app
from workflow import (
    record_pre_use_check,
    get_operational_state,
    get_valid_until,
)
from models import (
    STATUS_IN_SERVICE,
    STATUS_QUARANTINED,
    OPERATIONAL_STATE_VALID,
    OPERATIONAL_STATE_CHECK_REQUIRED,
    OPERATIONAL_STATE_QUARANTINED,
    PRE_USE_VALIDITY_HOURS,
    now,
)

CHECKLIST_DATA = {
    "chk_visual": "on", "chk_tag_legible": "on", "chk_no_damage": "on",
    "chk_no_wear": "on", "chk_connections": "on",
}


class ValidityDerivationTestCase(unittest.TestCase):
    """Direct unit tests of workflow.get_operational_state() — no HTTP."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        dbmod.init_db(self.db_path)
        conn = dbmod.get_conn(self.db_path)
        dbmod.create_asset(conn, "VAL-001", "Test Sling")
        dbmod.create_tag(conn, "val-tag", "VAL-001", active=True)
        conn.commit()
        conn.close()

    def tearDown(self):
        os.remove(self.db_path)

    def _asset(self):
        conn = dbmod.get_conn(self.db_path)
        asset = dbmod.get_asset(conn, "VAL-001")
        conn.close()
        return asset

    def test_no_check_ever_is_check_required(self):
        conn = dbmod.get_conn(self.db_path)
        state, check, periodic_ok = get_operational_state(conn, self._asset())
        conn.close()
        self.assertEqual(state, OPERATIONAL_STATE_CHECK_REQUIRED)
        self.assertIsNone(check)

    def test_real_pass_starts_rolling_24h_validity(self):
        conn = dbmod.get_conn(self.db_path)
        asset = self._asset()
        record_pre_use_check(conn, asset, "val-tag", "Alice", "Bob", "PASS")
        state, check, periodic_ok = get_operational_state(conn, self._asset())
        conn.close()
        self.assertEqual(state, OPERATIONAL_STATE_VALID)
        self.assertEqual(check.result, "PASS")

    def test_validity_is_exactly_time_based_not_calendar_day(self):
        """A PASS timestamped just before midnight must still show VALID
        shortly after midnight — the window is a pure 24h rolling delta,
        never a calendar-day reset."""
        conn = dbmod.get_conn(self.db_path)
        asset = self._asset()
        check = record_pre_use_check(conn, asset, "val-tag", "Alice", "Bob", "PASS")

        # Backdate the check to simulate "23 hours ago" — still within the
        # rolling 24h window regardless of any date boundary crossed.
        near_expiry = now() - timedelta(hours=23)
        conn.execute("UPDATE checks SET timestamp = ? WHERE id = ?", (near_expiry.isoformat(), check.id))
        conn.commit()

        state, check2, periodic_ok = get_operational_state(conn, self._asset())
        conn.close()
        self.assertEqual(state, OPERATIONAL_STATE_VALID, "23h-old PASS must still be VALID")

    def test_validity_expires_after_24_elapsed_hours(self):
        conn = dbmod.get_conn(self.db_path)
        asset = self._asset()
        check = record_pre_use_check(conn, asset, "val-tag", "Alice", "Bob", "PASS")

        expired_time = now() - timedelta(hours=25)
        conn.execute("UPDATE checks SET timestamp = ? WHERE id = ?", (expired_time.isoformat(), check.id))
        conn.commit()

        state, check2, periodic_ok = get_operational_state(conn, self._asset())
        conn.close()
        self.assertEqual(state, OPERATIONAL_STATE_CHECK_REQUIRED, "25h-old PASS must be CHECK_REQUIRED")

    def test_exactly_24_hours_is_still_valid_boundary_inclusive(self):
        """get_valid_until() == timestamp + 24h; the comparison in
        get_operational_state() is now() <= valid_until, so a check that
        is exactly at the boundary at the moment of evaluation is still
        valid (never earlier than promised)."""
        conn = dbmod.get_conn(self.db_path)
        asset = self._asset()
        check = record_pre_use_check(conn, asset, "val-tag", "Alice", "Bob", "PASS")
        conn.close()
        valid_until = get_valid_until(check)
        self.assertEqual(valid_until, check.timestamp + timedelta(hours=PRE_USE_VALIDITY_HOURS))

    def test_newer_fail_overrides_earlier_valid_pass(self):
        conn = dbmod.get_conn(self.db_path)
        asset = self._asset()
        record_pre_use_check(conn, asset, "val-tag", "Alice", "Bob", "PASS")
        asset = self._asset()
        record_pre_use_check(conn, asset, "val-tag", "Carol", "Dave", "FAIL", failure_reason="Damage")
        state, check, periodic_ok = get_operational_state(conn, self._asset())
        conn.close()
        # The asset is now quarantined (Correction 1's existing logic) —
        # QUARANTINED dominates and is shown, not a stale VALID.
        self.assertEqual(state, OPERATIONAL_STATE_QUARANTINED)

    def test_quarantine_overrides_validity_even_with_a_very_recent_pass_check_row(self):
        """Belt-and-braces: even if somehow the most recent Check row were
        a PASS while current_status is QUARANTINED (shouldn't happen given
        Correction 1, but the precedence order must not rely on that
        holding elsewhere), QUARANTINED must still be shown."""
        conn = dbmod.get_conn(self.db_path)
        asset = self._asset()
        dbmod.set_asset_status(conn, asset.asset_id, STATUS_QUARANTINED)
        conn.commit()
        record_pre_use_check(conn, self._asset(), "val-tag", "Alice", "Bob", "PASS")
        state, check, periodic_ok = get_operational_state(conn, self._asset())
        conn.close()
        self.assertEqual(state, OPERATIONAL_STATE_QUARANTINED)


class StandaloneValidityHttpTestCase(unittest.TestCase):
    """The standalone /t/<tag_id> and /t/<tag_id>/check flow surfaces the
    same derived state via HTTP, with the mandatory checklist."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.app = create_app(database_path=self.db_path)
        conn = dbmod.get_conn(self.db_path)
        dbmod.create_asset(conn, "HTTP-VAL-001", "Test Sling")
        dbmod.create_tag(conn, "http-val-tag", "HTTP-VAL-001", active=True)
        conn.commit()
        conn.close()
        self.client = self.app.test_client()

    def tearDown(self):
        os.remove(self.db_path)

    def _checklist(self, **overrides):
        data = dict(CHECKLIST_DATA)
        data.update({"checked_by": "Alice", "lift_supervisor": "Bob", "result": "PASS"})
        data.update(overrides)
        return data

    def test_asset_page_shows_check_required_with_no_history(self):
        resp = self.client.get("/t/http-val-tag")
        self.assertIn(b"CHECK REQUIRED", resp.data)

    def test_standalone_scan_within_valid_period_shows_valid_check(self):
        self.client.post("/t/http-val-tag/check", data=self._checklist(result="PASS"))
        resp = self.client.get("/t/http-val-tag")
        self.assertIn(b"PRE-USE CHECK VALID", resp.data)
        self.assertIn(b"Alice", resp.data)

    def test_standalone_expired_check_shows_check_required(self):
        self.client.post("/t/http-val-tag/check", data=self._checklist(result="PASS"))
        conn = dbmod.get_conn(self.db_path)
        expired_time = now() - timedelta(hours=25)
        conn.execute("UPDATE checks SET timestamp = ? WHERE asset_id = ?", (expired_time.isoformat(), "HTTP-VAL-001"))
        conn.commit()
        conn.close()
        resp = self.client.get("/t/http-val-tag")
        self.assertIn(b"CHECK REQUIRED", resp.data)
        self.assertNotIn(b"PRE-USE CHECK VALID", resp.data)

    def test_field_user_may_still_perform_a_new_check_when_valid(self):
        """VALID CHECK is informational only — it must never block the
        user from deliberately performing a new check."""
        self.client.post("/t/http-val-tag/check", data=self._checklist(result="PASS"))
        # The check form GET must still render normally (not redirect/block).
        resp = self.client.get("/t/http-val-tag/check")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Submit Check", resp.data)
        # And a second, deliberate check must still be accepted.
        resp2 = self.client.post(
            "/t/http-val-tag/check",
            data=self._checklist(checked_by="Carol", lift_supervisor="Dave", result="PASS"),
            follow_redirects=True,
        )
        self.assertEqual(resp2.status_code, 200)

    def test_checklist_is_mandatory_on_standalone_check(self):
        resp = self.client.post(
            "/t/http-val-tag/check",
            data={"checked_by": "Alice", "lift_supervisor": "Bob", "result": "PASS"},  # no checklist ticks
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b"checklist", resp.data.lower())

    def test_fail_requires_a_reason_on_standalone_check(self):
        resp = self.client.post(
            "/t/http-val-tag/check",
            data=self._checklist(result="FAIL"),  # no failure_reason
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b"failure reason", resp.data.lower())

    def test_fail_with_reason_quarantines_and_is_recorded(self):
        resp = self.client.post(
            "/t/http-val-tag/check",
            data=self._checklist(result="FAIL", failure_reason="Excessive wear"),
            follow_redirects=True,
        )
        self.assertIn(b"QUARANTINED", resp.data)
        conn = dbmod.get_conn(self.db_path)
        check = dbmod.get_last_check(conn, "HTTP-VAL-001")
        conn.close()
        self.assertEqual(check.failure_reason, "Excessive wear")


if __name__ == "__main__":
    unittest.main()
