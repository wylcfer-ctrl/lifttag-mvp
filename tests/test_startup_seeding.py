"""
Tests for automatic startup seeding on the Render Free (ephemeral,
disposable) deployment — added 2026-08-16.

Render's Free plan has no persistent disk and does not run a Pre-Deploy
Command, so seeding can no longer happen as a separate deploy step (see
docs/decision-log.md). Instead, create_app() itself idempotently seeds the
five fixed demo assets on every startup (see app.py / seed_data.py). These
tests exist specifically to prove that mechanism is safe:

1. A completely empty database is automatically seeded just by calling
   create_app() — no separate `python seed.py` step is required.
2. The root Test Harness ("/") shows all five assets.
3. All five simulated tap URLs (using the fixed demo Tag IDs) resolve.
4. Repeated application startup does not create duplicate assets or tags.
5. Startup does not release a quarantined asset while the current
   (ephemeral) database still exists.
6. The fixed demo Tag IDs never change across repeated initialisation.
7. Unknown and revoked tag handling still works in this startup-seeded
   context.

Written with unittest (standard library, no extra install required). Also
discoverable and runnable by pytest, if installed, with no changes.
"""
import os
import tempfile
import unittest

import db as dbmod
from app import create_app
from workflow import assign_tag
from models import STATUS_QUARANTINED, STATUS_IN_SERVICE
from seed_data import TEST_ASSETS, DEMO_TAG_IDS


class StartupSeedingTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.db_path)  # simulate a genuinely fresh/missing database file

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_empty_database_is_automatically_seeded_at_startup(self):
        """No seed.py call anywhere here — create_app() alone must seed."""
        self.assertFalse(os.path.exists(self.db_path))
        create_app(database_path=self.db_path)

        conn = dbmod.get_conn(self.db_path)
        for asset_id, _equipment_type in TEST_ASSETS:
            asset = dbmod.get_asset(conn, asset_id)
            self.assertIsNotNone(asset, f"{asset_id} must exist after startup with no prior seeding")
            self.assertEqual(asset.current_status, STATUS_IN_SERVICE)
        conn.close()

    def test_root_test_harness_shows_all_five_assets(self):
        app = create_app(database_path=self.db_path)
        client = app.test_client()

        resp = client.get("/")
        body = resp.data.decode("utf-8")
        self.assertEqual(resp.status_code, 200)
        for asset_id, _equipment_type in TEST_ASSETS:
            self.assertIn(asset_id, body)
        for tag_id in DEMO_TAG_IDS.values():
            self.assertIn(f"/t/{tag_id}", body, f"tap link for {tag_id} must be a clickable /t/<tag_id> link")

    def test_all_five_simulated_tap_urls_resolve(self):
        app = create_app(database_path=self.db_path)
        client = app.test_client()

        for asset_id, tag_id in DEMO_TAG_IDS.items():
            resp = client.get(f"/t/{tag_id}")
            self.assertEqual(resp.status_code, 200, f"{tag_id} must resolve to an asset page")
            self.assertIn(asset_id.encode(), resp.data)
            self.assertIn(b"Start Pre-Use Check", resp.data)

    def test_repeated_startup_does_not_duplicate_assets_or_tags(self):
        create_app(database_path=self.db_path)
        create_app(database_path=self.db_path)
        create_app(database_path=self.db_path)

        conn = dbmod.get_conn(self.db_path)
        for asset_id, _equipment_type in TEST_ASSETS:
            asset_count = conn.execute(
                "SELECT COUNT(*) AS n FROM assets WHERE asset_id = ?", (asset_id,)
            ).fetchone()["n"]
            self.assertEqual(asset_count, 1, f"{asset_id} must exist exactly once after repeated startup")

            tag_count = conn.execute(
                "SELECT COUNT(*) AS n FROM tags WHERE asset_id = ? AND active = 1", (asset_id,)
            ).fetchone()["n"]
            self.assertEqual(tag_count, 1, f"{asset_id} must have exactly one active tag after repeated startup")
        conn.close()

    def test_startup_does_not_release_a_quarantined_asset(self):
        from workflow import record_pre_use_check

        create_app(database_path=self.db_path)
        conn = dbmod.get_conn(self.db_path)
        asset = dbmod.get_asset(conn, "SLING-001")
        record_pre_use_check(conn, asset, DEMO_TAG_IDS["SLING-001"], "Alice", "Bob", "FAIL")
        conn.close()

        conn = dbmod.get_conn(self.db_path)
        self.assertEqual(dbmod.get_asset(conn, "SLING-001").current_status, STATUS_QUARANTINED)
        conn.close()

        # Simulate the app restarting several times against the same
        # (still-existing) ephemeral database file — e.g. multiple gunicorn
        # worker starts, or a manual restart before the filesystem resets.
        for _ in range(3):
            create_app(database_path=self.db_path)

        conn = dbmod.get_conn(self.db_path)
        asset_after = dbmod.get_asset(conn, "SLING-001")
        conn.close()
        self.assertEqual(
            asset_after.current_status, STATUS_QUARANTINED,
            "repeated startup seeding must never release a quarantined asset",
        )

    def test_fixed_demo_tag_ids_unchanged_across_repeated_initialisation(self):
        create_app(database_path=self.db_path)
        conn = dbmod.get_conn(self.db_path)
        first = {asset_id: dbmod.get_active_tag(conn, asset_id).tag_id for asset_id, _ in TEST_ASSETS}
        conn.close()

        self.assertEqual(first, DEMO_TAG_IDS, "seeded tag IDs must match the documented fixed demo scheme")

        for _ in range(3):
            create_app(database_path=self.db_path)

        conn = dbmod.get_conn(self.db_path)
        again = {asset_id: dbmod.get_active_tag(conn, asset_id).tag_id for asset_id, _ in TEST_ASSETS}
        conn.close()

        self.assertEqual(again, first, "fixed demo Tag IDs must never change across repeated initialisation")

    def test_unknown_tag_still_fails_safe_in_startup_seeded_app(self):
        app = create_app(database_path=self.db_path)
        client = app.test_client()

        resp = client.get("/t/does-not-exist")
        self.assertEqual(resp.status_code, 404)
        self.assertIn(b"nrecognised", resp.data)

    def test_revoked_tag_still_fails_safe_in_startup_seeded_app(self):
        app = create_app(database_path=self.db_path)
        conn = dbmod.get_conn(self.db_path)
        asset = dbmod.get_asset(conn, "SLING-001")
        old_tag_id = DEMO_TAG_IDS["SLING-001"]
        assign_tag(conn, asset, actor="test-harness")  # revokes the fixed demo tag, issues a new random one
        conn.close()

        client = app.test_client()
        resp = client.get(f"/t/{old_tag_id}")
        self.assertEqual(resp.status_code, 200)
        body = resp.data.lower()
        self.assertIn(b"no longer active", body)
        self.assertNotIn(b"start pre-use check", body)


if __name__ == "__main__":
    unittest.main()
