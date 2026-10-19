/* Tallymark: "View BoC" panel.
 *
 * Self-contained, no dependencies. Shows the Bill of Characteristics a run was checked
 * against, with a per-row status derived from the run's own data:
 *
 *   GET /runs/{id}                        filename, simulated flag, sheets checked, status
 *   GET /runs/{id}/rows                   BoC rows (char_no, sheet, zone, requirement, places, bbox, bbox_source)
 *   GET /runs/{id}/flags                  flags (nearest_char_no for unaccounted, char_no for places)
 *   GET /runs/{id}/sheets/{n}/coverage.json   covered regions with the char_nos that cover them
 *
 * API:
 *   TallyBocView.open({ runId, onLocate, activeCharNo?, closeOnLocate?, apiBase?, filter? })  -> Promise
 *     filter: optional starting status filter, one of "all" | "covered" | "missing" | "flagged"
 *   TallyBocView.close()
 *   TallyBocView.isOpen() -> boolean
 *
 * onLocate({ sheet, bbox, char_no }) fires when a row is clicked (bbox is null for a row the
 * app could not place). closeOnLocate: "auto" (default, closes only in the narrow modal
 * layout so the canvas becomes visible), true or false.
 *
 * The stylesheet boc-view.css is loaded automatically from next to this script when the page
 * does not already link it.
 */
(function () {
  "use strict";

  var GDT_SYMBOLS = "⌖⏥⌓⌒⊥∥∠○◎⌯↗⌰⏤▱⌭";
  // Misc Technical, arrows, math operators, enclosed letters, geometric shapes: glyphs most UI fonts lack
  var SYMBOL_RE = /([←-⇿∀-⋿⌀-⏿Ⓐ-ⓩ■-◿])/;
  var DATUM_RE = /^[A-Z][ⓂⓁⓈ]?$/;
  var COUNT_RE = /^\d+X$/i;

  var STATUS = {
    covered: { label: "Covered", hint: "Drawing text at this row's location matches its requirement." },
    text: { label: "Placed by text match", hint: "The BoC gave no location, so the app placed this row by matching its requirement text on the sheet." },
    unmatched: { label: "Text not found", hint: "The row has a location, but no drawing text there matches its requirement." },
    missing: { label: "Not located", hint: "The app could not find this requirement on the drawing." },
    located: { label: "Located", hint: "Placed on the drawing; coverage detail is not available for this sheet." },
    skipped: { label: "Sheet not checked", hint: "This sheet was skipped in the check." }
  };
  var FILTERS = [
    { key: "all", label: "All" },
    { key: "covered", label: "Covered" },
    { key: "missing", label: "Not located" },
    { key: "flagged", label: "Near a flag" }
  ];

  var state = {
    open: false,
    runId: null,
    onLocate: null,
    closeOnLocate: "auto",
    apiBase: "",
    data: null,
    loading: false,
    error: null,
    filter: "all",
    query: "",
    sort: { key: "char", dir: 1 },
    activeKey: null,
    prevFocus: null,
    loadToken: 0
  };
  var cache = {};
  var ui = null;

  // ------------------------------------------------------------------ helpers
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === null || v === undefined || v === false) return;
        if (k === "class") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k.slice(0, 2) === "on") node.addEventListener(k.slice(2), v);
        else node.setAttribute(k, v === true ? "" : v);
      });
    }
    (children || []).forEach(function (c) {
      if (c === null || c === undefined || c === false) return;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }

  function svgIcon(path, cls) {
    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    svg.setAttribute("class", cls || "tbv-i");
    var p = document.createElementNS(ns, "path");
    p.setAttribute("d", path);
    svg.appendChild(p);
    return svg;
  }
  var ICON = {
    close: "M6 6l12 12M18 6L6 18",
    search: "M10.5 17a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13zM15.5 15.5L20 20",
    file: "M7 3h7l4 4v14H7zM14 3v4h4M10 12h6M10 16h6",
    sort: "M8 10l4-4 4 4M8 14l4 4 4-4",
    pin: "M12 21s-6-5.6-6-10.5A6 6 0 0 1 18 10.5C18 15.4 12 21 12 21zM12 12.5a2 2 0 1 0 0-4 2 2 0 0 0 0 4z",
    info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 11v5M12 8h.01"
  };

  function naturalCompare(a, b) {
    return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
  }

  function fold(s) {
    return String(s || "").toLowerCase().replace(/\s+/g, " ");
  }

  async function getJSON(url) {
    var res = await fetch(url, { headers: { Accept: "application/json" } });
    if (!res.ok) {
      var msg = res.status === 404 ? "This run was not found. It may have been deleted." : "The server answered " + res.status + ".";
      try {
        var body = await res.json();
        if (body && typeof body.detail === "string") msg = body.detail;
      } catch (e) { /* not JSON */ }
      var err = new Error(msg);
      err.status = res.status;
      throw err;
    }
    return res.json();
  }

  function ensureStylesheet() {
    if (document.querySelector('link[href*="boc-view.css"], style[data-tbv]')) return;
    var src = (document.currentScript && document.currentScript.src) || scriptSrc;
    if (!src) return;
    document.head.appendChild(el("link", { rel: "stylesheet", href: src.replace(/boc-view\.js(\?.*)?$/, "boc-view.css") }));
  }
  var scriptSrc = document.currentScript ? document.currentScript.src : null;

  function isModalLayout() {
    return window.matchMedia ? window.matchMedia("(max-width: 720px)").matches : false;
  }

  // ------------------------------------------------------------------ requirement rendering
  /* Appends text, wrapping glyphs most UI fonts lack in a span that uses a symbol font stack. */
  function appendSymbolText(parent, text) {
    String(text).split(SYMBOL_RE).forEach(function (part, i) {
      if (!part) return;
      if (i % 2 === 1) parent.appendChild(el("span", { class: "tbv-sym", text: part }));
      else parent.appendChild(document.createTextNode(part));
    });
  }

  /* "⌖ Ø.025 Ⓜ C A B" renders as a feature control frame: [⌖][Ø.025 Ⓜ][C][A][B].
     Leading "2X" and trailing notes ("4X INDIVIDUALLY", "2 SURFACES") stay as plain text. */
  function renderRequirement(text) {
    var wrap = el("span", { class: "tbv-req" });
    var tokens = String(text || "").trim().split(/\s+/).filter(Boolean);
    var i = 0;
    var prefix = [];
    while (i < tokens.length && COUNT_RE.test(tokens[i])) prefix.push(tokens[i++]);
    var head = tokens[i];
    if (!head || head.length !== 1 || GDT_SYMBOLS.indexOf(head) < 0 || i + 1 >= tokens.length) {
      appendSymbolText(wrap, text || "");
      return wrap;
    }
    var tol = [];
    var j = i + 1;
    while (j < tokens.length && !(tol.length && DATUM_RE.test(tokens[j]))) tol.push(tokens[j++]);
    var datums = [];
    while (j < tokens.length && DATUM_RE.test(tokens[j]) && datums.length < 3) datums.push(tokens[j++]);
    var rest = tokens.slice(j);

    if (prefix.length) wrap.appendChild(el("span", { class: "tbv-req-pre", text: prefix.join(" ") + " " }));
    var frame = el("span", { class: "tbv-fcf", role: "img", "aria-label": "Feature control frame " + text });
    var cells = [head, tol.join(" ")].concat(datums);
    cells.forEach(function (c, k) {
      var cell = el("span", { class: "tbv-fcf-cell" + (k === 0 ? " tbv-fcf-sym" : ""), "aria-hidden": "true" });
      appendSymbolText(cell, c);
      frame.appendChild(cell);
    });
    wrap.appendChild(frame);
    if (rest.length) {
      var tail = el("span", { class: "tbv-req-post" });
      appendSymbolText(tail, rest.join(" "));
      wrap.appendChild(tail);
    }
    return wrap;
  }

  // ------------------------------------------------------------------ data
  async function loadData(runId) {
    var base = state.apiBase + "/runs/" + encodeURIComponent(runId);
    var results = await Promise.all([getJSON(base), getJSON(base + "/rows"), getJSON(base + "/flags")]);
    var run = results[0], rows = results[1] || [], flags = results[2] || [];
    if (run.status !== "complete" && run.status !== "done") {
      var e = new Error(run.status === "error"
        ? "This check stopped with an error, so there is no BoC to show for it."
        : "This check has not finished yet. The BoC appears here once it completes.");
      e.pending = run.status !== "error";
      throw e;
    }

    var sheetsChecked = Array.isArray(run.sheets_checked) ? run.sheets_checked : null;
    var sheets = sheetsChecked || Array.from(new Set(rows.map(function (r) { return r.sheet; })));
    var coverage = {};
    await Promise.all(sheets.map(function (n) {
      return getJSON(base + "/sheets/" + n + "/coverage.json")
        .then(function (c) { coverage[n] = c; })
        .catch(function () { coverage[n] = null; });
    }));

    var coveredBy = {};
    Object.keys(coverage).forEach(function (n) {
      var c = coverage[n];
      if (!c || !c.covered) return;
      c.covered.forEach(function (reg) {
        (reg.char_nos || []).forEach(function (cn) { coveredBy[n + "|" + cn] = true; });
      });
    });

    var decorated = rows.map(function (r, idx) {
      var status;
      var checked = !sheetsChecked || sheetsChecked.indexOf(r.sheet) >= 0;
      if (!checked) status = "skipped";
      else if (!r.bbox) status = "missing";
      else if (r.bbox_source === "text_match") status = "text";
      else if (!coverage[r.sheet]) status = "located";
      else status = coveredBy[r.sheet + "|" + r.char_no] ? "covered" : "unmatched";

      var related = flags.filter(function (f) {
        if (f.sheet !== r.sheet) return false;
        return f.char_no === r.char_no || f.nearest_char_no === r.char_no;
      }).map(function (f) {
        var own = f.char_no === r.char_no;
        return {
          id: f.flag_id,
          kind: own ? "places" : "near",
          label: own ? "Places short " + f.flag_id : "Near " + f.flag_id,
          title: (f.title || "") + (f.drawing_text ? ": " + (f.transcription || f.drawing_text) : "")
        };
      });

      return {
        key: r.sheet + "|" + r.char_no + "|" + idx,
        idx: idx,
        row: r,
        status: status,
        flags: related,
        search: fold(r.char_no) + " \u0001 " + fold(r.requirement)
      };
    });

    return {
      runId: runId,
      filename: (run.boc && run.boc.filename) || "BoC",
      simulated: !!(run.boc && run.boc.simulated),
      sheetCount: sheets.length,
      rows: decorated
    };
  }

  function matchesFilter(d) {
    switch (state.filter) {
      case "covered": return d.status === "covered" || d.status === "text" || d.status === "located";
      case "missing": return d.status === "missing" || d.status === "unmatched";
      case "flagged": return d.flags.length > 0;
      default: return true;
    }
  }

  function visibleRows() {
    if (!state.data) return [];
    var q = fold(state.query).trim();
    var list = state.data.rows.filter(function (d) {
      return matchesFilter(d) && (!q || d.search.indexOf(q) >= 0);
    });
    var dir = state.sort.dir;
    list.sort(function (a, b) {
      var c = state.sort.key === "sheet"
        ? (a.row.sheet - b.row.sheet) || naturalCompare(a.row.char_no, b.row.char_no)
        : naturalCompare(a.row.char_no, b.row.char_no) || (a.row.sheet - b.row.sheet);
      return (c || a.idx - b.idx) * dir;
    });
    return list;
  }

  // ------------------------------------------------------------------ DOM
  function build() {
    var closeBtn = el("button", { type: "button", class: "tbv-iconbtn tbv-close", "aria-label": "Close Bill of Characteristics", title: "Close (Esc)", onclick: close }, [svgIcon(ICON.close)]);
    var sub = el("p", { class: "tbv-sub", id: "tbv-sub" });
    var note = el("div", { class: "tbv-note", hidden: true }, [
      svgIcon(ICON.info),
      el("span", null, [el("strong", { text: "Simulated export." }), " This BoC was prepared for the demo, not exported by an extractor."])
    ]);

    var search = el("input", {
      type: "search", class: "tbv-search-input", placeholder: "Search char no or requirement",
      "aria-label": "Search char no or requirement", autocomplete: "off", spellcheck: "false"
    });
    search.addEventListener("input", function () { state.query = search.value; renderBody(); });

    var chips = el("div", { class: "tbv-chips", role: "group", "aria-label": "Filter rows by status" });
    FILTERS.forEach(function (f) {
      chips.appendChild(el("button", {
        type: "button", class: "tbv-chip", "data-filter": f.key, "aria-pressed": f.key === state.filter ? "true" : "false",
        onclick: function (ev) {
          state.filter = f.key;
          renderBody();
          ev.currentTarget.scrollIntoView({ block: "nearest", inline: "nearest" });
        }
      }, [el("span", { class: "tbv-chip-label", text: f.label }), el("span", { class: "tbv-chip-count" })]));
    });

    var sortSel = el("select", { class: "tbv-sort-select", "aria-label": "Sort rows" }, [
      el("option", { value: "char:1", text: "Char No ↑" }),
      el("option", { value: "char:-1", text: "Char No ↓" }),
      el("option", { value: "sheet:1", text: "Sheet ↑" }),
      el("option", { value: "sheet:-1", text: "Sheet ↓" })
    ]);
    sortSel.addEventListener("change", function () {
      var p = sortSel.value.split(":");
      state.sort = { key: p[0], dir: Number(p[1]) };
      renderBody();
    });

    var tools = el("div", { class: "tbv-tools" }, [
      el("label", { class: "tbv-search" }, [svgIcon(ICON.search), search]),
      el("div", { class: "tbv-tools-row" }, [chips, el("label", { class: "tbv-sort" }, [el("span", { class: "tbv-sort-label", text: "Sort" }), sortSel])])
    ]);

    function sortHeader(key, label) {
      var btn = el("button", { type: "button", class: "tbv-th-sort", "data-sort": key, onclick: function () {
        state.sort = state.sort.key === key ? { key: key, dir: -state.sort.dir } : { key: key, dir: 1 };
        renderBody();
      } }, [label, svgIcon(ICON.sort, "tbv-i tbv-sort-i")]);
      return el("th", { scope: "col", class: "tbv-col-" + key, "data-sort-col": key }, [btn]);
    }

    var thead = el("thead", null, [el("tr", null, [
      sortHeader("char", "Char No"),
      sortHeader("sheet", "Sheet"),
      el("th", { scope: "col", class: "tbv-col-zone", text: "Zone" }),
      el("th", { scope: "col", class: "tbv-col-req", text: "Requirement" }),
      el("th", { scope: "col", class: "tbv-col-places", text: "Places" }),
      el("th", { scope: "col", class: "tbv-col-status", text: "Status" })
    ])]);
    var tbody = el("tbody");
    tbody.addEventListener("click", function (ev) {
      var tr = ev.target.closest("tr[data-key]");
      if (tr) activate(tr.getAttribute("data-key"), true);
    });
    tbody.addEventListener("keydown", onRowKey);
    var table = el("table", { class: "tbv-table", "aria-describedby": "tbv-foot" }, [
      el("caption", { class: "tbv-vh", text: "BoC rows. Activate a row to find it on the drawing." }), thead, tbody
    ]);
    var stateBox = el("div", { class: "tbv-state", hidden: true, role: "status" });
    var scroller = el("div", { class: "tbv-scroll" }, [table, stateBox]);
    var foot = el("footer", { class: "tbv-foot", id: "tbv-foot", "aria-live": "polite" });

    var panel = el("section", {
      class: "tbv-panel", role: "dialog", "aria-modal": "true", "aria-labelledby": "tbv-title", "aria-describedby": "tbv-sub", tabindex: "-1"
    }, [
      el("header", { class: "tbv-head" }, [
        el("div", { class: "tbv-titles" }, [el("h2", { class: "tbv-title", id: "tbv-title", text: "Bill of Characteristics" }), sub]),
        closeBtn
      ]),
      note, tools, scroller, foot
    ]);
    var backdrop = el("div", { class: "tbv-backdrop", onclick: close });
    var root = el("div", { class: "tbv-root", hidden: true }, [backdrop, panel]);
    root.addEventListener("keydown", onKey);
    document.body.appendChild(root);

    ui = { root: root, panel: panel, sub: sub, note: note, search: search, chips: chips, sortSel: sortSel, thead: thead,
      tbody: tbody, table: table, stateBox: stateBox, scroller: scroller, foot: foot, tools: tools };
  }

  function renderHeader() {
    var d = state.data;
    ui.sub.textContent = "";
    if (d) {
      ui.sub.appendChild(svgIcon(ICON.file));
      ui.sub.appendChild(el("span", { class: "tbv-file", title: d.filename, text: d.filename }));
      ui.sub.appendChild(el("span", { class: "tbv-counts" }, [
        el("span", { class: "tbv-dot", "aria-hidden": "true", text: "·" }),
        d.rows.length + (d.rows.length === 1 ? " row" : " rows"),
        el("span", { class: "tbv-dot", "aria-hidden": "true", text: "·" }),
        d.sheetCount + (d.sheetCount === 1 ? " sheet" : " sheets")
      ]));
    } else {
      ui.sub.textContent = state.loading ? "Loading…" : "";
    }
    ui.note.hidden = !(d && d.simulated);
  }

  function renderState() {
    var box = ui.stateBox;
    box.textContent = "";
    var showTable = !!state.data && !state.loading && !state.error;
    ui.table.hidden = !showTable;
    ui.tools.classList.toggle("tbv-disabled", !showTable);
    ui.tools.querySelectorAll("input, button, select").forEach(function (n) { n.disabled = !showTable; });
    if (state.loading) {
      box.hidden = false;
      box.className = "tbv-state tbv-loading";
      box.appendChild(el("span", { class: "tbv-spinner", "aria-hidden": "true" }));
      box.appendChild(el("span", { text: "Loading the Bill of Characteristics…" }));
      var sk = el("div", { class: "tbv-skeleton", "aria-hidden": "true" });
      for (var i = 0; i < 7; i++) sk.appendChild(el("span"));
      box.appendChild(sk);
    } else if (state.error) {
      box.hidden = false;
      box.className = "tbv-state tbv-error" + (state.error.pending ? " tbv-pending" : "");
      box.appendChild(el("p", { class: "tbv-state-title", text: state.error.pending ? "Not ready yet" : "Couldn’t load the BoC" }));
      box.appendChild(el("p", { class: "tbv-state-msg", text: state.error.message || "Something went wrong." }));
      box.appendChild(el("button", { type: "button", class: "tbv-btn", onclick: function () { load(state.runId, true); } }, ["Try again"]));
    } else {
      box.hidden = true;
    }
  }

  function renderBody() {
    if (!ui) return;
    // chips + sort controls
    var counts = { all: 0, covered: 0, missing: 0, flagged: 0 };
    if (state.data) {
      var saved = state.filter;
      FILTERS.forEach(function (f) {
        state.filter = f.key;
        counts[f.key] = state.data.rows.filter(matchesFilter).length;
      });
      state.filter = saved;
    }
    ui.chips.querySelectorAll(".tbv-chip").forEach(function (c) {
      var k = c.getAttribute("data-filter");
      c.setAttribute("aria-pressed", k === state.filter ? "true" : "false");
      c.querySelector(".tbv-chip-count").textContent = state.data ? String(counts[k]) : "";
    });
    ui.sortSel.value = state.sort.key + ":" + state.sort.dir;
    ui.thead.querySelectorAll("th[data-sort-col]").forEach(function (th) {
      var k = th.getAttribute("data-sort-col");
      if (k === state.sort.key) th.setAttribute("aria-sort", state.sort.dir > 0 ? "ascending" : "descending");
      else th.removeAttribute("aria-sort");
    });

    ui.tbody.textContent = "";
    if (!state.data) { ui.foot.textContent = ""; return; }
    var list = visibleRows();
    var frag = document.createDocumentFragment();
    var hasActive = list.some(function (d) { return d.key === state.activeKey; });
    list.forEach(function (d, i) {
      var r = d.row;
      var isActive = d.key === state.activeKey;
      var tr = el("tr", {
        "data-key": d.key, "data-status": d.status, class: isActive ? "tbv-active" : null,
        tabindex: (isActive || (!hasActive && i === 0)) ? "0" : "-1",
        "aria-current": isActive ? "true" : null
      });
      tr.appendChild(el("td", { class: "tbv-col-char" }, [el("span", { class: "tbv-cn", text: r.char_no })]));
      tr.appendChild(el("td", { class: "tbv-col-sheet", text: String(r.sheet) }));
      tr.appendChild(el("td", { class: "tbv-col-zone", text: r.zone || "—" }));
      var reqTd = el("td", { class: "tbv-col-req" }, [renderRequirement(r.requirement)]);
      reqTd.appendChild(el("span", { class: "tbv-meta" }, [
        "Sheet " + r.sheet + (r.zone ? " · Zone " + r.zone : "") + " · " + r.places + (r.places === 1 ? " place" : " places")
      ]));
      tr.appendChild(reqTd);
      tr.appendChild(el("td", { class: "tbv-col-places", text: String(r.places) }));
      var st = STATUS[d.status];
      var statusTd = el("td", { class: "tbv-col-status" }, [
        el("span", { class: "tbv-pill tbv-pill-" + d.status, title: st.hint }, [el("span", { class: "tbv-pill-dot", "aria-hidden": "true" }), st.label])
      ]);
      d.flags.forEach(function (f) {
        statusTd.appendChild(el("span", { class: "tbv-flag tbv-flag-" + f.kind, title: f.title }, [f.label]));
      });
      tr.appendChild(statusTd);
      frag.appendChild(tr);
    });
    ui.tbody.appendChild(frag);

    var total = state.data.rows.length;
    ui.foot.textContent = "";
    if (!list.length) {
      var empty = el("tr", { class: "tbv-empty" }, [el("td", { colspan: "6" }, [
        el("p", { class: "tbv-state-title", text: "No rows match" }),
        el("p", { class: "tbv-state-msg", text: state.query ? "Nothing matches “" + state.query.trim() + "” with this filter." : "No rows have this status." }),
        el("button", { type: "button", class: "tbv-btn", onclick: function () {
          state.query = ""; ui.search.value = ""; state.filter = "all"; renderBody(); ui.search.focus();
        } }, ["Clear search and filter"])
      ])]);
      ui.tbody.appendChild(empty);
    }
    ui.foot.appendChild(el("span", { text: list.length === total ? total + " rows" : "Showing " + list.length + " of " + total + " rows" }));
    ui.foot.appendChild(el("span", { class: "tbv-foot-hint" }, [svgIcon(ICON.pin), "Click a row to find it on the drawing"]));
  }

  function render() {
    renderHeader();
    renderState();
    renderBody();
  }

  // ------------------------------------------------------------------ behaviour
  function activate(key, fromPointer) {
    if (!state.data) return;
    var d = state.data.rows.find(function (x) { return x.key === key; });
    if (!d) return;
    state.activeKey = key;
    ui.tbody.querySelectorAll("tr[data-key]").forEach(function (tr) {
      var on = tr.getAttribute("data-key") === key;
      tr.classList.toggle("tbv-active", on);
      if (on) tr.setAttribute("aria-current", "true"); else tr.removeAttribute("aria-current");
      tr.tabIndex = on ? 0 : -1;
      if (on && fromPointer && document.activeElement !== tr) tr.focus({ preventScroll: true });
    });
    var payload = { sheet: d.row.sheet, bbox: d.row.bbox ? Object.assign({}, d.row.bbox) : null, char_no: d.row.char_no };
    if (typeof state.onLocate === "function") {
      try { state.onLocate(payload); } catch (e) { if (window.console) console.error("TallyBocView onLocate failed", e); }
    }
    if (state.closeOnLocate === true || (state.closeOnLocate === "auto" && isModalLayout())) close();
  }

  function onRowKey(ev) {
    var tr = ev.target.closest && ev.target.closest("tr[data-key]");
    if (!tr) return;
    var rows = Array.prototype.slice.call(ui.tbody.querySelectorAll("tr[data-key]"));
    var i = rows.indexOf(tr);
    var next = null;
    if (ev.key === "ArrowDown") next = rows[Math.min(i + 1, rows.length - 1)];
    else if (ev.key === "ArrowUp") next = rows[Math.max(i - 1, 0)];
    else if (ev.key === "Home") next = rows[0];
    else if (ev.key === "End") next = rows[rows.length - 1];
    else if (ev.key === "Enter" || ev.key === " ") {
      ev.preventDefault();
      activate(tr.getAttribute("data-key"), false);
      return;
    }
    if (next) {
      ev.preventDefault();
      rows.forEach(function (r) { r.tabIndex = r === next ? 0 : -1; });
      next.focus();
    }
  }

  function focusables() {
    return Array.prototype.slice.call(ui.panel.querySelectorAll(
      'button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex="0"], a[href]'
    )).filter(function (n) { return !n.closest("[hidden]") && n.offsetParent !== null; });
  }

  function onKey(ev) {
    if (ev.key === "Escape") {
      ev.preventDefault();
      ev.stopPropagation();
      close();
      return;
    }
    if (ev.key !== "Tab") return;
    var f = focusables();
    if (!f.length) { ev.preventDefault(); ui.panel.focus(); return; }
    var first = f[0], last = f[f.length - 1];
    if (ev.shiftKey && (document.activeElement === first || document.activeElement === ui.panel)) {
      ev.preventDefault(); last.focus();
    } else if (!ev.shiftKey && document.activeElement === last) {
      ev.preventDefault(); first.focus();
    }
  }

  // keeps focus inside while open, even when something outside grabs it programmatically
  function onFocusIn(ev) {
    if (state.open && ui && !ui.panel.contains(ev.target)) ui.panel.focus();
  }

  async function load(runId, force) {
    var token = ++state.loadToken;
    if (!force && cache[runId]) {
      state.data = cache[runId];
      state.loading = false;
      state.error = null;
      render();
      return;
    }
    state.data = null;
    state.loading = true;
    state.error = null;
    render();
    try {
      var data = await loadData(runId);
      if (token !== state.loadToken) return;
      cache[runId] = data;
      state.data = data;
    } catch (e) {
      if (token !== state.loadToken) return;
      state.error = { message: e.message, pending: !!e.pending };
    }
    state.loading = false;
    render();
    // the search box was disabled while loading, so focus could only land on the panel
    if (state.open && document.activeElement === ui.panel && !ui.search.disabled) ui.search.focus();
    if (state.open && !state.error) {
      var active = ui.tbody.querySelector("tr.tbv-active");
      if (active) active.scrollIntoView({ block: "center" });
    }
  }

  function open(opts) {
    opts = opts || {};
    if (!opts.runId) throw new Error("TallyBocView.open needs a runId");
    ensureStylesheet();
    if (!ui) build();
    var sameRun = state.runId === opts.runId;
    state.runId = opts.runId;
    state.onLocate = opts.onLocate || null;
    state.closeOnLocate = opts.closeOnLocate === undefined ? "auto" : opts.closeOnLocate;
    state.apiBase = (opts.apiBase || "").replace(/\/$/, "");
    if (!sameRun) {
      state.activeKey = null;
      state.query = "";
      state.filter = "all";
      ui.search.value = "";
    }
    if (opts.filter && FILTERS.some(function (f) { return f.key === opts.filter; })) state.filter = opts.filter;
    if (!state.open) {
      state.prevFocus = document.activeElement;
      state.open = true;
      ui.root.hidden = false;
      document.documentElement.classList.add("tbv-lock");
      document.addEventListener("focusin", onFocusIn);
      // next frame so the slide-in transition runs
      requestAnimationFrame(function () { ui.root.classList.add("tbv-in"); });
    }
    var p = load(opts.runId, false).then(function () {
      if (opts.activeCharNo != null && state.data) {
        var hit = state.data.rows.find(function (d) { return String(d.row.char_no) === String(opts.activeCharNo); });
        if (hit) {
          state.activeKey = hit.key;
          renderBody();
          var tr = ui.tbody.querySelector("tr.tbv-active");
          if (tr) tr.scrollIntoView({ block: "center" });
        }
      }
    });
    setTimeout(function () {
      if (!state.open) return;
      if (!ui.search.disabled) ui.search.focus(); else ui.panel.focus();
    }, 30);
    return p;
  }

  function close() {
    if (!state.open || !ui) return;
    state.open = false;
    ui.root.classList.remove("tbv-in");
    document.removeEventListener("focusin", onFocusIn);
    document.documentElement.classList.remove("tbv-lock");
    var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    setTimeout(function () { if (!state.open) ui.root.hidden = true; }, reduce ? 0 : 220);
    var prev = state.prevFocus;
    state.prevFocus = null;
    if (prev && typeof prev.focus === "function" && document.contains(prev)) prev.focus();
  }

  function isOpen() {
    return state.open;
  }

  window.TallyBocView = { open: open, close: close, isOpen: isOpen };
})();
