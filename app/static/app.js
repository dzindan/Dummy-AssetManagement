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

// Keeps --topbar-h (style.css) in sync with the sticky topbar's actual
// rendered height, since it wraps onto two lines at narrow widths - the
// table's own sticky header rows read this variable to sit directly below
// the topbar instead of both fighting over top:0.
(function () {
  const topbar = document.querySelector(".topbar");
  if (!topbar) return;
  function syncTopbarHeight() {
    document.documentElement.style.setProperty("--topbar-h", topbar.offsetHeight + "px");
  }
  syncTopbarHeight();
  window.addEventListener("resize", syncTopbarHeight);
})();

// Resizable table columns - opt-in via <table data-resizable-table="some-id">
// (Manage Assets: 13 columns is too wide for everyone's monitor to show
// comfortably at once). Widths start at whatever the browser's normal
// content-based auto layout already picked (measured before switching to
// table-layout:fixed, so nothing jumps on load), then a drag handle on each
// header's right edge lets a column be narrowed/widened from there. Saved
// per column index in localStorage, keyed by page path + the table's id, so
// a resize survives a reload or a re-filter instead of resetting every time.
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll("[data-resizable-table]").forEach(function (table) {
    const headerRow = table.querySelector("thead tr");
    if (!headerRow) return;
    const ths = Array.from(headerRow.children);

    const storageKey = "colwidths:" + location.pathname + ":" + table.dataset.resizableTable;
    let saved = {};
    try {
      saved = JSON.parse(localStorage.getItem(storageKey) || "{}");
    } catch (e) {
      saved = {};
    }

    // Measure every column's auto-computed width *before* setting any of
    // them - setting th[0]'s width while the table is still auto-layout
    // can itself shift how the browser auto-sizes the still-unmeasured
    // th[1], th[2]... (they all collapse to an equal share otherwise),
    // so all the reads have to happen first, then all the writes.
    const autoWidths = ths.map(function (th) {
      return th.getBoundingClientRect().width;
    });
    ths.forEach(function (th, i) {
      th.style.width = autoWidths[i] + "px";
    });
    table.classList.add("resizable-table-active");
    ths.forEach(function (th, i) {
      if (saved[i]) th.style.width = saved[i] + "px";

      const handle = document.createElement("div");
      handle.className = "col-resize-handle";
      th.appendChild(handle);

      handle.addEventListener("mousedown", function (e) {
        e.preventDefault();
        const startX = e.clientX;
        const startWidth = th.getBoundingClientRect().width;
        handle.classList.add("resizing");

        function onMove(moveEvent) {
          th.style.width = Math.max(40, startWidth + (moveEvent.clientX - startX)) + "px";
        }
        function onUp() {
          document.removeEventListener("mousemove", onMove);
          document.removeEventListener("mouseup", onUp);
          handle.classList.remove("resizing");
          saved[i] = parseInt(th.style.width, 10);
          localStorage.setItem(storageKey, JSON.stringify(saved));
        }
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
      });
    });
  });
});

// Floating scroll-to-top/scroll-to-bottom buttons (every page, see base.html).
document.addEventListener("click", function (e) {
  const btn = e.target.closest("[data-scroll-to]");
  if (!btn) return;
  if (btn.dataset.scrollTo === "top") {
    window.scrollTo(0, 0);
  } else {
    window.scrollTo(0, document.body.scrollHeight);
  }
});

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
    if (!wasOpen) {
      container.classList.add("open");
      // Reopening fresh always shows the full list - a leftover filter
      // from last time would otherwise silently hide options the user
      // never meant to exclude.
      const search = container.querySelector(".multiselect-search");
      if (search && search.value) {
        search.value = "";
        container.querySelectorAll(".multiselect-panel label").forEach(function (label) {
          label.style.display = "";
        });
      }
    }
    return;
  }
  if (!e.target.closest(".multiselect-panel")) {
    document.querySelectorAll(".multiselect.open").forEach(function (el) {
      el.classList.remove("open");
    });
  }
});

// Type-to-filter inside a multiselect panel (Manage Assets: Branch has 100+
// options, tedious to scroll through) - narrows the visible checkboxes by
// substring match; doesn't touch which ones are checked, so typing to find
// one more option never un-checks ones already picked.
document.addEventListener("input", function (e) {
  if (!e.target.matches(".multiselect-search")) return;
  const query = e.target.value.trim().toLowerCase();
  const panel = e.target.closest(".multiselect-panel");
  panel.querySelectorAll("label").forEach(function (label) {
    label.style.display = label.textContent.toLowerCase().includes(query) ? "" : "none";
  });
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
