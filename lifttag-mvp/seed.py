"""
LiftTag MVP — standalone seed script (local/manual convenience only).

As of 2026-08-16, seeding also happens automatically on every application
startup (see app.py / seed_data.py) — this is what makes the app usable on
Render's Free plan, which has no persistent disk and no Pre-Deploy Command.
This script is kept for local convenience: it lets you seed and print the
five simulated NFC tap URLs without first starting the dev server, and it
is still used directly by tests/test_seed.py and tests/test_persistence.py.

Calling seed() below is always safe and idempotent, whether or not the
five demo assets already exist (e.g. because create_app() already seeded
them) — see seed_data.seed_demo_data for exactly what that guarantees.

This is fictitious test data only. Do not use real asset names, real
employee names, or real company data here.

Usage:
    python seed.py
    BASE_URL=https://your-deployed-app.example.com python seed.py
"""
import os

import db as dbmod
from app import create_app
from seed_data import seed_demo_data


def seed(database_path=None, base_url="http://localhost:5000"):
    app = create_app(database_path=database_path)  # this call already seeds (see app.py)
    db_path = app.config["DATABASE_PATH"]
    conn = dbmod.get_conn(db_path)

    pairs = seed_demo_data(conn)  # idempotent no-op if create_app() already seeded
    conn.close()

    urls = [
        (asset_id, tag_id, f"{base_url.rstrip('/')}/t/{tag_id}")
        for asset_id, tag_id in pairs
    ]
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
