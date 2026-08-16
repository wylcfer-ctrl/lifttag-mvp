"""
LiftTag MVP — seed script.

Creates the database (if not already present) and the five fictitious
test assets, each with one active Tag ID, then prints the simulated NFC
tap URLs for each.

This is fictitious test data only. Do not use real asset names, real
employee names, or real company data here.

Safe to run any number of times, anywhere in the deployment lifecycle
(first deploy, every redeploy, every restart): it only ever creates an
asset/tag the first time it's missing, and never touches an asset's
current_status, its checks, or its audit events. See
tests/test_persistence.py for the tests that prove this.

Usage:
    python seed.py
    BASE_URL=https://your-deployed-app.example.com python seed.py

On Render, this runs automatically via `preDeployCommand` in render.yaml
on every deploy — you do not need to run it by hand. When run there with
no BASE_URL set, it automatically uses Render's own RENDER_EXTERNAL_URL
environment variable, so the printed/seeded URLs always match the real
public hostname.
"""
import os

import db as dbmod
from app import create_app
from workflow import assign_tag
from models import STATUS_IN_SERVICE

TEST_ASSETS = [
    ("SLING-001", "Web Sling"),
    ("SLING-002", "Web Sling"),
    ("CHAIN-001", "Chain Sling"),
    ("SHACKLE-001", "Shackle"),
    ("BEAM-001", "Lifting Beam"),
]


def seed(database_path=None, base_url="http://localhost:5000"):
    app = create_app(database_path=database_path)
    db_path = app.config["DATABASE_PATH"]
    conn = dbmod.get_conn(db_path)

    urls = []
    for asset_id, equipment_type in TEST_ASSETS:
        asset = dbmod.get_asset(conn, asset_id)
        if asset is None:
            dbmod.create_asset(conn, asset_id, equipment_type, "VALID", STATUS_IN_SERVICE)
            conn.commit()
            asset = dbmod.get_asset(conn, asset_id)

        tag = dbmod.get_active_tag(conn, asset_id)
        if tag is None:
            tag = assign_tag(conn, asset, actor="seed-script")

        urls.append((asset_id, tag.tag_id, f"{base_url.rstrip('/')}/t/{tag.tag_id}"))

    conn.close()
    return urls, app


if __name__ == "__main__":
    # Precedence: explicit BASE_URL > Render's own external URL > localhost.
    base_url = (
        os.environ.get("BASE_URL")
        or os.environ.get("RENDER_EXTERNAL_URL")
        or "http://localhost:5000"
    )
    db_path = os.environ.get("DATABASE_PATH", "lifttag.db")
    seeded, _ = seed(database_path=db_path, base_url=base_url)

    print("\nLiftTag MVP — Test Environment — Fictitious Data Only — Not for Operational Use\n")
    print("Seeded fictitious test assets and simulated NFC tap URLs:\n")
    for asset_id, tag_id, url in seeded:
        print(f"  {asset_id:14s} tag_id={tag_id:12s} {url}")
    print(f"\nOr open {base_url.rstrip('/')}/ for the Test Harness index of all five.\n")
