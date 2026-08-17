"""
Deployment-readiness tests (added 2026-08-16).

These exist specifically to prove the two corrections requested before
deployment:

1. Repeated seeding never regenerates the five Tag IDs once they exist.
2. A quarantined asset's status, and its Tag ID, survive an application
   restart (simulated here as a fresh create_app()/process against the
   same on-disk database — since state lives entirely in the SQLite file,
   not in process memory, this is an accurate simulation of a real
   process restart).

Written with unittest (standard library, no extra install required). Also
discoverable and runnable by pytest, if installed, with no changes.
"""
import os
import tempfile
import unittest

import db as dbmod
from app import create_app
from workflow import record_pre_use_check
from models import STATUS_QUARANTINED
from seed import seed
from seed_data import TEST_ASSETS


class RepeatedSeedingPreservesTagIdsTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        os.remove(self.db_path)

    def test_repeated_seeding_does_not_change_existing_tag_ids(self):
        first_urls, _ = seed(database_path=self.db_path, base_url="http://localhost:5000")
        first_by_asset = {asset_id: tag_id for asset_id, tag_id, _ in first_urls}

        # Re-run seeding several times, as would happen if a deploy script
        # runs seed.py on every release.
        for _ in range(3):
            urls_again, _ = seed(database_path=self.db_path, base_url="http://localhost:5000")
            by_asset_again = {asset_id: tag_id for asset_id, tag_id, _ in urls_again}
            self.assertEqual(
                by_asset_again,
                first_by_asset,
                "re-seeding must never regenerate an existing asset's Tag ID",
            )

    def test_seed_creates_each_asset_and_tag_only_once_in_the_database(self):
        seed(database_path=self.db_path, base_url="http://localhost:5000")
        seed(database_path=self.db_path, base_url="http://localhost:5000")

        conn = dbmod.get_conn(self.db_path)
        for asset_id, _ in TEST_ASSETS:
            row_count = conn.execute(
                "SELECT COUNT(*) AS n FROM assets WHERE asset_id = ?", (asset_id,)
            ).fetchone()["n"]
            self.assertEqual(row_count, 1, f"{asset_id} must exist exactly once")

            tag_count = conn.execute(
                "SELECT COUNT(*) AS n FROM tags WHERE asset_id = ? AND active = 1", (asset_id,)
            ).fetchone()["n"]
            self.assertEqual(tag_count, 1, f"{asset_id} must have exactly one active tag")
        conn.close()

    def test_repeated_seeding_after_quarantine_disturbs_nothing(self):
        """This is the scenario that actually happens on Render: seed.py
        (via preDeployCommand) runs again on every redeploy, potentially
        long after real test checks and a real quarantine have happened.
        Re-seeding at that point must not duplicate assets, must not change
        the existing Tag ID, must not remove or alter any check, must not
        remove or alter any audit event, and must absolutely not release a
        quarantined asset."""
        urls, _ = seed(database_path=self.db_path, base_url="http://localhost:5000")
        asset_id, tag_id, _ = urls[0]

        conn = dbmod.get_conn(self.db_path)
        asset = dbmod.get_asset(conn, asset_id)
        record_pre_use_check(conn, asset, tag_id, "Alice", "Bob", "FAIL")  # -> quarantines
        record_pre_use_check(conn, asset, tag_id, "Carol", "Dave", "PASS")  # stays quarantined
        conn.close()

        conn = dbmod.get_conn(self.db_path)
        checks_before = conn.execute(
            "SELECT id, result, checked_by, lift_supervisor, timestamp FROM checks "
            "WHERE asset_id = ? ORDER BY id", (asset_id,),
        ).fetchall()
        events_before = dbmod.list_audit_events(conn, asset_id)
        status_before = dbmod.get_asset(conn, asset_id).current_status
        conn.close()

        self.assertEqual(len(checks_before), 2)
        self.assertEqual(status_before, STATUS_QUARANTINED)

        # Re-seed several times, as preDeployCommand will on every redeploy.
        for _ in range(3):
            seed(database_path=self.db_path, base_url="http://localhost:5000")

        conn = dbmod.get_conn(self.db_path)
        asset_count = conn.execute(
            "SELECT COUNT(*) AS n FROM assets WHERE asset_id = ?", (asset_id,)
        ).fetchone()["n"]
        checks_after = conn.execute(
            "SELECT id, result, checked_by, lift_supervisor, timestamp FROM checks "
            "WHERE asset_id = ? ORDER BY id", (asset_id,),
        ).fetchall()
        events_after = dbmod.list_audit_events(conn, asset_id)
        asset_after = dbmod.get_asset(conn, asset_id)
        tag_after = dbmod.get_active_tag(conn, asset_id)
        conn.close()

        self.assertEqual(asset_count, 1, "re-seeding must not duplicate the asset")
        self.assertEqual(tag_after.tag_id, tag_id, "re-seeding must not change the existing Tag ID")
        self.assertEqual(
            [tuple(r) for r in checks_after], [tuple(r) for r in checks_before],
            "re-seeding must not remove or alter any check",
        )
        self.assertEqual(
            [(e.event_type, e.previous_state, e.new_state) for e in events_after],
            [(e.event_type, e.previous_state, e.new_state) for e in events_before],
            "re-seeding must not remove or alter any audit event",
        )
        self.assertEqual(
            asset_after.current_status, STATUS_QUARANTINED,
            "re-seeding must never release a quarantined asset",
        )


class RestartSurvivalTestCase(unittest.TestCase):
    """
    Simulates an application/process restart by discarding the in-process
    Flask app and database connection entirely and starting fresh ones
    against the same on-disk SQLite file — the same thing that happens
    when a real deployed process restarts.
    """

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        os.remove(self.db_path)

    def test_quarantined_status_survives_restart(self):
        # --- "Before restart": seed, then fail a check to quarantine an asset.
        urls, app_before = seed(database_path=self.db_path, base_url="http://localhost:5000")
        asset_id, tag_id, _ = urls[0]

        conn = dbmod.get_conn(self.db_path)
        asset = dbmod.get_asset(conn, asset_id)
        record_pre_use_check(conn, asset, tag_id, "Alice", "Bob", "FAIL")
        conn.close()

        # Drop every in-process reference. Nothing below reuses `app_before`,
        # `asset`, or the first `conn` — a real process restart would have
        # none of this in memory either.
        del app_before, asset, conn

        # --- "After restart": brand new app instance, brand new connection,
        # same on-disk database file.
        app_after = create_app(database_path=self.db_path)
        conn_after = dbmod.get_conn(self.db_path)

        restarted_asset = dbmod.get_asset(conn_after, asset_id)
        restarted_tag = dbmod.get_active_tag(conn_after, asset_id)
        events = dbmod.list_audit_events(conn_after, asset_id)
        conn_after.close()

        self.assertEqual(restarted_asset.current_status, STATUS_QUARANTINED)
        self.assertEqual(restarted_tag.tag_id, tag_id, "Tag ID must be unchanged across a restart")
        # TAG_ASSIGNED comes from seeding itself; CHECK_FAIL + STATUS_CHANGE
        # from the pre-use check recorded before the simulated restart.
        self.assertEqual(
            [e.event_type for e in events],
            ["TAG_ASSIGNED", "CHECK_FAIL", "STATUS_CHANGE"],
            "audit history must be unchanged across a restart",
        )

    def test_pass_after_restart_still_does_not_release_quarantine(self):
        """Belt-and-braces: Correction 1 must hold even across a restart."""
        urls, _ = seed(database_path=self.db_path, base_url="http://localhost:5000")
        asset_id, tag_id, _ = urls[0]

        conn = dbmod.get_conn(self.db_path)
        asset = dbmod.get_asset(conn, asset_id)
        record_pre_use_check(conn, asset, tag_id, "Alice", "Bob", "FAIL")
        conn.close()

        # Simulate restart.
        create_app(database_path=self.db_path)
        conn2 = dbmod.get_conn(self.db_path)
        asset_after_restart = dbmod.get_asset(conn2, asset_id)
        record_pre_use_check(conn2, asset_after_restart, tag_id, "Carol", "Dave", "PASS")
        conn2.close()

        conn3 = dbmod.get_conn(self.db_path)
        final_asset = dbmod.get_asset(conn3, asset_id)
        conn3.close()
        self.assertEqual(final_asset.current_status, STATUS_QUARANTINED)


if __name__ == "__main__":
    unittest.main()
