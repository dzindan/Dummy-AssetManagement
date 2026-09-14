// Per-row "Update" buttons on the CUCM Phone Scan page, mirroring Network
// Check's own apply-update flow (see network_check.js) but against
// /cucm-scan/apply and only for the two fields that page's own model/serial
// mismatch badges can offer (see CUCM_UPDATABLE_FIELDS in
// app/routes/cucm_scan.py - User is deliberately not offered here).
// readJson() (used below) lives in api_utils.js, loaded by cucm_scan.html
// before this file.
//
// This page is a classic full-page form POST (like Network Check's own
// scan used to be, before that page moved to polling JS) - results are
// server-rendered directly into #cucm-results-panel, not fetched via JS.
// That means a plain GET reload (navigating away to Manage Assets and back,
// or reopening the tab) would normally come back to a blank results panel,
// even though nothing new was scanned - so persist the last rendered
// results/form values here and restore them on a plain GET, same "keep
// results until the next scan" behavior as Network Check.
const numEl = document.getElementById("num");
const ipEl = document.getElementById("ip");
const modelEl = document.getElementById("model");
const autosplitEl = document.querySelector('input[name="autosplit"]');
const resultsPanel = document.getElementById("cucm-results-panel");
const scanMethod = document.querySelector("h1[data-scan-method]")?.dataset.scanMethod;

const SCAN_STORAGE_KEY = "cucm_scan_result";

function saveScanState() {
  try {
    sessionStorage.setItem(
      SCAN_STORAGE_KEY,
      JSON.stringify({
        num: numEl.value,
        ip: ipEl.value,
        model: modelEl.value,
        autosplit: autosplitEl.checked,
        resultsHtml: resultsPanel.innerHTML,
      })
    );
  } catch {
    // Storage unavailable (private mode, quota) - nothing to persist, ignore.
  }
}

function restoreScanState() {
  let saved = null;
  try {
    saved = JSON.parse(sessionStorage.getItem(SCAN_STORAGE_KEY) || "null");
  } catch {
    saved = null;
  }
  if (!saved) return;
  if (saved.num !== undefined) numEl.value = saved.num;
  if (saved.ip !== undefined) ipEl.value = saved.ip;
  if (saved.model) modelEl.value = saved.model;
  autosplitEl.checked = !!saved.autosplit;
  if (saved.resultsHtml) resultsPanel.innerHTML = saved.resultsHtml;
}

// (Re-)wires the Update / Update All buttons against whatever results table
// is currently inside #cucm-results-panel - called once on initial load and
// again after restoreScanState() replaces that panel's contents, since the
// old DOM nodes (and any listeners bound directly to them) are gone once
// innerHTML is reassigned.
function wireResultsPanel() {
  const resultsBody = document.getElementById("cucm-results-body");
  const bulkActions = document.getElementById("cucm-bulk-actions");
  const updateAllBtn = document.getElementById("cucm-update-all-btn");
  const ignoreAllBtn = document.getElementById("cucm-ignore-all-btn");
  const bulkStatus = document.getElementById("cucm-bulk-status");
  if (!resultsBody) return;

  // Reset every time this panel is (re)wired - i.e. per page load/restore,
  // same as Network Check's own `ignoredAll`: dismissing mismatches is a
  // "for this view" convenience, not persisted, so a fresh scan or a
  // restored older result both come back with every Update button visible.
  let ignoredAll = false;

  function collectUpdateButtons() {
    return Array.from(resultsBody.querySelectorAll(".update-btn"));
  }

  function refreshBulkVisibility() {
    if (!bulkActions) return;
    if (ignoredAll) {
      bulkActions.style.display = "none";
      return;
    }
    bulkActions.style.display = collectUpdateButtons().length > 0 ? "flex" : "none";
  }

  // Patches the matched row's "Imported" cell + badge in place and drops the
  // button that triggered it, instead of re-running the whole (potentially
  // slow, live) scan just to reflect one write.
  function applyToRow(item) {
    const row = resultsBody.querySelector(`tr[data-ip="${CSS.escape(item.ip || "")}"]`);
    if (!row) return;
    const importedCell = row.querySelector(`.cucm-imported-cell[data-field="${item.field}"]`);
    const matchCell = row.querySelector(`.cucm-match-cell[data-field="${item.field}"]`);
    if (importedCell) importedCell.textContent = item.new_value;
    if (matchCell) matchCell.innerHTML = '<span class="badge badge-added">MATCH</span>';
  }

  async function applyUpdates(updates) {
    if (updates.length === 0) return;
    let resp;
    try {
      resp = await fetch("/cucm-scan/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
        body: JSON.stringify({ updates }),
      });
    } catch {
      if (bulkStatus) bulkStatus.textContent = "Network error applying update.";
      return;
    }
    let data;
    try {
      data = await readJson(resp);
    } catch (err) {
      if (bulkStatus) bulkStatus.textContent = err.message;
      return;
    }
    if (!resp.ok) {
      if (bulkStatus) bulkStatus.textContent = data.error || "Error applying update.";
      return;
    }
    data.applied.forEach(applyToRow);
    if (bulkStatus) bulkStatus.textContent = `Updated ${data.applied.length} value(s).`;
    refreshBulkVisibility();
    saveScanState();
  }

  resultsBody.addEventListener("click", (e) => {
    const btn = e.target.closest(".update-btn");
    if (!btn) return;
    applyUpdates([JSON.parse(btn.dataset.update)]);
  });

  if (updateAllBtn) {
    updateAllBtn.addEventListener("click", () => {
      applyUpdates(collectUpdateButtons().map((btn) => JSON.parse(btn.dataset.update)));
    });
  }

  if (ignoreAllBtn) {
    ignoreAllBtn.addEventListener("click", () => {
      ignoredAll = true;
      collectUpdateButtons().forEach((btn) => btn.remove());
      refreshBulkVisibility();
    });
  }

  refreshBulkVisibility();
}

// A POST (the user just clicked "Scan") always saves its own outcome
// instead of restoring anything - that click IS the "new scan" that's
// allowed to change what's shown. Only a plain GET (no submission this
// load) restores the last saved outcome.
if (scanMethod === "POST") {
  saveScanState();
  wireResultsPanel();
} else {
  restoreScanState();
  wireResultsPanel();
}
