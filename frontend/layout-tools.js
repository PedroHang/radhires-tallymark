/* Tallymark layout tools: resizable side panes + interface scale.
 *
 * Self-contained, no dependencies, no build step. Exposes window.TallyLayout:
 *
 *   TallyLayout.makeResizable(paneEl, {
 *     side: "left" | "right",   // which side of the workspace the pane sits on (required)
 *     min: 160, max: 560,       // pane width bounds, local CSS px
 *     defaultWidth,             // reset target; defaults to the pane's width at init
 *     storageKey,               // localStorage key for the width (optional)
 *     onResize(width),          // rAF-throttled; also fires after keyboard / reset / restore
 *     minCenter: 360,           // the flexible center column never gets narrower than this
 *     center,                   // the flexible center element; auto-detected if omitted
 *     breakpoint: 900,          // below this viewport width resizing is disabled
 *     label: "Resize panel",    // accessible name of the handle
 *   }) -> { handle, getWidth(), setWidth(px), reset(), refresh(), destroy() }
 *
 *   TallyLayout.uiScale = {
 *     steps: [85, 90, 100, 110, 120, 130],
 *     get(), set(pct), step(+1 | -1), reset(), mountControl(containerEl, {shortcuts}),
 *     factor(),                 // current zoom factor (1.1 for 110 %)
 *     onChange(fn) -> unsubscribe,
 *     zoomOf(el),               // effective CSS zoom of an element
 *     localRect(el),            // getBoundingClientRect() converted to el's parent's CSS px
 *     clientToLocal(el, x, y),  // pointer client coords -> px inside el, in el's own CSS px
 *     bindShortcuts(), unbindShortcuts(),
 *   }
 *
 * Scale mechanism: CSS `zoom` on <html>. See the notes above `applyScale` for why, and
 * for the two integration rules (exempt the drawing viewport, fix 100vh).
 */
(function () {
  "use strict";

  var SCALE_KEY = "tallymark.uiScale";
  var STEPS = [85, 90, 100, 110, 120, 130];
  var DEFAULT_SCALE = 100;
  var EVT = "tallymark:uiscale";

  function store(key, value) {
    if (!key) return;
    try { localStorage.setItem(key, String(value)); } catch (e) { /* private mode, blocked storage */ }
  }
  function recall(key) {
    if (!key) return null;
    try { return localStorage.getItem(key); } catch (e) { return null; }
  }
  function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }

  /* ------------------------------------------------------------------ zoom helpers */

  /* Effective CSS zoom of an element (product of every ancestor's zoom). Chromium 128+ and
   * Firefox 126+ expose it as Element.currentCSSZoom; older engines get the manual product. */
  function zoomOf(el) {
    if (!el) return 1;
    if (typeof el.currentCSSZoom === "number" && el.currentCSSZoom > 0) return el.currentCSSZoom;
    var z = 1;
    for (var n = el; n && n.nodeType === 1; n = n.parentElement) {
      var v = parseFloat(getComputedStyle(n).zoom);
      if (v > 0) z *= v;
    }
    return z;
  }

  /* getBoundingClientRect() reports zoomed (visual) px, but style.left/width/transform are read in
   * the element's local CSS px. Mixing the two is the classic bug under `zoom`. localRect() divides
   * by the zoom of the element's parent, i.e. the space you would position a sibling in. */
  function localRect(el) {
    var r = el.getBoundingClientRect();
    var z = zoomOf(el.parentElement || el);
    return { left: r.left / z, top: r.top / z, right: r.right / z, bottom: r.bottom / z, width: r.width / z, height: r.height / z, x: r.left / z, y: r.top / z };
  }

  /* Pointer clientX/clientY -> coordinates inside el, in el's own CSS px (what you would put in
   * el.firstChild.style.left). clientX and getBoundingClientRect() are both visual px, so the
   * difference is visual; dividing by el's zoom brings it back to local px. */
  function clientToLocal(el, clientX, clientY) {
    var r = el.getBoundingClientRect();
    var z = zoomOf(el);
    return { x: (clientX - r.left) / z, y: (clientY - r.top) / z };
  }

  /* ------------------------------------------------------------------ resizable panes */

  var supportsPointer = typeof window.PointerEvent === "function";

  function makeResizable(pane, opts) {
    if (!pane) throw new Error("TallyLayout.makeResizable: pane element is required");
    opts = opts || {};
    var side = opts.side === "right" ? "right" : "left";
    var sign = side === "left" ? 1 : -1; // dragging right grows a left pane, shrinks a right one
    var min = opts.min != null ? opts.min : 120;
    var max = opts.max != null ? opts.max : 640;
    var minCenter = opts.minCenter != null ? opts.minCenter : 360;
    var breakpoint = opts.breakpoint != null ? opts.breakpoint : 900;
    var storageKey = opts.storageKey || null;
    var onResize = typeof opts.onResize === "function" ? opts.onResize : null;
    var STEP = 16;

    var container = pane.parentElement;
    var original = { width: pane.style.width, flex: pane.style.flex, maxWidth: pane.style.maxWidth };
    var defaultWidth = opts.defaultWidth != null ? opts.defaultWidth : (pane.offsetWidth || min);

    function findCenter() {
      if (opts.center) return opts.center;
      var kids = container ? container.children : [];
      for (var i = 0; i < kids.length; i++) {
        var k = kids[i];
        if (k === pane || k.classList.contains("tl-handle")) continue;
        if (parseFloat(getComputedStyle(k).flexGrow) > 0) return k;
      }
      return null;
    }

    var handle = document.createElement("div");
    handle.className = "tl-handle tl-handle-" + side;
    handle.setAttribute("role", "separator");
    handle.setAttribute("aria-orientation", "vertical");
    handle.setAttribute("tabindex", "0");
    handle.setAttribute("aria-label", opts.label || (side === "left" ? "Resize left panel" : "Resize right panel"));
    if (!pane.id) pane.id = "tl-pane-" + Math.random().toString(36).slice(2, 8);
    handle.setAttribute("aria-controls", pane.id);
    handle.title = "Drag to resize. Arrow keys nudge, double-click or Enter resets.";
    if (side === "left") pane.insertAdjacentElement("afterend", handle);
    else pane.insertAdjacentElement("beforebegin", handle);

    var width = null;      // the width currently applied (local px), null while disabled
    var preferred = null;  // the width the user chose; re-applied when room comes back
    var enabled = false;
    var rafId = 0;

    /* Largest width the pane may take right now without squeezing the center below minCenter. */
    function currentMax() {
      var center = findCenter();
      var cap = max;
      if (center) {
        var room = pane.offsetWidth + center.offsetWidth - minCenter;
        cap = Math.min(cap, room);
      }
      return Math.max(min, Math.floor(cap));
    }

    function emit() {
      if (!onResize || rafId) return;
      rafId = requestAnimationFrame(function () { rafId = 0; if (enabled) onResize(width); });
    }

    /* user = true for drag / keyboard / reset: the result becomes the remembered preference.
     * Automatic re-clamps (window or UI scale shrank the room) never overwrite the preference. */
    function apply(w, persist, user) {
      w = Math.round(clamp(w, min, currentMax()));
      width = w;
      if (user) preferred = w;
      pane.style.width = w + "px";
      pane.style.flex = "0 0 " + w + "px";
      pane.style.maxWidth = "none";
      handle.setAttribute("aria-valuemin", String(min));
      handle.setAttribute("aria-valuemax", String(currentMax()));
      handle.setAttribute("aria-valuenow", String(w));
      handle.setAttribute("aria-valuetext", w + " pixels");
      if (persist) store(storageKey, preferred);
      emit();
      return w;
    }

    function startWidth() {
      var saved = parseInt(recall(storageKey), 10);
      return saved > 0 ? saved : defaultWidth;
    }

    function paneVisible() { return pane.getClientRects().length > 0; }

    /* Enable/disable for the breakpoint and for panes the app hides with display:none. */
    function refresh() {
      var wide = window.innerWidth >= breakpoint;
      if (wide && !enabled) {
        enabled = true;
        if (preferred == null) preferred = clamp(startWidth(), min, max);
        apply(preferred, false);
      } else if (!wide && enabled) {
        enabled = false;
        width = null;
        pane.style.width = original.width;
        pane.style.flex = original.flex;
        pane.style.maxWidth = original.maxWidth;
      } else if (enabled) {
        apply(preferred, false); // re-fit after the container changed size
      }
      handle.hidden = !enabled || !paneVisible();
    }

    /* pointer drag */
    var drag = null;
    function onDown(e) {
      if (!enabled || (e.button != null && e.button !== 0)) return;
      e.preventDefault();
      // client deltas are visual px; the pane's width is in its parent's local px
      drag = { id: e.pointerId, x: e.clientX, w: pane.offsetWidth, z: zoomOf(container || pane) };
      if (handle.setPointerCapture && e.pointerId != null) {
        try { handle.setPointerCapture(e.pointerId); } catch (err) { /* synthetic events */ }
      }
      handle.classList.add("tl-active");
      document.documentElement.classList.add("tl-resizing");
      handle.focus({ preventScroll: true });
    }
    function onMove(e) {
      if (!drag || (drag.id != null && e.pointerId != null && e.pointerId !== drag.id)) return;
      apply(drag.w + (sign * (e.clientX - drag.x)) / drag.z, false, true);
    }
    function onUp(e) {
      if (!drag) return;
      if (handle.releasePointerCapture && e && e.pointerId != null) {
        try { handle.releasePointerCapture(e.pointerId); } catch (err) { /* not captured */ }
      }
      drag = null;
      handle.classList.remove("tl-active");
      document.documentElement.classList.remove("tl-resizing");
      store(storageKey, preferred);
    }

    function onKey(e) {
      if (!enabled) return;
      var k = e.key, w = width;
      if (k === "ArrowLeft") w = width - STEP * sign;
      else if (k === "ArrowRight") w = width + STEP * sign;
      else if (k === "Home") w = min;
      else if (k === "End") w = currentMax();
      else if (k === "Enter") w = defaultWidth;
      else return;
      e.preventDefault();
      e.stopPropagation();
      apply(w, true, true);
    }

    function reset() { if (enabled) apply(defaultWidth, true, true); else { preferred = defaultWidth; store(storageKey, defaultWidth); } }

    if (supportsPointer) {
      handle.addEventListener("pointerdown", onDown);
      handle.addEventListener("pointermove", onMove);
      handle.addEventListener("pointerup", onUp);
      handle.addEventListener("pointercancel", onUp);
      handle.addEventListener("lostpointercapture", onUp);
    } else {
      handle.addEventListener("mousedown", function (e) {
        onDown(e);
        function mv(ev) { onMove(ev); }
        function up(ev) { onUp(ev); window.removeEventListener("mousemove", mv); window.removeEventListener("mouseup", up); }
        window.addEventListener("mousemove", mv);
        window.addEventListener("mouseup", up);
      });
    }
    handle.addEventListener("dblclick", reset);
    handle.addEventListener("keydown", onKey);

    var ro = typeof ResizeObserver === "function" ? new ResizeObserver(function () { refresh(); }) : null;
    if (ro) { ro.observe(container || pane); ro.observe(pane); }
    window.addEventListener("resize", refresh);
    window.addEventListener(EVT, refresh);

    refresh();

    return {
      handle: handle,
      getWidth: function () { return enabled ? width : pane.offsetWidth; },
      setWidth: function (px) { return enabled ? apply(px, true, true) : null; },
      reset: reset,
      refresh: refresh,
      destroy: function () {
        if (ro) ro.disconnect();
        window.removeEventListener("resize", refresh);
        window.removeEventListener(EVT, refresh);
        if (rafId) cancelAnimationFrame(rafId);
        handle.remove();
        pane.style.width = original.width;
        pane.style.flex = original.flex;
        pane.style.maxWidth = original.maxWidth;
      },
    };
  }

  /* ------------------------------------------------------------------ interface scale */

  /* Mechanism: CSS `zoom` on <html>, plus a mirror custom property --tally-ui-zoom.
   *
   * Why zoom and not a root font-size / --ui-scale variable: app.css sizes everything in px
   * (fonts, paddings, button heights, panel widths), so a rem or variable based scale would only
   * work after rewriting the stylesheet. `zoom` scales every length, border, icon and fixed
   * overlay at once, re-lays-out (no blurry transform scaling, no overflow), and is standardized
   * (Chromium 128+, Firefox 126+, Safari).
   *
   * What zoom changes for script (measured in Chromium, see the harness):
   *   - getBoundingClientRect() and event.clientX/Y are VISUAL px (already multiplied by zoom).
   *   - offsetWidth/clientWidth and anything you write to style.* are LOCAL px (before zoom).
   *   - native hit-testing (event.target, elementFromPoint) is always correct.
   * Code that copies a getBoundingClientRect() value into style.* is off by the zoom factor.
   * The drawing viewport does exactly that, so it is exempted: an element with
   * [data-ui-scale-exempt] gets zoom: 1 / factor, its effective zoom is 1, visual px == local px,
   * and the existing canvas math (fit, pan, wheel zoom, overlays, hover tip) is untouched.
   *
   * 100vh is also multiplied by zoom, so a full-height shell overflows (or leaves a gap) unless it
   * uses height: 100% or carries [data-ui-scale-fill] (height: calc(100vh / factor)). */
  var listeners = [];
  var current = DEFAULT_SCALE;

  function snap(pct) {
    pct = Number(pct);
    if (!isFinite(pct)) return DEFAULT_SCALE;
    var best = STEPS[0];
    for (var i = 0; i < STEPS.length; i++) if (Math.abs(STEPS[i] - pct) < Math.abs(best - pct)) best = STEPS[i];
    return best;
  }

  function applyScale(pct) {
    var root = document.documentElement;
    var f = pct / 100;
    root.style.setProperty("--tally-ui-zoom", String(f));
    root.style.zoom = pct === 100 ? "" : String(f);
    root.setAttribute("data-ui-scale", String(pct));
  }

  function setScale(pct, opts) {
    var next = snap(pct);
    var changed = next !== current;
    current = next;
    applyScale(next);
    store(SCALE_KEY, next);
    syncControls();
    if (changed && !(opts && opts.silent)) {
      listeners.slice().forEach(function (fn) { try { fn(next); } catch (e) { console.error(e); } });
      window.dispatchEvent(new CustomEvent(EVT, { detail: { scale: next, factor: next / 100 } }));
      // Layout size changed without a real window resize; let resize-driven code (canvas fit) refit.
      window.dispatchEvent(new Event("resize"));
    }
    return next;
  }

  function stepScale(dir) {
    var i = STEPS.indexOf(current);
    if (i < 0) i = STEPS.indexOf(DEFAULT_SCALE);
    return setScale(STEPS[clamp(i + (dir < 0 ? -1 : 1), 0, STEPS.length - 1)]);
  }

  /* controls */
  var controls = [];
  function syncControls() {
    controls.forEach(function (c) {
      c.pct.textContent = current + "%";
      c.pct.setAttribute("aria-label", "Interface size " + current + " percent. Click to reset to 100 percent.");
      c.pct.classList.toggle("tl-is-default", current === DEFAULT_SCALE);
      c.dec.disabled = current <= STEPS[0];
      c.inc.disabled = current >= STEPS[STEPS.length - 1];
    });
  }

  var MOD = /Mac|iPhone|iPad/.test(navigator.platform || "") ? "Ctrl+Option" : "Ctrl+Alt";

  function mountControl(containerEl, o) {
    if (!containerEl) throw new Error("TallyLayout.uiScale.mountControl: container is required");
    o = o || {};
    var wrap = document.createElement("div");
    wrap.className = "tl-scale";
    wrap.setAttribute("role", "group");
    wrap.setAttribute("aria-label", "Interface size");
    wrap.innerHTML =
      '<button type="button" class="tl-scale-btn tl-scale-dec" title="Smaller interface (' + MOD + '+-)" aria-label="Smaller interface"><span aria-hidden="true">A</span><span class="tl-scale-sign" aria-hidden="true">&minus;</span></button>' +
      '<button type="button" class="tl-scale-pct" title="Interface size. Click to reset to 100% (' + MOD + '+0)"></button>' +
      '<button type="button" class="tl-scale-btn tl-scale-inc" title="Larger interface (' + MOD + '+=)" aria-label="Larger interface"><span aria-hidden="true">A</span><span class="tl-scale-sign" aria-hidden="true">+</span></button>';
    var c = { el: wrap, dec: wrap.querySelector(".tl-scale-dec"), pct: wrap.querySelector(".tl-scale-pct"), inc: wrap.querySelector(".tl-scale-inc") };
    c.dec.addEventListener("click", function () { stepScale(-1); });
    c.inc.addEventListener("click", function () { stepScale(1); });
    c.pct.addEventListener("click", function () { setScale(DEFAULT_SCALE); });
    containerEl.appendChild(wrap);
    controls.push(c);
    syncControls();
    if (o.shortcuts !== false) bindShortcuts();
    return wrap;
  }

  /* Ctrl+= / Ctrl+- belong to browser zoom, so the app uses Ctrl+Alt (Ctrl+Option on macOS).
   * Matched on event.code so it works across layouts; ignored while typing because on Windows
   * AltGr reports as Ctrl+Alt and produces characters on some layouts (e.g. ABNT2, German). */
  var shortcutsBound = false;
  function onShortcut(e) {
    if (!e.ctrlKey || !e.altKey || e.shiftKey || e.metaKey) return;
    var t = e.target;
    if (t && t.closest && t.closest("input, textarea, select, [contenteditable=''], [contenteditable='true']")) return;
    var code = e.code;
    if (!code) { // synthetic / remote-input events may carry only e.key
      code = { "=": "Equal", "+": "Equal", "-": "Minus", "_": "Minus", "0": "Digit0" }[e.key] || "";
    }
    if (code === "Equal" || code === "NumpadAdd") stepScale(1);
    else if (code === "Minus" || code === "NumpadSubtract") stepScale(-1);
    else if (code === "Digit0" || code === "Numpad0") setScale(DEFAULT_SCALE);
    else return;
    e.preventDefault();
    e.stopPropagation();
  }
  function bindShortcuts() { if (!shortcutsBound) { window.addEventListener("keydown", onShortcut, true); shortcutsBound = true; } }
  function unbindShortcuts() { window.removeEventListener("keydown", onShortcut, true); shortcutsBound = false; }

  /* initial state: honour what the <head> snippet already applied, otherwise read storage now */
  (function init() {
    var saved = parseInt(recall(SCALE_KEY), 10);
    current = STEPS.indexOf(saved) >= 0 ? saved : DEFAULT_SCALE;
    applyScale(current);
  })();

  window.TallyLayout = {
    makeResizable: makeResizable,
    uiScale: {
      steps: STEPS.slice(),
      storageKey: SCALE_KEY,
      event: EVT,
      get: function () { return current; },
      set: function (pct) { return setScale(pct); },
      step: stepScale,
      reset: function () { return setScale(DEFAULT_SCALE); },
      factor: function () { return current / 100; },
      onChange: function (fn) { listeners.push(fn); return function () { listeners = listeners.filter(function (f) { return f !== fn; }); }; },
      mountControl: mountControl,
      bindShortcuts: bindShortcuts,
      unbindShortcuts: unbindShortcuts,
      zoomOf: zoomOf,
      localRect: localRect,
      clientToLocal: clientToLocal,
    },
  };
})();
