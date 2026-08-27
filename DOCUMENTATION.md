# Asset Management Tool — Documentation

An offline IT asset management and hand-over form tool built for Shinhan Bank
Vietnam's branch equipment tracking. It runs as a small local web app (no
internet connection required), and can optionally be shared by several
computers on the same office network.

---

## 1. What it does

1. **Cleans and normalizes** messy per-branch Excel asset reports (inconsistent
   sheet names, column order, and device-name spellings) into one consistent
   database.
2. **Looks up a user by ID, or an asset by Serial/Service Tag**, and
   generates a printable Hand-Over / Receiving form (`.docx`), pre-filled
   from the asset and user data, with a full review step before anything is
   finalized, and a permanent log of every hand-over.
3. **Detects month-to-month changes** automatically, right on the Import
   Data page, the moment a new monthly report is uploaded (added / removed /
   changed equipment per branch, vs. that branch's previous import),
   exportable to Excel.
4. **Visualizes trends**: the Dashboard's "Assets by Branch (by month)" table
   shows every branch's asset count for each month (Jan-Dec) of a selectable
   reporting year, with every month's actual added/removed vs. the one
   before it (matched by asset identity, not a naive count difference - see
   §4). A separate "Year Comparison" panel shows each year's end-of-year
   total and its change vs. the year before. Click a branch name for its own
   page — the same idea broken down by device type (still a rolling 6-month
   window there),
   alongside the trend chart, current asset list (linking to **Manage
   Assets** for filtering/editing), and Excel export.
5. **Tracks a user's equipment over time**: the **User History** page shows
   every month a person appears in an import, with what was added/removed
   compared to the previous month — e.g. June: A1/A2/A3 → July: B1/B2/B3.
6. Can run on **one computer as a shared server**, with everyone else on the
   same network just opening a browser — no install needed on their end.
7. **Data storage location is configurable** (Settings → Data Storage
   Location) — redirect the database and generated files to a different
   drive or a shared network folder at any time; existing data is moved
   automatically.
8. **Every import is logged** (Import Data → View Import History) — a
   permanent audit trail of every branch-codes/user-IDs/asset-report/Total
   Asset import ever run, with timestamp, source file, row count, and
   success/error.
9. **Catches duplicate-content files before importing them**: if two files
   selected for the same Monthly Asset Reports upload are byte-for-byte
   identical (the same report saved under two names, a duplicate download,
   etc.), a confirmation step lets you pick which one to actually import
   instead of silently importing the same data twice.
10. **Warns before re-importing the same period**: if a file's branch
    already has an import recorded for the Reporting Month you're about to
    use (most often from forgetting to change that field away from today's
    pre-filled default), a warning shows the existing import's date and row
    count before anything happens - **Import Anyway** proceeds (adds a new
    import on top, nothing overwritten), or go back and fix the month. This
    only catches "same branch, same period" - it can't tell whether the
    *content* actually changed, so an intentional corrected re-upload for
    the same month still shows this warning; that's expected.
11. **Saves every diff report permanently**: the `asset_diff_report.xlsx`
    produced by a Monthly Asset Reports import (see #3) is automatically
    archived to disk the moment it's generated — Import Data → **Saved Diff
    Reports** lists every one ever produced, newest first, with a Download
    link, so it can be pulled up again without re-importing or hunting for
    the original Cleaning Report page.
12. **Grouped navigation**: the top menu collapses related pages into
    dropdowns — **Assets** (Manage Assets, User History, Network Check) and
    **Hand-Over** (Lookup & Hand-Over, History) — alongside standalone
    Dashboard, Import Data, and Settings links, so related functionality
    lives in one place instead of a long flat list.
13. **Network Check** (Assets → Network Check): picks a branch, pings every
    IP address recorded against that branch's current assets, and for every
    machine that responds, pulls its PC serial number, monitor serial
    number, MAC address, and currently logged-on user straight off the
    network (no agent installed on the target - `ping`, `quser`, and
    PowerShell `Get-WmiObject` over WMI/RPC) and compares the serials/user
    against what was imported, flagging MATCH / MISMATCH / N/A per field
    (MAC address is shown for reference only - nothing imported records one
    to compare against). Exports to Excel. Each mismatched value gets its own
    **Update** button - clicking it writes the live-scanned value into the
    database immediately (no re-import needed); **Update All Mismatches** /
    **Ignore All** above the table apply that same action in bulk once
    every mismatch on screen has been reviewed. Every actual write is
    recorded to `network_check_log`, viewable from the page's **View Update
    Log** link (when, which asset, old value, new value).
    This only works for machines reachable from wherever the tool is
    running - same LAN or VPN, WMI/RPC not blocked by firewall, and an
    account with admin rights on the target machines; an unreachable branch
    just shows every IP as "No response," which is a network/permissions
    condition, not a data problem. See §4 for how live results are matched
    against imported rows.
    A **Scan hardware** checkbox (checked by default) can be unchecked to
    skip the WMI hardware queries entirely - by far the slowest step per
    host - for a much faster pass when only "is it alive / who's logged in"
    is needed. The hardware-derived columns show "Not scanned" instead of a
    bare "-" when this is off, so that's never confused with a live read
    that came back empty.
14. **Export duplicates to Excel**: wherever duplicate serials are shown -
    the per-import Cleaning Report, and the system-wide Duplicate Check
    page - an **Export to Excel** link downloads exactly those flagged rows
    (Serial, Branch, Device, Model, Full Name, User ID, Status, Asset ID).
15. **Bulk delete**: Manage Assets, Duplicate Check, and the Cleaning
    Report's duplicate section all have row checkboxes, a "select all on
    this page" checkbox, and a **Delete Selected** button - for clearing
    out several duplicate/junk rows in one action instead of one at a time.
16. **Multi-select filters on Manage Assets**: Branch, Device, and Status
    are each a checkbox dropdown instead of a single dropdown - pick several
    branches, several device types, or several statuses at once (OR'd
    together within each field, AND'd across fields), instead of filtering
    one value at a time.
17. **Status Mapping** and **Model Mapping** (Settings): full parity with
    Device Name Mapping - a standard list (Status seeded with WAREHOUSE/
    USING LOCAL/USING INTERNET; Model seeded with ~40 common models grouped
    by manufacturer: Dell, HP, Cisco, Samsung, Synkey, Kodak, Lenovo,
    Logitech), an alias table for known spelling variants, and an
    **Unmapped** pool (drag-and-drop or dropdown+Assign, same UI as
    devices) for anything a cleaning report doesn't recognize. Unlike
    Device Name Mapping, both are normalized **at import time** - the
    Status/Model columns get rewritten to their canonical form, with the
    original text preserved in `status_raw`/`model_device_raw` for
    traceability (same pattern as `device_name_raw`). Manage Assets' Status
    filter and the asset edit form's suggestion dropdown both read from the
    Status standard list; existing rows imported before this feature
    existed were retroactively queued into the Unmapped pools on first
    startup after upgrading (see §4), so nothing already in the database is
    invisible to it.
18. **Login + role-based permissions** (Settings → Manage Users & Roles):
    every user logs in; a one-time `/setup` page creates the first Admin
    account on an empty database. Three built-in roles (Viewer/Editor/
    Admin) are seeded automatically, and an Admin can create additional
    **custom roles** with any combination of permissions. Any account can
    self-service a forgotten password via a **security question** (Settings
    → My Account → pick one of 5 fixed questions and set an answer; "Forgot
    password?" on the login page asks it back) rather than needing another
    Admin to reset it. See §8 for what this login system does and doesn't
    cover.
19. **Usage Duration** column (Manage Assets, Branch Detail, and their
    Excel exports, right next to Handover Date): a computed "how long has
    this been in use" figure - a plain calendar-year count (current year
    minus the Handover Date's own year), not a day-accurate elapsed time.
    Blank or unparseable Handover Date shows as blank rather than a bogus
    "0 years".
20. **Activity Log** (Settings → Activity Log): a general-purpose audit
    trail (`activity_log` table, `db.log_activity()`) for every hand-edit
    that doesn't already have its own dedicated log page - Manage Assets
    edits (field-level old/new value, only for fields that actually
    changed) and deletes, Settings/Mapping changes (branch aliases, device/
    status/model standard names and aliases, data storage location), and
    Users & Roles changes (accounts, roles, permissions). Records who did
    it (`performed_by`, the logged-in account's username) and when.
    Filterable by category, exportable to Excel. Imports keep their own
    Import History (§ import_log, now also recording `imported_by`) and
    Network Check keeps its own log - this doesn't duplicate either. Hand-
    over forms record who generated them in `handover_records.created_by`
    directly rather than through this table, since History already is
    their dedicated log.
21. **CUCM Phone Scan** (Assets → CUCM Phone Scan): queries a Cisco Call
    Manager (CUCM) directly over its AXL/RisPort70 API for currently-
    registered IP phones (by extension mask, IP mask, or model), rather
    than only checking phones already sitting in imported data - finds
    phones CUCM knows about even before they're imported into Manage
    Assets. Each phone's serial number is scraped from its own embedded web
    page (CUCM's API doesn't expose serial numbers). Cross-checks every
    scanned phone against the current Manage Assets state by IP: Serial and
    Model both get a MATCH/MISMATCH/NOT IMPORTED badge - Model comparison
    runs the live model name through the same Model Mapping standard-name
    table used at import time, not an ad-hoc rule. An **Auto-split scan**
    option splits the query across configurable number-mask ranges
    (Settings → CUCM Connection) to work around CUCM's ~1000-device-per-
    request limit. Connection details (CUCM IP, AXL username/password,
    optional local WSDL path, auto-split ranges) live in Settings → CUCM
    Connection - the AXL password is never written in plaintext to the
    Activity Log, only that it changed.

---

## 2. Architecture

- **Backend**: Python + Flask, serving server-rendered HTML (Jinja2
  templates) — no separate frontend build step.
- **Storage**: SQLite (`app.db`), single file. Chosen because this is a
  small-team internal tool; see §8 for its concurrency characteristics.
- **Charts**: hand-rolled inline SVG (`app/charts.py`) — no JS charting
  library, so the app stays fully offline with nothing to bundle or fetch.
- **Hand-over documents**: generated with `docxtpl`, from a template that is
  the bank's actual `HAND OVER FORM - 2025.docx`, edited in place (not
  rebuilt from scratch) so its fonts, logo, and layout are untouched — see
  §6.
- **Packaging**: PyInstaller `--onefile` produces a single
  `AssetManagementTool.exe` that bundles the Python runtime, all templates,
  static assets, and the docx template. No Python installation is required
  on a machine that just runs the `.exe`.

### Project layout

```
app/
  __init__.py        Flask app factory (blueprint registration, version footer)
  version.py           APP_VERSION string, shown in every page footer
  network.py            get_lan_ip() - LAN IP detection for shared-server mode
  paths.py               Where the exe's writable data lives (see §5)
  db.py                   Schema + connection helper (WAL mode, busy timeout)
  importer.py             Excel ingestion: header detection, branch resolution,
                           device-name normalization
  queries.py               "Current state" queries (see §4)
  diffing.py                Per-branch two-batch diff (added/removed/changed)
  analytics.py               Item-count-by-month trend queries
  charts.py                   Inline-SVG line chart renderer
  handover.py                  Hand-over .docx rendering + history logging +
                                 stamping asset_items.handover_date (§6)
  text_utils.py                 Tiny string/date helpers (IP cleaning,
                                 usage_duration_years, ...) shared across
                                 importer/db/routes
  scanner.py                     Network Check's ping/quser/WMI probes
  cucm.py                         CUCM AXL/RisPort70 client for CUCM Phone
                                   Scan (§1 #21) - connection config lives in
                                   the settings table, not a file
  routes/                       One module per page (dashboard, import_data,
                                 lookup, history, settings, branch_detail,
                                 asset_edit, user_history, network_check,
                                 cucm_scan, user_admin) - asset_edit.py
                                 serves both the Manage Assets list/filter
                                 page and the single-asset edit form;
                                 update_compare.py is now just the Excel
                                 diff-export endpoint (see §4 - the upload +
                                 diff flow itself lives on Import Data)
  templates/                     Jinja2 HTML - all UI text in English
  static/                          style.css, app.js (auto-uppercase), logo
templates_docx/
  handover_template.docx           Generated by scripts/build_handover_template.py
scripts/
  build_handover_template.py        Regenerates the docx template (see §6)
main.py                              Production entrypoint (binds 0.0.0.0)
main_devserver.py                     Dev-only entrypoint (localhost, fixed port)
build.spec                            PyInstaller spec
```

---

## 3. Source data expected

- `Asset reports/*.xlsx` — one branch/month per file. Sheet names and column
  layouts vary between files; the importer auto-detects the equipment sheet
  by scanning the first ~10 rows of every sheet for a header row containing
  at least `DEVICE NAME` + a serial-number column (tolerating aliases like
  `SERIAL/SERVICE TAG`, `NOTE` vs `REMARK`, a plain `BRANCH` column vs
  `BRANCH / DEPT`, etc. — see `HEADER_ALIASES` in `importer.py`). If a file
  has no separate "Branch/TO/Center Name:" label row (some files only carry
  the branch name in its own per-row column), the importer falls back to the
  first non-empty value in that column as the file's branch hint, so it
  still resolves instead of landing entirely in the "unresolved" bucket.
- `IDFromAither/*.xlsx` — two files: a branch master list (`Branch No`,
  `Local/Eng Branch Name`) and a user/banker list (`Branch ID`, `User No`,
  `User Name`...). Import Data has a dedicated upload for each
  (**Branch Codes** / **User IDs**), so picking the wrong file for the wrong
  slot fails immediately with a clear "unrecognized layout" error instead of
  silently guessing; the "import all from folder" shortcut still
  auto-detects each file's type, since that path is for a whole folder of
  mixed files at once.
- `Total Asset - *.xlsx` — an aggregate of every branch across several
  months in one sheet (has `Month`/`Years` columns). Used once, as a
  historical baseline, via Import Data → section 3.
- `HAND OVER FORM - *.docx` — the hand-over form template. Only needed once,
  by `scripts/build_handover_template.py`, to produce
  `templates_docx/handover_template.docx`.

None of these original files are ever modified by the app.

---

## 4. Data model & key design decisions

Tables (see `app/db.py` for the full schema):

- `branches`, `users` — loaded from the ID master files.
- `import_batches` — one row per import. Carries `period` ("YYYY-MM"): the
  month the data is *about*, distinct from `imported_at` (when it was
  actually imported). Set explicitly at upload time (defaults to the current
  month); this is what the trend charts group by.
- `asset_items` — every row from every import, ever (nothing is deleted).
  `device_name` is the normalized/canonical name; `device_name_raw` keeps
  what the source file actually said.
- `handover_records` — one row per generated hand-over form, written only
  when the user clicks **Confirm & Download** on the Review page (see §6).
- `settings`, `branch_aliases` — user-editable configuration (Settings page).
- `device_standard_names` — the editable master list of recognized device
  types (seeded with common ones, but fully add/rename/delete-able in
  Settings → Device Name Mapping).
- `device_aliases` — maps a raw spelling variant (e.g. "IPPHONE") to one
  entry in `device_standard_names` (e.g. "IP PHONE").
- `status_standard_names` / `status_aliases` / `status_unmapped` and
  `model_standard_names` / `model_aliases` / `model_unmapped` — full parity
  with the device tables: a standard list, an alias table mapping raw
  spelling variants to a standard name, and an unmapped-value queue with an
  occurrence count. Managed in Settings → Status Mapping / Model Mapping.
  Model is seeded with ~40 common models grouped by manufacturer (Dell, HP,
  Cisco, Samsung, Synkey, Kodak, Lenovo, Logitech) plus a conservative set
  of obvious spacing/typo aliases - real asset report data has ~217
  distinct raw model strings, so most of the long tail starts out in the
  Unmapped pool rather than being guessed at.
- `device_unmapped` — a raw device name seen during import that didn't match
  any standard name or alias, with an occurrence count. Surfaces in the
  Device Name Mapping UI until someone assigns it; disappears automatically
  once it's mapped (or added as its own standard name).
- `branch_unresolved` — the same idea as `device_unmapped`, but for branch
  labels: a free-text branch hint from an import that didn't match any
  branch (even a renamed copy of the same file, or a genuinely new label),
  with an occurrence count. Surfaces in Settings → Branch Name Aliases →
  "Unresolved Branch Labels" until assigned; unlike devices, branches are a
  closed set from the official master list, so assigning only offers
  existing branches, not "create a new one."
- `diff_reports` — one row per auto-saved `asset_diff_report.xlsx` (see §1
  #11): `period`, the `batch_ids` it was built from, the branches it covers,
  and the `file_path` on disk under `exports/diff_reports/`. Written by
  `import_data._save_diff_report` right after a Monthly Asset Reports (or
  Total Asset baseline) import produces at least one diff — not on every
  page view, only once per actual import.

**Query performance / indexes** (`app/db.py`): every foreign-key-ish column
that gets filtered or joined on has an index - `asset_items(batch_id)`,
`asset_items(asset_key)`, `asset_items(user_id_norm)`, `asset_items(branch_no)`,
plus `asset_items(branch_no, batch_id)` specifically for the `CURRENT_ASSETS_CTE`
join below, which is by far the most frequently run query in the app
(Dashboard, Manage Assets, Lookup, Branch Detail, and Duplicate Check all go
through it). `users.user_no_norm` (indexed) exists for the same reason:
`importer.find_user()` used to fall back to pulling and normalizing every
row in Python (a full ~8,000-row scan) whenever a typed User ID didn't
exactly match the stored format (e.g. missing a leading zero) - now that
fallback is an indexed lookup instead. `user_no_norm` is populated at
import time (`import_user_file`) and backfilled once for any database
upgraded from before this column existed (`db._backfill_user_no_norm`,
same one-time-catch-up pattern as the Status/Model Unmapped backfills
above). `normalize_user_id` itself lives in `text_utils.py`, not
`importer.py`, specifically so `db.py` can call it for this backfill
without a circular import.

**"Current state" is computed per branch, not globally** (`app/queries.py`):
for each branch, take every row from that branch's single most-recent
batch **by reporting period** (`import_batches.period`, "YYYY-MM" — the
"Reporting month" field on the import form), not by `batch_id`/import order.
`batch_id` only breaks a tie when the exact same period gets re-imported (a
correction supersedes the earlier upload of that month). This matters
because (a) branches get re-imported on independent schedules — a global
"latest batch" would only reflect whichever branch happened to be imported
last, hiding everyone else — (b) each monthly import is a *full replacement*
of that branch's list, so a device missing from the new file means it's
gone, not still current — and (c) files don't always land in period order
(a June report arriving before a backfilled February one is normal), so
ordering by `batch_id` alone would make an actually-newer period look stale
just because it happened to be imported first. `get_previous_batch_for_branch`
(used for diffing) follows the same period-first rule when picking a
branch's "previous" snapshot to compare against.

**Branch name resolution** (`importer.resolve_branch`): asset report files
label their branch with a free-text string that often doesn't match the
official branch master list verbatim (spacing differences like "HANOI" vs
"HA NOI"). `normalize_branch_text` also expands the **"T.O" abbreviation**
(matched with a word boundary, so it only fires on the literal token "T.O"
or "T.O." - never inside an unrelated word like "AUTO") to its full
"TRANSACTION OFFICE" before any comparison happens, so a label like "South
Saigon T.O" resolves automatically against a branch master eng_name like
"...TRANSACTION OFFICE" without a manual alias - the space-insensitive
matching below absorbs remaining spelling differences ("Saigon" vs "Sai
Gon") on top of that. Every branch-master candidate is then scored — exact
match, "looks like a physical BRANCH/TRANSACTION OFFICE" (preferred over a
head-office department containing the same city word), and candidate length
— in a single space-normalized comparison pass. A single-branch report file
resolves its branch **once per file** from the sheet's own label and stamps
every row with it, rather than trusting each row's often-terse per-row
branch text. Genuinely novel abbreviations/nicknames still need a manual
alias in Settings, same as before.

**Device/status/model normalization** (`importer._normalize_via_alias_table`,
used by `normalize_device_name`/`normalize_status`/`normalize_model_device`):
one shared implementation, since all three fields get the exact same
treatment - look up the alias table (seeded with common variants); anything
that doesn't match an alias or a standard name passes through uppercased
as-is (so no data is lost) and is recorded in the matching `*_unmapped`
table, flagged in that import's Cleaning Report with a direct link to the
mapping UI. Unlike device names, Status and Model also keep the original
text in `status_raw`/`model_device_raw` before normalization overwrites the
column - added when this system was extended past devices, so a database
created before that has these columns backfilled empty for old rows (see
`db._add_missing_columns`) and every existing distinct Status/Model value
gets a one-time pass into the Unmapped pool on first startup after
upgrading (`db._backfill_unmapped`), since those rows never had a chance to
be flagged at their own import time.

The **Cleaning Report's unrecognized-device/status/model badges are
re-derived live** from `asset_items` (`queries.find_unrecognized_in_batch`)
whenever the report is viewed, the same way `find_duplicate_serials_in_batch`
already worked - both exist because the Cleaning Report became revisitable
by `batch_ids` (see the multi-file-import fix), so nothing about it can
depend on the one-shot in-memory report object from the original POST.

**Renaming a standard name into an existing one merges instead of crashing**
(`settings._rename_or_merge_standard`, shared by the Device/Status/Model
"Rename" buttons): `name` is a `PRIMARY KEY` on all three standard-name
tables, so a plain `UPDATE ... SET name = ?` throws a `UNIQUE constraint
failed` (surfaced to the user as an unhandled 500) the moment someone
renames an entry to a name that already exists as its own standard entry -
e.g. renaming "TV" into an existing "TABLET" entry. Since that's a
perfectly reasonable thing to want (combining two standard entries that
turned out to mean the same thing), the rename routes check for this first
and merge instead: every alias that pointed at the old name gets moved
over to the target name, the old name itself is kept on as an alias of the
target (so a raw value that still says the old name in some future import
keeps resolving correctly), and the now-redundant old standard entry is
removed - only falling back to a plain rename when the target name doesn't
already exist.

**User-name normalization** (`importer._ingest_asset_rows`): the same idea,
but for people. An asset report's free-text "FULL NAME" column is whatever
the branch typed - spacing, punctuation, and even wrong names entirely
(copy-paste mistakes between rows) all happen in practice - while the
IDFromAither user list (`users.user_name`) is the authoritative record for
that person, keyed by User No. Every row's `full_name` is standardized to
the Aither spelling whenever its `user_id_norm` resolves to a known user;
the report's own text is preserved separately as `full_name_raw` for
traceability (never shown in the UI, same as `device_name_raw`). Unmatched
IDs (shared equipment, contractors not yet in Aither, etc.) keep the
report's own name, just uppercased for consistency. This has caught real
data entry errors in practice (e.g. a report listing the wrong person
entirely for a given User No, most likely from a copy-pasted row).

**Network Check comparison** (`app/scanner.py` + `routes/network_check.py`):
`scanner.py` is kept in sync with a standalone internal IP Scanner tool this
same author maintains separately - `ping`/`ping -a`/`quser`/PowerShell
`Get-WmiObject` run as subprocesses per target IP, no extra dependency
beyond the stdlib and openpyxl (already used elsewhere); when that tool
gains a new capability (e.g. MAC address lookup via
`Win32_NetworkAdapterConfiguration`), the same function is ported over here
so both tools stay capable of the same live queries. Target-range parsing
(CIDR/dash ranges) and manual concurrency selection from that tool are
deliberately *not* ported - Network Check always derives its target list
from a branch's own imported asset IPs, never free-text input. For a chosen
branch, every current asset row with a non-empty `ip` is grouped by that IP
(a PC and its monitor typically share the same person's IP), scanned
concurrently, then `compare_result()` matches: the live BIOS serial
(`Win32_BIOS.SerialNumber`) against whichever imported row's `device_name`
is PC/NOTEBOOK/SERVER PC; the live `WmiMonitorID` serial(s) against the
imported LCD row; and the live logged-on session username(s) (`DOMAIN\user`
stripped to just `user`) against the imported row's `user_id_norm`. Each
comparison is `None` ("N/A") when there's nothing on one side to compare -
either nothing was imported for that field (e.g. no LCD row for that IP),
*or* the live read itself came back empty (machine offline, or reachable by
`ping` but WMI/RPC blocked by firewall so hardware/session queries fail) -
rather than a false MISMATCH. This distinction matters for the Update
button below: it only ever offers a value that was actually read live.
The live MAC address is shown alongside these as plain information, not a
MATCH/MISMATCH field - nothing in the imported asset data records a MAC
address to compare it against. Scan state lives in an in-memory dict keyed
by a scan ID (same pattern the standalone tool used), not the database -
it's a live, ephemeral network snapshot, not something to persist across
restarts.

**Applying a Network Check mismatch** (`network_check.apply_updates`,
`UPDATABLE_FIELDS`): `compare_result()` also returns which `asset_items`
id(s) each field would need to write to - `pc_asset_ids`/`monitor_asset_ids`
are the single matched PC/LCD row, while `user_asset_ids` is *every* row at
that IP (a PC and its monitor are treated as one desk/person, so correcting
the assigned user corrects it everywhere at that IP, not just one device).
Clicking a mismatch's **Update** button (or **Update All Mismatches**)
POSTs `{ip, field, asset_ids, value}` pairs to `/network-check/scan/<id>/
apply`, which writes `serial_tag` (for pc_serial/monitor_serial) or
`user_id_raw` (for user) directly - mirroring how Manage Assets' own edit
form treats `user_id_raw` as a plain field, not re-deriving `user_id_norm`
or the Aither-canonical `full_name` the way import-time normalization does
(see the user-name normalization note above). Every actual change (skipped
if the value already matches) is logged to `network_check_log` with the
old and new value, and the in-memory scan snapshot is mutated in place so
the same scan's next poll immediately shows MATCH without re-scanning.
**Ignore All** is client-side only - it just stops showing Update buttons
for that scan session; nothing is written or logged.

**Duplicate export and bulk delete** (`asset_edit.build_duplicates_workbook`,
`asset_edit.bulk_delete`): the same workbook builder backs both duplicate
export links - `asset_edit.export_duplicates` (system-wide, from
`find_current_duplicate_serials`) and `import_data.export_duplicates`
(scoped to one import's `batch_ids`, from `find_duplicate_serials_in_batch`)
- the only difference is whether each row carries a joined
`branch_eng_name` or falls back to raw `branch_dept` text. Bulk delete is a
single shared route (`POST /assets/bulk-delete`, an `asset_ids` list) used
from three pages (Manage Assets, Duplicate Check, Cleaning Report); the
front end (`app.js`) builds and submits a throwaway `<form>` from whichever
checkboxes share a `data-bulk-delete-checkbox-class` value, rather than
wrapping the whole table in a `<form>` - the duplicate-check tables already
have their own per-row delete `<form>`s, and HTML doesn't allow nesting
forms. "Select all" only affects checkboxes currently rendered on screen -
Manage Assets is unpaginated (the full filtered result set renders at
once), so this covers every row currently visible under the active
per-column header filters.

**Period-conflict warning** (`importer.peek_asset_report_branch`,
`queries.find_existing_batch_for_branch_period`,
`import_data._check_period_conflicts`): before actually importing, each
file's branch is resolved via a lightweight peek - re-opens and re-parses
just enough of the workbook (header detection + branch hint, no row
ingestion, no batch created) to answer "which branch would this become,"
deliberately duplicating rather than sharing code with the real
`import_asset_report` ingest path so a change made for this check can never
affect what actually gets imported. If that branch already has a batch
stamped with the period about to be used, `import_period_warning.html`
shows the existing import's date/label/row count before anything is
written, with **Import Anyway** (posts to `confirm_period_import`, which
skips the check and imports straight away) or **Cancel**. This check runs
*after* the duplicate-content-file check (a different question - "are two
of these files byte-identical" vs. "does this branch already have this
period") - both can fire in the same upload, one after the other. All
three asset-report entry points (direct upload, the folder shortcut, and
confirming past a duplicate-content prompt) funnel into one shared
`_run_asset_report_imports` for the actual import + cleanup, so the check
only had to be wired in once per entry point rather than duplicating the
import loop three times.

**Month-over-month change tracking** (`analytics._walk_period_changes`,
backing the Dashboard's branch table and the Branch Detail device table):
for a given branch (or device type within a branch), each month's set of
`asset_key`s is diffed against the *previous* month's set it has data for -
added = present now but not before, removed = present before but not now.
This is deliberately **not** a plain count difference: swapping 5 devices
for 5 different ones nets to a count delta of zero but is real asset
movement, and only an identity-based diff (the same approach `diffing.py`
and User History already use) catches that. It's also why summing each
month's *count* into a running "Total" would be wrong - a branch having 400
assets in June and 400 in July is not "800 assets total".

Two different display rules sit on top of that diff, one per consumer:

- **Branch Detail's device-type table** (`analytics._limit_to_recent_months`):
  only the **6 most recent months** are shown as columns, and only the
  **single most recent month** carries an added/removed indicator (comparing
  it to the month right before it) - older visible columns show a plain
  count with no delta, since this table answers "what changed most
  recently" for one branch, not a full historical audit trail (that's what
  User History and the per-import diff on the Import Data page are for).
- **Dashboard's branch table** (`analytics._prepare_year_columns`): shows a
  fixed **Jan-Dec** for a selectable reporting year, and *every* populated
  column carries its own added/removed indicator, diffed against whatever
  period actually precedes it in the data (which can be outside the
  selected year - January's delta compares against December of the year
  before). A `year` query param on both `/` and `/export` picks the year;
  it defaults to the most recent year with data. `get_available_report_years()`
  drives the selector by scanning `import_batches.period` for distinct
  `YYYY` prefixes. A separate **Year Comparison** panel
  (`analytics.get_year_comparison_table`) shows one row per year - that
  year's end-of-year snapshot count (bounded `period <= "YYYY-12"`, same
  per-branch latest-period logic as `CURRENT_ASSETS_CTE` in queries.py, just
  time-bounded) and its change vs. the year before.

**The Cleaning Report is revisitable, not a one-shot response**
(`GET /import/result?batch_ids=1,2,3`, importer's `upload_asset_reports` /
`upload_total_asset` / `import_from_folder` all redirect there after
processing, rather than rendering the report directly): everything on the
page - stats, duplicates, the diff - is recomputed live from `batch_ids`
alone, not read from the in-memory report objects created during the
original upload. That's what lets every **Edit**/**Delete** link on the page
point its `next` back at that same URL: fixing one duplicated row out of a
multi-file import and returning shows every other file's results exactly as
before, instead of landing back on an empty Import Data page (the original
in-memory-only version lost everything else in the batch the moment you
navigated away to edit a single row).

**Diff Excel export** (`update_compare.export`, `asset_diff_report.xlsx`):
the "Details" sheet's columns mirror the source asset report's own column
names/order (Branch/Dept, Device Name, Model, User ID, Full Name, Serial,
Status, Remark, Position, Handover Date, IP), with the resolved branch and
the change columns (Type / Field Changed / Old / New) appended at the end -
so a changed/added/removed row can be located and cross-checked against the
original spreadsheet without guessing which physical asset it refers to,
instead of only showing Device + Serial.

**Duplicate-content file check** (`import_data._group_by_content`, runs
before any parsing/importing happens): every file selected for a Monthly
Asset Reports upload (manual upload or the "import from folder" shortcut)
is hashed (SHA-256 of the raw bytes); if two or more files in the same
submission hash identically, importing is paused and a confirmation page
lists each such group, letting you pick exactly one file per group to
import - the others are skipped, not imported as if they were separate new
data. This is a plain byte-hash comparison, so it only catches genuinely
identical files (e.g. the same report re-saved or re-downloaded under a
different name) - two files with the same *data* but different formatting
or a re-saved timestamp won't match, and will need to be caught by eye like
before.

**Duplicate serial detection**: within a single import, if the same serial
number appears on more than one row, every occurrence is flagged in that
import's Cleaning Report - shown as a full side-by-side table of each
duplicated row's actual data (device, model, full name, user ID, branch/dept,
status), not just the serial and a count, since the two rows often turn out
to genuinely differ (e.g. one assigned to a person and marked in use, the
other unassigned and marked broken) rather than being an exact copy-paste.
Each row has its own **Edit** link straight into `/assets/<id>/edit` to fix
whichever one is wrong, and a **Delete** link (with a confirmation prompt) to
remove it outright when it's simply a bad duplicate - the same Edit link is
also on every asset row on the Branch Detail and Manage Assets pages for
general corrections. Every editable field there is force-uppercased on save
(except Handover Date, which is a date, not free text), matching the
all-caps convention used everywhere else data ends up on a printed form or
report.

**Duplicate Check** (`/assets/duplicates`, linked from Import Data, Manage
Assets, and every Cleaning Report): the same idea as the above, but scoped to
*every current asset across all branches* instead of just the rows from one
just-uploaded file - catches duplicates that arise from two different
branches' files listing the same serial, or drift between separately-timed
monthly imports, which the per-import check can't see. Run it any time, not
just right after an import.

**Manage Assets** (`/assets/`): a single filterable, unpaginated table over
*every currently-assigned asset* across all branches (server-side: branch,
device, status, and a free-text search over serial/model/name/user ID/dept;
client-side: a dropdown filter per column, built from the distinct values
actually present in the rendered rows), with an Edit link per row. This is what clicking a branch name on the Dashboard
now opens (pre-filtered to that branch) — its old destination, the Branch
Detail page (trend chart + Excel export), is still one click further via a
link shown when a single branch is filtered. The Branch filter dropdown only
lists branches that actually have a current asset (rather than every branch
in the master list), plus an **"(Unresolved / unmatched)"** option whenever
some current assets' branch text failed to auto-resolve to a branch_no —
selecting it surfaces exactly those rows so they can be fixed (via a Settings
alias, or by editing the row's Branch/Dept text directly) instead of
disappearing into an unlabeled "Unknown" bucket on the Dashboard with no way
to drill into it.

**Import History** (`/import/history`, linked from Import Data): a permanent
log of every import ever run - branch codes, user IDs, monthly asset
reports, and each Total Asset baseline period - independent of
`import_batches` (which only exists to drive "current state"/diffing for
asset reports). Every importer function logs to this table on both success
and failure, so a bad file (wrong layout, unreadable, etc.) still leaves an
audit trail instead of vanishing.

**User Asset History** (`/user-history/`): unlike every other view in this
app, which only shows *current* state, this one deliberately shows a user's
**entire history** across every import batch they ever appeared in, newest
period first. Each period lists what changed since the previous one they
appeared in (Added / Removed), so a swap like "June: A1, A2, A3 → July: B1,
B2, B3" is visible at a glance without cross-referencing two exports by
hand.

**Device Name Mapping UI** (Settings → Device Name Mapping): the standard
list is fully editable (add/rename/delete). Unmapped names appear as small
cards you can either **drag onto** the standard name they belong to, or
assign with the dropdown-and-button next to each card (identical outcome —
dragging is a shortcut, not a requirement). Each card also has a second,
inline option to type a brand-new standard name and assign it in one step
("Create & Assign") — equivalent to adding it in the Standard Device Names
box above and then mapping it, just without leaving the card. Deleting a
standard name that still has aliases moves those raw names back into the
unmapped pool rather than losing them.

**Branch Name Mapping** (Settings → Branch Name Aliases → "Unresolved
Branch Labels"): the branch equivalent of the above. Every time a branch
label fails to resolve during import - a genuinely new label, or just a
renamed copy of a file whose sheet-level label was never in the master list
to begin with - it's recorded with an occurrence count instead of silently
producing an "Unresolved" bucket on the Dashboard with no way back. Each
entry has a dropdown of existing branches and an **Assign** button (no
drag-and-drop here - the branch list is ~140 entries long, too many for a
bucket grid to make sense) which creates the alias and clears the entry;
future imports using that same label resolve automatically from then on -
and, importantly, `importer.reresolve_unresolved_assets` also retroactively
fixes every already-imported row for that label immediately, rather than
leaving it stuck as "Unresolved" until the next re-import. This checks two
things, since a hint can end up unresolved either way: a row's own
`branch_dept` text (multi-branch imports, e.g. Total Asset baseline, where
each row is resolved independently), and `import_batches.label` (regular
single-branch report files, which stamp *every* row with one file-level
resolution - so a row's own raw text can legitimately differ from the label
that actually drove its resolution, e.g. label "South saigon T.O" but a
row's own `branch_dept` just says "SOUTH SAIGON"). Unlike device names,
there's no "create a new branch" option - branches only ever come from the
official IDFromAither master list, so assigning to one of the existing
branches is the only valid action. A **Dismiss** button is also available
for a one-off typo that's already been fixed at the source and won't recur.

---

## 5. Where data lives

Everything the app writes — the database, generated hand-over `.docx`
files, and temporary upload files — lives in:

```
%LOCALAPPDATA%\AssetManagementTool\
  app.db
  handovers\   (every generated hand-over form)
  uploads\     (temporary, cleaned up after each import)
  exports\
```

This is **independent of where the `.exe` itself is placed**, so it keeps
working even if the exe sits in a read-only folder. It also means: **the
database is per-machine**, unless you're using the shared-server mode in
§7 — running the exe on two different computers gives each its own
separate, empty-until-imported database.

### Changing where data lives

Settings → Data Storage Location lets you redirect all of the above to any
other folder (a drive with more space, a shared network path, etc.):

- Enter a full path and click **Change Location**. The app verifies the
  folder is writable, then moves `app.db` (and its `-wal`/`-shm` sidecar
  files if present), `handovers/`, `exports/`, and `uploads/` there —
  nothing already imported is lost. Existing files already in the *target*
  folder (e.g. switching back to a folder used before) are never
  overwritten.
- The pointer to the current location is itself always kept at the default
  `%LOCALAPPDATA%\AssetManagementTool\data_location.json`, since that's the
  one place guaranteed to exist before any choice is made. The change takes
  effect immediately — no restart needed.
- **Reset to Default** points back at `%LOCALAPPDATA%\AssetManagementTool`
  without moving files back automatically — do that by hand first if you
  want to keep using that data.
- If you point two machines at the same network folder, be aware this still
  relies on SQLite's WAL mode over that filesystem, which is only reliable
  on filesystems with proper file locking — prefer the shared-server model
  in §7 for multi-machine use unless you've verified your network share
  supports it.

---

## 6. The hand-over form, in detail

### Generating a form (Lookup & Hand-Over page)

0. Search by **User ID** (the usual case) or by **Serial / Service Tag** —
   useful when you have an asset in hand but not its assigned user. A serial
   search lists every current asset matching that number with its current
   holder, and a "Build Hand-Over" link to switch straight into the normal
   User ID flow for that person.
1. Enter a User ID → shows the person's name/branch/status and every asset
   currently assigned to them.
2. Pick which assets to include, and for each one, its **Hand-Over
   Condition** (`NEW` or `WAREHOUSE`) — this is independent of the asset's
   day-to-day operational status shown next to it.
3. Set the **Hand-Over Date** (defaults to today, editable), the **Type**
   (`ASSIGNMENT`/`TEMP` = handing equipment *out*; `RETURN` = receiving it
   *back* — labelled accordingly so the direction is unambiguous), the
   **Reason**, the Receiving Party / ICT Representative details (all
   auto-uppercased as you type), and the two **Signature** names (default to
   the Receiving Party / ICT Rep above, but independently editable in case
   the actual signer is someone else).
4. **Review** shows the complete form exactly as it will be generated.
   Nothing is saved yet.
5. **Confirm & Download** is the only action that renders the `.docx` and
   writes a row to the hand-over history — clicking Review, or navigating
   away, never logs anything. It also stamps every included asset's
   `handover_date` in Manage Assets with the form's own Hand-Over Date
   (`handover.apply_handover_date()`) — previously only the history log knew
   this date; Manage Assets kept whatever an import last carried, often
   blank. Logged to Activity Log like a manual edit, only when the value
   actually changes (re-generating a form with the same date doesn't spam
   the log).

### The template itself

`scripts/build_handover_template.py` starts from the real
`HAND OVER FORM - 2025.docx` and edits it **in place**, run by run —
replacing only the specific text runs that hold variable values with Jinja
placeholders (`{{ receiving_name }}`, etc.), and using a docxtpl row-loop
(`{%tr for item in assets %}` / `{%tr endfor %}`) for the repeating
equipment table. It never rebuilds the document from scratch, so the
original fonts, the Shinhan logo embedded in the page header, margins, and
column widths all survive untouched.

One final pass forces every run in the document to **Times New Roman**,
except the checkbox-glyph runs (`□`/`☑`/`☐`), which must stay **MS Gothic**
or the glyphs won't render — the source document's own default theme font
(Calibri) otherwise leaks through in a few places (the equipment table body,
some paragraphs) that don't set `rFonts` explicitly.

To regenerate the template (e.g. if the bank issues a new form design), edit
`scripts/build_handover_template.py` to match the new file's structure and
run `python scripts/build_handover_template.py`.

---

## 7. Running it

### Single computer

Just run `AssetManagementTool.exe` (or `python main.py` from source). It
prints a `127.0.0.1` URL and opens it in your default browser automatically.

### Shared across several computers on the same network

Pick **one** computer to act as the server (any of them; it just needs to
stay on and keep the app open while others are using it):

1. Run the exe there as normal. Its console window prints **two** addresses:
   a `127.0.0.1` one (for that computer) and a LAN one, e.g.
   `http://192.168.1.50:8737` (for everyone else). The LAN address is also
   shown any time on the Settings page under "Share With Other Computers".
2. If Windows Firewall prompts to allow the app, allow it on **Private
   networks** — otherwise other computers can't reach it.
3. On every other computer: just open that LAN address in any browser. No
   install needed.

All data (branches, assets, hand-over history) is now genuinely shared —
everyone is reading and writing the same database on the server machine.
The server computer must stay powered on and keep the app running for
others to use it; closing that console window stops it for everyone.

This uses SQLite in WAL mode with a 30-second lock-wait timeout
(`app/db.py`), which comfortably handles a handful of people browsing/
looking things up while occasionally one of them runs an import — see §8
for the caveat. If the data folder (Settings → Data Storage Location) is a
network path (UNC or a mapped drive), `get_connection()` detects it
(`paths.is_network_path`) and falls back to the plain rollback-journal mode
instead of WAL, since WAL's shared-memory locking isn't reliable over
SMB/NFS - same lock-wait behavior, just without WAL's extra reader/writer
concurrency.

---

## 8. Known limitations

- **Login + role-based permissions.** The app requires a login (`app/auth.py`,
  `app/routes/auth.py`) - first launch on an empty database walks through a
  one-time `/setup` page to create the initial Admin account. Every account
  has exactly one role; three built-ins are seeded automatically (Viewer:
  browse only, Editor: day-to-day import/edit/hand-over/network-check/
  mapping work, Admin: everything including Settings > Users & Roles),
  and an Admin can define additional **custom roles** with any combination
  of permissions from there. Read access itself isn't gated - anyone logged
  in can view any page; permissions control *write* actions (import, edit/
  delete assets, generate hand-overs, run Network Check, manage the device/
  status/model/branch mapping tables, change settings, manage other
  accounts). Accepted limitations of this login system, not solved by
  design:
  - **No TLS.** The bundled Flask server is a development server with no
    HTTPS - credentials and session cookies travel in cleartext on the LAN,
    same trust model as every other request this app already makes. Only
    use it on a trusted local network; never expose the LAN URL to the
    internet.
  - **No brute-force/lockout protection** on the login form - acceptable
    for a small internal team on a private LAN, not suitable if this ever
    became internet-facing.
  - **No CSRF tokens**, consistent with the rest of the app (none exists
    anywhere else in it either).
  - **Password reset without an existing session** works two ways: another
    Admin can reset anyone's password from Settings → Users & Roles (needs
    `manage_users`), or the account holder can self-service it with a
    **security question** (`app/routes/auth.py`'s `/forgot-password`,
    `app/routes/settings.py`'s `set_security_question_route`) - one of 5
    fixed, deliberately lighthearted questions (`auth.SECURITY_QUESTIONS`),
    chosen and answered from Settings → My Account; only the answer's hash
    is stored, like a password. Unlike the one-time recovery key this
    replaced, the question/answer is reusable and isn't cleared after a
    successful reset. An account with neither an active Admin nor a saved
    security question has no self-service recovery path short of editing
    `accounts.password_hash` directly in `app.db`.
- **SQLite concurrency.** Reads are unaffected by a concurrent writer (WAL
  mode), but two people importing large files (like the Total Asset
  baseline) at the exact same moment could see a "database is locked" error
  if one write takes longer than the 30s timeout. In practice, avoid running
  two large imports simultaneously.
- **Per-machine database unless shared.** Running the exe on two computers
  independently (not in the shared-server setup of §7) gives each its own
  separate database — they do not sync with each other.
- **Branch/device name matching is heuristic.** New branch or device-name
  spellings not seen before may need a manual alias added in Settings; the
  Cleaning Report flags anything that didn't auto-resolve.

## 9. Troubleshooting a rebuild that "didn't take"

Two footguns to know about if you ever rebuild the exe yourself:

1. **PyInstaller caches `build/` across runs.** Deleting only
   `dist/AssetManagementTool.exe` and rebuilding can silently reuse a stale
   cached analysis, producing a "new" exe that still runs old code. Always
   delete both `build/` and `dist/` (and any `__pycache__` folders) before
   rebuilding:
   ```
   rm -rf build dist
   find . -iname "__pycache__" -exec rm -rf {} +
   pyinstaller build.spec --noconfirm --clean
   ```
2. **A running process is unaffected by rebuilding the exe on disk.** If a
   copy of the app is already running (its console window still open),
   rebuilding `AssetManagementTool.exe` does nothing for that already-running
   process — it keeps whatever code it loaded at startup. Always fully close
   every running instance (check Task Manager for `AssetManagementTool.exe`)
   before launching the freshly-built one.

Every page's footer shows a `Build <version>` string (`app/version.py`) —
bump it when you make a change, so you can visually confirm which build is
actually running after a rebuild.
