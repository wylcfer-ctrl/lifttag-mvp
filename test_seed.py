"""
Tests for the seed script: every configured demo asset gets created, each
with a unique, working tap URL.

Updated 2026-08-17 (Inspection Session v1): two demo assets (BIN-001,
SHACKLE-002) were added to seed_data.TEST_ASSETS to properly demonstrate
the new multi-asset session workflow (requirement 17). This test now
derives its expectations from seed_data.TEST_ASSETS / DEMO_TAG_IDS instead
of a hardcoded set of five, so it stays correct as demo data evolves, and
explicitly still asserts the original five fixed URLs are present and
unchanged.

Written with unittest (standard library, no extra install required). Also
discoverable and runnable by pytest, if installed, with no changes.
"""
import os
import tempfile
import unittest

from seed import seed
from seed_data import TEST_ASSETS, DEMO_TAG_IDS

ORIGINAL_FIVE_TAG_IDS = {
    "SLING-001": "demo-sling-001",
    "SLING-002": "demo-sling-002",
    "CHAIN-001": "demo-chain-001",
    "SHACKLE-001": "demo-shackle-001",
    "BEAM-001": "demo-beam-001",
}


class SeedTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        os.remove(self.db_path)

    def test_seed_creates_every_configured_demo_asset_with_unique_tags(self):
        urls, _app = seed(database_path=self.db_path, base_url="http://localhost:5000")

        expected_asset_ids = {asset_id for asset_id, _equipment_type in TEST_ASSETS}
        self.assertEqual(len(urls), len(TEST_ASSETS))
        asset_ids = {a for a, _, _ in urls}
        self.assertEqual(asset_ids, expected_asset_ids)

        tag_ids = [t for _, t, _ in urls]
        self.assertEqual(len(tag_ids), len(set(tag_ids)), "tag IDs must be unique")

        for asset_id, tag_id, url in urls:
            self.assertEqual(url, f"http://localhost:5000/t/{tag_id}")
            self.assertEqual(tag_id, DEMO_TAG_IDS[asset_id])

        # Re-running seed against the same DB must not duplicate assets/tags.
        urls_again, _ = seed(database_path=self.db_path, base_url="http://localhost:5000")
        self.assertEqual(len(urls_again), len(TEST_ASSETS))
        self.assertEqual({t for _, t, _ in urls_again}, set(tag_ids))

    def test_original_five_fixed_urls_are_unchanged(self):
        """The five URLs that may already be written to physical NFC tags,
        or shared as the live Render Free demo links, must never move."""
        urls, _app = seed(database_path=self.db_path, base_url="https://lifttag-mvp.onrender.com")
        by_asset = {a: (t, u) for a, t, u in urls}

        for asset_id, expected_tag_id in ORIGINAL_FIVE_TAG_IDS.items():
            tag_id, url = by_asset[asset_id]
            self.assertEqual(tag_id, expected_tag_id)
            self.assertEqual(url, f"https://lifttag-mvp.onrender.com/t/{expected_tag_id}")


if __name__ == "__main__":
    unittest.main()
