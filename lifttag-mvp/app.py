"""
LiftTag MVP — Flask application.

LiftTag MVP — Test Environment — Fictitious Data Only — Not for Operational Use

This is a deliberately small demonstrator of the tap -> identify -> check ->
record -> verify -> audit concept. It is not a production system. See
README.md for the full list of limitations.

SECURITY NOTE (Correction 2, approved 2026-08-16): the Tag ID in /t/<tag_id>
is an opaque, non-sequential ROUTING token (randomly generated for a normal
deployment; fixed test-only values on the disposable Render Free demo
environment — see seed_data.py). It is NOT authentication or authorisation.
Anyone who has a URL can open it. This is acceptable only because all data
here is fictitious and this environment is explicitly labelled as not for
operational use. Production authentication/authorisation is deferred and is
not part of this MVP.

FREE-TIER STARTUP SEEDING (added 2026-08-16): Render's Free plan has an
ephemeral filesystem and does not provide the paid persistent disk or
Pre-Deploy Command this project originally prepared for a paid deployment
(see docs/decision-log.md and README.md "Free-tier disposable deployment").
So that the app is usable on Render Free without a manual `python seed.py`
step, create_app() below idempotently seeds the five fixed demo assets on
every startup, via seed_data.seed_demo_data(). This never deletes or alters
an asset, check, audit event, or quarantine status that already exists in
the current database (see tests/test_startup_seeding.py).
"""
import os

from flask import Flask, render_template, redirect, url_for, request, abort

import db as dbmod
from workflow import (
    resolve_tag,
    record_pre_use_check,
    UnknownTagError,
    RevokedTagError,
    add_asset_to_session,
    remove_item_from_active_session,
    record_session_item_check,
    complete_session,
    SessionNotReadyError,
    SessionCompletedError,
    commission_tag,
    replace_tag,
    AssetAlreadyHasActiveTagError,
    NoActiveTagToReplaceError,
    TagIdAlreadyExistsError,
)
from models import RESULT_PASS, RESULT_FAIL, SESSION_STATUS_COMPLETED
from seed_data import seed_demo_data, DEMO_TAG_IDS
import csv_import

TEST_BANNER = "LiftTag MVP — Test Environment — Fictitious Data Only — Not for Operational Use"


def create_app(database_path=None):
    app = Flask(__name__)
    db_path = database_path or os.environ.get("DATABASE_PATH", "lifttag.db")
    app.config["DATABASE_PATH"] = db_path
    dbmod.init_db(db_path)

    # Idempotent — see seed_data.seed_demo_data docstring. Runs on every
    # startup so the Test Harness is never empty, without requiring a
    # manual seeding step or a Pre-Deploy Command (unavailable on Render
    # Free).
    _seed_conn = dbmod.get_conn(db_path)
    seed_demo_data(_seed_conn)
    _seed_conn.close()

    @app.context_processor
    def inject_banner():
        return {"TEST_BANNER": TEST_BANNER}

    def _resolve_or_fail_safe(conn, tag_id):
        """Shared fail-safe handling for unknown/revoked tags across routes."""
        try:
            return resolve_tag(conn, tag_id), None
        except UnknownTagError:
            return None, (render_template("tag_unknown.html", tag_id=tag_id), 404)
        except RevokedTagError as e:
            historical_asset = dbmod.get_asset(conn, e.tag.asset_id)
            return None, (render_template("tag_revoked.html", tag=e.tag, asset=historical_asset), 200)

    @app.route("/")
    def index():
        conn = dbmod.get_conn(db_path)
        assets = dbmod.list_assets(conn)
        rows = []
        for a in assets:
            tag = dbmod.get_active_tag(conn, a.asset_id)
            rows.append(
                {
                    "asset": a,
                    "tag": tag,
                    "tap_url": url_for("tag_entry", tag_id=tag.tag_id, _external=True) if tag else None,
                }
            )
        sessions = dbmod.list_sessions(conn)
        session_rows = []
        for s in sessions:
            active_count = len(dbmod.list_session_items(conn, s.id, include_removed=False))
            session_rows.append({"session": s, "active_count": active_count})
        conn.close()
        return render_template("index.html", rows=rows, session_rows=session_rows)

    @app.route("/t/<tag_id>")
    def tag_entry(tag_id):
        conn = dbmod.get_conn(db_path)
        resolved, fail_response = _resolve_or_fail_safe(conn, tag_id)
        if fail_response:
            conn.close()
            return fail_response
        tag, asset = resolved
        last_check = dbmod.get_last_check(conn, asset.asset_id)
        open_sessions = dbmod.list_open_sessions(conn)
        conn.close()
        return render_template(
            "asset.html", asset=asset, tag=tag, last_check=last_check, open_sessions=open_sessions
        )

    @app.route("/t/<tag_id>/check", methods=["GET", "POST"])
    def check(tag_id):
        conn = dbmod.get_conn(db_path)
        resolved, fail_response = _resolve_or_fail_safe(conn, tag_id)
        if fail_response:
            conn.close()
            return fail_response
        tag, asset = resolved

        if request.method == "POST":
            checked_by = request.form.get("checked_by", "").strip()
            lift_supervisor = request.form.get("lift_supervisor", "").strip()
            result = request.form.get("result", "").strip().upper()

            errors = []
            if not checked_by:
                errors.append("Checked By is required.")
            if not lift_supervisor:
                errors.append("Lift Supervisor is required.")
            if result not in ("PASS", "FAIL"):
                errors.append("Result must be PASS or FAIL.")

            if errors:
                conn.close()
                return render_template("check_form.html", asset=asset, tag=tag, errors=errors), 400

            check_record = record_pre_use_check(
                conn,
                asset=asset,
                tag_id_used=tag.tag_id,
                checked_by=checked_by,
                lift_supervisor=lift_supervisor,
                result=result,
            )
            check_id = check_record.id
            conn.close()
            return redirect(url_for("check_result", tag_id=tag_id, check_id=check_id))

        conn.close()
        return render_template("check_form.html", asset=asset, tag=tag, errors=None)

    @app.route("/t/<tag_id>/check/result/<int:check_id>")
    def check_result(tag_id, check_id):
        conn = dbmod.get_conn(db_path)
        resolved, fail_response = _resolve_or_fail_safe(conn, tag_id)
        if fail_response:
            conn.close()
            return fail_response
        tag, asset = resolved

        check_record = dbmod.get_check(conn, check_id)
        conn.close()
        if check_record is None or check_record.asset_id != asset.asset_id:
            abort(404)
        return render_template("check_result.html", asset=asset, tag=tag, check=check_record)

    @app.route("/asset/<asset_id>/audit")
    def audit_history(asset_id):
        conn = dbmod.get_conn(db_path)
        asset = dbmod.get_asset(conn, asset_id)
        if asset is None:
            conn.close()
            abort(404)
        events = dbmod.list_audit_events(conn, asset_id)
        conn.close()
        return render_template("audit_history.html", asset=asset, events=events)

    # --- Inspection Session v1 (added 2026-08-17) ---------------------------
    #
    # Multi-asset session workflow (requirements 1-16). Everything mutating
    # below goes through workflow.py, which itself reuses resolve_tag() and
    # record_pre_use_check() unchanged — see workflow.py for why. Every
    # route here 404s a missing session/item, and every mutating route
    # returns 409 with a clear message if the session is already COMPLETED
    # (requirement 13 — completed sessions are immutable).

    def _get_session_or_404(conn, session_id):
        session = dbmod.get_session(conn, session_id)
        if session is None:
            conn.close()
            abort(404)
        return session

    def _session_items_context(conn, session_id):
        active_items = dbmod.list_session_items(conn, session_id, include_removed=False)
        removed_items = [
            i for i in dbmod.list_session_items(conn, session_id, include_removed=True)
            if i.item_status != "ACTIVE"
        ]
        active = []
        attention_count = 0
        for item in active_items:
            asset = dbmod.get_asset(conn, item.asset_id)
            active.append({"item": item, "asset": asset})
            if item.check_result == RESULT_FAIL or asset.current_status != "IN SERVICE" or asset.periodic_inspection_status != "VALID":
                attention_count += 1
        removed = []
        for item in removed_items:
            removed.append({"item": item, "asset": dbmod.get_asset(conn, item.asset_id)})
        return active, removed, attention_count

    @app.route("/session/new", methods=["GET", "POST"])
    def session_new():
        if request.method == "POST":
            lift_supervisor = request.form.get("lift_supervisor", "").strip()
            slinger_signaller = request.form.get("slinger_signaller", "").strip()

            errors = []
            if not lift_supervisor:
                errors.append("Lift Supervisor is required.")
            if not slinger_signaller:
                errors.append("Slinger / Signaller is required.")

            if errors:
                return render_template("session_new.html", errors=errors, form=request.form), 400

            conn = dbmod.get_conn(db_path)
            # Server-generated timestamp only (requirement 2) — no client-supplied
            # date/time is accepted anywhere in this form.
            session_id = dbmod.create_session(conn, lift_supervisor, slinger_signaller)
            dbmod.insert_session_event(conn, session_id, "SESSION_CREATED", f"{slinger_signaller} / {lift_supervisor}")
            conn.commit()
            conn.close()
            return redirect(url_for("session_detail", session_id=session_id))

        return render_template("session_new.html", errors=None, form={})

    @app.route("/session/<int:session_id>")
    def session_detail(session_id):
        conn = dbmod.get_conn(db_path)
        session = _get_session_or_404(conn, session_id)
        active, removed, attention_count = _session_items_context(conn, session_id)
        just_checked = request.args.get("just_checked", type=int)
        just_removed = request.args.get("just_removed", type=int)
        conn.close()
        return render_template(
            "session.html", session=session, active=active, removed=removed,
            attention_count=attention_count, just_checked=just_checked, just_removed=just_removed,
            complete_error=None,
        )

    @app.route("/session/<int:session_id>/add")
    def session_add_picker(session_id):
        conn = dbmod.get_conn(db_path)
        session = _get_session_or_404(conn, session_id)
        if session.status == SESSION_STATUS_COMPLETED:
            conn.close()
            return redirect(url_for("session_detail", session_id=session_id))

        active_asset_ids = {i.asset_id for i in dbmod.list_session_items(conn, session_id, include_removed=False)}
        choices = []
        for asset_id, tag_id in sorted(DEMO_TAG_IDS.items()):
            asset = dbmod.get_asset(conn, asset_id)
            choices.append({
                "asset_id": asset_id,
                "tag_id": tag_id,
                "asset": asset,
                "already_active": asset_id in active_asset_ids,
            })

        added_asset_id = request.args.get("added")
        added_asset = dbmod.get_asset(conn, added_asset_id) if added_asset_id else None
        duplicate_asset_id = request.args.get("duplicate")
        unknown_tag_id = request.args.get("unknown")
        revoked_tag_id = request.args.get("revoked")
        conn.close()
        return render_template(
            "session_add.html", session=session, choices=choices,
            added_asset=added_asset, duplicate_asset_id=duplicate_asset_id,
            unknown_tag_id=unknown_tag_id, revoked_tag_id=revoked_tag_id,
        )

    @app.route("/session/<int:session_id>/add/<tag_id>")
    def session_add_tap(session_id, tag_id):
        """
        The simulated-NFC 'tap' endpoint (requirement 5). A GET, matching the
        existing /t/<tag_id> tap route's precedent — a real NFC tap or deep
        link is itself navigation, not a form submission, and duplicate
        protection already makes a repeated tap harmless (a no-op showing
        "already part of this inspection"), so this stays safe despite being
        a mutating GET. Swapping this simulated picker for a real NFC/deep
        -link handler later only means changing where tag_id comes from —
        this route's logic does not need to change.
        """
        conn = dbmod.get_conn(db_path)
        session = _get_session_or_404(conn, session_id)
        if session.status == SESSION_STATUS_COMPLETED:
            conn.close()
            return redirect(url_for("session_detail", session_id=session_id))

        try:
            item, created, asset = add_asset_to_session(conn, session, tag_id, actor="session-add")
        except UnknownTagError:
            conn.close()
            return redirect(url_for("session_add_picker", session_id=session_id, unknown=tag_id))
        except RevokedTagError as e:
            conn.close()
            return redirect(url_for("session_add_picker", session_id=session_id, revoked=e.tag.tag_id))

        conn.close()
        if created:
            return redirect(url_for("session_add_picker", session_id=session_id, added=asset.asset_id))
        return redirect(url_for("session_add_picker", session_id=session_id, duplicate=asset.asset_id))

    @app.route("/session/<int:session_id>/item/<int:item_id>/check", methods=["GET", "POST"])
    def session_item_check(session_id, item_id):
        conn = dbmod.get_conn(db_path)
        session = _get_session_or_404(conn, session_id)
        item = dbmod.get_session_item(conn, item_id)
        if item is None or item.session_id != session_id:
            conn.close()
            abort(404)
        if session.status == SESSION_STATUS_COMPLETED or item.item_status != "ACTIVE":
            conn.close()
            return redirect(url_for("session_detail", session_id=session_id))
        asset = dbmod.get_asset(conn, item.asset_id)

        checklist_fields = [
            ("chk_visual", "Visual condition acceptable"),
            ("chk_tag_legible", "Identification / tag legible"),
            ("chk_no_damage", "No obvious damage or deformation"),
            ("chk_no_wear", "No unacceptable wear"),
            ("chk_connections", "Components / connections appear serviceable"),
        ]

        if request.method == "POST":
            result = request.form.get("result", "").strip().upper()
            failure_reason = request.form.get("failure_reason", "").strip()
            checked_fields = [key for key, _label in checklist_fields if request.form.get(key) == "on"]
            checklist_confirmed = len(checked_fields) == len(checklist_fields)

            errors = []
            if result not in (RESULT_PASS, RESULT_FAIL):
                errors.append("Select PASS or FAIL.")
            if not checklist_confirmed:
                errors.append("Confirm every checklist point before submitting.")
            if result == RESULT_FAIL and not failure_reason:
                errors.append("A failure reason / defect note is required for a FAIL result.")

            if errors:
                conn.close()
                return render_template(
                    "session_item_check.html", session=session, item=item, asset=asset,
                    checklist_fields=checklist_fields, errors=errors, form=request.form,
                ), 400

            record_session_item_check(
                conn, session, item, asset,
                checked_by=session.slinger_signaller, lift_supervisor=session.lift_supervisor,
                result=result, failure_reason=(failure_reason or None),
                checklist_confirmed=True, actor="session-check",
            )
            conn.close()
            return redirect(url_for("session_detail", session_id=session_id, just_checked=item_id))

        conn.close()
        return render_template(
            "session_item_check.html", session=session, item=item, asset=asset,
            checklist_fields=checklist_fields, errors=None, form={},
        )

    @app.route("/session/<int:session_id>/item/<int:item_id>/remove", methods=["POST"])
    def session_item_remove(session_id, item_id):
        conn = dbmod.get_conn(db_path)
        session = _get_session_or_404(conn, session_id)
        item = dbmod.get_session_item(conn, item_id)
        if item is None or item.session_id != session_id:
            conn.close()
            abort(404)
        try:
            remove_item_from_active_session(conn, session, item, actor="session-remove")
        except SessionCompletedError:
            conn.close()
            return redirect(url_for("session_detail", session_id=session_id))
        conn.close()
        return redirect(url_for("session_detail", session_id=session_id, just_removed=item_id))

    @app.route("/session/<int:session_id>/complete", methods=["POST"])
    def session_complete(session_id):
        conn = dbmod.get_conn(db_path)
        session = _get_session_or_404(conn, session_id)
        try:
            complete_session(conn, session, actor="session-complete")
            conn.close()
            return redirect(url_for("session_detail", session_id=session_id))
        except SessionNotReadyError as e:
            active, removed, attention_count = _session_items_context(conn, session_id)
            conn.close()
            return render_template(
                "session.html", session=e.session, active=active, removed=removed,
                attention_count=attention_count, just_checked=None, just_removed=None,
                complete_error="This session is not READY yet — every active item must PASS before it can be completed.",
            ), 400
        except SessionCompletedError:
            conn.close()
            return redirect(url_for("session_detail", session_id=session_id))

    @app.route("/session/<int:session_id>/audit")
    def session_audit(session_id):
        conn = dbmod.get_conn(db_path)
        session = _get_session_or_404(conn, session_id)
        session_events = dbmod.list_session_events(conn, session_id)
        asset_events = dbmod.list_audit_events_for_session(conn, session_id)
        conn.close()

        merged = (
            [{"kind": "session", "timestamp": e["timestamp"], "row": e} for e in session_events]
            + [{"kind": "asset", "timestamp": e.timestamp.isoformat(), "row": e} for e in asset_events]
        )
        merged.sort(key=lambda x: x["timestamp"])
        return render_template("session_audit.html", session=session, merged=merged)

    # --- Asset Registry / Tag Commissioning (added 2026-08-17) --------------
    #
    # Deliberately separated from the field routes above (requirement 11):
    # every route here is prefixed /admin/ and is about managing the
    # permanent equipment registry and its NFC tag assignments. A field
    # worker never needs any of these — they only ever tap a tag (/t/<id>)
    # or use an Inspection Session (/session/...), both unchanged.

    @app.route("/admin")
    def admin_home():
        return render_template("admin_home.html")

    @app.route("/admin/assets")
    def admin_assets():
        conn = dbmod.get_conn(db_path)
        query = request.args.get("q", "")
        results = dbmod.search_assets(conn, query)
        rows = []
        for a in results:
            rows.append({"asset": a, "active_tag": dbmod.get_active_tag(conn, a.asset_id)})
        conn.close()
        return render_template("admin_assets.html", query=query, rows=rows)

    @app.route("/admin/assets/<asset_id>")
    def admin_asset_detail(asset_id):
        conn = dbmod.get_conn(db_path)
        asset = dbmod.get_asset(conn, asset_id)
        if asset is None:
            conn.close()
            abort(404)
        active_tag = dbmod.get_active_tag(conn, asset_id)
        tag_history = dbmod.list_tags_for_asset(conn, asset_id)
        conn.close()
        return render_template(
            "admin_asset_detail.html", asset=asset, active_tag=active_tag, tag_history=tag_history
        )

    @app.route("/admin/assets/<asset_id>/assign-tag", methods=["GET", "POST"])
    def admin_assign_tag(asset_id):
        conn = dbmod.get_conn(db_path)
        asset = dbmod.get_asset(conn, asset_id)
        if asset is None:
            conn.close()
            abort(404)
        existing_active = dbmod.get_active_tag(conn, asset_id)
        if existing_active is not None:
            # Requirement 5: never silently overwrite — send the admin to the
            # explicit replace-tag workflow instead of assigning here.
            conn.close()
            return redirect(url_for("admin_replace_tag", asset_id=asset_id))

        if request.method == "POST":
            custom_tag_id = (request.form.get("tag_id") or "").strip() or None
            try:
                commission_tag(conn, asset, tag_id=custom_tag_id, actor="admin-commissioning")
                conn.close()
                return redirect(url_for("admin_asset_detail", asset_id=asset_id))
            except TagIdAlreadyExistsError as e:
                conn.close()
                return render_template(
                    "admin_assign_tag.html", asset=asset,
                    errors=[f"Tag ID '{e.tag_id}' already exists — choose a different one or leave it blank to auto-generate."],
                    form=request.form,
                ), 400

        conn.close()
        return render_template("admin_assign_tag.html", asset=asset, errors=None, form={})

    @app.route("/admin/assets/<asset_id>/replace-tag", methods=["GET", "POST"])
    def admin_replace_tag(asset_id):
        conn = dbmod.get_conn(db_path)
        asset = dbmod.get_asset(conn, asset_id)
        if asset is None:
            conn.close()
            abort(404)
        existing_active = dbmod.get_active_tag(conn, asset_id)
        if existing_active is None:
            # Nothing to replace — send the admin to the initial commissioning
            # workflow instead.
            conn.close()
            return redirect(url_for("admin_assign_tag", asset_id=asset_id))

        if request.method == "POST":
            custom_tag_id = (request.form.get("tag_id") or "").strip() or None
            try:
                replace_tag(conn, asset, new_tag_id_value=custom_tag_id, actor="admin-commissioning")
                conn.close()
                return redirect(url_for("admin_asset_detail", asset_id=asset_id))
            except TagIdAlreadyExistsError as e:
                conn.close()
                return render_template(
                    "admin_replace_tag.html", asset=asset, existing_active=existing_active,
                    errors=[f"Tag ID '{e.tag_id}' already exists — choose a different one or leave it blank to auto-generate."],
                    form=request.form,
                ), 400

        conn.close()
        return render_template(
            "admin_replace_tag.html", asset=asset, existing_active=existing_active, errors=None, form={}
        )

    @app.route("/admin/import", methods=["GET", "POST"])
    def admin_import():
        if request.method == "POST":
            csv_text = request.form.get("csv_text", "")
            action = request.form.get("action", "preview")
            conn = dbmod.get_conn(db_path)

            if action == "confirm":
                results = csv_import.commit_import(conn, csv_text, actor="admin-import")
                conn.close()
                imported = [r for r in results if r.get("imported")]
                skipped = [r for r in results if not r.get("imported")]
                return render_template(
                    "admin_import.html", stage="result", results=results,
                    imported_count=len(imported), skipped_count=len(skipped),
                    csv_text=csv_text, columns=csv_import.IMPORT_COLUMNS,
                )

            results = csv_import.preview_import(conn, csv_text)
            conn.close()
            ok_count = len([r for r in results if r["status"] == "OK"])
            return render_template(
                "admin_import.html", stage="preview", results=results, ok_count=ok_count,
                csv_text=csv_text, columns=csv_import.IMPORT_COLUMNS,
            )

        return render_template(
            "admin_import.html", stage="form", results=None, csv_text="", columns=csv_import.IMPORT_COLUMNS
        )

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
