"""
Tests for the seed script: exactly the five required fictitious assets,
each with a unique, working tap URL.

Written with unittest (standard library, no extra install required). Also
discoverable and runnable by pytest, if installed, with no changes.
"""
import os
import tempfile
import unittest

from seed import seed


class SeedTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        os.remove(self.db_path)

    def test_seed_creates_exactly_five_assets_with_unique_tags(self):
        urls, _app = seed(database_path=self.db_path, base_url="http://localhost:5000")

        self.assertEqual(len(urls), 5)
        asset_ids = {a for a, _, _ in urls}
        self.assertEqual(
            asset_ids, {"SLING-001", "SLING-002", "CHAIN-001", "SHACKLE-001", "BEAM-001"}
        )

        tag_ids = [t for _, t, _ in urls]
        self.assertEqual(len(tag_ids), len(set(tag_ids)), "tag IDs must be unique")

        for asset_id, tag_id, url in urls:
            self.assertEqual(url, f"http://localhost:5000/t/{tag_id}")

        # Re-running seed against the same DB must not duplicate assets/tags.
        urls_again, _ = seed(database_path=self.db_path, base_url="http://localhost:5000")
        self.assertEqual(len(urls_again), 5)
        self.assertEqual({t for _, t, _ in urls_again}, set(tag_ids))


if __name__ == "__main__":
    unittest.main()
