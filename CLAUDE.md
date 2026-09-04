# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read DOCUMENTATION.md first

`DOCUMENTATION.md` in this repo is an exhaustive, actively-maintained design
doc — every feature, data-model decision, and non-obvious behavior is
explained there in depth (normalization rules, "current state" computation,
Network Check matching, hand-over form generation, etc.). Read the relevant
section there before touching related code; don't re-derive behavior from
scratch by reading routes/queries alone.

## Commands

- Dev server: `python main_devserver.py` (localhost, fixed port 8737)
- Prod entrypoint: `python main.py` (binds 0.0.0.0, for LAN-shared mode)
- All tests: `python -m unittest discover -s tests -p "test_*.py"`
- Single test file: `python tests/test_auth.py`
- Single test case: `python -m unittest tests.test_auth.<ClassName>.<test_method>`
- Rebuild the Windows exe (always delete build/dist first — PyInstaller
  caches stale analysis otherwise, see DOCUMENTATION.md §9):
  ```
  rm -rf build dist
  find . -iname "__pycache__" -exec rm -rf {} +
  pyinstaller build.spec --noconfirm --clean
  ```
  Bump `APP_VERSION` in `app/version.py` first, and fully close any already-
  running `AssetManagementTool.exe` before launching the fresh build — a
  running process keeps whatever code it loaded at startup.
- Regenerate the hand-over docx template after the bank issues a new form:
  `python scripts/build_handover_template.py`

No linter/formatter is configured in this project.

## Architecture essentials

- Flask + server-rendered Jinja2, single-file SQLite (`app.db`, WAL mode),
  PyInstaller `--onefile` packaging. No JS framework/build step — trend
  charts are hand-rolled SVG (`app/charts.py` shapes data server-side,
  `app/static/trend_chart.js` draws + wires interaction client-side).
- One concern per module in `app/` (importer, queries, diffing, analytics,
  handover, scanner, cucm, text_utils...), one route module per page under
  `app/routes/`.
- **"Current state" is always per-branch, by latest reporting `period`**
  (not import order/`batch_id`) — `queries.CURRENT_ASSETS_CTE` is the single
  most-reused query in the app (Dashboard, Manage Assets, Lookup, Branch
  Detail, Duplicate Check all join through it). Get this wrong and every
  page that shows "current" assets is wrong.
- Device/Status/Model normalization all share one implementation
  (`importer._normalize_via_alias_table`): unrecognized raw values pass
  through uppercased (never dropped) and land in a `*_unmapped` queue
  surfaced in Settings for manual mapping.
- App data (db, exports, handovers, uploads) always lives under
  `%LOCALAPPDATA%\AssetManagementTool\` or a user-redirected location via
  Settings (`app/paths.py`) — never next to the exe/source, and never
  assume a fixed path when writing new code that touches storage.
- `app/scanner.py` (Network Check) is deliberately kept in sync with the
  sibling `IP Scanner` project (alias **IS**) this same author maintains —
  when IS gains a live-query capability, port the same function here too.

## Data files are gitignored on purpose

`Asset reports/`, `IDFromAither/`, `Total Asset*.xlsx`, `HAND OVER FORM*.docx`
are real Shinhan Bank branch/company data living in this OneDrive-synced
folder — never remove their `.gitignore` lines, never commit a file matching
them.

## Testing pattern

Tests override `%LOCALAPPDATA%` to a fresh temp dir before calling
`create_app()`, so each test gets an isolated throwaway SQLite DB, then
drive the app through `app.test_client()` like a real browser (see
`tests/test_auth.py`).
