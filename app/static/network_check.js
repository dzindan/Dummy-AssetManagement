const form = document.getElementById("scan-form");
const branchSelect = document.getElementById("branch_no");
const includeHardwareCheckbox = document.getElementById("include_hardware");
const scanBtn = document.getElementById("scan-btn");
const stopBtn = document.getElementById("stop-btn");
const progressEl = document.getElementById("progress");
const errorEl = document.getElementById("error");
const exportLink = document.getElementById("export-link");
const table = document.getElementById("results-table");
const tbody = document.getElementById("results-body");
const emptyHint = document.getElementById("empty-hint");
const bulkActions = document.getElementById("bulk-actions");
const updateAllBtn = document.getElementById("update-all-btn");
const ignoreAllBtn = document.getElementById("ignore-all-btn");
const bulkStatus = document.getElementById("bulk-status");

let pollTimer = null;
let currentScanId = null;
let lastResults = [];
let ignoredAll = false;
let scanIncludesHardware = true;

// Persisted so results survive navigating away and back (nav links are real
// page loads, not SPA routes) or reopening the tab - cleared only when a new
// scan actually starts, per "keep results until the next scan" requirement.
const SCAN_STORAGE_KEY = "networkCheckScan";

function saveScanState(scanId, branchNo, includeHardware) {
  try {
    sessionStorage.setItem(SCAN_STORAGE_KEY, JSON.stringify({ scanId, branchNo, includeHardware }));
  } catch {
    // Storage unavailable (private mode, quota) - restoring on reload just won't work.
  }
}

function clearScanState() {
  try {
    sessionStorage.removeItem(SCAN_STORAGE_KEY);
  } catch {
    // See saveScanState.
  }
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

// readJson() (every fetch() below reads its response through it) now lives
// in api_utils.js, loaded by network_check.html before this file.

// Shared by poll() and the scan-start submit handler below - both reset the
// same set of controls and show the same error banner on failure; poll()
// additionally needs its interval stopped since a mid-poll failure would
// otherwise keep retrying against a scan it can no longer reach.
function showScanError(message, { stopPolling = false } = {}) {
  if (stopPolling) clearInterval(pollTimer);
  scanBtn.disabled = false;
  stopBtn.style.display = "none";
  errorEl.style.display = "block";
  errorEl.textContent = message;
}

function matchBadge(value) {
  if (value === null || value === undefined) return '<span class="badge badge-unchanged">N/A</span>';
  return value
    ? '<span class="badge badge-added">MATCH</span>'
    : '<span class="badge badge-removed">MISMATCH</span>';
}

function listOrValue(list, value) {
  if (list && list.length) return list.map(escapeHtml).join(", ");
  return escapeHtml(value) || "-";
}

// Hardware-derived live cells (PC/monitor serial, MAC) read as a plain "-"
// when nothing came back, indistinguishable from "we asked and got nothing"
// - which is misleading when hardware scanning was unchecked entirely and
// nothing was ever asked for.
function hardwareCell(list, value) {
  if (!scanIncludesHardware) return '<span class="muted">Not scanned</span>';
  return listOrValue(list, value);
}

// Every field a mismatch can be corrected on: how to read its live value off
// a compare object, which asset_items id(s) an "Update" writes to, and the
// field key the /apply endpoint expects.
const UPDATABLE_FIELDS = [
  { key: "pc_serial", match: (c) => c.pc_match, liveValue: (c) => c.live_pc_serial, assetIds: (c) => c.pc_asset_ids },
  {
    key: "monitor_serial",
    match: (c) => c.monitor_match,
    liveValue: (c) => (c.live_monitor_serials || [])[0],
    assetIds: (c) => c.monitor_asset_ids,
  },
  {
    key: "user",
    match: (c) => c.user_match,
    liveValue: (c) => (c.live_users || [])[0],
    assetIds: (c) => c.user_asset_ids,
  },
];

function importedCell(ip, fieldDef, c, importedText) {
  const text = escapeHtml(importedText) || "-";
  const isMismatch = fieldDef.match(c) === false;
  const liveValue = fieldDef.liveValue(c);
  const assetIds = fieldDef.assetIds(c) || [];
  if (ignoredAll || !isMismatch || !liveValue || assetIds.length === 0) {
    return text;
  }
  // URI-encoded (not HTML-escaped) since this is JSON going into an HTML
  // attribute - encodeURIComponent leaves no quote characters behind to
  // break the attribute, unlike escapeHtml which only handles text content.
  const payload = encodeURIComponent(JSON.stringify({ ip, field: fieldDef.key, asset_ids: assetIds, value: liveValue }));
  return `${text} <button type="button" class="update-btn" data-update="${payload}">Update</button>`;
}

function renderResults(results) {
  lastResults = results;
  tbody.innerHTML = results
    .map((r) => {
      const c = r.compare || {};
      const rowClass = r.alive ? "alive" : "dead";
      return `<tr class="${rowClass}">
        <td>${escapeHtml(r.ip)}</td>
        <td>${r.alive ? "Alive" : "No response"}</td>
        <td>${escapeHtml(r.hostname) || "-"}</td>
        <td>${hardwareCell(null, c.live_mac)}</td>
        <td>${hardwareCell(null, c.live_pc_serial)}</td>
        <td>${importedCell(r.ip, UPDATABLE_FIELDS[0], c, c.imported_pc_serial)}</td>
        <td>${matchBadge(c.pc_match)}</td>
        <td>${hardwareCell(c.live_monitor_serials)}</td>
        <td>${importedCell(r.ip, UPDATABLE_FIELDS[1], c, c.imported_monitor_serial)}</td>
        <td>${matchBadge(c.monitor_match)}</td>
        <td>${listOrValue(c.live_users)}</td>
        <td>${importedCell(r.ip, UPDATABLE_FIELDS[2], c, c.imported_user)}</td>
        <td>${matchBadge(c.user_match)}</td>
      </tr>`;
    })
    .join("");
  updateBulkActionsVisibility();
}

function collectAllMismatches() {
  const updates = [];
  for (const r of lastResults) {
    const c = r.compare || {};
    for (const fieldDef of UPDATABLE_FIELDS) {
      if (fieldDef.match(c) !== false) continue;
      const value = fieldDef.liveValue(c);
      const assetIds = fieldDef.assetIds(c) || [];
      if (!value || assetIds.length === 0) continue;
      updates.push({ ip: r.ip, field: fieldDef.key, asset_ids: assetIds, value });
    }
  }
  return updates;
}

function updateBulkActionsVisibility() {
  if (ignoredAll) {
    bulkActions.style.display = "none";
    return;
  }
  bulkActions.style.display = collectAllMismatches().length > 0 ? "flex" : "none";
}

async function applyUpdates(updates) {
  if (!currentScanId || updates.length === 0) return;
  const resp = await fetch(`/network-check/scan/${currentScanId}/apply`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
    body: JSON.stringify({ updates }),
  });
  let data;
  try {
    data = await readJson(resp);
  } catch (err) {
    bulkStatus.textContent = err.message;
    return;
  }
  if (!resp.ok) {
    bulkStatus.textContent = data.error || "Error applying update.";
    return;
  }
  bulkStatus.textContent = `Updated ${data.applied.length} value(s).`;
  await poll(currentScanId);
}

tbody.addEventListener("click", (e) => {
  const btn = e.target.closest(".update-btn");
  if (!btn) return;
  const update = JSON.parse(decodeURIComponent(btn.dataset.update));
  applyUpdates([update]);
});

updateAllBtn.addEventListener("click", () => {
  applyUpdates(collectAllMismatches());
});

ignoreAllBtn.addEventListener("click", () => {
  ignoredAll = true;
  bulkStatus.textContent = "Mismatches ignored for this scan.";
  renderResults(lastResults);
});

async function poll(scanId) {
  const resp = await fetch(`/network-check/scan/${scanId}`, {
    headers: { "X-Requested-With": "fetch" },
  });
  let data;
  try {
    data = await readJson(resp);
  } catch (err) {
    showScanError(err.message, { stopPolling: true });
    return;
  }
  if (!resp.ok) {
    if (resp.status === 404) clearScanState();
    showScanError(data.error || "Error fetching scan results.", { stopPolling: true });
    return;
  }
  scanIncludesHardware = data.include_hardware !== false;
  progressEl.textContent = `Scanning ${data.branch_label}: ${data.done}/${data.total}`;
  table.style.display = "table";
  emptyHint.style.display = "none";
  renderResults(data.results);

  if (data.finished) {
    clearInterval(pollTimer);
    scanBtn.disabled = false;
    stopBtn.style.display = "none";
    stopBtn.disabled = false;
    progressEl.textContent = data.stopped
      ? `Stopped: ${data.done}/${data.total}`
      : `Done: ${data.done}/${data.total}`;
    exportLink.href = `/network-check/scan/${scanId}/export.xlsx`;
    exportLink.style.display = "inline-block";
  }
}

stopBtn.addEventListener("click", async () => {
  if (!currentScanId) return;
  stopBtn.disabled = true;
  stopBtn.textContent = "Stopping...";
  await fetch(`/network-check/scan/${currentScanId}/stop`, {
    method: "POST",
    headers: { "X-Requested-With": "fetch" },
  });
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  errorEl.style.display = "none";
  exportLink.style.display = "none";
  table.style.display = "none";
  tbody.innerHTML = "";
  progressEl.textContent = "";
  bulkStatus.textContent = "";
  bulkActions.style.display = "none";
  emptyHint.style.display = "block";
  ignoredAll = false;
  lastResults = [];
  clearScanState();

  const branchNo = branchSelect.value;
  if (!branchNo) {
    errorEl.style.display = "block";
    errorEl.textContent = "Select a branch first.";
    return;
  }

  scanBtn.disabled = true;
  stopBtn.disabled = false;
  stopBtn.textContent = "Stop";
  stopBtn.style.display = "inline-block";

  const resp = await fetch("/network-check/scan", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
    body: JSON.stringify({ branch_no: branchNo, include_hardware: includeHardwareCheckbox.checked }),
  });
  let data;
  try {
    data = await readJson(resp);
  } catch (err) {
    showScanError(err.message);
    return;
  }

  if (!resp.ok) {
    showScanError(data.error || "Unknown error.");
    return;
  }

  currentScanId = data.scan_id;
  scanIncludesHardware = data.include_hardware !== false;
  progressEl.textContent = `Scanning ${data.branch_label}: 0/${data.total}`;
  saveScanState(data.scan_id, branchNo, includeHardwareCheckbox.checked);
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => poll(data.scan_id), 1000);
  poll(data.scan_id);
});

// Runs once on every page load. A saved scan id from a previous visit (same
// tab) means the user navigated away and back, or reopened this tab - pull
// the latest state for it straight from the server and, if it's still
// running, keep polling. A 404 (server restarted, scan_id unknown) or any
// other fetch failure just means there's nothing to restore - fall back to
// the normal empty state silently rather than surfacing an error banner for
// a scan the user never asked about this load.
async function restoreScan() {
  let saved = null;
  try {
    saved = JSON.parse(sessionStorage.getItem(SCAN_STORAGE_KEY) || "null");
  } catch {
    saved = null;
  }
  if (!saved || !saved.scanId) return;

  let resp;
  try {
    resp = await fetch(`/network-check/scan/${saved.scanId}`, { headers: { "X-Requested-With": "fetch" } });
  } catch {
    return;
  }
  if (!resp.ok) {
    if (resp.status === 404) clearScanState();
    return;
  }
  let data;
  try {
    data = await readJson(resp);
  } catch {
    return;
  }

  if (saved.branchNo) branchSelect.value = saved.branchNo;
  includeHardwareCheckbox.checked = saved.includeHardware !== false;
  currentScanId = saved.scanId;
  scanIncludesHardware = data.include_hardware !== false;
  table.style.display = "table";
  emptyHint.style.display = "none";
  renderResults(data.results);

  if (data.finished) {
    progressEl.textContent = data.stopped
      ? `Stopped: ${data.done}/${data.total}`
      : `Done: ${data.done}/${data.total}`;
    exportLink.href = `/network-check/scan/${saved.scanId}/export.xlsx`;
    exportLink.style.display = "inline-block";
  } else {
    progressEl.textContent = `Scanning ${data.branch_label}: ${data.done}/${data.total}`;
    scanBtn.disabled = true;
    stopBtn.disabled = false;
    stopBtn.textContent = "Stop";
    stopBtn.style.display = "inline-block";
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(() => poll(saved.scanId), 1000);
  }
}

restoreScan();
