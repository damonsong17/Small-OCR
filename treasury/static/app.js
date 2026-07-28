/* Treasury dashboard front end.
 *
 * Plain ES2018, no build step, no dependencies — the air-gapped box gets the
 * same files that are in the repo. Panels arrive as JSON from /api/metric/<id>
 * and are rendered generically, so a new Python metric needs no change here.
 *
 * Every label that came from a spreadsheet is inserted with textContent.
 */
(function () {
  "use strict";

  var state = {
    view: "overview",
    params: {},
    panel: null,
    meta: null,
    world: null,
    mapMode: "bubbles",
    mapColour: "type",
    selectedCountry: "",
    transform: { k: 1, x: 0, y: 0 }
  };

  var $ = function (id) { return document.getElementById(id); };

  // ---------------------------------------------------------------- utilities
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        var value = attrs[key];
        if (value === null || value === undefined || value === false) return;
        if (key === "text") node.textContent = value;
        else if (key === "class") node.className = value;
        else if (key === "onclick") node.addEventListener("click", value);
        else node.setAttribute(key, value === true ? "" : value);
      });
    }
    (children || []).forEach(function (child) {
      if (child === null || child === undefined) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  var SVGNS = "http://www.w3.org/2000/svg";
  function svg(tag, attrs, children) {
    var node = document.createElementNS(SVGNS, tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        var value = attrs[key];
        if (value === null || value === undefined || value === false) return;
        if (key === "text") node.textContent = value;
        else node.setAttribute(key, value);
      });
    }
    (children || []).forEach(function (child) { if (child) node.appendChild(child); });
    return node;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function get(url) {
    return fetch(url, { headers: { Accept: "application/json" } })
      .then(function (r) { return r.json(); });
  }
  function post(url) {
    return fetch(url, { method: "POST" }).then(function (r) { return r.json(); });
  }

  // --------------------------------------------------------------- formatting
  function compact(value) {
    var abs = Math.abs(value);
    if (abs >= 1e12) return (value / 1e12).toFixed(2) + "tn";
    if (abs >= 1e9) return (value / 1e9).toFixed(2) + "bn";
    if (abs >= 1e6) return (value / 1e6).toFixed(2) + "m";
    if (abs >= 1e3) return (value / 1e3).toFixed(1) + "k";
    if (abs === 0) return "0";
    return value.toFixed(2);
  }

  function grouped(value, places) {
    return Number(value).toLocaleString(undefined, {
      minimumFractionDigits: places === undefined ? 0 : places,
      maximumFractionDigits: places === undefined ? 0 : places
    });
  }

  function format(value, kind, full) {
    if (value === null || value === undefined || value === "") return "—";
    if (typeof value === "boolean") return value ? "yes" : "no";
    var numeric = typeof value === "number";
    if (numeric && value === 0) value = 0;      // kill negative zero
    if (numeric && !isFinite(value)) return "—";
    switch (kind) {
      case "money":
      case "money_ccy":
        if (!numeric) return String(value);
        return full ? grouped(value) : compact(value);
      case "ratio": return numeric ? value.toFixed(2) + "×" : String(value);
      case "pct": return numeric ? (value * 100).toFixed(1) + "%" : String(value);
      case "rate": return numeric ? (value * 100).toFixed(3) + "%" : String(value);
      case "bps": return numeric ? Math.round(value) + " bps" : String(value);
      case "price":
        if (!numeric) return String(value);
        return value.toLocaleString(undefined, { minimumFractionDigits: 4,
                                                 maximumFractionDigits: 6 });
      case "days": return numeric ? Math.round(value) + " d" : String(value);
      case "number": return numeric ? grouped(value, value % 1 ? 2 : 0) : String(value);
      default: return String(value);
    }
  }

  function isNumericFormat(kind) {
    return ["money", "money_ccy", "ratio", "pct", "rate", "bps", "days", "number",
            "price"]
      .indexOf(kind) >= 0;
  }

  function markClass(series) {
    if (series.role) return "mark-" + series.role;
    return "mark-s" + (series.slot || 1);
  }
  function swatchClass(series) {
    if (series.role) return "sw-" + series.role;
    return "sw-s" + (series.slot || 1);
  }

  // ------------------------------------------------------------------ tooltip
  var tip = { node: null };
  function showTip(x, y, title, rows) {
    if (!tip.node) tip.node = $("tooltip");
    var box = tip.node;
    clear(box);
    if (title) box.appendChild(el("div", { class: "tt-title", text: title }));
    rows.forEach(function (row) {
      box.appendChild(el("div", { class: "tt-row" }, [
        el("span", { class: "tt-key" }, [
          row.swatch ? el("span", { class: "tt-line " + row.swatch }) : null,
          el("span", { text: row.label })
        ]),
        el("span", { class: "tt-value", text: row.value })
      ]));
    });
    box.hidden = false;
    var rect = box.getBoundingClientRect();
    var left = Math.min(x + 14, window.innerWidth - rect.width - 10);
    var top = Math.min(y + 14, window.innerHeight - rect.height - 10);
    box.style.left = Math.max(8, left) + "px";
    box.style.top = Math.max(8, top) + "px";
  }
  function hideTip() { if (tip.node) tip.node.hidden = true; }
  document.addEventListener("scroll", hideTip, true);

  // ------------------------------------------------------------------- scales
  function niceTicks(min, max, count) {
    if (min === max) { min = Math.min(0, min); max = max || 1; }
    var span = max - min;
    if (span === 0) span = Math.abs(max) || 1;
    var raw = span / (count || 5);
    var mag = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10));
    var norm = raw / mag;
    var step = (norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1) * mag;
    var lo = Math.floor(min / step) * step;
    var hi = Math.ceil(max / step) * step;
    var ticks = [];
    for (var v = lo; v <= hi + step / 2; v += step) {
      ticks.push(Math.abs(v) < step / 1e6 ? 0 : v);
    }
    return { ticks: ticks, min: lo, max: hi };
  }

  function barPath(x, y, w, h, r, up) {
    // 4px rounded data-end, square at the baseline.
    r = Math.max(0, Math.min(r, w / 2, Math.abs(h)));
    if (h <= 0.5) return "M" + x + "," + y + "h" + w;
    if (up) {
      return "M" + x + "," + (y + h) + "V" + (y + r) +
        "a" + r + "," + r + " 0 0 1 " + r + "," + -r +
        "h" + (w - 2 * r) +
        "a" + r + "," + r + " 0 0 1 " + r + "," + r +
        "V" + (y + h) + "Z";
    }
    return "M" + x + "," + y + "V" + (y + h - r) +
      "a" + r + "," + r + " 0 0 0 " + r + "," + r +
      "h" + (w - 2 * r) +
      "a" + r + "," + r + " 0 0 0 " + r + "," + -r +
      "V" + y + "Z";
  }

  // -------------------------------------------------------------- chart cards
  function chartCard(chart) {
    var card = el("div", { class: "card" });
    var head = el("div", { class: "chart-head" }, [
      el("h3", { text: chart.title || "" })
    ]);
    var toggle = el("button", {
      class: "toggle", type: "button", "aria-pressed": "false", text: "Table"
    });
    head.appendChild(toggle);
    card.appendChild(head);
    if (chart.note) card.appendChild(el("p", { class: "card-note", text: chart.note }));

    var body = el("div", { class: "chart-body" });
    card.appendChild(body);

    var showingTable = false;
    function draw() {
      clear(body);
      if (showingTable) {
        body.appendChild(chartTable(chart));
      } else {
        body.appendChild(renderChart(chart, body.clientWidth || 460));
        var legend = buildLegend(chart);
        if (legend) body.appendChild(legend);
      }
    }
    toggle.addEventListener("click", function () {
      showingTable = !showingTable;
      toggle.setAttribute("aria-pressed", String(showingTable));
      toggle.textContent = showingTable ? "Chart" : "Table";
      draw();
    });
    requestAnimationFrame(draw);
    onResize(draw);
    return card;
  }

  var resizeHandlers = [];
  function onResize(fn) { resizeHandlers.push(fn); }
  window.addEventListener("resize", debounce(function () {
    resizeHandlers.forEach(function (fn) { try { fn(); } catch (e) { /* gone */ } });
  }, 180));
  function debounce(fn, ms) {
    var timer;
    return function () {
      clearTimeout(timer);
      timer = setTimeout(fn, ms);
    };
  }

  function buildLegend(chart) {
    var many = (chart.series || []).length > 1;
    if (!many) return null;
    var shape = chart.type === "line" ? "line" : "rect";
    var legend = el("div", { class: "legend" });
    chart.series.forEach(function (series) {
      legend.appendChild(el("span", { class: "legend-item" }, [
        el("span", { class: "legend-swatch " + swatchClass(series), "data-shape": shape }),
        el("span", { text: series.name })
      ]));
    });
    return legend;
  }

  function chartTable(chart) {
    var columns = [{ key: "_c", label: "", format: "text" }];
    (chart.series || []).forEach(function (series, index) {
      columns.push({ key: "s" + index, label: series.name, format: chart.format });
    });
    var rows = (chart.categories || []).map(function (category, row) {
      var record = { _c: category };
      (chart.series || []).forEach(function (series, index) {
        record["s" + index] = series.values[row];
      });
      return record;
    });
    return tableNode({ columns: columns, rows: rows }, true);
  }

  function renderChart(chart, width) {
    width = Math.max(width, 320);
    switch (chart.type) {
      case "hbar": return renderHBar(chart, width);
      case "line": return renderLine(chart, width);
      case "waterfall": return renderWaterfall(chart, width);
      default: return renderBar(chart, width);
    }
  }

  // ---------------------------------------------------------------- bar chart
  function renderBar(chart, width) {
    var height = 280;
    var m = { top: 14, right: 18, bottom: 42, left: 62 };
    var series = chart.series || [];
    var categories = chart.categories || [];
    var plotW = width - m.left - m.right;
    var plotH = height - m.top - m.bottom;

    var lows = [0], highs = [0];
    if (chart.stacked) {
      categories.forEach(function (_, i) {
        var up = 0, down = 0;
        series.forEach(function (s) {
          var v = Number(s.values[i]) || 0;
          if (v >= 0) up += v; else down += v;
        });
        highs.push(up); lows.push(down);
      });
    } else {
      series.forEach(function (s) {
        s.values.forEach(function (v) {
          v = Number(v) || 0;
          highs.push(v); lows.push(v);
        });
      });
    }
    (chart.reference || []).forEach(function (r) { highs.push(r.value); lows.push(r.value); });
    var scale = niceTicks(Math.min.apply(null, lows), Math.max.apply(null, highs), 5);
    var y = function (v) {
      return m.top + plotH - (v - scale.min) / (scale.max - scale.min) * plotH;
    };
    var zero = y(0);
    var band = plotW / Math.max(categories.length, 1);
    var root = svg("svg", {
      class: "chart", viewBox: "0 0 " + width + " " + height,
      height: height, role: "img"
    });

    scale.ticks.forEach(function (t) {
      root.appendChild(svg("line", {
        class: t === 0 ? "axis-line" : "grid-line",
        x1: m.left, x2: width - m.right, y1: y(t), y2: y(t)
      }));
      root.appendChild(svg("text", {
        class: "axis-text", x: m.left - 8, y: y(t) + 4,
        "text-anchor": "end", text: format(t, chart.format)
      }));
    });

    var gap = 2;
    var groupCount = chart.stacked ? 1 : series.length;
    var slotW = Math.min(24, Math.max(4, (band * 0.7) / groupCount - (groupCount > 1 ? gap : 0)));
    var groupW = slotW * groupCount + gap * (groupCount - 1);

    categories.forEach(function (category, i) {
      var centre = m.left + band * (i + 0.5);
      if (chart.stacked) {
        var up = 0, down = 0;
        series.forEach(function (s) {
          var v = Number(s.values[i]) || 0;
          if (!v) return;
          var start = v >= 0 ? up : down;
          var end = start + v;
          var top = y(Math.max(start, end));
          var bottom = y(Math.min(start, end));
          var h = Math.max(bottom - top - gap, 0.5);
          root.appendChild(svg("path", {
            class: markClass(s),
            d: barPath(centre - slotW / 2, v >= 0 ? top : top + gap, slotW, h, 4, v >= 0)
          }));
          if (v >= 0) up = end; else down = end;
        });
      } else {
        series.forEach(function (s, j) {
          var v = Number(s.values[i]) || 0;
          var x = centre - groupW / 2 + j * (slotW + gap);
          var top = Math.min(y(v), zero);
          var h = Math.abs(y(v) - zero);
          root.appendChild(svg("path", {
            class: markClass(s), d: barPath(x, top, slotW, h, 4, v >= 0)
          }));
        });
      }
      // Only label as many ticks as the band width can hold without collisions.
      var perLabel = Math.max(1, Math.ceil((String(category).length * 6.4 + 8) / band));
      if (i % perLabel === 0) {
        root.appendChild(svg("text", {
          class: "axis-text", x: centre, y: height - m.bottom + 16,
          "text-anchor": "middle", text: String(category)
        }));
      }
    });

    (chart.reference || []).forEach(function (ref) {
      root.appendChild(svg("line", {
        class: "ref-line", x1: m.left, x2: width - m.right, y1: y(ref.value), y2: y(ref.value)
      }));
      if (ref.label) {
        root.appendChild(svg("text", {
          class: "ref-text", x: width - m.right, y: y(ref.value) - 4,
          "text-anchor": "end", text: ref.label
        }));
      }
    });

    if (chart.axis_label) {
      root.appendChild(svg("text", {
        class: "axis-text", x: m.left, y: height - 6, text: chart.axis_label
      }));
    }

    categories.forEach(function (category, i) {
      var hit = svg("rect", {
        class: "hit", x: m.left + band * i, y: m.top, width: band, height: plotH,
        tabindex: "0", role: "button",
        "aria-label": category + ": " + series.map(function (s) {
          return s.name + " " + format(s.values[i], chart.format);
        }).join(", ")
      });
      function show(ev) {
        var point = ev.touches ? ev.touches[0] : ev;
        var box = hit.getBoundingClientRect();
        showTip(point.clientX || box.left + box.width / 2,
          point.clientY || box.top, String(category),
          series.map(function (s) {
            return {
              label: s.name, swatch: swatchClass(s),
              value: format(s.values[i], chart.format, true)
            };
          }));
      }
      hit.addEventListener("pointermove", show);
      hit.addEventListener("focus", show);
      hit.addEventListener("pointerleave", hideTip);
      hit.addEventListener("blur", hideTip);
      root.appendChild(hit);
    });
    return root;
  }

  // --------------------------------------------------------- horizontal bars
  function renderHBar(chart, width) {
    var series = chart.series || [];
    var categories = chart.categories || [];
    var rowH = series.length > 1 ? 44 : 30;
    var m = { top: 8, right: 96, bottom: 26, left: Math.min(220, Math.max(110, width * 0.3)) };
    var height = m.top + m.bottom + rowH * Math.max(categories.length, 1);
    var plotW = width - m.left - m.right;

    var values = [0];
    series.forEach(function (s) {
      s.values.forEach(function (v) { values.push(Number(v) || 0); });
    });
    var scale = niceTicks(Math.min.apply(null, values), Math.max.apply(null, values), 4);
    var x = function (v) {
      return m.left + (v - scale.min) / (scale.max - scale.min) * plotW;
    };
    var zero = x(0);
    var root = svg("svg", {
      class: "chart", viewBox: "0 0 " + width + " " + height, height: height, role: "img"
    });

    scale.ticks.forEach(function (t) {
      root.appendChild(svg("line", {
        class: t === 0 ? "axis-line" : "grid-line",
        x1: x(t), x2: x(t), y1: m.top, y2: height - m.bottom
      }));
      root.appendChild(svg("text", {
        class: "axis-text", x: x(t), y: height - m.bottom + 15,
        "text-anchor": "middle", text: format(t, chart.format)
      }));
    });

    var gap = 2;
    var barH = Math.min(24, (rowH - 8) / series.length - (series.length > 1 ? gap : 0));
    categories.forEach(function (category, i) {
      var top = m.top + rowH * i + (rowH - (barH * series.length + gap * (series.length - 1))) / 2;
      root.appendChild(svg("text", {
        class: "axis-text", x: m.left - 10, y: m.top + rowH * i + rowH / 2 + 4,
        "text-anchor": "end",
        text: truncate(String(category), Math.max(8, Math.floor((m.left - 16) / 6.1)))
      }));
      series.forEach(function (s, j) {
        var v = Number(s.values[i]) || 0;
        var left = Math.min(x(v), zero);
        var w = Math.abs(x(v) - zero);
        var yPos = top + j * (barH + gap);
        root.appendChild(svg("path", {
          class: markClass(s),
          d: hBarPath(left, yPos, Math.max(w, 1), barH, 4, v >= 0)
        }));
        if (series.length === 1) {
          root.appendChild(svg("text", {
            class: "value-label",
            x: v >= 0 ? x(v) + 8 : x(v) - 8, y: yPos + barH / 2 + 4,
            "text-anchor": v >= 0 ? "start" : "end",
            text: format(v, chart.format)
          }));
        }
      });
      var hit = svg("rect", {
        class: "hit", x: m.left, y: m.top + rowH * i, width: plotW, height: rowH,
        tabindex: "0", role: "button",
        "aria-label": category + ": " + series.map(function (s) {
          return s.name + " " + format(s.values[i], chart.format);
        }).join(", ")
      });
      function show(ev) {
        var point = ev.touches ? ev.touches[0] : ev;
        var box = hit.getBoundingClientRect();
        showTip(point.clientX || box.left, point.clientY || box.top, String(category),
          series.map(function (s) {
            return {
              label: s.name, swatch: swatchClass(s),
              value: format(s.values[i], chart.format, true)
            };
          }));
      }
      hit.addEventListener("pointermove", show);
      hit.addEventListener("focus", show);
      hit.addEventListener("pointerleave", hideTip);
      hit.addEventListener("blur", hideTip);
      root.appendChild(hit);
    });
    return root;
  }

  function hBarPath(x, y, w, h, r, right) {
    r = Math.max(0, Math.min(r, h / 2, w));
    if (right) {
      return "M" + x + "," + y + "h" + (w - r) +
        "a" + r + "," + r + " 0 0 1 " + r + "," + r +
        "v" + (h - 2 * r) +
        "a" + r + "," + r + " 0 0 1 " + -r + "," + r +
        "H" + x + "Z";
    }
    return "M" + (x + w) + "," + y + "H" + (x + r) +
      "a" + r + "," + r + " 0 0 0 " + -r + "," + r +
      "v" + (h - 2 * r) +
      "a" + r + "," + r + " 0 0 0 " + r + "," + r +
      "H" + (x + w) + "Z";
  }

  function truncate(text, max) {
    return text.length > max ? text.slice(0, max - 1) + "…" : text;
  }

  // --------------------------------------------------------------- line chart
  function renderLine(chart, width) {
    var height = 280;
    var m = { top: 16, right: 64, bottom: 42, left: 62 };
    var series = chart.series || [];
    var categories = chart.categories || [];
    var plotW = width - m.left - m.right;
    var plotH = height - m.top - m.bottom;

    var values = [];
    series.forEach(function (s) {
      s.values.forEach(function (v) { if (v !== null && v !== undefined) values.push(Number(v)); });
    });
    (chart.reference || []).forEach(function (r) { values.push(r.value); });
    if (!values.length) values = [0, 1];
    var scale = niceTicks(Math.min.apply(null, values), Math.max.apply(null, values), 5);
    var x = function (i) {
      return m.left + (categories.length <= 1 ? plotW / 2
        : (i / (categories.length - 1)) * plotW);
    };
    var y = function (v) {
      return m.top + plotH - (v - scale.min) / (scale.max - scale.min) * plotH;
    };
    var root = svg("svg", {
      class: "chart", viewBox: "0 0 " + width + " " + height, height: height, role: "img"
    });

    scale.ticks.forEach(function (t) {
      root.appendChild(svg("line", {
        class: t === 0 ? "axis-line" : "grid-line",
        x1: m.left, x2: width - m.right, y1: y(t), y2: y(t)
      }));
      root.appendChild(svg("text", {
        class: "axis-text", x: m.left - 8, y: y(t) + 4, "text-anchor": "end",
        text: format(t, chart.format)
      }));
    });

    var every = Math.max(1, Math.ceil(categories.length / 12));
    categories.forEach(function (category, i) {
      if (i % every && i !== categories.length - 1) return;
      root.appendChild(svg("text", {
        class: "axis-text", x: x(i), y: height - m.bottom + 16,
        "text-anchor": "middle", text: String(category)
      }));
    });

    (chart.reference || []).forEach(function (ref) {
      root.appendChild(svg("line", {
        class: "ref-line", x1: m.left, x2: width - m.right,
        y1: y(ref.value), y2: y(ref.value)
      }));
      if (ref.label) {
        root.appendChild(svg("text", {
          class: "ref-text", x: m.left + 4, y: y(ref.value) - 4, text: ref.label
        }));
      }
    });

    var crosshair = svg("line", {
      class: "crosshair", x1: 0, x2: 0, y1: m.top, y2: m.top + plotH, opacity: "0"
    });
    root.appendChild(crosshair);

    series.forEach(function (s) {
      var d = "", started = false, last = -1;
      s.values.forEach(function (v, i) {
        if (v === null || v === undefined) return;
        d += (started ? "L" : "M") + x(i) + "," + y(Number(v));
        started = true;
        last = i;
      });
      if (!started) return;
      root.appendChild(svg("path", {
        class: "line-" + (s.role ? s.role : "s" + (s.slot || 1)), d: d,
        fill: "none", "stroke-width": "2", "stroke-linejoin": "round",
        "stroke-linecap": "round"
      }));
      root.appendChild(svg("circle", {
        class: markClass(s) + " mark-ring", cx: x(last), cy: y(Number(s.values[last])), r: 4.5
      }));
      if (series.length <= 2) {
        root.appendChild(svg("text", {
          class: "value-label", x: x(last) + 9, y: y(Number(s.values[last])) + 4,
          text: format(s.values[last], chart.format)
        }));
      }
    });

    var hit = svg("rect", {
      class: "hit", x: m.left, y: m.top, width: plotW, height: plotH, tabindex: "0"
    });
    hit.addEventListener("pointermove", function (ev) {
      var box = root.getBoundingClientRect();
      var ratio = width / box.width;
      var local = (ev.clientX - box.left) * ratio;
      var index = Math.round((local - m.left) / (plotW || 1) * (categories.length - 1));
      index = Math.max(0, Math.min(categories.length - 1, index));
      crosshair.setAttribute("x1", x(index));
      crosshair.setAttribute("x2", x(index));
      crosshair.setAttribute("opacity", "1");
      showTip(ev.clientX, ev.clientY, String(categories[index]),
        series.map(function (s) {
          return {
            label: s.name, swatch: swatchClass(s),
            value: format(s.values[index], chart.format, true)
          };
        }));
    });
    hit.addEventListener("pointerleave", function () {
      crosshair.setAttribute("opacity", "0");
      hideTip();
    });
    root.appendChild(hit);
    if (chart.axis_label) {
      root.appendChild(svg("text", {
        class: "axis-text", x: m.left, y: height - 6, text: chart.axis_label
      }));
    }
    return root;
  }

  // ---------------------------------------------------------------- waterfall
  function renderWaterfall(chart, width) {
    var height = 290;
    var m = { top: 16, right: 18, bottom: 52, left: 66 };
    var categories = chart.categories || [];
    var values = (chart.series && chart.series[0] ? chart.series[0].values : []) || [];
    var kinds = chart.kinds || [];
    var plotW = width - m.left - m.right;
    var plotH = height - m.top - m.bottom;

    var steps = [], running = 0, extent = [0];
    values.forEach(function (raw, i) {
      var v = Number(raw) || 0;
      var isTotal = kinds[i] === "total";
      var start = isTotal ? 0 : running;
      var end = isTotal ? v : running + v;
      steps.push({ start: start, end: end, value: v, total: isTotal });
      running = end;
      extent.push(start, end);
    });
    var scale = niceTicks(Math.min.apply(null, extent), Math.max.apply(null, extent), 5);
    var y = function (v) {
      return m.top + plotH - (v - scale.min) / (scale.max - scale.min) * plotH;
    };
    var band = plotW / Math.max(steps.length, 1);
    var barW = Math.min(48, band * 0.6);
    var root = svg("svg", {
      class: "chart", viewBox: "0 0 " + width + " " + height, height: height, role: "img"
    });

    scale.ticks.forEach(function (t) {
      root.appendChild(svg("line", {
        class: t === 0 ? "axis-line" : "grid-line",
        x1: m.left, x2: width - m.right, y1: y(t), y2: y(t)
      }));
      root.appendChild(svg("text", {
        class: "axis-text", x: m.left - 8, y: y(t) + 4, "text-anchor": "end",
        text: format(t, chart.format)
      }));
    });

    steps.forEach(function (step, i) {
      var centre = m.left + band * (i + 0.5);
      var top = Math.min(y(step.start), y(step.end));
      var h = Math.max(Math.abs(y(step.end) - y(step.start)), 1);
      var rising = step.end >= step.start;
      var cls = step.total ? "mark-total" : (step.value >= 0 ? "mark-s1" : "mark-s8");
      root.appendChild(svg("path", {
        class: cls, d: barPath(centre - barW / 2, top, barW, h, 4, rising)
      }));
      if (i < steps.length - 1) {
        root.appendChild(svg("line", {
          class: "grid-line", x1: centre + barW / 2, x2: m.left + band * (i + 1.5) - barW / 2,
          y1: y(step.end), y2: y(step.end)
        }));
      }
      root.appendChild(svg("text", {
        class: "value-label", x: centre, y: top - 6, "text-anchor": "middle",
        text: format(step.value, chart.format)
      }));
      wrapLabel(root, String(categories[i] || ""), centre, height - m.bottom + 16, band);

      var hit = svg("rect", {
        class: "hit", x: m.left + band * i, y: m.top, width: band, height: plotH,
        tabindex: "0",
        "aria-label": categories[i] + ": " + format(step.value, chart.format)
      });
      function show(ev) {
        showTip(ev.clientX || 0, ev.clientY || 0, String(categories[i]), [
          { label: step.total ? "Total" : "Change", value: format(step.value, chart.format, true) },
          { label: "Running", value: format(step.end, chart.format, true) }
        ]);
      }
      hit.addEventListener("pointermove", show);
      hit.addEventListener("focus", show);
      hit.addEventListener("pointerleave", hideTip);
      hit.addEventListener("blur", hideTip);
      root.appendChild(hit);
    });
    return root;
  }

  function wrapLabel(root, text, x, y, maxWidth) {
    var words = text.split(" ");
    var lines = [];
    var current = "";
    var perLine = Math.max(6, Math.floor(maxWidth / 6.5));
    words.forEach(function (word) {
      if ((current + " " + word).trim().length > perLine) {
        if (current) lines.push(current);
        current = word;
      } else {
        current = (current + " " + word).trim();
      }
    });
    if (current) lines.push(current);
    lines.slice(0, 2).forEach(function (line, i) {
      root.appendChild(svg("text", {
        class: "axis-text", x: x, y: y + i * 13, "text-anchor": "middle", text: line
      }));
    });
  }

  // ------------------------------------------------------------------- tables
  function tableNode(table, compactMode) {
    var wrap = el("div", { class: "table-wrap" });
    var node = el("table");
    var head = el("tr");
    (table.columns || []).forEach(function (column) {
      head.appendChild(el("th", {
        class: isNumericFormat(column.format) ? "num" : "", text: column.label
      }));
    });
    node.appendChild(el("thead", null, [head]));

    var body = el("tbody");
    (table.rows || []).forEach(function (row) {
      var tr = el("tr");
      (table.columns || []).forEach(function (column) {
        var value = row[column.key];
        var numeric = isNumericFormat(column.format);
        var cell = el("td", {
          class: (numeric ? "num " : "") + (numeric && value < 0 ? "neg" : ""),
          text: format(value, column.format, !compactMode)
        });
        tr.appendChild(cell);
      });
      body.appendChild(tr);
    });
    node.appendChild(body);

    if (table.total) {
      var foot = el("tr");
      (table.columns || []).forEach(function (column) {
        var value = table.total[column.key];
        foot.appendChild(el("td", {
          class: isNumericFormat(column.format) ? "num" : "",
          text: value === undefined ? "" : format(value, column.format, true)
        }));
      });
      node.appendChild(el("tfoot", null, [foot]));
    }
    wrap.appendChild(node);
    return wrap;
  }

  function tableCard(table) {
    var card = el("div", { class: "card" }, [el("h3", { text: table.title || "" })]);
    if (table.note) card.appendChild(el("p", { class: "card-note", text: table.note }));
    card.appendChild(tableNode(table));
    if (!(table.rows || []).length) {
      card.appendChild(el("p", { class: "empty", text: "Nothing to show." }));
    }
    return card;
  }

  // -------------------------------------------------------------- panel parts
  function heroCard(hero, historyId) {
    var card = el("div", { class: "card" });
    var block = el("div", { class: "hero", "data-status": hero.status || "neutral" }, [
      el("div", null, [
        el("div", { class: "hero-label", text: hero.label }),
        el("div", { class: "hero-value", text: format(hero.value, hero.format) })
      ]),
      hero.status && hero.status !== "neutral"
        ? el("span", { class: "status-chip", "data-status": hero.status, text: hero.status })
        : null,
      hero.caption ? el("span", { class: "hero-caption", text: hero.caption }) : null
    ]);
    card.appendChild(block);
    if (historyId) {
      var slot = el("div");
      card.appendChild(slot);
      get("api/history/" + encodeURIComponent(historyId) + "/hero").then(function (data) {
        var points = (data.points || []).filter(function (p) { return p.value !== null; });
        if (points.length < 2) return;
        slot.appendChild(el("p", { class: "card-note", text: "History" }));
        var chart = {
          type: "line", title: "", format: hero.format,
          categories: points.map(function (p) { return p.as_of.slice(5); }),
          series: [{ name: hero.label, values: points.map(function (p) { return p.value; }), slot: 1 }]
        };
        slot.appendChild(renderChart(chart, slot.clientWidth || 460));
      });
    }
    return card;
  }

  function kpiGrid(kpis) {
    var grid = el("div", { class: "kpis" });
    kpis.forEach(function (kpi) {
      grid.appendChild(el("div", { class: "kpi", "data-status": kpi.status || "neutral" }, [
        el("div", { class: "kpi-label", text: kpi.label }),
        el("div", { class: "kpi-value", text: format(kpi.value, kpi.format) }),
        kpi.hint ? el("div", { class: "kpi-hint", text: kpi.hint }) : null
      ]));
    });
    return grid;
  }

  function notesCard(notes) {
    var list = el("ul");
    notes.filter(Boolean).forEach(function (note) {
      list.appendChild(el("li", { text: note }));
    });
    return el("div", { class: "card notes" }, [
      el("h3", { text: "Notes and assumptions" }), list
    ]);
  }

  function renderPanel(panel) {
    var content = $("content");
    clear(content);
    $("panel-title").textContent = panel.title || "";
    $("panel-subtitle").textContent = panel.subtitle || "";

    if (panel.error) {
      content.appendChild(el("div", { class: "card error" }, [
        el("strong", { text: "This panel could not be computed" }),
        el("p", { text: panel.error }),
        panel.traceback ? el("pre", { text: panel.traceback }) : null
      ]));
      (panel.notes || []).forEach(function (note) {
        content.appendChild(el("p", { class: "notes", text: note }));
      });
      return;
    }

    if (panel.hero) content.appendChild(heroCard(panel.hero, panel.id));
    if ((panel.kpis || []).length) content.appendChild(kpiGrid(panel.kpis));
    if (panel.map) content.appendChild(mapCard(panel.map));

    if ((panel.charts || []).length) {
      var charts = el("div", { class: "charts" });
      panel.charts.forEach(function (chart) { charts.appendChild(chartCard(chart)); });
      content.appendChild(charts);
    }
    (panel.tables || []).forEach(function (table) {
      content.appendChild(tableCard(table));
    });
    if ((panel.notes || []).filter(Boolean).length) {
      content.appendChild(notesCard(panel.notes));
    }
    renderParams(panel.params);
  }

  function renderParams(params) {
    var control = $("currency-control");
    var select = $("currency-select");
    if (!params || !params.currency) {
      control.hidden = true;
      return;
    }
    control.hidden = false;
    clear(select);
    (params.currency.options || []).forEach(function (option) {
      var node = el("option", { value: option, text: option === "ALL" ? "All (base)" : option });
      if (option === params.currency.value) node.selected = true;
      select.appendChild(node);
    });
  }

  // ---------------------------------------------------------------------- map
  var ROBINSON_X = [1, 0.9986, 0.9954, 0.99, 0.9822, 0.973, 0.96, 0.9427, 0.9216,
    0.8962, 0.8679, 0.835, 0.7986, 0.7597, 0.7186, 0.6732, 0.6213, 0.5722, 0.5322];
  var ROBINSON_Y = [0, 0.062, 0.124, 0.186, 0.248, 0.31, 0.372, 0.434, 0.4958,
    0.5571, 0.6176, 0.6769, 0.7346, 0.7903, 0.8435, 0.8936, 0.9394, 0.9761, 1];

  function robinson(lon, lat) {
    var sign = lat < 0 ? -1 : 1;
    var abs = Math.min(Math.abs(lat), 90);
    var i = Math.min(Math.floor(abs / 5), 17);
    var t = (abs - i * 5) / 5;
    var ax = ROBINSON_X[i] + (ROBINSON_X[i + 1] - ROBINSON_X[i]) * t;
    var by = ROBINSON_Y[i] + (ROBINSON_Y[i + 1] - ROBINSON_Y[i]) * t;
    return [0.8487 * ax * (lon * Math.PI / 180), -1.3523 * by * sign];
  }

  var MAP_W = 2 * 0.8487 * Math.PI;
  var MAP_H = 2 * 1.3523;

  function mapCard(map) {
    var card = el("div", { class: "card map-card" });
    var toolbar = el("div", { class: "map-toolbar" });
    var wrap = el("div", { class: "map-wrap" });
    var legend = el("div", { class: "map-legend" });

    var colourBtn = el("button", {
      class: "toggle", type: "button", "aria-pressed": "false",
      text: "Shade countries by exposure"
    });
    colourBtn.addEventListener("click", function () {
      state.mapColour = state.mapColour === "type" ? "exposure" : "type";
      colourBtn.setAttribute("aria-pressed", String(state.mapColour === "exposure"));
      draw();
    });
    var resetBtn = el("button", { class: "toggle", type: "button", text: "Reset view" });
    resetBtn.addEventListener("click", function () {
      state.transform = { k: 1, x: 0, y: 0 };
      draw();
    });
    toolbar.appendChild(colourBtn);
    toolbar.appendChild(resetBtn);
    toolbar.appendChild(el("span", {
      class: "scale",
      text: "Drag to pan · scroll to zoom · click a country for its overlay"
    }));

    card.appendChild(el("h3", { text: "Client map" }));
    card.appendChild(toolbar);
    card.appendChild(wrap);
    card.appendChild(legend);

    function draw() {
      clear(wrap);
      clear(legend);
      var width = Math.max(wrap.clientWidth || card.clientWidth - 40, 420);
      wrap.appendChild(buildMap(map, width, draw));
      buildMapLegend(map, legend);
    }

    if (!state.world) {
      wrap.appendChild(el("p", { class: "empty", text: "Loading world outline…" }));
      get("world.geo.json").then(function (data) {
        state.world = data;
        draw();
      }).catch(function () {
        clear(wrap);
        wrap.appendChild(el("p", {
          class: "empty",
          text: "world.geo.json is missing — run scripts/build_world_map.py to rebuild it."
        }));
      });
    } else {
      requestAnimationFrame(draw);
    }
    onResize(function () { if (state.world) draw(); });
    return card;
  }

  function exposureBands(map) {
    var values = Object.keys(map.countries).map(function (iso) {
      return map.countries[iso].funding + map.countries[iso].exposure;
    }).filter(function (v) { return v > 0; }).sort(function (a, b) { return a - b; });
    if (!values.length) return [];
    var bands = [];
    for (var i = 1; i <= 4; i++) bands.push(values[Math.floor(values.length * i / 5)]);
    return bands;
  }

  function typeSlots(map) {
    var counts = {};
    (map.points || []).forEach(function (p) {
      counts[p.type] = (counts[p.type] || 0) + 1;
    });
    var ordered = Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a]; });
    var slots = {};
    // Three categorical slots validate all-pairs; the tail folds into "Other".
    ordered.slice(0, 3).forEach(function (type, i) { slots[type] = i + 1; });
    return { slots: slots, other: ordered.slice(3) };
  }

  function buildMap(map, width, redraw) {
    var height = Math.round(width * (MAP_H / MAP_W) * 1.02);
    var root = svg("svg", {
      class: "map", viewBox: "0 0 " + width + " " + height, height: height,
      role: "img", "aria-label": "World map of client locations"
    });
    var scale = width / MAP_W;
    var group = svg("g", {
      transform: "translate(" + state.transform.x + "," + state.transform.y +
        ") scale(" + state.transform.k + ")"
    });
    root.appendChild(group);

    var project = function (lon, lat) {
      var p = robinson(lon, lat);
      return [(p[0] + MAP_W / 2) * scale, (p[1] + MAP_H / 2) * scale];
    };

    var bands = exposureBands(map);
    function quantile(value) {
      if (!value) return null;
      for (var i = 0; i < bands.length; i++) if (value <= bands[i]) return i;
      return 4;
    }

    (state.world.features || []).forEach(function (feature) {
      var iso = feature.properties.iso2;
      var stats = map.countries[iso];
      var d = "";
      feature.geometry.coordinates.forEach(function (polygon) {
        polygon.forEach(function (ring) {
          ring.forEach(function (point, i) {
            var p = project(point[0], point[1]);
            d += (i ? "L" : "M") + p[0].toFixed(1) + "," + p[1].toFixed(1);
          });
          d += "Z";
        });
      });
      var q = state.mapColour === "exposure" && stats
        ? quantile(stats.funding + stats.exposure) : null;
      var path = svg("path", {
        class: "country" + (stats ? " interactive" : "") +
          (state.selectedCountry === iso ? " selected" : ""),
        d: d, "data-q": q === null ? null : String(q)
      });
      if (stats) {
        path.setAttribute("tabindex", "0");
        path.setAttribute("role", "button");
        path.setAttribute("aria-label", feature.properties.name + ", " +
          stats.clients + " clients");
        var openIt = function () { openCountry(iso, redraw); };
        path.addEventListener("click", openIt);
        path.addEventListener("keydown", function (ev) {
          if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); openIt(); }
        });
        path.addEventListener("pointermove", function (ev) {
          showTip(ev.clientX, ev.clientY, feature.properties.name, [
            { label: "Clients", value: format(stats.clients, "number") },
            { label: "Funding", value: format(stats.funding, "money", true) },
            { label: "Exposure", value: format(stats.exposure, "money", true) }
          ]);
        });
        path.addEventListener("pointerleave", hideTip);
      }
      group.appendChild(path);
    });

    var types = typeSlots(map);
    var maxTotal = Math.max.apply(null,
      (map.points || []).map(function (p) { return p.total || 0; }).concat([1]));
    (map.points || []).slice().sort(function (a, b) { return b.total - a.total; })
      .forEach(function (point) {
        if (point.lat === null || point.lat === undefined) return;
        var p = project(point.lon, point.lat);
        var r = 4 + 18 * Math.sqrt(Math.max(point.total, 0) / maxTotal);
        var slot = types.slots[point.type];
        var bubble = svg("circle", {
          class: "bubble " + (slot ? "mark-s" + slot : "mark-neutral") +
            (point.breach ? " breach" : ""),
          cx: p[0], cy: p[1], r: r / state.transform.k, "fill-opacity": "0.82",
          tabindex: "0", role: "button",
          "aria-label": point.name + ", " + point.country + ", total business " +
            format(point.total, "money", true)
        });
        function show(ev) {
          var rows = [
            { label: "Funding", value: format(point.funding, "money", true) },
            { label: "Lending", value: format(point.lending, "money", true) },
            { label: "Undrawn", value: format(point.undrawn, "money", true) },
            { label: "Deals", value: format(point.deals, "number") }
          ];
          if (point.utilisation !== null && point.utilisation !== undefined) {
            rows.push({ label: "Limit used", value: format(point.utilisation, "pct") });
          }
          if (point.breach) rows.push({ label: "Status", value: "over limit" });
          var box = bubble.getBoundingClientRect();
          showTip(ev.clientX || box.left, ev.clientY || box.top,
            point.name + " · " + (point.city || point.country || point.type), rows);
        }
        bubble.addEventListener("pointermove", show);
        bubble.addEventListener("focus", show);
        bubble.addEventListener("pointerleave", hideTip);
        bubble.addEventListener("blur", hideTip);
        bubble.addEventListener("click", function () {
          if (point.iso2) openCountry(point.iso2, redraw);
        });
        group.appendChild(bubble);
      });

    // pan + zoom
    var dragging = false, originX = 0, originY = 0;
    root.addEventListener("pointerdown", function (ev) {
      dragging = true;
      originX = ev.clientX - state.transform.x;
      originY = ev.clientY - state.transform.y;
      root.classList.add("dragging");
      root.setPointerCapture(ev.pointerId);
    });
    root.addEventListener("pointermove", function (ev) {
      if (!dragging) return;
      state.transform.x = ev.clientX - originX;
      state.transform.y = ev.clientY - originY;
      group.setAttribute("transform", "translate(" + state.transform.x + "," +
        state.transform.y + ") scale(" + state.transform.k + ")");
    });
    ["pointerup", "pointercancel", "pointerleave"].forEach(function (name) {
      root.addEventListener(name, function () {
        if (!dragging) return;
        dragging = false;
        root.classList.remove("dragging");
      });
    });
    root.addEventListener("wheel", function (ev) {
      ev.preventDefault();
      var box = root.getBoundingClientRect();
      var ratio = width / box.width;
      var px = (ev.clientX - box.left) * ratio;
      var py = (ev.clientY - box.top) * ratio;
      var factor = ev.deltaY < 0 ? 1.2 : 1 / 1.2;
      var next = Math.max(1, Math.min(12, state.transform.k * factor));
      factor = next / state.transform.k;
      state.transform.x = px - (px - state.transform.x) * factor;
      state.transform.y = py - (py - state.transform.y) * factor;
      state.transform.k = next;
      redraw();
    }, { passive: false });

    return root;
  }

  function buildMapLegend(map, legend) {
    if (state.mapColour === "exposure") {
      var bar = el("span", { class: "scale" }, [el("span", { text: "Lower" })]);
      var strip = el("span", { class: "scale-bar" });
      strip.style.background =
        "linear-gradient(90deg, var(--seq-100), var(--seq-250), var(--seq-400), " +
        "var(--seq-550), var(--seq-700))";
      bar.appendChild(strip);
      bar.appendChild(el("span", { text: "Higher total business" }));
      legend.appendChild(bar);
      return;
    }
    var types = typeSlots(map);
    Object.keys(types.slots).forEach(function (type) {
      legend.appendChild(el("span", { class: "legend-item" }, [
        el("span", { class: "legend-swatch sw-s" + types.slots[type] }),
        el("span", { text: prettyType(type) })
      ]));
    });
    if (types.other.length) {
      legend.appendChild(el("span", { class: "legend-item" }, [
        el("span", { class: "legend-swatch sw-neutral" }),
        el("span", { text: "Other (" + types.other.map(prettyType).join(", ") + ")" })
      ]));
    }
    legend.appendChild(el("span", { class: "legend-item" }, [
      el("span", { class: "legend-swatch sw-breach" }),
      el("span", { text: "Ringed = over its limit" })
    ]));
    legend.appendChild(el("span", {
      class: "scale", text: "Bubble area = funding + exposure"
    }));
  }

  var ACRONYMS = { FI: 1, SME: 1, PSE: 1, HQLA: 1, NOP: 1 };
  function prettyType(type) {
    var raw = String(type || "");
    if (ACRONYMS[raw.toUpperCase()]) return raw.toUpperCase();
    return raw.toLowerCase().replace(/_/g, " ")
      .replace(/^./, function (c) { return c.toUpperCase(); });
  }

  // ------------------------------------------------------------------- drawer
  function openCountry(iso2, redraw) {
    state.selectedCountry = iso2;
    if (redraw) redraw();
    var drawer = $("drawer");
    var body = $("drawer-body");
    drawer.hidden = false;
    drawer.setAttribute("aria-hidden", "false");
    $("scrim").hidden = false;
    clear(body);
    body.appendChild(el("p", { class: "empty", text: "Loading…" }));
    $("drawer-title").textContent = iso2;
    $("drawer-sub").textContent = "";
    get("api/country/" + encodeURIComponent(iso2)).then(function (data) {
      $("drawer-title").textContent = data.name || iso2;
      $("drawer-sub").textContent = [data.iso2, data.currency].filter(Boolean).join(" · ");
      clear(body);
      (data.sections || []).forEach(function (section) {
        body.appendChild(renderSection(section));
      });
      if (!(data.sections || []).length) {
        body.appendChild(el("p", { class: "empty", text: "Nothing recorded here yet." }));
      }
    });
  }

  function closeDrawer() {
    $("drawer").hidden = true;
    $("drawer").setAttribute("aria-hidden", "true");
    $("scrim").hidden = true;
    state.selectedCountry = "";
  }

  function renderSection(section) {
    var node = el("div", { class: "section" }, [el("h4", { text: section.title })]);
    if (section.kind === "kv") {
      var list = el("dl", { class: "kv" });
      (section.pairs || []).forEach(function (pair) {
        list.appendChild(el("dt", { text: pair.label }));
        list.appendChild(el("dd", {
          text: pair.format ? format(pair.value, pair.format, true) : String(pair.value)
        }));
      });
      node.appendChild(list);
    } else if (section.kind === "list") {
      var items = el("ul");
      (section.items || []).forEach(function (item) {
        items.appendChild(el("li", { text: item }));
      });
      node.appendChild(items);
    } else if (section.kind === "table") {
      node.appendChild(tableNode(section));
    } else if (section.kind === "meter") {
      var pct = section.ratio === null || section.ratio === undefined
        ? null : Math.max(0, Math.min(1, section.ratio));
      var status = section.ratio === null || section.ratio === undefined ? ""
        : section.ratio > 1 ? "critical" : section.ratio > 0.8 ? "warning" : "";
      node.appendChild(el("div", { class: "meter-row" }, [
        el("span", { text: section.label }),
        el("span", {
          text: format(section.used, section.format, true) +
            (section.limit ? " of " + format(section.limit, section.format, true) : " (no limit)")
        })
      ]));
      var track = el("div", { class: "meter-track" });
      var fill = el("div", { class: "meter-fill", "data-status": status });
      fill.style.width = (pct === null ? 0 : pct * 100) + "%";
      track.appendChild(fill);
      node.appendChild(track);
    }
    if (section.note) node.appendChild(el("p", { class: "note", text: section.note }));
    return node;
  }

  // ---------------------------------------------------------------- navigation
  function buildNav(meta) {
    var nav = $("nav");
    clear(nav);
    var overview = el("button", {
      class: "nav-item", type: "button", "data-view": "overview",
      "aria-current": String(state.view === "overview")
    }, [el("span", { text: "Overview" }), el("span", { class: "dot" })]);
    overview.addEventListener("click", function () { show("overview"); });
    nav.appendChild(overview);

    var groups = {};
    (meta.metrics || []).forEach(function (item) {
      (groups[item.group] = groups[item.group] || []).push(item);
    });
    Object.keys(groups).forEach(function (group) {
      nav.appendChild(el("div", { class: "nav-group", text: group }));
      groups[group].forEach(function (item) {
        var button = el("button", {
          class: "nav-item", type: "button", "data-view": item.id,
          "aria-current": String(state.view === item.id),
          title: item.available ? item.subtitle : "Needs " + item.missing.join(", ")
        }, [
          el("span", { text: item.title }),
          el("span", { class: "dot" })
        ]);
        if (!item.available) button.setAttribute("disabled", "");
        button.addEventListener("click", function () { show(item.id); });
        nav.appendChild(button);
      });
    });
    highlightNav(state.view);
  }

  function show(view) {
    state.view = view;
    var content = $("content");
    content.dataset.loading = "true";
    hideTip();
    if (view === "overview") {
      $("currency-control").hidden = true;
      get("api/overview").then(function (data) {
        content.dataset.loading = "false";
        renderOverview(data);
        highlightNav(view);
      });
      return;
    }
    var query = "";
    if (state.params.currency && view === "cash_projection") {
      query = "?currency=" + encodeURIComponent(state.params.currency);
    }
    get("api/metric/" + encodeURIComponent(view) + query).then(function (panel) {
      content.dataset.loading = "false";
      state.panel = panel;
      renderPanel(panel);
      highlightNav(view);
    });
  }

  function setNavStatus(id, status) {
    var node = $("nav").querySelector('[data-view="' + id + '"] .dot');
    if (node) node.setAttribute("data-status", status || "");
  }

  function highlightNav(view) {
    Array.prototype.forEach.call($("nav").querySelectorAll(".nav-item"), function (node) {
      node.setAttribute("aria-current", String(node.dataset.view === view));
    });
  }

  function renderOverview(data) {
    var content = $("content");
    clear(content);
    $("panel-title").textContent = "Overview";
    $("panel-subtitle").textContent = state.meta
      ? state.meta.entity + " · as at " + state.meta.as_of +
        " · reported in " + state.meta.base_currency
      : "";
    if (data.error) {
      content.appendChild(el("div", { class: "card error" }, [
        el("strong", { text: "No data loaded" }), el("p", { text: data.error })
      ]));
      return;
    }
    if (!(data.cards || []).length) {
      content.appendChild(el("div", { class: "card" }, [
        el("h3", { text: "Nothing to show yet" }),
        el("p", {
          text: "Drop your spreadsheets into the data folder and press Reload data. " +
            "The Data sources panel explains what was understood."
        })
      ]));
      return;
    }
    var grid = el("div", { class: "overview" });
    data.cards.forEach(function (card) {
      setNavStatus(card.id, card.hero ? card.hero.status : "");
      var node = el("button", { class: "card ov-card", type: "button" }, [
        el("div", { class: "kpi-label", text: card.title })
      ]);
      if (card.hero) {
        node.appendChild(el("div", {
          class: "ov-hero", text: format(card.hero.value, card.hero.format)
        }));
        node.appendChild(el("div", { class: "kpi-hint", text: card.hero.label }));
        if (card.hero.status && card.hero.status !== "neutral") {
          node.appendChild(el("span", {
            class: "status-chip", "data-status": card.hero.status, text: card.hero.status
          }));
        }
      }
      var list = el("dl", { class: "ov-kpis" });
      (card.kpis || []).forEach(function (kpi) {
        list.appendChild(el("dt", { text: kpi.label }));
        list.appendChild(el("dd", { text: format(kpi.value, kpi.format) }));
      });
      node.appendChild(list);
      node.addEventListener("click", function () { show(card.id); });
      grid.appendChild(node);
    });
    content.appendChild(grid);
  }

  // --------------------------------------------------------------------- boot
  function applyTheme(mode) {
    document.documentElement.setAttribute("data-theme", mode);
    $("theme-label").textContent = mode === "auto" ? "Auto" : mode === "dark" ? "Dark" : "Light";
    try { localStorage.setItem("treasury-theme", mode); } catch (e) { /* private mode */ }
  }

  function renderState(meta) {
    state.meta = meta;
    if (!meta.ok) {
      $("asof").textContent = "load failed";
      $("content").appendChild(el("div", { class: "card error" }, [
        el("strong", { text: "Could not load the data folder" }),
        el("pre", { text: meta.error || "" })
      ]));
      return;
    }
    $("entity").textContent = meta.entity;
    $("asof").textContent = "as at " + meta.as_of + " · " + meta.base_currency;
    var counts = $("counts");
    clear(counts);
    Object.keys(meta.counts).forEach(function (name) {
      counts.appendChild(el("span", { text: name.replace(/_/g, " ") + " " + meta.counts[name] }));
    });
    var warnings = $("warnings");
    clear(warnings);
    if ((meta.warnings || []).length) {
      warnings.hidden = false;
      warnings.appendChild(el("strong", { text: "Check the data" }));
      var list = el("ul");
      meta.warnings.forEach(function (warning) {
        list.appendChild(el("li", { text: warning }));
      });
      warnings.appendChild(list);
    } else {
      warnings.hidden = true;
    }
    buildNav(meta);
  }

  function boot() {
    var saved = "auto";
    try { saved = localStorage.getItem("treasury-theme") || "auto"; } catch (e) { /* ignore */ }
    applyTheme(saved);
    $("theme").addEventListener("click", function () {
      var order = ["auto", "light", "dark"];
      var current = document.documentElement.getAttribute("data-theme") || "auto";
      applyTheme(order[(order.indexOf(current) + 1) % order.length]);
      if (state.view !== "overview") show(state.view);
    });
    $("reload").addEventListener("click", function (ev) {
      var button = ev.currentTarget;
      button.dataset.busy = "true";
      post("api/reload").then(function (meta) {
        button.dataset.busy = "false";
        renderState(meta);
        show(state.view);
      });
    });
    $("snapshot").addEventListener("click", function (ev) {
      var button = ev.currentTarget;
      button.dataset.busy = "true";
      post("api/snapshot").then(function (result) {
        button.dataset.busy = "false";
        button.textContent = result.error ? "Snapshot failed" : "Snapshot saved";
        setTimeout(function () { button.textContent = "Snapshot"; }, 2500);
      });
    });
    $("currency-select").addEventListener("change", function (ev) {
      state.params.currency = ev.target.value;
      show(state.view);
    });
    $("drawer-close").addEventListener("click", closeDrawer);
    $("scrim").addEventListener("click", closeDrawer);
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") closeDrawer();
    });

    get("api/state").then(function (meta) {
      renderState(meta);
      show("overview");
    });
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
