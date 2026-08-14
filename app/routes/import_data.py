import datetime as dt
import glob
import hashlib
import json
import os
import uuid

from flask import Blueprint, flash, redirect, render_template, request, send_file, url_for

from ..auth import require_permission
from ..db import get_connection, get_setting
from ..diffing import diff_batch
from ..exports import build_diff_workbook, build_duplicates_workbook, build_import_template_workbook, build_workbook, send_workbook
from ..importer import (
    BRANCH_FILE_REQUIRED_COLUMNS,
    HEADER_ALIASES,
    USER_FILE_OPTIONAL_COLUMNS,
    USER_FILE_REQUIRED_COLUMNS,
    CleaningReport,
    import_asset_report,
    import_branch_file,
    import_id_file,
    import_total_asset_history,
    import_user_file,
    peek_asset_report_branch,
)
from ..paths import get_diff_reports_dir, get_uploads_dir
from ..queries import (
    find_duplicate_serials_in_batch,
    find_existing_batch_for_branch_period,
    find_unrecognized_in_batch,
    get_branch,
)

bp = Blueprint("import_data", __name__, url_prefix="/import")


def _save_upload(file_storage) -> str:
    ext = os.path.splitext(file_storage.filename)[1] or ".xlsx"
    dest = os.path.join(get_uploads_dir(), f"{uuid.uuid4().hex}{ext}")
    file_storage.save(dest)
    return dest


@bp.route("/")
def index():
    return render_template(
        "import_data.html",
        active_page="import",
        asset_reports_folder=get_setting("asset_reports_folder", ""),
        id_files_folder=get_setting("id_files_folder", ""),
        current_month=dt.date.today().strftime("%Y-%m"),
    )


# --- Download templates -----------------------------------------------------
# Headers always come from importer.py's own HEADER_ALIASES/required-column
# constants - never a second hand-typed copy - so a template can't silently
# drift from what the importer actually accepts.

# Keyed by field name (not a positional list) so re-ordering HEADER_ALIASES
# can never silently misalign the example row against its headers.
EXAMPLE_ASSET_REPORT_ROW = {
    "branch_dept": "HANOI BRANCH", "device_name": "PC", "user_id": "14501864",
    "full_name": "NGUYEN VAN A", "model_device": "DELL OPTIPLEX 7090", "serial_tag": "SN123456",
    "status": "IN USE", "remark": "", "position": "TELLER",
    "handover_date": "2024-01-15", "ip": "10.10.1.23",
}
EXAMPLE_BRANCH_ROW = ["001", "CHI NHANH HA NOI", "SHINHAN BANK VIETNAM(E) HANOI BRANCH"]
EXAMPLE_USER_ROW = ["001", "14501864", "NGUYEN VAN A", "NGUYEN VAN A", "K001", "ACTIVE"]


@bp.route("/templates/asset-report")
def template_asset_report():
    headers = [aliases[0] for aliases in HEADER_ALIASES.values()]
    example = [EXAMPLE_ASSET_REPORT_ROW[field] for field in HEADER_ALIASES]
    wb = build_import_template_workbook(headers, example)
    return send_workbook(wb, "asset_report_template.xlsx")


@bp.route("/templates/branch-codes")
def template_branch_codes():
    wb = build_import_template_workbook(BRANCH_FILE_REQUIRED_COLUMNS, EXAMPLE_BRANCH_ROW)
    return send_workbook(wb, "branch_codes_template.xlsx")


@bp.route("/templates/user-ids")
def template_user_ids():
    headers = USER_FILE_REQUIRED_COLUMNS + USER_FILE_OPTIONAL_COLUMNS
    wb = build_import_template_workbook(headers, EXAMPLE_USER_ROW)
    return send_workbook(wb, "user_ids_template.xlsx")


IMPORT_LOG_SQL = "SELECT * FROM import_log ORDER BY id DESC LIMIT 500"

IMPORT_HISTORY_EXPORT_COLUMNS = [
    ("Imported At", "imported_at"),
    ("Kind", "kind"),
    ("Source File", "source_file"),
    ("Period", "period"),
    ("Rows Processed", "rows_processed"),
    ("Result", "result"),
]


@bp.route("/history")
def history():
    conn = get_connection()
    try:
        rows = conn.execute(IMPORT_LOG_SQL).fetchall()
    finally:
        conn.close()
    return render_template("import_history.html", active_page="import", rows=rows)


@bp.route("/history/export")
def export_history():
    conn = get_connection()
    try:
        rows = conn.execute(IMPORT_LOG_SQL).fetchall()
    finally:
        conn.close()
    wb = build_workbook("Import History", IMPORT_HISTORY_EXPORT_COLUMNS, rows)
    return send_workbook(wb, "import_history.xlsx")


@bp.route("/diff-reports")
def diff_reports():
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM diff_reports ORDER BY id DESC LIMIT 500").fetchall()
    finally:
        conn.close()
    return render_template("diff_reports.html", active_page="import", rows=rows)


@bp.route("/diff-reports/<int:report_id>/download")
def download_diff_report(report_id):
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM diff_reports WHERE id = ?", (report_id,)).fetchone()
    finally:
        conn.close()
    if not row or not os.path.exists(row["file_path"]):
        flash("That saved report file could not be found - it may have been moved or deleted on disk.", "error")
        return redirect(url_for("import_data.diff_reports"))
    return send_file(
        row["file_path"],
        as_attachment=True,
        download_name=row["label"] or "asset_diff_report.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@bp.route("/duplicates/export")
def export_duplicates():
    """Exports the duplicate-serial rows flagged on the Cleaning Report for
    one or more of this import's batches - not the broader system-wide
    check (see asset_edit.export_duplicates for that)."""
    batch_ids = [int(x) for x in request.args.get("batch_ids", "").split(",") if x.strip().isdigit()]
    if not batch_ids:
        flash("Nothing to export.", "error")
        return redirect(url_for("import_data.index"))

    conn = get_connection()
    try:
        dupes = []
        for batch_id in batch_ids:
            dupes.extend(find_duplicate_serials_in_batch(conn, batch_id))
    finally:
        conn.close()

    if not dupes:
        flash("No duplicate serials found in this import.", "error")
        return redirect(url_for("import_data.index"))

    return send_workbook(build_duplicates_workbook(dupes), "duplicate_assets.xlsx")


def _upload_id_files(importer_fn, file_type: str):
    """Shared by the Branch Codes and User IDs uploads below - same
    save/import/cleanup shape, differing only in which importer function
    runs and the file_type tag attached to each result."""
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        flash("No files selected.", "error")
        return redirect(url_for("import_data.index"))

    results = []
    for f in files:
        path = _save_upload(f)
        try:
            result = importer_fn(path)
            result.setdefault("file_type", file_type)
        finally:
            os.remove(path)
        results.append((f.filename, result))

    return render_template("import_result_id.html", active_page="import", results=results)


@bp.route("/branches", methods=["POST"])
@require_permission("import_data")
def upload_branch_file():
    """Branch master list only (Branch No, Local/Eng name...) - kept separate
    from user-ID import so uploading the wrong file gives an immediate, clear
    error instead of relying on auto-detection."""
    return _upload_id_files(import_branch_file, "branches")


@bp.route("/users", methods=["POST"])
@require_permission("import_data")
def upload_user_file():
    """User/banker list only (Branch ID, User No, User Name...)."""
    return _upload_id_files(import_user_file, "users")


def _run_diffs(reports):
    """Compare every successfully-imported report's batch against that
    branch's previous import - shown right on the Cleaning Report so nobody
    has to import the same file a second time through a separate page just
    to see what changed."""
    conn = get_connection()
    try:
        all_diffs = []
        batch_ids = []
        for report in reports:
            if report.error or not report.batch_id:
                continue
            batch_ids.append(report.batch_id)
            all_diffs.extend(diff_batch(conn, report.batch_id, fallback_branch_no=report.branch_no))
    finally:
        conn.close()
    return all_diffs, batch_ids


def _result_redirect(batch_ids: list[int]):
    if not batch_ids:
        flash("No files were imported successfully.", "error")
        return redirect(url_for("import_data.index"))
    return redirect(url_for("import_data.result", batch_ids=",".join(str(b) for b in batch_ids)))


def _save_diff_report(batch_ids: list[int], period: str) -> None:
    """Archive a copy of the asset_diff_report.xlsx to disk right after an
    import produces one, so it can be re-downloaded later without having to
    remember/reconstruct the batch_ids that produced it (the on-demand
    /update/export link on the Cleaning Report still works too - this is
    just a permanent second copy)."""
    if not batch_ids:
        return
    conn = get_connection()
    try:
        all_diffs = []
        for batch_id in batch_ids:
            all_diffs.extend(diff_batch(conn, batch_id))
        if not all_diffs:
            return
        wb = build_diff_workbook(all_diffs)
        timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_period = (period or "unknown-period").replace("/", "-")
        filename = f"asset_diff_report_{safe_period}_{timestamp}.xlsx"
        file_path = os.path.join(get_diff_reports_dir(), filename)
        wb.save(file_path)
        branch_labels = ", ".join(sorted({d.branch_label for d in all_diffs if d.branch_label}))
        conn.execute(
            "INSERT INTO diff_reports (created_at, period, batch_ids, branch_labels, file_path, label) "
            "VALUES (datetime('now'), ?, ?, ?, ?, ?)",
            (period, ",".join(str(b) for b in batch_ids), branch_labels, file_path, filename),
        )
        conn.commit()
    finally:
        conn.close()


def _hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def _group_by_content(file_entries: list[dict]) -> tuple[list[list[dict]], list[dict]]:
    """Group a batch of files (each {'filename', 'path'}) by exact file
    content - catches the case of the same report saved under two different
    names for the same month (a renamed copy, a duplicate download, etc.)
    before it gets imported twice as if it were two branches' worth of new
    data. Returns (duplicate_groups, unique_files)."""
    for item in file_entries:
        item["hash"] = _hash_file(item["path"])
    groups: dict[str, list[dict]] = {}
    for item in file_entries:
        groups.setdefault(item["hash"], []).append(item)
    duplicate_groups = [g for g in groups.values() if len(g) > 1]
    unique_files = [g[0] for g in groups.values() if len(g) == 1]
    return duplicate_groups, unique_files


def _check_period_conflicts(file_entries: list[dict], period: str) -> list[dict]:
    """Which of these files' branches already have an import for this
    period - e.g. the Reporting Month field got left at its pre-filled
    default (today) instead of being changed to match the report, or the
    same file is genuinely being re-imported. Doesn't block anything by
    itself; routes/import_data.py's callers show this as a warning the user
    can proceed past."""
    resolved_period = (period or "").strip() or dt.date.today().strftime("%Y-%m")
    conflicts = []
    conn = get_connection()
    try:
        for item in file_entries:
            peek = peek_asset_report_branch(conn, item["path"])
            if not peek["branch_no"]:
                continue
            existing = find_existing_batch_for_branch_period(conn, peek["branch_no"], resolved_period)
            if existing:
                conflicts.append(
                    {
                        "filename": item["filename"],
                        "branch_matched": peek["branch_matched"],
                        "period": resolved_period,
                        "existing_imported_at": existing["imported_at"],
                        "existing_label": existing["label"],
                        "existing_row_count": existing["row_count"],
                    }
                )
    finally:
        conn.close()
    return conflicts


def _run_asset_report_imports(files: list[dict], period: str, source: str) -> list[int]:
    """Actually imports each file - shared by every asset-report entry
    point (direct upload, after resolving duplicate-content files, after
    confirming past a period-conflict warning) once any checks have passed
    or been explicitly bypassed. Cleans up temp upload copies afterward;
    files sourced from a configured folder are the user's own and are
    never touched."""
    batch_ids = []
    for item in files:
        path, filename = item["path"], item["filename"]
        if not os.path.exists(path):
            flash(f"{filename}: file no longer available, skipped.", "error")
            continue
        report = import_asset_report(path, source_label=filename, period=period)
        if report.error:
            flash(f"{report.source_file}: {report.error}", "error")
        elif report.batch_id:
            batch_ids.append(report.batch_id)

    if source == "upload":
        for item in files:
            if os.path.exists(item["path"]):
                try:
                    os.remove(item["path"])
                except OSError:
                    pass

    _save_diff_report(batch_ids, period)
    return batch_ids


@bp.route("/asset-reports", methods=["POST"])
@require_permission("import_data")
def upload_asset_reports():
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        flash("No files selected.", "error")
        return redirect(url_for("import_data.index"))

    period = request.form.get("period", "").strip()
    saved = [{"filename": f.filename, "path": _save_upload(f)} for f in files]

    duplicate_groups, unique_files = _group_by_content(saved)
    if duplicate_groups:
        return render_template(
            "import_duplicate_files.html",
            active_page="import",
            period=period,
            source="upload",
            duplicate_groups=duplicate_groups,
            unique_files=unique_files,
        )

    conflicts = _check_period_conflicts(saved, period)
    if conflicts:
        return render_template(
            "import_period_warning.html",
            active_page="import",
            conflicts=conflicts,
            files=saved,
            period=period,
            source="upload",
        )

    batch_ids = _run_asset_report_imports(saved, period, "upload")
    return _result_redirect(batch_ids)


@bp.route("/asset-reports/confirm", methods=["POST"])
@require_permission("import_data")
def confirm_asset_reports():
    """Second step after upload_asset_reports (or the folder shortcut)
    flagged duplicate-content files: import only the file chosen per
    duplicate group, plus every file that had no duplicate to begin with."""
    period = request.form.get("period", "").strip()
    source = request.form.get("source", "upload")
    group_count = int(request.form.get("group_count", "0") or "0")

    to_import: list[tuple[str, str]] = []
    to_cleanup: list[str] = []

    for i in range(group_count):
        choice = request.form.get(f"choice_{i}", "")
        if "::" in choice:
            path, filename = choice.split("::", 1)
            to_import.append((path, filename))
        to_cleanup.extend(request.form.getlist(f"group_files_{i}"))

    for value in request.form.getlist("unique_files"):
        if "::" in value:
            path, filename = value.split("::", 1)
            to_import.append((path, filename))
            to_cleanup.append(path)

    chosen_paths = {path for path, _ in to_import}
    if source == "upload":
        # Temp copies from *other* files in each duplicate group - safe to
        # remove now regardless of what happens next. The chosen files
        # themselves are cleaned up later, by _run_asset_report_imports,
        # only once they've actually been imported (which might not be
        # until after a period-conflict warning is confirmed).
        for path in set(to_cleanup) - chosen_paths:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    files = [{"filename": filename, "path": path} for path, filename in to_import]
    conflicts = _check_period_conflicts(files, period)
    if conflicts:
        return render_template(
            "import_period_warning.html",
            active_page="import",
            conflicts=conflicts,
            files=files,
            period=period,
            source=source,
        )

    batch_ids = _run_asset_report_imports(files, period, source)
    return _result_redirect(batch_ids)


@bp.route("/asset-reports/confirm-period", methods=["POST"])
@require_permission("import_data")
def confirm_period_import():
    """User clicked "Import Anyway" on the period-conflict warning (see
    _check_period_conflicts) - just import the same file list, no more
    checks. Also reachable if the warning was skipped over (no conflicts),
    since import_period_warning.html's own "Import Anyway" is the only
    thing that ever posts here."""
    period = request.form.get("period", "").strip()
    source = request.form.get("source", "upload")
    files = []
    for value in request.form.getlist("files"):
        if "::" in value:
            path, filename = value.split("::", 1)
            files.append({"path": path, "filename": filename})

    batch_ids = _run_asset_report_imports(files, period, source)
    return _result_redirect(batch_ids)


@bp.route("/total-asset", methods=["POST"])
@require_permission("import_data")
def upload_total_asset():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("No file selected.", "error")
        return redirect(url_for("import_data.index"))

    path = _save_upload(f)
    try:
        reports = import_total_asset_history(path)
    finally:
        os.remove(path)

    batch_ids = []
    for r in reports:
        if r.error:
            flash(f"{r.source_file}: {r.error}", "error")
        elif r.batch_id:
            batch_ids.append(r.batch_id)

    return _result_redirect(batch_ids)


@bp.route("/from-folder", methods=["POST"])
@require_permission("import_data")
def import_from_folder():
    kind = request.form.get("kind")
    folder = get_setting("asset_reports_folder" if kind == "asset_reports" else "id_files_folder", "")
    if not folder or not os.path.isdir(folder):
        flash(f"Configured folder does not exist: {folder or '(not set)'}. Set it in Settings first.", "error")
        return redirect(url_for("import_data.index"))

    xlsx_files = sorted(glob.glob(os.path.join(folder, "*.xlsx")))
    if not xlsx_files:
        flash(f"No .xlsx files found in {folder}.", "error")
        return redirect(url_for("import_data.index"))

    if kind == "asset_reports":
        period = request.form.get("period", "").strip()
        saved = [{"filename": os.path.basename(path), "path": path} for path in xlsx_files]

        duplicate_groups, unique_files = _group_by_content(saved)
        if duplicate_groups:
            return render_template(
                "import_duplicate_files.html",
                active_page="import",
                period=period,
                source="folder",
                duplicate_groups=duplicate_groups,
                unique_files=unique_files,
            )

        conflicts = _check_period_conflicts(saved, period)
        if conflicts:
            return render_template(
                "import_period_warning.html",
                active_page="import",
                conflicts=conflicts,
                files=saved,
                period=period,
                source="folder",
            )

        batch_ids = _run_asset_report_imports(saved, period, "folder")
        return _result_redirect(batch_ids)

    results = [(os.path.basename(path), import_id_file(path)) for path in xlsx_files]
    return render_template("import_result_id.html", active_page="import", results=results)


@bp.route("/result")
def result():
    """Revisitable version of the Cleaning Report, keyed only by batch_ids
    (recomputed live from the DB) rather than the one-shot in-memory report
    object from the original upload - so editing or deleting one duplicate
    row and coming back still shows every other file from that same import,
    instead of landing back on an empty Import Data page."""
    batch_ids = [int(x) for x in request.args.get("batch_ids", "").split(",") if x.strip().isdigit()]
    if not batch_ids:
        flash("Nothing to show - the import result may have expired.", "error")
        return redirect(url_for("import_data.index"))

    conn = get_connection()
    try:
        reports = []
        for batch_id in batch_ids:
            batch = conn.execute("SELECT * FROM import_batches WHERE id = ?", (batch_id,)).fetchone()
            if not batch:
                continue
            rows = conn.execute("SELECT * FROM asset_items WHERE batch_id = ?", (batch_id,)).fetchall()
            branch_no = next((r["branch_no"] for r in rows if r["branch_no"]), "")
            branch_row = get_branch(conn, branch_no)
            report = CleaningReport(
                source_file=batch["label"] or f"Batch #{batch_id}",
                sheet_name=batch["sheet_name"] or "",
                rows_read=len(rows),
                rows_imported=len(rows),
                branch_hint=batch["branch_hint"] or "",
                branch_matched=branch_row["eng_name"] if branch_row else "",
                branch_no=branch_no,
                batch_id=batch_id,
                unmapped_columns=json.loads(batch["unmapped_columns_json"]) if batch["unmapped_columns_json"] else [],
            )
            report.duplicate_serials = find_duplicate_serials_in_batch(conn, batch_id)
            report.unrecognized_devices = find_unrecognized_in_batch(
                conn, batch_id, "device_name_raw", "device_aliases", "device_standard_names"
            )
            report.unrecognized_statuses = find_unrecognized_in_batch(
                conn, batch_id, "status_raw", "status_aliases", "status_standard_names"
            )
            report.unrecognized_models = find_unrecognized_in_batch(
                conn, batch_id, "model_device_raw", "model_aliases", "model_standard_names"
            )
            reports.append(report)
    finally:
        conn.close()

    diffs, _ = _run_diffs(reports)
    return render_template(
        "import_result_assets.html",
        active_page="import",
        reports=reports,
        diffs=diffs,
        batch_ids=",".join(str(b) for b in batch_ids),
    )
