"""
Tests for the Asset Registry / Tag Commissioning increment — added
2026-08-17.

Covers: an existing Company Asset is structurally separate and permanent
from its (replaceable) NFC Tag; searching the registry by Asset ID or
Serial Number; the admin commissioning workflow (assign / explicit
replace, never a silent overwrite); that tag replacement never touches the
Asset ID, inspection/check history, or quarantine state; that a revoked
tag still fails safe; that an Inspection Session resolves a scanned tag
through to the pre-existing permanent asset rather than ever creating one;
and the CSV import format (preview/validate, then a separate commit,
never assigning a tag).

Written with unittest (standard library, no extra install required). Also
discoverable and runnable by pytest, if installed, with no changes.
"""
import os
import tempfile
import unittest

import db as dbmod
import csv_import
from app import create_app
from workflow import (
    record_pre_use_check,
    commission_tag,
    replace_tag,
    register_asset,
    AssetAlreadyHasActiveTagError,
    NoActiveTagToReplaceError,
    TagIdAlreadyExistsError,
    AssetAlreadyRegisteredError,
)
from models import STATUS_IN_SERVICE, STATUS_QUARANTINED
from seed_data import DEMO_TAG_IDS


class AssetRegistryTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.app = create_app(database_path=self.db_path)  # seeds demo assets automatically
        self.client = self.app.test_client()
        # Added 2026-08-18: /admin/* routes now require an AP or Supervisor
        # DEMO role (approved requirement — see workflow.py "Demo Role
        # Architecture"). This test class exercises admin routes
        # extensively, so every test in it acts as the bootstrap "Demo AP"
        # identity (auto-seeded by create_app() via seed_data.py) —
        # DEMO ACCESS — NOT AUTHENTICATED, same as the real app.
        with self.client.session_transaction() as sess:
            sess["demo_actor_name"] = "Demo AP"

    def tearDown(self):
        os.remove(self.db_path)

    def _register(self, asset_id="NEW-001", **kwargs):
        conn = dbmod.get_conn(self.db_path)
        asset = register_asset(conn, asset_id, kwargs.pop("equipment_type", "Test Widget"), actor="test", **kwargs)
        conn.close()
        return asset

    # --- permanent asset identity, distinct from Tag ID ---------------------

    def test_asset_identity_distinct_from_tag_id(self):
        """The asset survives with the same identity whether or not it has
        a tag at all, and its identity is never derived from a tag."""
        asset = self._register("BOLLARD-001", serial_number="SN-777")
        self.assertEqual(asset.asset_id, "BOLLARD-001")

        conn = dbmod.get_conn(self.db_path)
        self.assertIsNone(dbmod.get_active_tag(conn, "BOLLARD-001"), "a freshly registered asset has no tag yet")
        conn.close()

        tag = commission_tag(dbmod.get_conn(self.db_path), asset, tag_id="demo-bollard-001", actor="test")
        self.assertEqual(tag.asset_id, "BOLLARD-001")
        self.assertNotEqual(tag.tag_id, asset.asset_id, "Tag ID and Asset ID are never the same identifier")

    # --- registry search (requirement 2) ------------------------------------

    def test_search_by_asset_id(self):
        resp = self.client.get("/admin/assets?q=SHACKLE-001")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"SHACKLE-001", resp.data)
        self.assertNotIn(b"SLING-001", resp.data)

    def test_search_by_serial_number(self):
        self._register("PADEYE-001", serial_number="ACME-SN-42")
        resp = self.client.get("/admin/assets?q=ACME-SN-42")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"PADEYE-001", resp.data)

    def test_blank_search_lists_every_asset(self):
        resp = self.client.get("/admin/assets")
        self.assertEqual(resp.status_code, 200)
        for asset_id in DEMO_TAG_IDS:
            self.assertIn(asset_id.encode(), resp.data)

    # --- commissioning: assign / explicit replace, never silent overwrite --

    def test_assign_tag_to_existing_asset_with_no_tag(self):
        self._register("HOOK-001")
        resp = self.client.post("/admin/assets/HOOK-001/assign-tag", data={"tag_id": "demo-hook-001"})
        self.assertEqual(resp.status_code, 302)

        conn = dbmod.get_conn(self.db_path)
        tag = dbmod.get_active_tag(conn, "HOOK-001")
        conn.close()
        self.assertIsNotNone(tag)
        self.assertEqual(tag.tag_id, "demo-hook-001")

    def test_duplicate_active_tag_assignment_is_rejected_at_workflow_layer(self):
        conn = dbmod.get_conn(self.db_path)
        asset = dbmod.get_asset(conn, "SHACKLE-001")  # already has an active demo tag
        with self.assertRaises(AssetAlreadyHasActiveTagError):
            commission_tag(conn, asset, tag_id="demo-shackle-001-again", actor="test")
        conn.close()

    def test_second_active_tag_requires_explicit_replacement_route(self):
        """The route itself must never silently overwrite — it redirects to
        the explicit replace-tag workflow instead of assigning."""
        resp = self.client.get("/admin/assets/SHACKLE-001/assign-tag")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/replace-tag", resp.headers["Location"])

        # And it must not have created a second active tag as a side effect.
        conn = dbmod.get_conn(self.db_path)
        active_tags = conn.execute(
            "SELECT COUNT(*) AS n FROM tags WHERE asset_id = ? AND active = 1", ("SHACKLE-001",)
        ).fetchone()["n"]
        conn.close()
        self.assertEqual(active_tags, 1)

    def test_commissioning_requires_asset_to_already_be_registered(self):
        """The NFC tag does not create the equipment (core requirement) —
        commissioning an unregistered Asset ID 404s."""
        resp = self.client.get("/admin/assets/DOES-NOT-EXIST/assign-tag")
        self.assertEqual(resp.status_code, 404)

    def test_tag_id_collision_is_rejected(self):
        self._register("CLAMP-001")
        conn = dbmod.get_conn(self.db_path)
        asset = dbmod.get_asset(conn, "CLAMP-001")
        with self.assertRaises(TagIdAlreadyExistsError):
            # demo-sling-001 already exists (assigned to SLING-001).
            commission_tag(conn, asset, tag_id=DEMO_TAG_IDS["SLING-001"], actor="test")
        conn.close()

    # --- tag replacement (requirement 6) ------------------------------------

    def test_replace_tag_revokes_old_and_assigns_new(self):
        old_tag_id = DEMO_TAG_IDS["BIN-001"]
        resp = self.client.post("/admin/assets/BIN-001/replace-tag", data={"tag_id": "demo-bin-001-v2"})
        self.assertEqual(resp.status_code, 302)

        conn = dbmod.get_conn(self.db_path)
        old_tag = dbmod.get_tag(conn, old_tag_id)
        new_tag = dbmod.get_active_tag(conn, "BIN-001")
        conn.close()
        self.assertFalse(old_tag.active, "the old tag must be revoked")
        self.assertEqual(new_tag.tag_id, "demo-bin-001-v2")

    def test_replace_tag_requires_an_existing_active_tag(self):
        self._register("SPREADER-001")  # never commissioned
        conn = dbmod.get_conn(self.db_path)
        asset = dbmod.get_asset(conn, "SPREADER-001")
        with self.assertRaises(NoActiveTagToReplaceError):
            replace_tag(conn, asset, actor="test")
        conn.close()

    def test_replace_tag_preserves_asset_id_and_history(self):
        conn = dbmod.get_conn(self.db_path)
        asset = dbmod.get_asset(conn, "SHACKLE-002")
        record_pre_use_check(conn, asset, DEMO_TAG_IDS["SHACKLE-002"], "Alice", "Bob", "PASS")
        checks_before = conn.execute("SELECT id FROM checks WHERE asset_id = ?", ("SHACKLE-002",)).fetchall()
        conn.close()

        conn = dbmod.get_conn(self.db_path)
        asset = dbmod.get_asset(conn, "SHACKLE-002")
        replace_tag(conn, asset, new_tag_id_value="demo-shackle-002-v2", actor="test")
        conn.close()

        conn = dbmod.get_conn(self.db_path)
        asset_after = dbmod.get_asset(conn, "SHACKLE-002")
        checks_after = conn.execute("SELECT id FROM checks WHERE asset_id = ?", ("SHACKLE-002",)).fetchall()
        conn.close()

        self.assertEqual(asset_after.asset_id, "SHACKLE-002", "Asset ID is unchanged by a tag replacement")
        self.assertEqual(len(checks_after), len(checks_before), "no check is created, altered, or removed by a tag replacement")

    def test_replace_tag_never_releases_quarantine(self):
        conn = dbmod.get_conn(self.db_path)
        asset = dbmod.get_asset(conn, "SHACKLE-002")
        record_pre_use_check(conn, asset, DEMO_TAG_IDS["SHACKLE-002"], "Alice", "Bob", "FAIL")  # quarantines
        conn.close()

        conn = dbmod.get_conn(self.db_path)
        quarantined = dbmod.get_asset(conn, "SHACKLE-002")
        self.assertEqual(quarantined.current_status, STATUS_QUARANTINED)
        replace_tag(conn, quarantined, new_tag_id_value="demo-shackle-002-v2", actor="test")
        conn.close()

        conn = dbmod.get_conn(self.db_path)
        after = dbmod.get_asset(conn, "SHACKLE-002")
        conn.close()
        self.assertEqual(after.current_status, STATUS_QUARANTINED, "replacing a tag must never release quarantine")

    def test_revoked_tag_remains_fail_safe_after_replacement(self):
        old_tag_id = DEMO_TAG_IDS["CHAIN-001"]
        self.client.post("/admin/assets/CHAIN-001/replace-tag", data={"tag_id": "demo-chain-001-v2"})

        resp = self.client.get(f"/t/{old_tag_id}")
        self.assertEqual(resp.status_code, 200)
        body = resp.data.lower()
        self.assertIn(b"no longer", body)
        self.assertNotIn(b"start pre-use check", body)

    # --- session resolves a tag to the pre-existing permanent asset ---------

    def test_session_add_never_creates_a_new_asset_from_a_tag(self):
        conn = dbmod.get_conn(self.db_path)
        asset_count_before = len(dbmod.list_assets(conn))
        session_id = dbmod.create_session(conn, "Sup", "Slinger")
        conn.commit()
        conn.close()

        resp = self.client.get(f"/session/{session_id}/add/{DEMO_TAG_IDS['SLING-001']}")
        self.assertEqual(resp.status_code, 302)

        conn = dbmod.get_conn(self.db_path)
        asset_count_after = len(dbmod.list_assets(conn))
        items = dbmod.list_session_items(conn, session_id, include_removed=False)
        conn.close()

        self.assertEqual(asset_count_after, asset_count_before, "adding a tag to a session must never create a new asset row")
        self.assertEqual(items[0].asset_id, "SLING-001", "the session item references the pre-existing permanent asset")

    def test_session_add_resolves_through_a_replaced_tag(self):
        """After a tag replacement, the *new* tag must resolve to the same
        permanent asset for session purposes — proving the session layer
        goes through the tag to the asset, not the other way round."""
        self.client.post("/admin/assets/BEAM-001/replace-tag", data={"tag_id": "demo-beam-001-v2"})

        conn = dbmod.get_conn(self.db_path)
        session_id = dbmod.create_session(conn, "Sup", "Slinger")
        conn.commit()
        conn.close()

        self.client.get(f"/session/{session_id}/add/demo-beam-001-v2")
        conn = dbmod.get_conn(self.db_path)
        items = dbmod.list_session_items(conn, session_id, include_removed=False)
        conn.close()
        self.assertEqual(items[0].asset_id, "BEAM-001")

    # --- repeated commissioning / registration is idempotent ---------------

    def test_repeated_registration_does_not_duplicate_asset(self):
        self._register("DUPTEST-001")
        conn = dbmod.get_conn(self.db_path)
        with self.assertRaises(AssetAlreadyRegisteredError):
            register_asset(conn, "DUPTEST-001", "Test Widget", actor="test")
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM assets WHERE asset_id = ?", ("DUPTEST-001",)
        ).fetchone()["n"]
        conn.close()
        self.assertEqual(count, 1)

    def test_startup_demo_seeding_still_idempotent_after_registry_schema_change(self):
        conn = dbmod.get_conn(self.db_path)
        count_before = len(dbmod.list_assets(conn))
        conn.close()
        create_app(database_path=self.db_path)
        create_app(database_path=self.db_path)
        conn = dbmod.get_conn(self.db_path)
        count_after = len(dbmod.list_assets(conn))
        conn.close()
        self.assertEqual(count_after, count_before)

    def test_all_original_demo_urls_still_valid(self):
        for asset_id in ["SLING-001", "SLING-002", "CHAIN-001", "SHACKLE-001", "BEAM-001"]:
            resp = self.client.get(f"/t/{DEMO_TAG_IDS[asset_id]}")
            self.assertEqual(resp.status_code, 200)
            self.assertIn(asset_id.encode(), resp.data)


class CsvImportTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.app = create_app(database_path=self.db_path)
        self.client = self.app.test_client()
        # See AssetRegistryTestCase.setUp — same reason.
        with self.client.session_transaction() as sess:
            sess["demo_actor_name"] = "Demo AP"

    def tearDown(self):
        os.remove(self.db_path)

    def _csv(self, rows):
        header = ",".join(csv_import.IMPORT_COLUMNS)
        lines = [header]
        for row in rows:
            lines.append(",".join(row.get(c, "") for c in csv_import.IMPORT_COLUMNS))
        return "\n".join(lines)

    def test_preview_validates_without_writing_anything(self):
        text = self._csv([{"asset_id": "IMP-001", "equipment_type": "Sling"}])
        conn = dbmod.get_conn(self.db_path)
        results = csv_import.preview_import(conn, text)
        exists = dbmod.get_asset(conn, "IMP-001")
        conn.close()
        self.assertEqual(results[0]["status"], "OK")
        self.assertIsNone(exists, "preview must never write to the database")

    def test_import_rejects_row_with_missing_asset_id(self):
        text = self._csv([{"asset_id": "", "equipment_type": "Sling"}])
        conn = dbmod.get_conn(self.db_path)
        results = csv_import.preview_import(conn, text)
        conn.close()
        self.assertEqual(results[0]["status"], "INVALID")

    def test_import_reports_duplicate_asset_id_already_registered(self):
        text = self._csv([{"asset_id": "SLING-001", "equipment_type": "Web Sling"}])
        conn = dbmod.get_conn(self.db_path)
        results = csv_import.preview_import(conn, text)
        conn.close()
        self.assertEqual(results[0]["status"], "DUPLICATE_ASSET_ID_ALREADY_REGISTERED")

    def test_import_reports_duplicate_asset_id_within_file(self):
        text = self._csv([
            {"asset_id": "IMP-002", "equipment_type": "Sling"},
            {"asset_id": "IMP-002", "equipment_type": "Sling (dup row)"},
        ])
        conn = dbmod.get_conn(self.db_path)
        results = csv_import.preview_import(conn, text)
        conn.close()
        self.assertEqual(results[0]["status"], "OK")
        self.assertEqual(results[1]["status"], "DUPLICATE_ASSET_ID_IN_FILE")

    def test_import_flags_duplicate_serial_number_as_warning_not_a_block(self):
        text = self._csv([
            {"asset_id": "IMP-003", "equipment_type": "Sling", "serial_number": "SAME-SN"},
            {"asset_id": "IMP-004", "equipment_type": "Sling", "serial_number": "SAME-SN"},
        ])
        conn = dbmod.get_conn(self.db_path)
        results = csv_import.preview_import(conn, text)
        conn.close()
        self.assertEqual(results[0]["status"], "OK")
        self.assertEqual(results[1]["status"], "OK")
        self.assertTrue(results[1]["serial_warning"])

    def test_commit_import_only_creates_ok_rows_and_assigns_no_tags(self):
        text = self._csv([
            {"asset_id": "IMP-005", "equipment_type": "Sling", "serial_number": "SN-5"},
            {"asset_id": "SLING-001", "equipment_type": "duplicate"},  # already registered
            {"asset_id": "", "equipment_type": "invalid"},  # invalid
        ])
        conn = dbmod.get_conn(self.db_path)
        results = csv_import.commit_import(conn, text, actor="test-import")
        new_asset = dbmod.get_asset(conn, "IMP-005")
        new_tag = dbmod.get_active_tag(conn, "IMP-005")
        total_assets = len(dbmod.list_assets(conn))
        conn.close()

        imported = [r for r in results if r.get("imported")]
        self.assertEqual(len(imported), 1)
        self.assertIsNotNone(new_asset)
        self.assertEqual(new_asset.serial_number, "SN-5")
        self.assertIsNone(new_tag, "import must never assign an NFC tag")

    def test_commit_import_logs_asset_registered_event(self):
        text = self._csv([{"asset_id": "IMP-006", "equipment_type": "Sling"}])
        conn = dbmod.get_conn(self.db_path)
        csv_import.commit_import(conn, text, actor="test-import")
        events = dbmod.list_audit_events(conn, "IMP-006")
        conn.close()
        self.assertTrue(any(e.event_type == "ASSET_REGISTERED" for e in events))

    def test_admin_import_route_end_to_end(self):
        text = self._csv([{"asset_id": "IMP-007", "equipment_type": "Sling"}])
        preview = self.client.post("/admin/import", data={"action": "preview", "csv_text": text})
        self.assertEqual(preview.status_code, 200)
        self.assertIn(b"OK", preview.data)

        confirm = self.client.post("/admin/import", data={"action": "confirm", "csv_text": text})
        self.assertEqual(confirm.status_code, 200)
        self.assertIn(b"Imported 1", confirm.data)

        detail = self.client.get("/admin/assets/IMP-007")
        self.assertEqual(detail.status_code, 200)


if __name__ == "__main__":
    unittest.main()
