"""
LiftTag MVP — shared demo seed data and idempotent seeding logic.

Kept in its own module, separate from both app.py and seed.py, to avoid a
circular import: seed.py needs create_app() from app.py, and app.py needs
this seeding logic to run automatically on startup (required for Render's
Free tier — see README.md "Free-tier disposable deployment"). Putting the
data/logic here lets both app.py and seed.py import it directly.

This is fictitious test data only. Do not use real asset names, real
employee names, or real company data here.
"""
import sqlite3

import db as dbmod
from models import STATUS_IN_SERVICE

TEST_ASSETS = [
    ("SLING-001", "Web Sling"),
    ("SLING-002", "Web Sling"),
    ("CHAIN-001", "Chain Sling"),
    ("SHACKLE-001", "Shackle"),
    ("BEAM-001", "Lifting Beam"),
]

# Fixed, predetermined TEST-ONLY Tag IDs for the five demo assets.
#
# WHY FIXED, NOT RANDOM: Render's Free plan (used for this disposable MVP
# environment) has an ephemeral filesystem and does not provide the paid
# persistent disk or Pre-Deploy Command this project originally prepared
# for a paid deployment (see docs/decision-log.md). The SQLite database
# may be recreated from scratch on every restart or redeploy. If Tag IDs
# were randomly generated on each seed (as models.new_tag_id() does, and
# as this project used for a persistent deployment), the simulated NFC
# URLs would change every time the free service restarts — which would
# break any physical NFC tag already written with the old URL. Fixed demo
# IDs keep the five simulated tap URLs stable across free-tier restarts,
# for as long as this fixed scheme is used.
#
# These are ROUTING identifiers only (Correction 2, Software MVP Design
# Proposal / README "Security note"). They are NOT authentication and NOT
# production Tag IDs. They are clearly namespaced with a "demo-" prefix so
# they can never be mistaken for a real, randomly-generated production Tag
# ID, and they remain separate from Asset IDs (SLING-001 etc.), exactly as
# required.
DEMO_TAG_IDS = {
    "SLING-001": "demo-sling-001",
    "SLING-002": "demo-sling-002",
    "CHAIN-001": "demo-chain-001",
    "SHACKLE-001": "demo-shackle-001",
    "BEAM-001": "demo-beam-001",
}


def seed_demo_data(conn):
    """
    Idempotently ensure the five fictitious demo assets and their fixed
    demo Tag IDs exist on the given (already-open) connection.

    Safe to call on application startup, including many times in a row
    against the same database (e.g. every gunicorn worker start, every
    restart, every redeploy), and safe to call against a database that
    already has real check/audit-event/quarantine activity recorded
    against these assets:
      - never deletes or duplicates an asset;
      - never changes an existing tag's tag_id;
      - never touches checks or audit_events for an existing asset;
      - never changes an existing asset's current_status — a quarantined
        demo asset stays quarantined for as long as the current database
        happens to survive.

    A create can lose a benign race (e.g. two processes starting at once
    against the same fresh database) — that is treated as "someone else
    already created it," not an error, so this stays safe even outside a
    single-worker deployment.

    Returns the list of (asset_id, tag_id) pairs for all five assets,
    regardless of whether they were just created or already existed.
    """
    result = []
    for asset_id, equipment_type in TEST_ASSETS:
        asset = dbmod.get_asset(conn, asset_id)
        if asset is None:
            try:
                dbmod.create_asset(conn, asset_id, equipment_type, "VALID", STATUS_IN_SERVICE)
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()  # another process created it first; that's fine

        tag = dbmod.get_active_tag(conn, asset_id)
        if tag is None:
            tag_id = DEMO_TAG_IDS[asset_id]
            try:
                dbmod.create_tag(conn, tag_id, asset_id, active=True)
                dbmod.insert_audit_event(
                    conn, asset_id, "TAG_ASSIGNED", "startup-seed", None, tag_id, reference=f"tag:{tag_id}"
                )
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()  # another process created it first; that's fine
            tag = dbmod.get_active_tag(conn, asset_id)

        result.append((asset_id, tag.tag_id))
    return result
