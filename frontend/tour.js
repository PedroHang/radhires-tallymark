/* Tallymark first-run tour.
 *
 * Self-contained: no dependencies, no build step. Load tour.css + tour.js, mark the
 * results screen with data-tour="<key>" attributes, then call
 * window.TallyTour.maybeStartFirstRun() once the results screen is showing.
 *
 * Edit the copy in TOUR_STEPS below. Each step:
 *   key        anchor key; the target is the first VISIBLE [data-tour="<key>"]
 *   title      short heading on the card
 *   text       2-3 plain sentences (HTML allowed, keep it simple)
 *   target     optional override: CSS selector, Element, or function returning one
 *   before     optional hook run before the step shows (may return a Promise),
 *              e.g. to open the first flag so the detail panel exists
 *   placement  optional preferred side: "right" | "left" | "bottom" | "top"
 * A step whose target is missing or hidden (after before() ran) is skipped.
 */
(function () {
  "use strict";

  var TOUR_STEPS = [
    {
      key: "summary",
      title: "Your results at a glance",
      text: "The big number is how many requirements on the drawing have no BoC row covering them. " +
            "Next to it you can see how much of the drawing your rows cover, and how many flags you have reviewed so far."
    },
    {
      key: "sheets",
      title: "Move between sheets",
      text: "A drawing can have several sheets. Pick one here to jump to it. " +
            "The badge next to each sheet shows how many flags are waiting on it."
    },
    {
      key: "canvas",
      title: "Read the drawing",
      text: "Grey areas are already covered by a BoC row. Red boxes are requirement text that nobody extracted. " +
            "Zoom in to get closer, and switch between Faded and Original to see the untouched drawing."
    },
    {
      key: "flags",
      title: "Work through the flags",
      text: "<b>U</b> is a requirement with no BoC row at all. <b>P</b> is a callout like \"4X\" whose row lists fewer places than the drawing shows. " +
            "Use the filters at the top to narrow the list."
    },
    {
      key: "detail",
      title: "Check the evidence",
      text: "Pick a flag to open this panel. It puts what the BoC says next to what is on the drawing, " +
            "and explains in plain words why it was flagged."
    },
    {
      key: "decisions",
      title: "Make the call",
      text: "<b>Confirm miss</b> if the BoC really is missing it. <b>Excluded by customer rule</b> if the customer's rules say it needs no row. " +
            "<b>Dismiss</b> if it is not a real requirement. Shortcuts: <kbd>C</kbd> <kbd>E</kbd> <kbd>D</kbd>."
    },
    {
      key: "export",
      title: "Hand it on",
      text: "Once every flag has a decision, export the exceptions as a CSV. " +
            "That file is the list you hand back to whoever owns the BoC."
    }
  ];

  var STORAGE_KEY = "tallymark.tour.v1";
  var PAD = 6;          // spotlight padding around the target, px
  var GAP = 14;         // distance between spotlight and card, px
  var MARGIN = 12;      // min distance from the viewport edge, px
  var SMALL = 640;      // below this width the card is docked at the bottom
  var BEFORE_TIMEOUT = 4000;

  var defaults = TOUR_STEPS.map(copyStep);
  var steps = TOUR_STEPS.map(copyStep);

  var ui = null;        // { root, blocker, spot, card, title, text, count, back, next, skip }
  var openState = null; // { list, index, lastFocus, raf, timer, ro, target, seq }
  var firstRunTimer = null;

  /* ------------------------------------------------------------ helpers */

  function copyStep(s) { var o = {}; for (var k in s) if (Object.prototype.hasOwnProperty.call(s, k)) o[k] = s[k]; return o; }

  function reducedMotion() {
    try { return window.matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) { return false; }
  }

  function readRecord() {
    try { var v = window.localStorage.getItem(STORAGE_KEY); return v ? JSON.parse(v) : null; } catch (e) { return null; }
  }
  function writeRecord(status, key) {
    try { window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ status: status, step: key || null, at: new Date().toISOString() })); } catch (e) {}
  }
  function clearRecord() {
    try { window.localStorage.removeItem(STORAGE_KEY); } catch (e) {}
  }

  function isVisible(el) {
    if (!el || !el.isConnected) return false;
    if (el.closest && el.closest("[hidden]")) return false;
    if (typeof el.checkVisibility === "function" && !el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return false;
    var r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    var cs = window.getComputedStyle(el);
    return cs.display !== "none" && cs.visibility !== "hidden" && parseFloat(cs.opacity || "1") > 0.05;
  }

  function resolveTarget(step) {
    var t = step.target;
    var cands = [];
    try {
      if (typeof t === "function") t = t(step);
      if (t && t.nodeType === 1) cands = [t];
      else if (typeof t === "string") cands = Array.prototype.slice.call(document.querySelectorAll(t));
      else if (step.key) cands = Array.prototype.slice.call(document.querySelectorAll('[data-tour="' + cssEscape(step.key) + '"]'));
    } catch (e) { cands = []; }
    for (var i = 0; i < cands.length; i++) if (isVisible(cands[i])) return cands[i];
    return null;
  }

  function cssEscape(s) {
    return (window.CSS && CSS.escape) ? CSS.escape(String(s)) : String(s).replace(/["\\]/g, "\\$&");
  }

  // A step "counts" for the "3 of 7" label (and for Back/Finish) if its target is visible now, it has a
  // before() hook, or its anchor is in the DOM (just hidden) and an earlier step's hook may reveal it.
  function isCandidate(step, idx, list) {
    if (typeof step.before === "function" || resolveTarget(step)) return true;
    if (!list || !step.key || typeof step.target !== "undefined") return false;
    var earlierHook = false;
    for (var j = 0; j < idx; j++) if (typeof list[j].before === "function") { earlierHook = true; break; }
    return earlierHook && !!document.querySelector('[data-tour="' + cssEscape(step.key) + '"]');
  }

  function withTimeout(p, ms) {
    return new Promise(function (resolve) {
      var done = false;
      var t = setTimeout(function () { if (!done) { done = true; resolve(); } }, ms);
      Promise.resolve(p).then(function () { if (!done) { done = true; clearTimeout(t); resolve(); } },
                              function (err) { if (!done) { done = true; clearTimeout(t); if (window.console) console.warn("[TallyTour] before() failed", err); resolve(); } });
    });
  }

  function nextFrame() { return new Promise(function (r) { requestAnimationFrame(function () { requestAnimationFrame(r); }); }); }

  function emit(name, detail) {
    try { document.dispatchEvent(new CustomEvent("tallytour:" + name, { detail: detail || {} })); } catch (e) {}
  }

  /* ------------------------------------------------------------ DOM */

  function build() {
    var root = document.createElement("div");
    root.className = "tt-root";
    root.innerHTML =
      '<div class="tt-blocker" aria-hidden="true"></div>' +
      '<div class="tt-spot" aria-hidden="true"></div>' +
      '<div class="tt-card" role="dialog" aria-modal="true" aria-labelledby="tt-title" aria-describedby="tt-text" tabindex="-1">' +
        '<div class="tt-head">' +
          '<span class="tt-count" id="tt-count"></span>' +
          '<button type="button" class="tt-skip" data-tt="skip">Skip tour</button>' +
        '</div>' +
        '<h2 class="tt-title" id="tt-title"></h2>' +
        '<div class="tt-text" id="tt-text"></div>' +
        '<div class="tt-dots" aria-hidden="true"></div>' +
        '<div class="tt-foot">' +
          '<span class="tt-hint" aria-hidden="true"><kbd>&larr;</kbd><kbd>&rarr;</kbd> to move, <kbd>Esc</kbd> to skip</span>' +
          '<button type="button" class="tt-btn tt-back" data-tt="back">Back</button>' +
          '<button type="button" class="tt-btn tt-next" data-tt="next">Next</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(root);
    var q = function (s) { return root.querySelector(s); };
    ui = {
      root: root, blocker: q(".tt-blocker"), spot: q(".tt-spot"), card: q(".tt-card"),
      title: q(".tt-title"), text: q(".tt-text"), count: q(".tt-count"), dots: q(".tt-dots"),
      back: q(".tt-back"), next: q(".tt-next"), skip: q(".tt-skip")
    };
    // Clicks on the dimmed area do nothing (and never reach the app underneath).
    ui.blocker.addEventListener("mousedown", swallow, true);
    ui.blocker.addEventListener("click", swallow, true);
    ui.blocker.addEventListener("wheel", swallowWheel, { passive: false });
    ui.back.addEventListener("click", function () { go(-1); });
    ui.next.addEventListener("click", function () { go(1); });
    ui.skip.addEventListener("click", function () { close("skip"); });
  }

  function swallow(e) { e.preventDefault(); e.stopPropagation(); }
  function swallowWheel(e) { e.preventDefault(); }

  /* ------------------------------------------------------------ flow */

  function start(opts) {
    opts = opts || {};
    if (openState) {
      if (!opts.force) return false;
      teardown(true);
    }
    var rec = readRecord();
    if (rec && !opts.force) return false;
    if (opts.steps) setSteps(opts.steps);
    if (!document.body) return false;

    var list = steps.slice();
    if (!list.some(function (st, k) { return isCandidate(st, k, list); })) return false;

    if (!ui || !ui.root.isConnected) build();
    ui.root.classList.remove("tt-leaving");
    ui.root.hidden = false;
    document.documentElement.classList.add("tt-active");

    openState = { list: list, index: -1, lastFocus: document.activeElement, raf: 0, timer: 0, ro: null, target: null, seq: 0 };

    window.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", schedule, true);
    window.addEventListener("scroll", schedule, true);
    document.addEventListener("focusin", onFocusIn, true);
    openState.timer = setInterval(schedule, 400); // catches layout shifts (panels animating in, images loading)

    // Start with the spotlight collapsed in the center so the first move animates in.
    setSpot({ left: window.innerWidth / 2, top: window.innerHeight / 2, width: 0, height: 0 }, true);
    requestAnimationFrame(function () { if (ui) ui.root.classList.add("tt-in"); });

    emit("start", {});
    var startAt = typeof opts.startAt === "number" ? Math.max(0, Math.min(list.length - 1, opts.startAt)) : 0;
    show(startAt, 1);
    return true;
  }

  // Show step i; if its target is missing, keep walking in direction dir. Off the end = finish.
  function show(i, dir) {
    if (!openState) return;
    var seq = ++openState.seq;
    var list = openState.list;
    if (i < 0) { i = 0; dir = 1; }
    if (i >= list.length) { close("finish"); return; }
    var step = list[i];
    var ready = typeof step.before === "function" ? withTimeout(safeCall(step.before, step), BEFORE_TIMEOUT).then(nextFrame) : Promise.resolve();
    ui.card.setAttribute("aria-busy", "true");
    ready.then(function () {
      if (!openState || seq !== openState.seq) return;
      ui.card.removeAttribute("aria-busy");
      var target = resolveTarget(step);
      if (!target) {
        var j = i + dir;
        if (j < 0) { // nothing visible behind us: walk forward from the start instead
          if (openState.index >= 0) { show(openState.index, 0); return; } else { show(i + 1, 1); return; }
        }
        if (dir === 0) { show(i + 1, 1); return; }
        show(j, dir);
        return;
      }
      openState.index = i;
      openState.target = target;
      watch(target);
      render(step, target);
      emit("step", { index: i, key: step.key });
    });
  }

  function safeCall(fn, step) {
    try { return fn.call(step, step); } catch (e) { return Promise.reject(e); }
  }

  function go(delta) {
    if (!openState) return;
    var i = openState.index + delta;
    if (delta > 0 && isLast()) { close("finish"); return; }
    if (i < 0) return;
    show(i, delta > 0 ? 1 : -1);
  }

  function isLast() {
    if (!openState) return true;
    for (var j = openState.index + 1; j < openState.list.length; j++) if (isCandidate(openState.list[j], j, openState.list)) return false;
    return true;
  }

  function isFirst() {
    if (!openState) return true;
    for (var j = openState.index - 1; j >= 0; j--) if (isCandidate(openState.list[j], j, openState.list)) return false;
    return true;
  }

  function close(reason) {
    if (!openState) return;
    var key = openState.index >= 0 ? openState.list[openState.index].key : null;
    writeRecord(reason === "finish" ? "completed" : "skipped", key);
    teardown(false);
    emit("end", { reason: reason, step: key });
  }

  function teardown(immediate) {
    var st = openState;
    openState = null;
    window.removeEventListener("keydown", onKey, true);
    window.removeEventListener("resize", schedule, true);
    window.removeEventListener("scroll", schedule, true);
    document.removeEventListener("focusin", onFocusIn, true);
    document.documentElement.classList.remove("tt-active");
    if (st) {
      clearInterval(st.timer);
      cancelAnimationFrame(st.raf);
      if (st.ro) st.ro.disconnect();
    }
    if (ui) {
      var root = ui.root;
      ui = null;
      var remove = function () { if (root.parentNode) root.parentNode.removeChild(root); };
      if (immediate || reducedMotion()) remove();
      else {
        root.classList.add("tt-leaving");
        root.classList.remove("tt-in");
        root.style.pointerEvents = "none"; // never block the page while fading out
        setTimeout(remove, 220);
      }
    }
    if (st && st.lastFocus && st.lastFocus.isConnected && typeof st.lastFocus.focus === "function") {
      try { st.lastFocus.focus({ preventScroll: true }); } catch (e) {}
    }
  }

  /* ------------------------------------------------------------ render + position */

  function render(step, target) {
    var list = openState.list;
    var visible = [];
    for (var j = 0; j < list.length; j++) if (j === openState.index || isCandidate(list[j], j, list)) visible.push(j);
    var pos = visible.indexOf(openState.index);

    ui.title.textContent = step.title || "";
    ui.text.innerHTML = step.text || "";
    ui.count.textContent = (pos + 1) + " of " + visible.length;
    ui.dots.innerHTML = visible.map(function (_, k) { return '<i class="' + (k === pos ? "on" : (k < pos ? "past" : "")) + '"></i>'; }).join("");
    ui.back.disabled = isFirst();
    var last = isLast();
    ui.next.textContent = last ? "Finish" : "Next";
    ui.card.setAttribute("data-step", step.key || "");
    fitAccentInk();

    ensureInView(target);
    position(true);

    if (!ui.card.contains(document.activeElement) || document.activeElement === ui.back && ui.back.disabled) {
      try { ui.next.focus({ preventScroll: true }); } catch (e) { ui.next.focus(); }
    }
  }

  // The Next button sits on the app's accent color, which may be dark (light theme) or light (dark
  // theme). Unless the app defines --accent-ink, pick dark or white text, whichever contrasts more.
  function fitAccentInk() {
    try {
      if (getComputedStyle(document.documentElement).getPropertyValue("--accent-ink").trim()) { ui.next.style.color = ""; return; }
      var m = getComputedStyle(ui.next).backgroundColor.match(/[\d.]+/g);
      if (!m || m.length < 3) return;
      var lin = function (v) { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
      var L = 0.2126 * lin(+m[0]) + 0.7152 * lin(+m[1]) + 0.0722 * lin(+m[2]);
      // Whichever of near-black (#0b1220, L ~ 0.006) or white gives the higher WCAG contrast.
      ui.next.style.color = (L + 0.05) / 0.056 > 1.05 / (L + 0.05) ? "#0b1220" : "#ffffff";
    } catch (e) {}
  }

  function ensureInView(el) {
    var r = el.getBoundingClientRect();
    var vw = window.innerWidth, vh = window.innerHeight;
    var fits = r.top >= 0 && r.left >= 0 && r.bottom <= vh && r.right <= vw;
    if (fits) return;
    try { el.scrollIntoView({ block: r.height > vh ? "start" : "nearest", inline: "nearest", behavior: reducedMotion() ? "auto" : "smooth" }); }
    catch (e) { el.scrollIntoView(); }
  }

  function watch(target) {
    if (openState.ro) openState.ro.disconnect();
    if (typeof ResizeObserver === "function") {
      openState.ro = new ResizeObserver(schedule);
      openState.ro.observe(target);
      openState.ro.observe(document.documentElement);
    }
  }

  function schedule() {
    if (!openState || openState.raf) return;
    openState.raf = requestAnimationFrame(function () {
      if (!openState) return;
      openState.raf = 0;
      if (openState.index < 0) return;
      var step = openState.list[openState.index];
      // The target may have been re-rendered or hidden by the app; re-resolve it.
      if (!openState.target || !isVisible(openState.target)) {
        var t = resolveTarget(step);
        if (!t) { show(openState.index + 1, 1); return; }
        openState.target = t;
        watch(t);
      }
      position(false);
    });
  }

  function targetRect() {
    var r = openState.target.getBoundingClientRect();
    var vw = window.innerWidth, vh = window.innerHeight;
    // Clip to the viewport so a huge target (the canvas) still gets a sensible outline.
    var left = Math.max(r.left - PAD, 2), top = Math.max(r.top - PAD, 2);
    var right = Math.min(r.right + PAD, vw - 2), bottom = Math.min(r.bottom + PAD, vh - 2);
    return { left: left, top: top, width: Math.max(0, right - left), height: Math.max(0, bottom - top) };
  }

  function setSpot(r, instant) {
    var s = ui.spot.style;
    if (instant) ui.spot.classList.add("tt-noanim");
    s.left = r.left + "px"; s.top = r.top + "px"; s.width = r.width + "px"; s.height = r.height + "px";
    if (instant) { void ui.spot.offsetWidth; ui.spot.classList.remove("tt-noanim"); }
  }

  function position(stepChanged) {
    if (!openState || !ui || !openState.target) return;
    var r = targetRect();
    var prev = openState.lastRect;
    if (!stepChanged && prev && prev.left === r.left && prev.top === r.top && prev.width === r.width && prev.height === r.height &&
        prev.vw === window.innerWidth && prev.vh === window.innerHeight) return;
    openState.lastRect = { left: r.left, top: r.top, width: r.width, height: r.height, vw: window.innerWidth, vh: window.innerHeight };
    setSpot(r, false);
    placeCard(r, openState.list[openState.index]);
  }

  function placeCard(r, step) {
    var card = ui.card;
    var vw = window.innerWidth, vh = window.innerHeight;
    card.classList.remove("tt-docked");
    card.style.left = "0px"; card.style.top = "0px";
    var cw = card.offsetWidth, ch = card.offsetHeight;

    var docked = vw < SMALL;
    var spaces = {
      right: vw - (r.left + r.width) - GAP - MARGIN,
      left: r.left - GAP - MARGIN,
      bottom: vh - (r.top + r.height) - GAP - MARGIN,
      top: r.top - GAP - MARGIN
    };
    var fitsSide = function (side) {
      return (side === "left" || side === "right") ? spaces[side] >= cw && vh - 2 * MARGIN >= ch
                                                   : spaces[side] >= ch && vw - 2 * MARGIN >= cw;
    };
    var order = ["right", "left", "bottom", "top"];
    if (step && step.placement && order.indexOf(step.placement) >= 0) order.unshift(step.placement);
    var side = null;
    if (!docked) for (var i = 0; i < order.length; i++) if (fitsSide(order[i])) { side = order[i]; break; }

    var x, y;
    if (!side) {
      // Small screens or no room beside the target: dock at the bottom (or top, if the target sits low).
      card.classList.add("tt-docked");
      cw = card.offsetWidth; ch = card.offsetHeight;
      x = (vw - cw) / 2;
      var targetMid = r.top + r.height / 2;
      var below = vh - MARGIN - ch;
      y = (targetMid > vh * 0.55 && r.top - GAP - ch >= MARGIN) ? MARGIN : below;
      if (r.height > vh * 0.6) y = below;
      side = "docked";
    } else if (side === "right" || side === "left") {
      x = side === "right" ? r.left + r.width + GAP : r.left - GAP - cw;
      y = r.top + r.height / 2 - ch / 2;
    } else {
      y = side === "bottom" ? r.top + r.height + GAP : r.top - GAP - ch;
      x = r.left + r.width / 2 - cw / 2;
    }
    x = Math.max(MARGIN, Math.min(x, vw - cw - MARGIN));
    y = Math.max(MARGIN, Math.min(y, vh - ch - MARGIN));
    card.style.left = Math.round(x) + "px";
    card.style.top = Math.round(y) + "px";
    card.setAttribute("data-side", side);
  }

  /* ------------------------------------------------------------ keyboard + focus */

  function onKey(e) {
    if (!openState || !ui) return;
    var k = e.key;
    if (k === "Escape" || k === "Esc") { e.preventDefault(); e.stopImmediatePropagation(); close("skip"); return; }
    if (k === "ArrowRight") { e.preventDefault(); e.stopImmediatePropagation(); go(1); return; }
    if (k === "ArrowLeft") { e.preventDefault(); e.stopImmediatePropagation(); if (!ui.back.disabled) go(-1); return; }
    if (k === "Tab") {
      var f = Array.prototype.filter.call(ui.card.querySelectorAll("button"), function (b) { return !b.disabled; });
      if (!f.length) return;
      var first = f[0], last = f[f.length - 1], a = document.activeElement;
      if (e.shiftKey && (a === first || !ui.card.contains(a))) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && (a === last || !ui.card.contains(a))) { e.preventDefault(); first.focus(); }
      e.stopImmediatePropagation();
      return;
    }
    // Everything else: keep the app's own shortcuts (C/E/D, ?, +/-) from firing behind the modal.
    // Enter/Space still activate the focused card button through the browser's default action.
    e.stopImmediatePropagation();
    if (!ui.card.contains(e.target)) e.preventDefault();
  }

  function onFocusIn(e) {
    if (!openState || !ui) return;
    if (!ui.card.contains(e.target)) {
      try { ui.next.focus({ preventScroll: true }); } catch (err) { ui.next.focus(); }
    }
  }

  /* ------------------------------------------------------------ public API */

  // Accepts strings (default step by key) or objects. An object whose key matches a default step
  // is merged over it, so { key: "detail", before: fn } adds a hook without restating the copy.
  function setSteps(arr) {
    if (!Array.isArray(arr)) throw new TypeError("TallyTour.setSteps expects an array");
    steps = arr.map(function (s) {
      var base = null;
      var key = typeof s === "string" ? s : s && s.key;
      for (var i = 0; i < defaults.length; i++) if (defaults[i].key === key) { base = copyStep(defaults[i]); break; }
      if (typeof s === "string") return base || { key: s, title: s, text: "" };
      var o = base || {};
      for (var k in s) if (Object.prototype.hasOwnProperty.call(s, k)) o[k] = s[k];
      return o;
    });
    return steps.slice();
  }

  function maybeStartFirstRun(opts) {
    opts = opts || {};
    if (readRecord() || openState) return false;
    if (firstRunTimer) return true;
    // Wait (up to ~15 s) for at least one anchor to be on screen, e.g. while results render.
    var tries = 0, maxTries = Math.ceil((opts.waitMs != null ? opts.waitMs : 15000) / 250);
    var attempt = function () {
      firstRunTimer = null;
      if (readRecord() || openState) return;
      if (steps.some(function (s) { return !!resolveTarget(s); })) { start({ steps: opts.steps }); return; }
      if (++tries < maxTries) firstRunTimer = setTimeout(attempt, 250);
    };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", attempt, { once: true });
    else attempt();
    return true;
  }

  function reset() {
    clearRecord();
    if (firstRunTimer) { clearTimeout(firstRunTimer); firstRunTimer = null; }
  }

  window.TallyTour = {
    start: function (opts) { return start(opts || {}); },
    maybeStartFirstRun: maybeStartFirstRun,
    reset: reset,
    isOpen: function () { return !!openState; },
    setSteps: setSteps,
    getSteps: function () { return steps.map(copyStep); },
    defaults: function () { return defaults.map(copyStep); },
    next: function () { go(1); },
    back: function () { go(-1); },
    close: function () { close("skip"); },
    status: function () { return readRecord(); },
    STORAGE_KEY: STORAGE_KEY
  };
})();
