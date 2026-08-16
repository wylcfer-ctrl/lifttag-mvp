"""
LiftTag MVP — Flask application.

LiftTag MVP — Test Environment — Fictitious Data Only — Not for Operational Use

This is a deliberately small demonstrator of the tap -> identify -> check ->
record -> verify -> audit concept. It is not a production system. See
README.md for the full list of limitations.

SECURITY NOTE (Correction 2, approved 2026-08-16): the Tag ID in /t/<tag_id>
is a random, non-sequential, opaque ROUTING token. It is NOT authentication
or authorisation. Anyone who has a URL can open it. This is acceptable only
because all data here is fictitious and this environment is explicitly
labelled as not for operational use. Production authentication/authorisation
is deferred and is not part of this MVP.
"""
import os

from flask import Flask, render_template, redirect, url_for, request, abort

import db as dbmod
from workflow import resolve_tag, record_pre_use_check, UnknownTagError, RevokedTagError

TEST_BANNER = "LiftTag MVP — Test Environment — Fictitious Data Only — Not for Operational Use"


def create_app(database_path=None):
    app = Flask(__name__)
    db_path = database_path or os.environ.get("DATABASE_PATH", "lifttag.db")
    app.config["DATABASE_PATH"] = db_path
    dbmod.init_db(db_path)

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
        conn.close()
        return render_template("index.html", rows=rows)

    @app.route("/t/<tag_id>")
    def tag_entry(tag_id):
        conn = dbmod.get_conn(db_path)
        resolved, fail_response = _resolve_or_fail_safe(conn, tag_id)
        if fail_response:
            conn.close()
            return fail_response
        tag, asset = resolved
        last_check = dbmod.get_last_check(conn, asset.asset_id)
        conn.close()
        return render_template("asset.html", asset=asset, tag=tag, last_check=last_check)

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

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
