# LiftTag MVP — Beginner-Friendly Deployment Guide (GitHub + Render)

This guide assumes no prior experience with GitHub or Render. Follow the steps in order. Nothing
in this repository deploys itself — every step below is something you do, on purpose, in your own
browser or terminal.

## A. Get this project into GitHub

1. Go to https://github.com and sign in (or create a free account).
2. Click the **+** icon (top right) → **New repository**.
3. Name it `lifttag-mvp`. Set visibility to **Private** (recommended — this is a test environment,
   not something that needs to be public). Do **not** tick "Add a README" (you already have one).
   Click **Create repository**.
4. GitHub shows you a page with commands. On your own computer, open a terminal in this project
   folder (the one containing `app.py`, `render.yaml`, etc.) and run:
   ```bash
   git init
   git add .
   git commit -m "LiftTag MVP — initial commit"
   git branch -M main
   git remote add origin https://github.com/<your-username>/lifttag-mvp.git
   git push -u origin main
   ```
   Replace `<your-username>` with your actual GitHub username. If you don't have `git` installed,
   GitHub's "uploading an existing folder" web page also works — drag the whole project folder in.
5. Refresh the GitHub page — you should see all the project files (`app.py`, `render.yaml`,
   `templates/`, `tests/`, etc.). You should **not** see any `.db` file or `__pycache__` folder —
   `.gitignore` deliberately excludes those.

## B–E. Create the service on Render

6. Go to https://render.com and sign in (or create a free account) — you can sign in directly with
   your GitHub account, which makes the next step easier.
7. Click **New** (top right) → **Blueprint**.
8. Render will ask you to connect a GitHub repository. Choose **Configure account** if this is
   your first time, grant Render access to the `lifttag-mvp` repository specifically (or to all
   repositories, your choice), then select `lifttag-mvp` from the list.
9. Render automatically detects `render.yaml` in the repository and shows you a preview: one web
   service named `lifttag-mvp`, on the **Starter** plan, with a **1GB persistent disk**. This is
   exactly the configuration prepared and described in `README.md`.
10. This is the point where the paid plan is actually created. Render will show pricing (Starter
    is $7/month, plus $0.25/GB/month for the 1GB disk — about $7.25/month total). Review it, then
    click **Apply** (or **Approve**/**Create**, wording may vary slightly by Render's current UI)
    to confirm you accept the paid plan and want to proceed.
11. Render will ask you to confirm payment details if you haven't already added a card to your
    Render account.

## F. Initial seeding — this happens automatically

12. You do **not** need to run any seed command yourself. Render builds the app
    (`pip install -r requirements.txt`), then automatically runs `python seed.py` (declared as
    `preDeployCommand` in `render.yaml`) before the new version goes live. This creates the five
    fictitious test assets and their Tag IDs on the persistent disk.
13. Watch the **Logs** tab while the first deploy runs. Near the end of the deploy log you'll see
    output starting with `LiftTag MVP — Test Environment...` followed by five lines like:
    ```
    SLING-001      tag_id=8f2kq1z9r4  https://lifttag-mvp.onrender.com/t/8f2kq1z9r4
    ```
    Those five lines are your five permanent test URLs — the ones you'll eventually write to the
    physical NFC tags. You can copy them straight from the log.

## G. Find the public HTTPS URL

14. At the top of the service's page in the Render dashboard, Render shows the live URL, in the
    form `https://lifttag-mvp.onrender.com` (the exact subdomain depends on name availability —
    Render shows you the real one). This is also visible in the deploy log lines from step 13.
15. Open that URL in a browser. You should see the **Test Harness** index page, listing all five
    fictitious assets, with the banner *"LiftTag MVP — Test Environment — Fictitious Data Only —
    Not for Operational Use"* at the top of the page.

## H. Verify the five persistent test Tag URLs

16. From the index page (step 15), click each of the five asset links in turn (or open each URL
    from the deploy log in step 13). Each should open an asset page showing: Asset ID, Equipment
    Type, Current Tag ID, Periodic Inspection Status, Current Equipment Status (`IN SERVICE`
    initially), Last Pre-Use Check (none yet), a **Start Pre-Use Check** button, and a **View
    Audit History** link.
17. If a URL instead shows an "unrecognised tag" or "no longer active" page, something is wrong —
    stop and check the deploy log for errors before continuing; do not proceed to writing tags to
    physical NFC hardware.

## I. Test with Phone A and Phone B

18. On **Phone A**, open one of the five URLs from step 13/16. Tap **Start Pre-Use Check**, fill in
    "Checked By" and "Lift Supervisor" with any test names, choose **FAIL**, and submit.
19. You should see a `QUARANTINED — DO NOT USE` result, prominently displayed.
20. On **Phone B** (a genuinely separate device, or a different browser/private window), open the
    **same** URL. It should also show `QUARANTINED — DO NOT USE` — this proves both devices are
    reading the same live server state, not a locally cached copy.
21. On either phone, submit another check with result **PASS** on the same URL. The page should
    still show `QUARANTINED — DO NOT USE`, with wording explaining that a PASS does not release a
    quarantined asset. This is the deliberate, approved safety behaviour (Correction 1) — it is
    not a bug.

## J. Confirm data survives a service restart

22. In the Render dashboard, on the `lifttag-mvp` service page, use **Manual Deploy → Restart
    service** (or trigger a redeploy by pushing any small change to GitHub, e.g. an edit to
    `README.md`).
23. Wait for the restart/redeploy to finish. Because `preDeployCommand` runs `python seed.py`
    again on every deploy, watch the log again — it will run once more, but since the five assets
    already exist it does not recreate them.
24. Reload the same tag URL from step 18 on either phone. It should still show
    `QUARANTINED — DO NOT USE`, with the same Tag ID as before. This confirms the SQLite database
    on the persistent disk survived the restart, and that the automatic re-seeding did not disturb
    the quarantine — exactly what
    `tests/test_persistence.py::test_repeated_seeding_after_quarantine_disturbs_nothing` and
    `RestartSurvivalTestCase` prove automatically in the test suite.

---

At this point the deployment is live, verified from two independent devices, and proven to survive
a restart. Nothing further is required for the controlled test environment described in the
Software MVP Design Proposal.
