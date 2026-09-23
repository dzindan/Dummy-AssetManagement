/* Interactive line chart with clickable slicer chips (app/charts.py
 * ships a small {periods, series} JSON payload via
 * trend_chart_payload()/per_series_trend_payloads(), and this file draws +
 * hovers it client-side so clicking a chip can show/hide a line without a
 * page reload, rescaling the y-axis to what's left). Each series' color comes from the
 * payload already resolved server-side (charts._assign_colors) and never
 * changes when chips are toggled - see that module's docstring.
 *
 * No build step, no chart library - matches how the rest of this app's
 * frontend works (plain files, no bundler) and keeps working fully offline
 * once packaged.
 */
(function (window) {
  "use strict";

  var MONTH_LABEL_RE = /^\d{4}-(0[1-9]|1[0-2])$/;
  var MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function shortPeriodLabel(period) {
    var m = MONTH_LABEL_RE.exec(period);
    return m ? MONTH_NAMES[Number(m[1]) - 1] + " " + period.slice(2, 4) : period;
  }

  function formatNumber(v) {
    return v.toLocaleString("en-US");
  }

  // escapeHtml() now lives in dom_utils.js, loaded by branch_detail.html /
  // dashboard.html before this file.

  function niceStep(maxVal) {
    var raw = (maxVal / 4) || 1;
    var magnitude = Math.pow(10, Math.floor(Math.log10(raw)));
    var mults = [1, 2, 5, 10];
    for (var i = 0; i < mults.length; i++) {
      var step = mults[i] * magnitude;
      if (step >= raw) { return step; }
    }
    return magnitude * 10;
  }

  function init(container, payload, options) {
    options = options || {};
    var emptyMessage = options.emptyMessage ||
      "Not enough historical data yet - import at least two months to see a trend.";
    var ariaLabel = options.ariaLabel || "Line chart of item counts by month";
    var hideLegend = !!options.hideLegend;
    var compact = !!options.compact;

    if (!payload || !payload.periods || payload.periods.length < 2 || !payload.series || !payload.series.length) {
      container.innerHTML = '<p class="muted">' + escapeHtml(emptyMessage) + "</p>";
      return;
    }

    var periods = payload.periods;
    var allSeries = payload.series; // [{name, color, values}], display order fixed (top-N desc, OTHER last)
    var n = periods.length;

    var gridColor = getComputedStyle(document.documentElement).getPropertyValue("--chart-grid").trim() || "#e5e7eb";
    var axisColor = getComputedStyle(document.documentElement).getPropertyValue("--muted").trim() || "#6b7280";
    var textColor = getComputedStyle(document.documentElement).getPropertyValue("--text").trim() || "#1f2937";
    var surfaceColor = getComputedStyle(document.documentElement).getPropertyValue("--panel").trim() || "#ffffff";

    var active = {};
    allSeries.forEach(function (s) { active[s.name] = true; });

    container.innerHTML =
      (hideLegend ? "" : '<div class="pill-slicer" role="group" aria-label="Filter which lines are shown"></div>') +
      '<div class="chart-wrap"></div>';
    var pillsEl = container.querySelector(".pill-slicer");
    var chartWrap = container.querySelector(".chart-wrap");
    var tip = document.createElement("div");
    tip.className = "crosshair-tip";
    chartWrap.appendChild(tip);

    if (!hideLegend) {
      allSeries.forEach(function (s) {
        var pill = document.createElement("button");
        pill.type = "button";
        pill.className = "pill active";
        pill.style.setProperty("--pill-border", s.color);
        pill.style.setProperty("--pill-tint", s.color + "1f");
        pill.innerHTML = '<span class="dot" style="background:' + s.color + '"></span>' + escapeHtml(s.name);
        pill.addEventListener("click", function () {
          active[s.name] = !active[s.name];
          pill.classList.toggle("active", active[s.name]);
          render();
        });
        pillsEl.appendChild(pill);
      });
    }

    var W = 900, H = compact ? 220 : 380;
    // PAD_L clears the y-axis tick labels (up to "20,000", 6 chars with the
    // thousands comma); PAD_R leaves room for the end-of-line value label.
    var PAD_L = 58, PAD_R = 44, PAD_T = 18, PAD_B = 30;
    var plotW = W - PAD_L - PAD_R, plotH = H - PAD_T - PAD_B;
    var lastN = n - 1; // safe: the guard above already requires n >= 2

    function xFor(i) { return PAD_L + (plotW * i) / lastN; }

    var hoverState = null;

    function render() {
      var visible = allSeries.filter(function (s) { return active[s.name]; });

      // Lines: the y-scale is driven by the largest single visible value, so
      // hiding a big series (e.g. CCTV's "Cameras") rescales the rest.
      var maxVal = 1;
      visible.forEach(function (s) { s.values.forEach(function (v) { if (v > maxVal) { maxVal = v; } }); });
      var step = niceStep(maxVal);
      var yMax = step * 4;
      function yFor(v) { return PAD_T + plotH * (1 - v / yMax); }

      var svg = [];
      svg.push('<svg viewBox="0 0 ' + W + ' ' + H + '" xmlns="http://www.w3.org/2000/svg" role="img" ' +
        'aria-label="' + escapeHtml(ariaLabel) + '" style="width:100%;height:auto;display:block;font-family:Segoe UI, Arial, sans-serif;">');

      for (var g = 0; g <= 4; g++) {
        var val = (yMax * g) / 4;
        var y = yFor(val);
        svg.push('<line x1="' + PAD_L + '" y1="' + y.toFixed(1) + '" x2="' + (W - PAD_R) + '" y2="' + y.toFixed(1) +
          '" stroke="' + gridColor + '" stroke-width="1"/>');
        svg.push('<text x="' + (PAD_L - 8) + '" y="' + (y + 4).toFixed(1) + '" font-size="11" fill="' + axisColor +
          '" text-anchor="end">' + formatNumber(Math.round(val)) + "</text>");
      }

      // Thin out x-axis labels once there's more history than a ~15-label
      // width comfortably fits, same idea as a normal time-axis chart -
      // full history (branch_detail/dashboard's chart source) can run to
      // dozens of months, unlike the old bar chart which just let bars get
      // thin.
      var labelEvery = Math.max(1, Math.ceil(n / 15));
      var shownLabels = [];
      for (var m = 0; m < n; m += labelEvery) { shownLabels.push(m); }
      var lastShown = shownLabels[shownLabels.length - 1];
      if (lastShown !== n - 1) {
        // Always label the latest period, but don't let it crowd into the
        // previous tick - swap it in instead of adding a 2nd nearby label.
        var MIN_LABEL_GAP_PX = 30;
        if (xFor(n - 1) - xFor(lastShown) < MIN_LABEL_GAP_PX) {
          shownLabels[shownLabels.length - 1] = n - 1;
        } else {
          shownLabels.push(n - 1);
        }
      }
      shownLabels.forEach(function (m) {
        svg.push('<text x="' + xFor(m).toFixed(1) + '" y="' + (H - PAD_B + 18) + '" font-size="11" fill="' + axisColor +
          '" text-anchor="middle">' + escapeHtml(shortPeriodLabel(periods[m])) + "</text>");
      });

      if (!visible.length) {
        svg.push('<text x="' + (W / 2) + '" y="' + (H / 2) + '" font-size="13" fill="' + axisColor +
          '" text-anchor="middle">No series selected - click a chip above to show it.</text>');
      }

      // Line mark spec: 2px lines, an end marker with a 2px surface ring so
      // it stays legible where lines cross, and a value label at the end of
      // each line in text ink (never the series color) - only when there
      // are few enough lines (<= 4) for the labels not to collide; the
      // chips + tooltip carry identity/values otherwise.
      var lastI = n - 1;
      var labelEnds = visible.length <= 4;
      visible.forEach(function (s) {
        var d = s.values.map(function (v, i) {
          return (i === 0 ? "M" : "L") + xFor(i).toFixed(1) + "," + yFor(v).toFixed(1);
        }).join(" ");
        svg.push('<path d="' + d + '" fill="none" stroke="' + s.color +
          '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>');
      });
      visible.forEach(function (s) {
        var ex = xFor(lastI), ey = yFor(s.values[lastI]);
        svg.push('<circle cx="' + ex.toFixed(1) + '" cy="' + ey.toFixed(1) + '" r="4" fill="' + s.color +
          '" stroke="' + surfaceColor + '" stroke-width="2"/>');
        if (labelEnds) {
          svg.push('<text x="' + (ex + 8).toFixed(1) + '" y="' + (ey + 4).toFixed(1) + '" font-size="11" font-weight="600" fill="' +
            textColor + '">' + formatNumber(s.values[lastI]) + "</text>");
        }
      });

      svg.push('<g class="hoverLayer" style="opacity:0">' +
        '<line class="crosshairLine" x1="0" y1="' + PAD_T + '" x2="0" y2="' + (H - PAD_B) + '" stroke="#9ca3af" stroke-width="1" stroke-dasharray="3,3"/>' +
        visible.map(function (s, idx) {
          return '<circle class="hoverDot" data-idx="' + idx + '" r="4" fill="' + s.color +
            '" stroke="' + surfaceColor + '" stroke-width="2"/>';
        }).join("") +
        "</g>");
      svg.push('<rect class="hoverCatcher" x="' + PAD_L + '" y="' + PAD_T + '" width="' + plotW + '" height="' + plotH + '" fill="transparent"/>');
      svg.push("</svg>");

      var oldSvg = chartWrap.querySelector("svg");
      if (oldSvg) { oldSvg.remove(); }
      chartWrap.insertAdjacentHTML("afterbegin", svg.join(""));

      hoverState = { xFor: xFor, yFor: yFor, visible: visible };
      wireHover();
    }

    function wireHover() {
      var svgEl = chartWrap.querySelector("svg");
      var catcher = chartWrap.querySelector(".hoverCatcher");
      var hoverLayer = chartWrap.querySelector(".hoverLayer");
      var crosshairLine = chartWrap.querySelector(".crosshairLine");
      if (!catcher || !hoverState.visible.length) { return; }

      catcher.addEventListener("mousemove", function (evt) {
        var rect = svgEl.getBoundingClientRect();
        var scale = W / rect.width;
        var localX = (evt.clientX - rect.left) * scale;
        var i = Math.round(((localX - PAD_L) / plotW) * lastN);
        i = Math.max(0, Math.min(n - 1, i));

        var x = hoverState.xFor(i);
        crosshairLine.setAttribute("x1", x.toFixed(1));
        crosshairLine.setAttribute("x2", x.toFixed(1));
        hoverLayer.querySelectorAll(".hoverDot").forEach(function (dot) {
          var s = hoverState.visible[Number(dot.getAttribute("data-idx"))];
          dot.setAttribute("cx", x.toFixed(1));
          dot.setAttribute("cy", hoverState.yFor(s.values[i]).toFixed(1));
        });
        hoverLayer.style.opacity = 1;

        var total = 0;
        var rows = hoverState.visible.map(function (s) {
          total += s.values[i];
          return { name: s.name, color: s.color, value: s.values[i] };
        }).sort(function (a, b) { return b.value - a.value; });

        var html = '<div class="tip-month">' + escapeHtml(periods[i]) + "</div>" + rows.map(function (r) {
          return '<div class="tip-row"><span class="dot" style="background:' + r.color + '"></span>' + escapeHtml(r.name) +
            '<span class="v">' + formatNumber(r.value) + "</span></div>";
        }).join("") + (rows.length > 1
          ? '<div class="tip-row tip-total">Total<span class="v">' + formatNumber(total) + "</span></div>"
          : "");
        tip.innerHTML = html;
        tip.classList.add("visible");

        var wrapRect = chartWrap.getBoundingClientRect();
        var tipX = ((x / W) * wrapRect.width) + 14;
        if (tipX + 170 > wrapRect.width) { tipX = ((x / W) * wrapRect.width) - 170; }
        tipX = Math.max(0, tipX);
        tip.style.left = tipX + "px";
      });

      catcher.addEventListener("mouseleave", function () {
        hoverLayer.style.opacity = 0;
        tip.classList.remove("visible");
      });
    }

    render();
  }

  // One compact, legend-less solo chart per entry of `payloadsByName` (from
  // charts.per_series_trend_payloads), each in its own .panel card - the
  // "per device type" grid that sits alongside the combined chart.
  // Looping in JS (rather than one templated container id per device in
  // Jinja) sidesteps having to sanitize device names like "DVR/CCTV
  // RECORDER" into DOM ids.
  function initGrid(container, payloadsByName, options) {
    options = options || {};
    container.innerHTML = "";
    var names = Object.keys(payloadsByName || {});
    if (!names.length) {
      container.innerHTML = '<p class="muted">' + escapeHtml(options.emptyMessage ||
        "Not enough historical data yet - import at least two months to see a trend.") + "</p>";
      return;
    }
    names.forEach(function (name) {
      var card = document.createElement("div");
      card.className = "panel";
      var payload = payloadsByName[name];
      var color = (payload && payload.series && payload.series[0] && payload.series[0].color) || "#6b7280";
      var heading = document.createElement("h3");
      heading.className = "chart-card-title";
      var dot = document.createElement("span");
      dot.className = "dot";
      dot.style.background = color;
      heading.appendChild(dot);
      heading.appendChild(document.createTextNode(name));
      var chartDiv = document.createElement("div");
      card.appendChild(heading);
      card.appendChild(chartDiv);
      container.appendChild(card);
      init(chartDiv, payloadsByName[name], {
        hideLegend: true,
        compact: true,
        ariaLabel: "Line chart of " + name + " counts by month",
        emptyMessage: options.emptyMessage,
      });
    });
  }

  window.TrendChart = { init: init, initGrid: initGrid };
})(window);
