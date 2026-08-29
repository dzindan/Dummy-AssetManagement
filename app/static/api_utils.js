// Shared by every page with its own live-scan/update flow (network_check.js,
// cucm_scan.js). Every fetch() there reads its JSON response through this
// instead of calling resp.json() directly - a session that expires mid-scan
// makes the server redirect to the login page's HTML instead of returning
// JSON, and resp.json() would throw on that, which left uncaught is
// invisible (no error banner, results just never show up again).
// Centralizing the parse means every call site gets the same clear "session
// expired" message instead of a silent dead end.
async function readJson(resp) {
  let data = null;
  try {
    data = await resp.json();
  } catch {
    // Not JSON - most likely a login-page redirect from an expired session.
  }
  if (resp.status === 401) {
    throw new Error((data && data.error) || "Your session has expired. Please log in again.");
  }
  if (data === null) {
    throw new Error(`Unexpected response from server (HTTP ${resp.status}).`);
  }
  return data;
}
