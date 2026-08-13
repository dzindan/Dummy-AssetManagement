// Restore scroll position across a form submit that reloads the same page
// (Assign/Dismiss/Save/Delete buttons all POST-redirect-GET back to
// wherever the user was) - without this, every one of those actions dumps
// the user back at the top of the page, which is especially annoying when
// working through a long list (Settings' mapping chips, Manage Assets,
// Duplicate Check) one row at a time. Keyed by path+query so it only
// restores when landing back on essentially the same page/filter/page-number.
(function () {
  const SCROLL_KEY_PREFIX = "scrollpos:";
  const key = SCROLL_KEY_PREFIX + location.pathname + location.search;

  window.addEventListener("beforeunload", function () {
    sessionStorage.setItem(key, String(window.scrollY));
  });

  const saved = sessionStorage.getItem(key);
  if (saved !== null) {
    sessionStorage.removeItem(key);
    window.scrollTo(0, parseInt(saved, 10) || 0);
  }
})();

// Multi-select filter dropdowns (Manage Assets: Branch/Device/Status) - a
// checkbox panel that opens/closes on click rather than hover, since the
// user needs it to stay open while checking several boxes. Native
// <select multiple> was rejected here because it requires ctrl+click, which
// isn't discoverable.
document.addEventListener("click", function (e) {
  const toggle = e.target.closest(".multiselect-toggle");
  if (toggle) {
    const container = toggle.closest(".multiselect");
    const wasOpen = container.classList.contains("open");
    document.querySelectorAll(".multiselect.open").forEach(function (el) {
      el.classList.remove("open");
    });
    if (!wasOpen) container.classList.add("open");
    return;
  }
  if (!e.target.closest(".multiselect-panel")) {
    document.querySelectorAll(".multiselect.open").forEach(function (el) {
      el.classList.remove("open");
    });
  }
});

// Auto-uppercase any input/textarea marked with the "uc" class, as the user
// types, preserving cursor position (naive value reassignment would jump
// the cursor to the end on every keystroke).
document.addEventListener("input", function (e) {
  if (!e.target.matches("input.uc, textarea.uc")) return;
  const el = e.target;
  const start = el.selectionStart;
  const end = el.selectionEnd;
  el.value = el.value.toUpperCase();
  if (start !== null && end !== null) {
    el.setSelectionRange(start, end);
  }
});

// Device Name Mapping (Settings): drag an unmapped device-name chip onto a
// standard-name bucket to assign it. The dropdown+button on each chip does
// the exact same thing via a normal form post, so dragging is a shortcut,
// not the only way.
document.addEventListener("dragstart", function (e) {
  const chip = e.target.closest(".device-chip");
  if (!chip) return;
  e.dataTransfer.setData("text/plain", chip.dataset.alias);
  e.dataTransfer.effectAllowed = "move";
});

document.addEventListener("dragover", function (e) {
  const zone = e.target.closest(".device-dropzone");
  if (!zone) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = "move";
});

document.addEventListener("dragenter", function (e) {
  const zone = e.target.closest(".device-dropzone");
  if (zone) zone.classList.add("drag-over");
});

document.addEventListener("dragleave", function (e) {
  const zone = e.target.closest(".device-dropzone");
  if (zone && !zone.contains(e.relatedTarget)) zone.classList.remove("drag-over");
});

document.addEventListener("drop", function (e) {
  const zone = e.target.closest(".device-dropzone");
  if (!zone) return;
  e.preventDefault();
  zone.classList.remove("drag-over");

  const alias = e.dataTransfer.getData("text/plain");
  const canonicalName = zone.dataset.name;
  if (!alias || !canonicalName) return;

  const formData = new FormData();
  formData.append("alias", alias);
  formData.append("canonical_name", canonicalName);

  fetch(zone.dataset.mapUrl, {
    method: "POST",
    headers: { "X-Requested-With": "fetch" },
    body: formData,
  })
    .then(() => window.location.reload())
    .catch(() => window.location.reload());
});

// Bulk select/delete (Manage Assets, Duplicate Check, Cleaning Report): a
// "select all" checkbox toggles every row checkbox sharing its target
// class, and the delete button builds+submits a throwaway form with the
// checked ids - avoids wrapping a <form> around tables that already have
// their own per-row delete forms (HTML doesn't allow nested forms).
document.addEventListener("change", function (e) {
  const selectAll = e.target.closest("[data-select-all]");
  if (!selectAll) return;
  const targetClass = selectAll.dataset.selectAll;
  document.querySelectorAll("." + targetClass).forEach(function (cb) {
    cb.checked = selectAll.checked;
  });
});

document.addEventListener("click", function (e) {
  const btn = e.target.closest("[data-bulk-delete-trigger]");
  if (!btn) return;
  const targetClass = btn.dataset.bulkDeleteCheckboxClass;
  const ids = Array.from(document.querySelectorAll("." + targetClass + ":checked")).map(function (cb) {
    return cb.value;
  });
  if (ids.length === 0) {
    alert("Select at least one row first.");
    return;
  }
  if (!confirm("Delete " + ids.length + " selected asset(s)? This cannot be undone.")) return;

  const form = document.createElement("form");
  form.method = "post";
  form.action = btn.dataset.bulkDeleteUrl;

  const nextInput = document.createElement("input");
  nextInput.type = "hidden";
  nextInput.name = "next";
  nextInput.value = btn.dataset.bulkDeleteNext || "";
  form.appendChild(nextInput);

  ids.forEach(function (id) {
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = "asset_ids";
    input.value = id;
    form.appendChild(input);
  });

  document.body.appendChild(form);
  form.submit();
});
