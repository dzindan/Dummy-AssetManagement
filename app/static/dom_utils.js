// Shared by every page that builds HTML strings from server/user-supplied
// text client-side (trend_chart.js, network_check.js) - a single DOM-based
// implementation instead of each file rolling its own regex-based escaper,
// so a fix to one (e.g. an attribute-context gap) reaches every call site.
function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}
