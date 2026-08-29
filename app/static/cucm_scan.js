// Per-row "Update" buttons on the CUCM Phone Scan page, mirroring Network
// Check's own apply-update flow (see network_check.js) but against
// /cucm-scan/apply and only for the two fields that page's own model/serial
// mismatch badges can offer (see CUCM_UPDATABLE_FIELDS in
// app/routes/cucm_scan.py - User is deliberately not offered here).
// readJson() (used below) lives in api_utils.js, loaded by cucm_scan.html
// before this file.
const resultsBody = document.getElementById("cucm-results-body");
const bulkActions = document.getElementById("cucm-bulk-actions");
const updateAllBtn = document.getElementById("cucm-update-all-btn");
const bulkStatus = document.getElementById("cucm-bulk-status");

if (resultsBody) {
  function collectUpdateButtons() {
    return Array.from(resultsBody.querySelectorAll(".update-btn"));
  }

  function refreshBulkVisibility() {
    if (!bulkActions) return;
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

  refreshBulkVisibility();
}
