# LiftTag MVP — Test Environment

**LiftTag MVP — Test Environment — Fictitious Data Only — Not for Operational Use**

A deliberately small demonstrator of the LiftTag concept:

```
NFC tap (simulated via URL) → Tag ID → Asset → Physical pre-use check →
PASS/FAIL → Status update → Audit record → Verification from another device
```

This exists to test the *workflow*, not to operate real lifting equipment. It does not authorise
production deployment or approve LiftTag as an operational safety system. See **Limitations** below.

## What this is not

- Not a production system, not certified, not connected to any company system or HPC.
- Not authenticated (see Security note).
- Not offline-capable (see Limitations).
- Uses only five fictitious test assets: `SLING-001`, `SLING-002`, `CHAIN-001`, `SHACKLE-001`, `BEAM-001`.
- Contains no real company data and no real employee data.

## Technology

Flask, Python's built-in `sqlite3` module (no ORM), and server-rendered templates. There is no
database server, no build step, and no non-standard-library dependency required to run the app or
its tests — only `Flask` itself. `gunicorn` is listed in `requirements.txt` for deployment only.

## Security note (Correction 2 — approved 2026-08-16)

The Tag ID in each `/t/<tag_id>` URL is an opaque, non-sequential routing token, but it is **not**
authentication or authorisation. Anyone who has a link can open it. This is acceptable only
because: all data is fictitious; this is a controlled demonstrator; and the environment is clearly
labelled as not for operational use. Production authentication/authorisation is deferred and is not
part of this MVP.

Two Tag ID schemes exist in this codebase, both equally non-authenticating:
- `models.new_tag_id()` — a randomly generated token, used when a genuinely new tag is issued
  (e.g. `workflow.assign_tag`).
- `seed_data.DEMO_TAG_IDS` — five **fixed, predetermined** `demo-...` tokens used only to seed the
  five fictitious demo assets, specifically so their simulated tap URLs stay stable across restarts
  on the ephemeral Render Free environment (see "Free-tier disposable deployment" below). Fixed or
  random, neither is authentication, and Asset ID and Tag ID remain distinct in both cases.

## Quarantine logic (Correction 1 — approved 2026-08-16)

A PASS pre-use check **never** releases an asset from quarantine:

| Current status | Result | New status | Audit events |
|---|---|---|---|
| IN SERVICE | PASS | stays IN SERVICE | `CHECK_PASS` |
| IN SERVICE | FAIL | becomes QUARANTINED — DO NOT USE | `CHECK_FAIL`, `STATUS_CHANGE` |
| QUARANTINED — DO NOT USE | PASS | stays QUARANTINED — DO NOT USE | `CHECK_PASS` (UI states PASS does not release quarantine) |
| QUARANTINED — DO NOT USE | FAIL | stays QUARANTINED — DO NOT USE | `CHECK_FAIL` only |

See `workflow.py::record_pre_use_check` for the implementation and `tests/test_workflow.py` for the
tests that prove it, including that ten consecutive PASS checks on a quarantined asset never release it.

There is **no** release-from-quarantine workflow in this MVP. None has been invented.

## Main asset screen

The asset page (`/t/<tag_id>` for an active tag) shows, in order: Asset ID (page heading),
Equipment Type, Current Tag ID, Periodic Inspection Status, Current Equipment Status (both as a
labelled field and, when quarantined, as a large red banner), Last Pre-Use Check, a "Start Pre-Use
Check" button, and a "View Audit History" link. Covered by
`tests/test_routes.py::test_asset_page_shows_all_eight_required_fields`. When quarantined, the
`QUARANTINED — DO NOT USE` banner is deliberately the most visually prominent element on the page
(covered by `test_quarantine_banner_is_prominent_on_asset_page`).

## Local setup

Requires only Python 3 and Flask.

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt   # installs Flask (+ gunicorn, for later deployment)

python app.py                     # runs at http://localhost:5000 — creates lifttag.db and
                                   # automatically seeds the 5 fictitious assets/tags at startup
```

`python seed.py` still works too (and is handy for printing the five tap URLs directly to your
terminal without starting the server), but it is no longer required — `python app.py` alone is
enough, because seeding now happens automatically inside `create_app()` (see "Free-tier disposable
deployment" below for why).

Then either open one of the printed URLs, or visit `http://localhost:5000/` for the
**Test Harness** index, which lists all five simulated tap links (clearly labelled as test-only,
not part of the real tap flow). Open the same link from a second phone/browser at the same time to
see requirement 8 (another device sees the latest server state) for yourself.

If `pip install` isn't possible in your environment, this app also runs against a Flask that is
already available system-wide — only `Flask` itself is required, everything else is the Python
standard library.

## Running the automated tests

```bash
python -m unittest discover -s tests -v
```

No extra install is required — the tests are written against the standard-library `unittest`
framework. If you have `pytest` installed, it will also discover and run the same test files with
no changes needed (`pytest`).

Test files:
- `tests/test_workflow.py` — unit tests for the safety-critical PASS/FAIL/quarantine state logic
  and for active/unknown/revoked Tag ID resolution. Includes explicit tests that ten consecutive
  PASS checks, and separately ten consecutive FAIL checks, on a quarantined asset never release it
  and never log a redundant `STATUS_CHANGE`.
- `tests/test_routes.py` — HTTP-level tests: full check submission flow, fail-safe pages for
  unknown/revoked tags, the persistent banner, "another device sees the latest state," that the
  asset page shows all eight required fields, and that the quarantine banner is prominent.
- `tests/test_seed.py` — confirms the seed script creates exactly the five required fictitious
  assets with unique tag URLs.
- `tests/test_persistence.py` — proves repeated seeding never regenerates an existing Tag ID, never
  duplicates an asset, never touches an existing check or audit event, and never releases a
  quarantined asset — including when re-seeding happens *after* real check/quarantine activity.
  Also proves a quarantined asset's status, Tag ID, and audit history all survive a simulated
  application restart (a fresh process against the same on-disk database file). Written for the
  originally-prepared paid/persistent architecture; still valid and still run.
- `tests/test_startup_seeding.py` — proves the automatic startup seeding this app now relies on for
  Render Free: a completely empty database is seeded just by calling `create_app()`; the root Test
  Harness shows all five assets with clickable `/t/<tag_id>` links; all five fixed demo tap URLs
  resolve; repeated startup never duplicates an asset or tag; repeated startup never releases a
  quarantined asset while the current database still exists; the fixed demo Tag IDs never change
  across repeated initialisation; and unknown/revoked tag handling still fails safe.

**Verified in this environment:** all 39 tests pass (`Ran 39 tests ... OK`), and the app was also
smoke-tested by actually running the dev server (with no prior seeding step) and curling the full
flow: index showed all five fixed `demo-...` tap links → FAIL on `demo-sling-001` → quarantine
banner shown → server killed and restarted against the same on-disk file (simulating a Render Free
restart while the ephemeral filesystem happens to still exist) → same URL still showed
`QUARANTINED — DO NOT USE`, not re-released → unknown tag still returned the fail-safe 404 page.

## Free-tier disposable deployment (live since 2026-08-16)

**Public URL:** https://lifttag-mvp.onrender.com — deployed on **Render's Free plan**.

This is deliberately **not** the persistent architecture originally prepared (see "Deployment
readiness" below, which remains accurate as a description of that separate, paid configuration).
Render Free does not provide a persistent disk or a Pre-Deploy Command, so this deployment uses a
different, simpler mechanism suited to a disposable demo:

- **Automatic startup seeding.** `create_app()` (in `app.py`) idempotently seeds the five
  fictitious demo assets every time the application starts — see `seed_data.py`. There is no
  manual `python seed.py` step and no `preDeployCommand`. This is what keeps the Test Harness from
  ever being empty on this environment.
- **Fixed demo Tag IDs**, not random ones. Because the database can be recreated from scratch on a
  redeploy or a platform-triggered restart of an ephemeral service, a randomly-generated Tag ID
  would produce a different URL each time — breaking any physical NFC tag already written with the
  old one. The five demo assets instead always seed to the same predetermined, clearly-labelled
  test-only tokens (`seed_data.DEMO_TAG_IDS`):

  | Asset ID | Tag ID | Public URL |
  |---|---|---|
  | `SLING-001` | `demo-sling-001` | `https://lifttag-mvp.onrender.com/t/demo-sling-001` |
  | `SLING-002` | `demo-sling-002` | `https://lifttag-mvp.onrender.com/t/demo-sling-002` |
  | `CHAIN-001` | `demo-chain-001` | `https://lifttag-mvp.onrender.com/t/demo-chain-001` |
  | `SHACKLE-001` | `demo-shackle-001` | `https://lifttag-mvp.onrender.com/t/demo-shackle-001` |
  | `BEAM-001` | `demo-beam-001` | `https://lifttag-mvp.onrender.com/t/demo-beam-001` |

  These are ROUTING identifiers only — not authentication, not production Tag IDs — and remain
  distinct from Asset IDs, exactly as required. See "Security note" above.
- **Same approved safety-state logic.** The Correction 1 PASS/FAIL/quarantine table below is
  completely unchanged by any of this — startup seeding never touches `current_status`, `checks`,
  or `audit_events` for an asset that already exists (see `tests/test_startup_seeding.py`).

### Explicit free-tier warning

- **Render Free storage is ephemeral.** Unlike the paid/persistent configuration below, nothing
  guarantees the SQLite file survives a restart, redeploy, or platform-triggered filesystem
  recreation.
- **Checks and audit history may be reset** whenever that happens — a quarantine recorded during
  testing may disappear along with the rest of the database, at which point the five demo assets
  simply reseed as `IN SERVICE` with their same fixed Tag IDs.
- **This limitation is acceptable only for this disposable MVP** — a demo used to prove the tap →
  identify → check → record → verify workflow and to test with physical NFC hardware once
  available. It is explicitly not acceptable for any operational or production use.
- **Production, offline, and persistence requirements are unchanged** by this free-tier
  accommodation. Offline-first remains a mandatory production requirement (Product & Safety
  Requirements) that this MVP does not validate; the persistent/paid architecture documented below
  remains the correct target whenever real, durable test data is needed again.

## Deployment readiness (added 2026-08-16)

**Status:** this section describes the paid, persistent configuration that was approved and
prepared first. It is currently **not the live deployment** (see "Free-tier disposable deployment"
above) but is kept here, unmodified, as the documented path back to persistent storage — see the
commented block at the bottom of `render.yaml` for how to restore it.

### Persistence decision: Option A — SQLite on a genuinely persistent disk

Per the deployment-readiness corrections, the database must not be silently lost on a restart or a
redeploy. Two options were considered:

- **A. SQLite + a real persistent disk/volume** — no code change; `DATABASE_PATH` already points
  wherever it's told to.
- **B. Migrate to managed Postgres** — would need `db.py` rewritten against a Postgres driver and
  slightly different SQL (e.g. `AUTOINCREMENT` → `SERIAL`), for a demonstrator with five assets and
  occasional test writes.

**Chosen: Option A.** Checked against Render's own documentation and pricing (2026): Render's disks
"preserve local filesystem changes across deploys and restarts," but only on a **paid** plan — the
free tier does not support them. The cheapest plan that does is **Starter at $7/month**, plus disk
storage at **$0.25/GB/month** (a 1GB disk is enough here) — about **$7.25/month total**. Render's
own managed Postgres would need the same $7/month web service *plus* a database, whose cheapest
paid tier is $6/month (its free tier expires after 30 days) — roughly $13/month, for a rewrite that
buys nothing this demonstrator needs. So Option A is both cheaper and requires zero code changes.
Fly.io and Railway were also checked and land in a similar place: neither offers a genuinely free
persistent-storage tier for a new project either, so the "must not silently lose data" requirement
means a small paid plan somewhere no matter which platform or which option — Option A just gets
there with the smallest total change.

If concurrent write volume ever becomes a real concern (it will not for five fictitious assets),
Option B remains available as a later migration — nothing about this design blocks it.

### Deployment configuration prepared

`render.yaml` (Render Blueprint) is included in this repo, declaring:
- a **Starter** web service running `gunicorn app:app`;
- a **1GB persistent disk** mounted at `/var/data`;
- `DATABASE_PATH=/var/data/lifttag.db`, so the SQLite file lives on that disk, not on the
  service's ephemeral filesystem;
- `preDeployCommand: "python seed.py"` — Render runs this automatically, once per deploy, after
  the build and before the new version goes live. **There is no manual seeding step.** `seed.py`
  reads Render's own `RENDER_EXTERNAL_URL` environment variable automatically, so it does not need
  a manually-typed URL either.

### Automatic, safe database setup — no manual file editing, ever

Two things happen automatically on every deploy, restart, and redeploy, and neither requires
opening a shell or touching a database file by hand:

1. **Schema creation.** `create_app()` calls `db.init_db(db_path)` on every startup (see
   `app.py`/`db.py`). It runs `CREATE TABLE IF NOT EXISTS` for all four tables — never
   `DROP`, never destructive — so it is safe to run on an empty disk (first deploy) or an
   already-populated one (every deploy after).
2. **Seeding.** `preDeployCommand` runs `python seed.py` automatically. `seed.py` only *creates*
   an asset or tag the first time it finds one missing (`get_asset`/`get_active_tag` checked
   first); it never updates `current_status`, never touches `checks`, and never touches
   `audit_events`. `tests/test_persistence.py::test_repeated_seeding_after_quarantine_disturbs_nothing`
   proves this holds even after a real quarantine has happened — the exact situation on a
   redeploy of a service that's already been used for testing.

You do not need Render's Shell tab, `render ssh`, or any manual database command at any point in
normal use of this deployment.

### Exact deployment instructions for this paid configuration (not currently in use — see status note above)

1. Push this folder (including `render.yaml`) to a new git repository.
2. In the Render dashboard: **New → Blueprint**, connect that repository. Render reads
   `render.yaml` and proposes the `lifttag-mvp` web service with its disk already configured —
   review and approve it there (this is the point at which the $7.25/month plan is actually
   provisioned; nothing has been created yet by writing this file).
3. Render assigns a public HTTPS URL of the form `https://lifttag-mvp.onrender.com` (the exact
   subdomain depends on name availability at creation time; Render will show the real one).
4. The first deploy builds the app, then automatically runs `python seed.py` via
   `preDeployCommand` before going live — this creates the five assets and their Tag IDs on the
   persistent disk with no manual step. The five tap URLs can be read from the deploy log, or by
   visiting `/` on the deployed URL (the Test Harness index).
5. Confirm the permanent banner is visible on the deployed URL: *"LiftTag MVP — Test Environment —
   Fictitious Data Only — Not for Operational Use"* (it renders on every page via `base.html`,
   unconditionally — there's no toggle to turn it off).
6. Test the two-phone scenario: open one of the five URLs on Phone A, submit a FAIL check; then
   open the *same* URL on Phone B and confirm it shows `QUARANTINED — DO NOT USE`.
7. Test restart survival: trigger a manual restart from the Render dashboard (**Manual Deploy →
   Restart service**, or a redeploy) and confirm the same URL still shows the same quarantined
   state afterwards — `preDeployCommand` will run `seed.py` again on that redeploy, and
   `test_repeated_seeding_after_quarantine_disturbs_nothing` is the automated proof that this
   cannot disturb the quarantine.

**Nothing has been deployed.** This is configuration and instructions only, per your explicit
"do not deploy without approval."

### No secrets or local artefacts committed

This app has no login, no session, and sets no Flask `SECRET_KEY` — there is no secret value
anywhere in the code or config to accidentally commit. `.gitignore` excludes `*.db` (and
`*.db-journal`/`*.db-wal`/`*.db-shm`), `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.venv/`, and
`.env`, so the local `lifttag.db` test database, bytecode caches, and any local `.env` file are
never pushed to git or deployed. `render.yaml`'s only environment values are `DATABASE_PATH` (a
path, not a secret) and `PYTHON_VERSION`.

### Final persistent public test URL format

Once deployed and seeded once, each of the five URLs will be:

```
https://<your-service>.onrender.com/t/<tag_id>
```

e.g. `https://lifttag-mvp.onrender.com/t/8f2kq1z9r4`. These are the exact URLs to later write to
the physical NFC tags (Design Proposal §10) — they will not change on restart or redeploy, because
the disk they're stored on doesn't reset, and `seed.py` will not touch them once they exist.

## Limitations (explicit)

- **Currently deployed on Render Free: ephemeral storage.** The live environment
  (https://lifttag-mvp.onrender.com) may lose its database — including real check/audit history and
  any quarantine recorded during testing — on restart, redeploy, or platform-triggered filesystem
  recreation. See "Free-tier disposable deployment" above. This is a disposable-demo accommodation
  only, not a change to the persistence requirement itself.
- **Online-only.** No offline mode, no local caching, no sync queue. Every page load re-reads the
  database, which is why a second device sees the latest state — but there is no offline resilience.
  Offline-first remains a mandatory production requirement (Product & Safety Requirements); this MVP
  does not validate it and must not be read as having done so.
- **No authentication.** The Tag ID is a routing token only (see Security note above).
- **No quarantine-release workflow**, by design, not by oversight.
- **"Checked By" / "Lift Supervisor" are free-text fields**, not verified identities.
- **Audit events are application-level append-only** — no route or UI can update or delete a row
  (see `db.py`, which has no `UPDATE`/`DELETE` against `audit_events` anywhere) — but there is no
  database-level immutability, cryptographic chaining, or tamper-proof storage yet. That is a later
  Safety/Compliance/Cyber Review-stage (Category B) requirement.
- **Periodic inspection status is a fixed fictitious value** per asset, not computed from any real
  inspection schedule.
- **SQLite, single file, no connection pooling.** Fine at this scale (five assets, occasional test
  writes); would need reconsidering for real concurrent load, which is not this MVP's purpose.
- **No production cybersecurity review has been performed.**
- **No role-based permissions** — a single implicit "tester" role for all users.
