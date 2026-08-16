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

The Tag ID in each `/t/<tag_id>` URL is a random, non-guessable, non-sequential token, but it is
**not** authentication or authorisation. Anyone who has a link can open it. This is acceptable only
because: all data is fictitious; this is a controlled demonstrator; and the environment is clearly
labelled as not for operational use. Production authentication/authorisation is deferred and is not
part of this MVP.

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

python seed.py                    # creates lifttag.db and the 5 fictitious assets/tags,
                                   # and prints their simulated NFC tap URLs

python app.py                     # runs at http://localhost:5000
```

Then either open one of the URLs `seed.py` printed, or visit `http://localhost:5000/` for the
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
  quarantined asset — including when re-seeding happens *after* real check/quarantine activity, the
  exact scenario that occurs every time `preDeployCommand` re-runs `seed.py` on a redeploy. Also
  proves a quarantined asset's status, Tag ID, and audit history all survive a simulated application
  restart (a fresh process against the same on-disk database file).

**Verified in this environment:** all 31 tests pass (`Ran 31 tests ... OK`), and the app was also
smoke-tested by actually running the dev server and curling the full flow (index → active tag →
FAIL → quarantine banner and labelled status field shown → re-ran `seed.py` and restarted the
server, same Tag ID still resolved to the same quarantined asset → PASS while quarantined → still
quarantined with the "does not release" message → audit history in the correct order → unknown/
revoked tags show the fail-safe pages).

## Deployment readiness (added 2026-08-16)

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

### Exact deployment instructions (not yet executed — awaiting approval)

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
