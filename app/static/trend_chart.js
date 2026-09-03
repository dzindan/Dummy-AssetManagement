/* Interactive line chart with clickable slicer chips, replacing the old
 * server-rendered stacked-bar SVG (app/charts.py used to draw it all in
 * Python; now Python only ships a small {periods, series} JSON payload via
 * trend_chart_payload(), and this file draws + hovers it client-side so
 * clicking a chip can show/hide a line without a page reload). Each
 * series' color comes from the payload already resolved server-side
 * (charts.stable_color_for) and never changes when chips are toggled - see
 * that module's docstring.
 *
 * No build step, no chart library - matches how the rest of this app's
 * frontend works (plain files, no bundler) and keeps working fully offline
 * once packaged.
 */
(function (window) {
  "use strict";

  var MONTH_LABEL_RE = /^\d{4}-(\d{2})$/;
  var MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function shortPeriodLabel(period) {
    var m = MONTH_LABEL_RE.exec(period);
    return m ? MONTH_NAMES[Number(m[1]) - 1] + " " + period.slice(2, 4) : period;
  }

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

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

    if (!payload || !payload.periods || !payload.periods.length || !payload.series || !payload.series.length) {
      container.innerHTML = '<p class="muted">' + escapeHtml(emptyMessage) + "</p>";
      return;
    }

    var periods = payload.periods;
    var allSeries = payload.series; // [{name, color, values}], display order fixed (top-N desc, OTHER last)
    var n = periods.length;

    var gridColor = getComputedStyle(document.documentElement).getPropertyValue("--chart-grid").trim() || "#e5e7eb";
    var axisColor = getComputedStyle(document.documentElement).getPropertyValue("--muted").trim() || "#6b7280";

    var active = {};
    allSeries.forEach(function (s) { active[s.name] = true; });

    container.innerHTML =
      '<div class="pill-slicer" role="group" aria-label="Filter which lines are shown"></div>' +
      '<div class="chart-wrap"></div>';
    var pillsEl = container.querySelector(".pill-slicer");
    var chartWrap = container.querySelector(".chart-wrap");
    var tip = document.createElement("div");
    tip.className = "crosshair-tip";
    chartWrap.appendChild(tip);

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

    var W = 900, H = 380;
    var PAD_L = 42, PAD_R = 18, PAD_T = 18, PAD_B = 30;
    var plotW = W - PAD_L - PAD_R, plotH = H - PAD_T - PAD_B;
    var lastN = Math.max(n - 1, 1); // avoid /0 when there's only one period on record

    function xFor(i) { return PAD_L + (plotW * i) / lastN; }

    var hoverState = null;

    function render() {
      var visible = allSeries.filter(function (s) { return active[s.name]; });

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
          '" text-anchor="end">' + Math.round(val) + "</text>");
      }

      // Thin out x-axis labels once there's more history than a ~15-label
      // width comfortably fits, same idea as a normal time-axis chart -
      // full history (branch_detail/dashboard's chart source) can run to
      // dozens of months, unlike the old bar chart which just let bars get
      // thin.
      var labelEvery = Math.max(1, Math.ceil(n / 15));
      for (var m = 0; m < n; m++) {
        if (m % labelEvery !== 0 && m !== n - 1) { continue; }
        svg.push('<text x="' + xFor(m).toFixed(1) + '" y="' + (H - PAD_B + 18) + '" font-size="11" fill="' + axisColor +
          '" text-anchor="middle">' + escapeHtml(shortPeriodLabel(periods[m])) + "</text>");
      }

      if (!visible.length) {
        svg.push('<text x="' + (W / 2) + '" y="' + (H / 2) + '" font-size="13" fill="' + axisColor +
          '" text-anchor="middle">No series selected - click a chip above to show it.</text>');
      }

      visible.forEach(function (s) {
        var d = s.values.map(function (v, i) { return (i === 0 ? "M" : "L") + xFor(i).toFixed(1) + "," + yFor(v).toFixed(1); }).join(" ");
        svg.push('<path d="' + d + '" fill="none" stroke="' + s.color + '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>');
        var lastI = s.values.length - 1;
        svg.push('<circle cx="' + xFor(lastI).toFixed(1) + '" cy="' + yFor(s.values[lastI]).toFixed(1) + '" r="4" fill="' + s.color + '"/>');
        svg.push('<text x="' + (xFor(lastI) + 8).toFixed(1) + '" y="' + (yFor(s.values[lastI]) + 4).toFixed(1) +
          '" font-size="11" font-weight="600" fill="' + s.color + '">' + s.values[lastI] + "</text>");
      });

      svg.push('<g class="hoverLayer" style="opacity:0">' +
        '<line class="crosshairLine" x1="0" y1="' + PAD_T + '" x2="0" y2="' + (H - PAD_B) + '" stroke="#9ca3af" stroke-width="1" stroke-dasharray="3,3"/>' +
        "</g>");
      svg.push('<rect class="hoverCatcher" x="' + PAD_L + '" y="' + PAD_T + '" width="' + plotW + '" height="' + plotH + '" fill="transparent"/>');
      svg.push("</svg>");

      var oldSvg = chartWrap.querySelector("svg");
      if (oldSvg) { oldSvg.remove(); }
      chartWrap.insertAdjacentHTML("afterbegin", svg.join(""));

      hoverState = { xFor: xFor, visible: visible };
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
        hoverLayer.style.opacity = 1;

        var rows = hoverState.visible.map(function (s) {
          return { name: s.name, color: s.color, value: s.values[i] };
        }).sort(function (a, b) { return b.value - a.value; });

        var html = '<div class="tip-month">' + escapeHtml(periods[i]) + "</div>" + rows.map(function (r) {
          return '<div class="tip-row"><span class="dot" style="background:' + r.color + '"></span>' + escapeHtml(r.name) +
            '<span class="v">' + r.value + "</span></div>";
        }).join("");
        tip.innerHTML = html;
        tip.classList.add("visible");

        var wrapRect = chartWrap.getBoundingClientRect();
        var tipX = ((x / W) * wrapRect.width) + 14;
        if (tipX + 170 > wrapRect.width) { tipX = ((x / W) * wrapRect.width) - 170; }
        tip.style.left = tipX + "px";
      });

      catcher.addEventListener("mouseleave", function () {
        hoverLayer.style.opacity = 0;
        tip.classList.remove("visible");
      });
    }

    render();
  }

  window.TrendChart = { init: init };
})(window);
