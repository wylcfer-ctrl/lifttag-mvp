"""
LiftTag MVP — Asset Registry CSV import (added 2026-08-17).

Defines the clean CSV import format (requirement 9) and a safe MVP import
capability: parse/validate first (never writes anything), then a separate,
explicit commit step that only creates genuinely new assets. Never assigns
an NFC Tag as part of import — commissioning a tag is always a distinct,
later admin action (requirements 4/9/11).

Column order in IMPORT_COLUMNS is the suggested/expected header row. Extra
columns in the file are ignored; missing optional columns are treated as
blank. Only asset_id and equipment_type are required.
"""
import csv
import io

import db as dbmod
from workflow import register_asset, AssetAlreadyRegisteredError

IMPORT_COLUMNS = [
    "asset_id",
    "serial_number",
    "equipment_type",
    "description",
    "manufacturer",
    "model",
    "wll",
    "company",
    "periodic_inspection_status",
    "periodic_inspection_due",
    "notes",
]

STATUS_OK = "OK"
STATUS_INVALID = "INVALID"
STATUS_DUPLICATE_IN_FILE = "DUPLICATE_ASSET_ID_IN_FILE"
STATUS_DUPLICATE_EXISTING = "DUPLICATE_ASSET_ID_ALREADY_REGISTERED"


def _clean(value):
    value = (value or "").strip()
    return value or None


def preview_import(conn, csv_text):
    """
    Parses and validates csv_text against the current registry, WITHOUT
    writing anything (requirement 9: "preview/validate before import").

    Returns a list of row-result dicts, one per data row (1-indexed to
    match a spreadsheet row number, header excluded):
        {
            "row_num": int,
            "data": {column: value, ...},   # cleaned (blank -> None)
            "status": one of STATUS_*,
            "errors": [str, ...],           # populated when INVALID
            "serial_warning": bool,         # True if serial_number
                                             #   duplicates another row in
                                             #   this file or an existing
                                             #   asset — reported, not
                                             #   blocking (requirement 9:
                                             #   "report duplicate serial
                                             #   numbers where applicable")
        }

    Only rows with status == STATUS_OK are eligible to actually be
    imported by commit_import().
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    seen_asset_ids = set()
    seen_serials = set()
    results = []

    for row_num, raw_row in enumerate(reader, start=1):
        data = {col: _clean(raw_row.get(col)) for col in IMPORT_COLUMNS}
        errors = []

        if not data["asset_id"]:
            errors.append("Missing asset_id.")
        if not data["equipment_type"]:
            errors.append("Missing equipment_type.")

        status = STATUS_OK
        serial_warning = False

        if errors:
            status = STATUS_INVALID
        elif data["asset_id"] in seen_asset_ids:
            status = STATUS_DUPLICATE_IN_FILE
            errors.append(f"asset_id '{data['asset_id']}' appears more than once in this file.")
        elif dbmod.get_asset(conn, data["asset_id"]) is not None:
            status = STATUS_DUPLICATE_EXISTING
            errors.append(f"asset_id '{data['asset_id']}' is already registered — not overwritten.")

        if data["asset_id"] and status not in (STATUS_DUPLICATE_IN_FILE,):
            seen_asset_ids.add(data["asset_id"])

        if data["serial_number"]:
            if data["serial_number"] in seen_serials:
                serial_warning = True
            seen_serials.add(data["serial_number"])
            existing_with_serial = conn.execute(
                "SELECT asset_id FROM assets WHERE serial_number = ? AND asset_id != ?",
                (data["serial_number"], data["asset_id"] or ""),
            ).fetchone()
            if existing_with_serial is not None:
                serial_warning = True

        results.append({
            "row_num": row_num,
            "data": data,
            "status": status,
            "errors": errors,
            "serial_warning": serial_warning,
        })

    return results


def commit_import(conn, csv_text, actor):
    """
    Re-validates csv_text (so a stale/tampered preview can never be
    trusted) and registers every row whose status is STATUS_OK, via
    workflow.register_asset() — which itself refuses to overwrite an
    existing Asset ID and logs ASSET_REGISTERED for each one. Never
    assigns an NFC Tag (requirement 9). Returns the same per-row results
    as preview_import(), with each OK row additionally marked "imported".
    """
    results = preview_import(conn, csv_text)
    for result in results:
        if result["status"] != STATUS_OK:
            continue
        data = result["data"]
        try:
            register_asset(
                conn,
                asset_id=data["asset_id"],
                equipment_type=data["equipment_type"],
                actor=actor,
                serial_number=data["serial_number"],
                description=data["description"],
                manufacturer=data["manufacturer"],
                model=data["model"],
                wll=data["wll"],
                company=data["company"],
                periodic_inspection_status=data["periodic_inspection_status"] or "VALID",
                periodic_inspection_due=data["periodic_inspection_due"],
                notes=data["notes"],
            )
            result["imported"] = True
        except AssetAlreadyRegisteredError:
            # Defence in depth against a race with another import/registration
            # between preview and commit — treat exactly like a pre-existing
            # duplicate rather than raising.
            result["status"] = STATUS_DUPLICATE_EXISTING
            result["errors"].append(f"asset_id '{data['asset_id']}' was registered by someone else just now.")
            result["imported"] = False
    return results
