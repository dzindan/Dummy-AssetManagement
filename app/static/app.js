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
    const y = parseInt(saved, 10) || 0;
    // A redirect that also carries a #fragment (e.g. Settings' Save
    // buttons, which jump back to the panel that was just saved) fights
    // this: the browser's own scroll-to-fragment can land after this
    // script runs and override the restore. Drop the fragment so nothing
    // re-triggers it, then reassert on 'load' too, since that native jump
    // can happen after this synchronous scrollTo.
    if (location.hash) {
      history.replaceState(null, "", location.pathname + location.search);
    }
    window.scrollTo(0, y);
    window.addEventListener("load", function () {
      window.scrollTo(0, y);
    });
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
// (Manage Assets: 14 columns is too wide for everyone's monitor to show
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

        // Screen pixels -> the table's own CSS pixels, so a drag follows
        // the mouse at any table-zoom level (see the zoom block below).
        const zoom = parseFloat(table.style.zoom) || 1;
        const startCss = parseFloat(th.style.width) || startWidth / zoom;
        function onMove(moveEvent) {
          th.style.width = Math.max(40, startCss + (moveEvent.clientX - startX) / zoom) + "px";
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

      // Double-click the divider = fit this column to its widest content.
      handle.addEventListener("dblclick", function (e) {
        e.preventDefault();
        autofit(i);
        localStorage.setItem(storageKey, JSON.stringify(saved));
      });
    });

    // Widest content in column i (header + every cell), in the table's own
    // CSS pixels. Cells clip with an ellipsis, but scrollWidth still reports
    // the full content width.
    function autofit(i) {
      // Narrow it first: scrollWidth can only report content wider than
      // the cell, so measuring at the current width could never shrink it.
      ths[i].style.width = "40px";
      let widest = 40;
      table.querySelectorAll("tr").forEach(function (tr) {
        const cell = tr.children[i];
        if (!cell || cell.colSpan > 1) return;
        const pad = cell.querySelector("input") ? 0 : 2;
        widest = Math.max(widest, cell.scrollWidth + pad);
      });
      widest = Math.min(widest, 600);
      ths[i].style.width = widest + "px";
      saved[i] = widest;
    }
    // Used by the table-zoom bar's "Auto-fit columns" / "Reset columns".
    table.autofitColumns = function () {
      ths.forEach(function (th, i) { autofit(i); });
      localStorage.setItem(storageKey, JSON.stringify(saved));
    };
    table.resetColumns = function () {
      localStorage.removeItem(storageKey);
      location.reload();
    };
  });
});

// Synced "shadow" horizontal scrollbar pinned to the bottom of the
// viewport for every .table-scroll table - its own native scrollbar sits
// at the table's bottom edge, which for a long table means scrolling the
// whole page all the way down just to even reach it. This mirror bar
// (position: sticky, see .table-scroll-shadow) stays glued to the bottom
// of the screen the entire time any part of the table is in view -
// putting it merely *above* the table instead (an earlier version of this)
// still scrolled it out of view along with everything else the moment you
// scrolled past it. Stays in sync with the real scrollbar in both
// directions; hidden entirely for a table that doesn't overflow.
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll(".table-scroll").forEach(function (scrollBox) {
    const inner = scrollBox.firstElementChild;
    if (!inner) return;

    // Wrapper spans exactly the table's own height (plus the shadow bar),
    // which is what bounds the sticky bar's "stuck" range - without this
    // wrapper, `position: sticky` would have nothing of the right height to
    // stick within and the bar would just glue to the bottom of the screen
    // forever, long after the table itself has scrolled out of view.
    const wrap = document.createElement("div");
    wrap.className = "table-scroll-wrap";
    scrollBox.parentNode.insertBefore(wrap, scrollBox);
    wrap.appendChild(scrollBox);

    const shadow = document.createElement("div");
    shadow.className = "table-scroll-shadow";
    const shadowInner = document.createElement("div");
    shadow.appendChild(shadowInner);
    wrap.appendChild(shadow);

    function syncWidth() {
      // Rendered width, so a table zoomed with the table-zoom control
      // below (CSS zoom) sizes the shadow bar by what's actually on screen.
      const width = Math.ceil(inner.getBoundingClientRect().width);
      shadowInner.style.width = width + "px";
      shadow.style.display = width > scrollBox.clientWidth ? "" : "none";
    }
    // Runs after the resizable-table block above (registered earlier on
    // this same DOMContentLoaded event, so it always finishes first),
    // meaning inner.scrollWidth already reflects any per-column widths
    // that block just set rather than the table's pre-resize auto layout.
    syncWidth();
    window.addEventListener("resize", syncWidth);

    // Re-check after column drag-resizing (app.js's own resizable-table
    // handler) or any other later DOM change - a plain 'resize' listener
    // above only catches the browser window itself resizing.
    new ResizeObserver(syncWidth).observe(inner);

    let syncing = false;
    shadow.addEventListener("scroll", function () {
      if (syncing) return;
      syncing = true;
      scrollBox.scrollLeft = shadow.scrollLeft;
      syncing = false;
    });
    scrollBox.addEventListener("scroll", function () {
      if (syncing) return;
      syncing = true;
      shadow.scrollLeft = scrollBox.scrollLeft;
      syncing = false;
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

// Excel-style inline editing - Manage Assets / Manage CCTV tables marked
// data-inline-edit (only rendered for accounts with edit permission). Each
// <td data-field> is a cell: click selects it, typing / Enter / F2 /
// double-click edits it, Enter saves and moves down, Tab saves and moves
// right (Shift+Tab left), arrows move, Esc cancels, leaving the cell saves.
// Every cell is saved on its own straight away (POST JSON {field, value} to
// the row's data-cell-url - same rules as the Edit page server-side).
(function () {
  const table = document.querySelector("table[data-inline-edit]");
  if (!table) return;
  const body = table.tBodies[0];
  let editing = null;

  body.querySelectorAll("td[data-field]").forEach(function (td) {
    td.tabIndex = 0;
  });

  let toastWrap = null;
  function toast(text, category) {
    if (!toastWrap) {
      toastWrap = document.createElement("div");
      toastWrap.className = "inline-toast-wrap";
      document.body.appendChild(toastWrap);
    }
    const t = document.createElement("div");
    t.className = "inline-toast flash-" + (category === "error" ? "error" : "success");
    t.textContent = text;
    toastWrap.appendChild(t);
    setTimeout(function () { t.remove(); }, category === "error" ? 8000 : 4000);
  }

  function editableRows() {
    return Array.from(body.rows).filter(function (r) { return r.dataset.cellUrl; });
  }

  function move(td, dRow, dCol) {
    const tr = td.parentElement;
    const cells = Array.from(tr.querySelectorAll("td[data-field]"));
    const idx = cells.indexOf(td);
    let target = null;
    if (dCol) {
      target = cells[idx + dCol];
    } else if (dRow) {
      const rows = editableRows();
      const other = rows[rows.indexOf(tr) + dRow];
      target = other ? other.querySelectorAll("td[data-field]")[idx] : null;
    }
    if (target) target.focus();
    return target;
  }

  function save(td, value) {
    const tr = td.parentElement;
    const field = td.dataset.field;
    const shownBefore = td.dataset.display;
    td.textContent = value;
    td.classList.remove("cell-error", "cell-saved");
    td.removeAttribute("title");
    td.classList.add("cell-saving");
    fetch(tr.dataset.cellUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
      body: JSON.stringify({ field: field, value: value }),
    })
      .then(function (r) {
        return r.json().then(function (j) { return { ok: r.ok, j: j }; });
      })
      .then(function (res) {
        if (!res.ok || !res.j.ok) throw new Error(res.j.error || "Save failed.");
        td.dataset.value = res.j.value;
        td.textContent = field === "branch_dept" ? res.j.branch_label : res.j.value;
        const usage = tr.querySelector('[data-derived="usage_duration"]');
        if (usage && res.j.usage_duration !== undefined) usage.textContent = res.j.usage_duration || "";
        td.classList.remove("cell-saving");
        void td.offsetWidth; // restart the highlight animation
        td.classList.add("cell-saved");
        (res.j.messages || []).forEach(function (m) { toast(m.text, m.category); });
      })
      .catch(function (err) {
        td.innerHTML = shownBefore;
        td.classList.remove("cell-saving");
        td.classList.add("cell-error");
        td.title = err.message;
        toast(err.message, "error");
      });
  }

  function finish(td, keep, then) {
    if (editing !== td) return;
    editing = null;
    const input = td.querySelector("input");
    const value = input ? input.value.trim() : "";
    td.classList.remove("cell-editing");
    const changed = keep && value !== (td.dataset.value || "").trim();
    if (changed) {
      save(td, value);
    } else {
      td.innerHTML = td.dataset.display;
    }
    if (then) then();
    else if (!changed) td.focus();
  }

  function startEdit(td, initial) {
    if (editing) return;
    editing = td;
    td.dataset.display = td.innerHTML;
    const input = document.createElement("input");
    input.type = "text";
    input.value = initial !== undefined ? initial : (td.dataset.value || "");
    if (document.getElementById("dl-" + td.dataset.field)) input.setAttribute("list", "dl-" + td.dataset.field);
    td.classList.add("cell-editing");
    td.textContent = "";
    td.appendChild(input);
    input.focus();
    if (initial === undefined) input.select();
    else input.setSelectionRange(input.value.length, input.value.length);

    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") {
        e.preventDefault();
        finish(td, true, function () { move(td, 1, 0) || td.focus(); });
      } else if (e.key === "Tab") {
        e.preventDefault();
        finish(td, true, function () { move(td, 0, e.shiftKey ? -1 : 1) || td.focus(); });
      } else if (e.key === "Escape") {
        e.preventDefault();
        finish(td, false);
      }
      e.stopPropagation();
    });
    input.addEventListener("blur", function () { finish(td, true, function () {}); });
  }

  body.addEventListener("keydown", function (e) {
    const td = e.target.closest && e.target.closest("td[data-field]");
    if (!td || editing) return;
    const arrows = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] };
    if (arrows[e.key]) {
      e.preventDefault();
      move(td, arrows[e.key][0], arrows[e.key][1]);
    } else if (e.key === "Tab") {
      if (move(td, 0, e.shiftKey ? -1 : 1)) e.preventDefault();
    } else if (e.key === "Enter" || e.key === "F2") {
      e.preventDefault();
      startEdit(td);
    } else if (e.key === "Delete" || e.key === "Backspace") {
      e.preventDefault();
      startEdit(td, "");
    } else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
      e.preventDefault();
      startEdit(td, e.key);
    }
  });

  body.addEventListener("dblclick", function (e) {
    const td = e.target.closest("td[data-field]");
    if (td && editing !== td) startEdit(td);
  });
})();

// Zoom for one table only, plus the column-width buttons (Manage Assets /
// Manage CCTV - wide tables you'd
// otherwise scroll left and right through): - / + buttons, "Fit width"
// (shrink until every column fits the screen), 100% to reset, and
// Ctrl + mouse wheel over the table. Page text, filters and menus keep
// their size. Remembered per page + table in localStorage. Uses CSS zoom,
// so clicks, column resizing and inline editing keep working at any level.
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll("table[data-resizable-table]").forEach(function (table) {
    const scrollBox = table.closest(".table-scroll");
    if (!scrollBox) return;
    const anchor = scrollBox.closest(".table-scroll-wrap") || scrollBox;
    const key = "tablezoom:" + location.pathname + ":" + table.dataset.resizableTable;
    const MIN = 40, MAX = 150, STEP = 10;

    const bar = document.createElement("div");
    bar.className = "table-zoom";
    bar.innerHTML =
      '<span class="muted">Table zoom</span>' +
      '<button type="button" class="secondary" data-zoom="-" title="Smaller (Ctrl + wheel down)">&minus;</button>' +
      '<button type="button" class="secondary table-zoom-level" data-zoom="reset" title="Back to 100%">100%</button>' +
      '<button type="button" class="secondary" data-zoom="+" title="Bigger (Ctrl + wheel up)">+</button>' +
      '<button type="button" class="secondary" data-zoom="fit" title="Shrink so every column fits the screen">Fit width</button>' +
      '<span class="muted table-zoom-sep">Columns</span>' +
      '<button type="button" class="secondary" data-cols="autofit" title="Size every column to its content (double-click a column divider to do just one)">Auto-fit columns</button>' +
      '<button type="button" class="secondary" data-cols="reset" title="Forget the widths you set and go back to the default">Reset columns</button>';
    anchor.parentNode.insertBefore(bar, anchor);
    const level = bar.querySelector(".table-zoom-level");

    let zoom = 100;
    function apply(value) {
      zoom = Math.max(MIN, Math.min(MAX, Math.round(value)));
      table.style.zoom = zoom === 100 ? "" : zoom / 100;
      level.textContent = zoom + "%";
      try { localStorage.setItem(key, String(zoom)); } catch (e) { /* private mode */ }
      window.dispatchEvent(new Event("resize")); // re-size the bottom shadow scrollbar
    }
    function fit() {
      table.style.zoom = "";
      const natural = table.getBoundingClientRect().width;
      apply(natural > scrollBox.clientWidth ? Math.floor((scrollBox.clientWidth / natural) * 100) : 100);
    }

    bar.addEventListener("click", function (e) {
      const colBtn = e.target.closest("[data-cols]");
      if (colBtn) {
        if (colBtn.dataset.cols === "autofit" && table.autofitColumns) table.autofitColumns();
        if (colBtn.dataset.cols === "reset" && table.resetColumns) table.resetColumns();
        window.dispatchEvent(new Event("resize"));
        return;
      }
      const btn = e.target.closest("[data-zoom]");
      if (!btn) return;
      const action = btn.dataset.zoom;
      if (action === "+") apply(Math.floor(zoom / STEP) * STEP + STEP);
      else if (action === "-") apply(Math.ceil(zoom / STEP) * STEP - STEP);
      else if (action === "reset") apply(100);
      else fit();
    });

    scrollBox.addEventListener("wheel", function (e) {
      if (!e.ctrlKey) return;
      e.preventDefault(); // zoom just the table, not the whole page
      apply(zoom + (e.deltaY < 0 ? STEP : -STEP));
    }, { passive: false });

    let saved = NaN;
    try { saved = parseInt(localStorage.getItem(key), 10); } catch (e) { /* ignore */ }
    if (saved && saved !== 100) apply(saved);
  });
});
