"use strict";
/* Tallymark frontend. Plain JS, no build step. Talks to the FastAPI backend on the same origin. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const EXCLUSION_SUMMARY = {
  title_block: "title block", border: "border", revision_block: "revision block", view_labels: "view labels",
  reference_dims: "reference dimensions (in parentheses)", scale_notes: "scale notes", basic_dims: "basic dimensions",
};
const DISP_LABEL = { open: "Open", confirmed: "Confirmed miss", excluded: "Excluded by rule", dismissed: "Dismissed" };
// Hex colours are for the drawing overlay (always on white paper); the CSS variables follow the UI theme.
const DISP_HEX = { open: "#8A94A3", confirmed: "#8E2A22", excluded: "#2F5D8A", dismissed: "#9AA3AE" };
const DISP_COLOR = { open: "var(--disp-open)", confirmed: "var(--disp-confirmed)", excluded: "var(--disp-excluded)", dismissed: "var(--disp-dismissed)" };
const icon = (name) => `<svg class="i" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const RULE_PRESETS = ["Reference dimension", "Title-block tolerance", "Basic dimension", "Other..."];
const SHORTCUTS = [
  ["J / K", "Next / previous flag"], ["C", "Confirm miss"], ["E", "Excluded by customer rule"], ["D", "Dismiss"],
  ["Ctrl+Z", "Undo last disposition"], ["F", "Fit sheet"], ["+ / -", "Zoom"], ["O", "Toggle Faded / Original"],
  ["1 / 2 / 3", "Go to sheet"], ["U / P / A", "Filter Unaccounted / Places / All"], ["B", "View the BoC"], ["Ctrl+E", "Export exceptions"],
  ["Ctrl+Alt+= / -", "Interface size"], ["?", "Shortcut help"],
];
const DISP_SHORT = { open: "Open", confirmed: "Confirmed miss", excluded: "Excluded", dismissed: "Dismissed" };
// UI zoom factor from layout-tools.js (1 when the module is missing). getBoundingClientRect() returns zoomed px,
// style.left/top on a fixed element take local px, so anything that copies one into the other goes through these.
const uiFactor = () => window.TallyLayout?.uiScale?.factor?.() || 1;
const localRect = (el) => window.TallyLayout?.uiScale?.localRect?.(el) || el.getBoundingClientRect();
const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
const STAGE_NAMES = ["Rendering sheets", "Reading text regions", "Matching to BoC rows", "Applying exclusions", "Classifying leftovers", "Checking repeated features"];

const state = {
  screen: "load",
  settings: null,
  files: { drawing: null, boc: null },
  sample: false,
  exclusions: {},
  extraRegex: "",
  useLlm: true,
  runId: null,
  run: null,
  flags: [],
  rows: [],
  coverage: {},
  sheet: 1,
  faded: true,
  filter: "all",
  selected: null,
  view: { s: 1, tx: 0, ty: 0, W: 0, H: 0 },
  locate: null, // a BoC row or covered region picked for display: { sheet, bbox, poly?, label, kind }
  panel: "flags", // right column tab: "flags" | "covered"
  coveredItems: [],
  coveredSel: null,
  coveredQuery: "",
  showCovered: false,
  dragMoved: false,
  undo: [],
  exportPulsed: false,
  skipSheets: [],
};

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  const isJson = (res.headers.get("content-type") || "").includes("json");
  const body = isJson ? await res.json() : await res.text();
  if (!res.ok) throw new Error((body && body.detail) || res.statusText);
  return body;
}

function show(screen) {
  state.screen = screen;
  $("#screen-load").hidden = screen !== "load";
  $("#screen-processing").hidden = screen !== "processing";
  $("#screen-results").hidden = screen !== "results";
  const results = screen === "results";
  $("#btn-export").hidden = !results;
  $("#tb-run-actions").hidden = !results;
  $("#btn-cancel").hidden = screen !== "processing";
  document.body.dataset.screen = screen;
  if (!results) window.TallyBocView?.close();
  closeHelpMenu();
  const fname = state.files.drawing?.filename;
  $("#crumb").hidden = !(fname && screen !== "load");
  $("#crumb-file").textContent = fname || "";
  $("#pill-sim").hidden = !(results && state.run?.boc?.simulated);
}

/* ------------------------------------------------------------------ load screen */
function fmtSize(n) {
  return n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} KB`;
}

function renderCard(kind) {
  const card = $(`#card-${kind}`);
  const f = state.files[kind];
  const chip = $(".file-chip", card);
  const reject = $(".reject", card);
  card.classList.toggle("rejected", !!f?.error);
  $(".dropzone", card).hidden = !!(f && !f.error);
  if (f && !f.error) {
    const meta = kind === "drawing"
      ? `${f.sheets} sheet${f.sheets === 1 ? "" : "s"} · ${fmtSize(f.size)}`
      : `${f.rows} rows · ${f.columns} columns${f.has_bbox ? "" : " · no location columns"}`;
    chip.innerHTML = `<span class="fname" title="${esc(f.filename)}">${esc(f.filename)}</span><span class="fmeta">${meta}</span><button class="fx" aria-label="Remove ${esc(f.filename)}">×</button>`;
    chip.hidden = false;
    $(".fx", chip).onclick = () => { state.files[kind] = null; state.sample = false; renderLoad(); };
  } else {
    chip.hidden = true;
  }
  if (f?.error) {
    reject.innerHTML = `<span>${esc(f.error)}</span><button class="btn btn-sm">Choose another file</button>`;
    reject.hidden = false;
    $("button", reject).onclick = () => $("input[type=file]", card).click();
  } else {
    reject.hidden = true;
  }
}

function renderLoad() {
  renderCard("drawing");
  renderCard("boc");
  const ready = state.files.drawing && !state.files.drawing.error && state.files.boc && !state.files.boc.error;
  $("#btn-run").disabled = !ready;
  $("#run-tip").title = ready ? "" : "Load a drawing and a BoC first.";
  const simulated = state.files.boc?.simulated || /simulated/i.test(state.files.boc?.filename || "");
  $("#banner-sim").hidden = !simulated;
  $("#ack").hidden = !/nist_ftc/i.test(state.files.drawing?.filename || "");
  const on = Object.entries(state.exclusions).filter(([, v]) => v).map(([k]) => EXCLUSION_SUMMARY[k] || k);
  $("#excl-summary").textContent = (on.length ? on.join(", ") : "none") + (state.extraRegex ? `, text matching ${state.extraRegex}` : "");
  renderModelLine();
  $("#banner-mismatch").hidden = true;
}

function renderModelLine() {
  const s = state.settings;
  const el = $("#model-line");
  if (!s) { el.innerHTML = ""; return; }
  if (!state.useLlm) {
    el.innerHTML = `<span class="dot" style="background:var(--muted)"></span><span>Model check off. Ambiguous leftovers are flagged for review without it.</span>`;
  } else if (s.credential_set) {
    const prov = (s.providers || []).find((p) => p.id === s.provider);
    el.innerHTML = `<span class="dot" style="background:var(--success)"></span><span>Model check on ambiguous leftovers: <span class="mono ink">${esc(s.model)}</span>${prov ? ` (${esc(prov.label)})` : ""}</span>`;
  } else {
    el.innerHTML = `<span class="dot" style="background:var(--amber)"></span><span>No API key. Ambiguous leftovers are flagged for review without a model check.</span><button class="link-btn" id="add-key">Add key</button>`;
    $("#add-key").onclick = openSettings;
  }
}

async function acceptFile(kind, file) {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("kind", kind);
  state.sample = false;
  state.files[kind] = { filename: file.name, pending: true };
  try {
    const info = await api("/uploads", { method: "POST", body: fd });
    state.files[kind] = info;
  } catch (e) {
    state.files[kind] = { filename: file.name, error: e.message };
  }
  renderLoad();
}

function wireDropCard(kind) {
  const card = $(`#card-${kind}`);
  const zone = $(".dropzone", card);
  const input = $("input[type=file]", card);
  zone.onclick = () => input.click();
  zone.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } };
  input.onchange = () => { if (input.files[0]) acceptFile(kind, input.files[0]); input.value = ""; };
  card.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("drag"); });
  card.addEventListener("dragleave", () => zone.classList.remove("drag"));
  card.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("drag");
    const f = e.dataTransfer.files[0];
    if (f) acceptFile(kind, f);
  });
}

async function useSample() {
  try {
    const s = await api("/samples");
    state.files.drawing = s.drawing;
    state.files.boc = s.boc;
    state.sample = true;
    renderLoad();
  } catch (e) {
    toast(`Sample could not load: ${e.message}`);
  }
}

async function startRun(force = false) {
  const d = state.files.drawing, b = state.files.boc;
  if (!d || !b || d.error || b.error) return;
  if (!force) {
    try {
      const pre = await api("/precheck", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ drawing_upload_id: d.upload_id, boc_upload_id: b.upload_id }) });
      if (pre.sheets_without_rows.length) {
        const list = pre.sheets_without_rows.join(", ");
        const one = pre.sheets_without_rows.length === 1;
        $("#mismatch-text").textContent = `Sheet${one ? "" : "s"} ${list} appear${one ? "s" : ""} in the drawing but no BoC rows reference ${one ? "it" : "them"}. Continue anyway?`;
        $("#banner-mismatch").hidden = false;
        return;
      }
    } catch (e) { /* precheck is advisory */ }
  }
  $("#banner-mismatch").hidden = true;
  const fd = new FormData();
  fd.append("drawing_upload_id", d.upload_id);
  fd.append("boc_upload_id", b.upload_id);
  fd.append("exclusions", JSON.stringify(Object.keys(state.exclusions).filter((k) => state.exclusions[k])));
  if (state.extraRegex) fd.append("extra_regex", state.extraRegex);
  fd.append("use_llm", state.useLlm ? "true" : "false");
  fd.append("skip_sheets", JSON.stringify(state.skipSheets));
  if (b.simulated) fd.append("simulated", "true");
  try {
    const res = await api("/runs", { method: "POST", body: fd });
    state.runId = res.run_id;
    // A host that runs the check synchronously (serverless) answers with the finished run: go straight to results.
    if (isFinished(res.status)) { clearTimeout(procTimer); await loadResults(); return; }
    beginProcessing();
  } catch (e) {
    toast(e.message);
  }
}

/* ------------------------------------------------------------------ processing */
let procTimer = null;
const isFinished = (status) => status === "complete" || status === "done";

function beginProcessing() {
  show("processing");
  state.run = null;
  state.selected = null;
  $("#proc-error").hidden = true;
  $(".proc-sheet").classList.remove("stopped");
  $("#proc-sheet").innerHTML = `<div class="proc-placeholder"></div>`;
  $("#proc-thumbs").innerHTML = "";
  const nSheets = state.files.drawing.sheets;
  $("#proc-title").textContent = `Checking ${nSheets} sheet${nSheets === 1 ? "" : "s"} against ${state.files.boc.rows} rows`;
  // Stages are shown ticking at a readable pace even when the run is cached and finishes instantly.
  let shown = 0;
  let lastTick = 0;
  let sheetsLoaded = false;
  const MIN_MS = reducedMotion ? 0 : 420;
  const tick = async () => {
    let doc;
    try { doc = await api(`/runs/${state.runId}`); } catch (e) { procTimer = setTimeout(tick, 1000); return; }
    if (state.screen !== "processing" || doc.run_id !== state.runId) return;
    state.run = doc;
    const now = performance.now();
    const doneCount = doc.stages.filter((s) => s.status === "done").length;
    if (shown < doneCount && now - lastTick >= MIN_MS) { shown += 1; lastTick = now; }
    if (!sheetsLoaded && doc.drawing.cache && shown >= 1) {
      sheetsLoaded = true;
      const sheets = doc.sheets_checked || [1];
      $("#proc-sheet").innerHTML = `<img alt="Sheet ${sheets[0]} being checked" src="/runs/${doc.run_id}/sheets/${sheets[0]}.png">`;
      $("#proc-thumbs").innerHTML = sheets.map((n, i) => `<div class="thumb ${i === 0 ? "active" : ""}"><span class="thumb-img"><img alt="" src="/runs/${doc.run_id}/sheets/${n}.png"></span><span class="thumb-foot"><span class="thumb-name">Sheet ${n}</span></span></div>`).join("");
    }
    renderStages(doc, shown);
    if (doc.status === "error") return showProcError(doc);
    if (isFinished(doc.status) && shown >= 6) {
      setTimeout(loadResults, MIN_MS / 2);
      return;
    }
    // Poll the run document: quickly while finished stages are still being revealed, once a second otherwise.
    procTimer = setTimeout(tick, shown < doneCount || isFinished(doc.status) ? 120 : 1000);
  };
  renderStages({ stages: STAGE_NAMES.map((n) => ({ name: n, status: "pending" })) }, 0);
  clearTimeout(procTimer);
  tick();
}

function renderStages(doc, shown) {
  const firstPending = shown;
  $("#stages").innerHTML = doc.stages.map((s, i) => {
    let cls = "pending", mark = "", count = "";
    if (i < shown) { cls = "done"; mark = icon("check"); count = s.count ?? ""; }
    else if (s.status === "error") { cls = "error"; mark = icon("x"); count = s.detail ?? ""; }
    else if (i === firstPending && doc.status !== "error") { cls = "running"; count = s.status === "running" ? (s.detail ?? "") : ""; }
    return `<li class="${cls}"><span class="mark" aria-hidden="true">${mark}</span><span class="st-name">${esc(s.name)}</span><span class="count">${esc(count)}</span></li>`;
  }).join("");
  const pct = Math.round((shown / 6) * 100);
  $("#progress-bar").style.width = `${pct}%`;
  $("#progress").setAttribute("aria-valuenow", String(pct));
  $("#progress-pct").textContent = `${pct}%`;
}

function showProcError(doc) {
  const e = doc.error || {};
  $(".proc-sheet").classList.add("stopped");
  const where = e.sheet ? ` on sheet ${e.sheet}` : "";
  $("#proc-error-text").textContent = `The check stopped at '${e.stage_name || "a stage"}'${where}. ${e.message || ""}`;
  $("#proc-error").hidden = false;
  const skip = $("#err-skip");
  skip.hidden = !e.sheet;
  if (e.sheet) {
    skip.textContent = `Skip sheet ${e.sheet}`;
    skip.onclick = () => { state.skipSheets = [...new Set([...state.skipSheets, e.sheet])]; startRun(true); };
  }
}

/* ------------------------------------------------------------------ results */
async function loadResults() {
  const [doc, flags, rows] = await Promise.all([
    api(`/runs/${state.runId}`), api(`/runs/${state.runId}/flags`), api(`/runs/${state.runId}/rows`),
  ]);
  state.run = doc;
  state.flags = flags;
  state.rows = rows;
  state.coverage = {};
  state.undo = [];
  state.filter = "all";
  state.selected = null;
  state.locate = null;
  state.coveredSel = null;
  state.coveredQuery = "";
  state.exportPulsed = false;
  state.faded = true;
  const sheets = doc.sheets_checked || [1];
  state.sheet = flags[0]?.sheet || sheets[0];
  await loadCoverageAll();
  show("results");
  $("#covered-search").value = "";
  renderTabs();
  setPanel("flags");
  setShowCovered(false);
  renderSummary();
  renderBanners();
  renderThumbs();
  renderChips();
  renderList();
  renderDetail();
  await setSheet(state.sheet, { fit: true });
  // First visit only: the tour waits for its anchors itself; give the layout a frame to settle first.
  requestAnimationFrame(() => setTimeout(() => window.TallyTour?.maybeStartFirstRun?.(), 250));
}

function counts() {
  const live = state.flags.filter((f) => f.disposition !== "dismissed");
  return {
    total: state.flags.length,
    live: live.length,
    u: state.flags.filter((f) => f.class === "unaccounted").length,
    p: state.flags.filter((f) => f.class === "places").length,
    open: state.flags.filter((f) => f.disposition === "open").length,
    reviewed: state.flags.filter((f) => f.disposition !== "open").length,
  };
}

function renderSummary() {
  const st = state.run.stats || {};
  const c = counts();
  const covered = st.covered || 0;
  const left = c.live;
  const denom = Math.max(1, covered + left);
  const pctCovered = Math.round((covered / denom) * 100);
  const allReviewed = c.total > 0 && c.open === 0;
  const revPct = c.total ? Math.round((c.reviewed / c.total) * 100) : 100;
  $("#summary").innerHTML = `
    <div class="sum-hero">
      <div class="big-num ${left === 0 ? "zero" : ""}">${left}</div>
      <div class="big-text"><div class="big-label">unaccounted</div><div class="big-sub">${left === 0 ? "every requirement has a row" : `requirement${left === 1 ? "" : "s"} no BoC row covers`}</div></div>
    </div>
    <div class="cov" aria-label="${covered} covered text regions, ${left} left, ${pctCovered} percent covered">
      <div class="cov-head"><span>Requirement text on the drawing covered by a BoC row</span><b>${pctCovered}%</b></div>
      <div class="cov-bar"><div class="c" style="width:${(covered / denom) * 100}%"></div><div class="l" style="width:${(left / denom) * 100}%"></div></div>
      <div class="cov-legend">
        <button class="cov-link" id="sum-covered" title="List the covered text regions and outline them on the drawing"><i class="sw sw-c"></i><b>${covered}</b> text regions covered${icon("chev-r")}</button>
        <span><i class="sw sw-l"></i><b>${left}</b> left uncovered</span>
        <span class="notices-slot" id="notices-slot"></span>
      </div>
    </div>
    <div class="stat-chips">
      <div class="stat"><b>${state.run.boc.rows}</b><span>BoC rows</span></div>
      <div class="stat" title="Every text region read on the drawing, excluding the title block, border and your exclusions."><b>${st.text_regions ?? 0}</b><span>text regions checked</span></div>
      <div class="stat stat-u" title="U: a requirement on the drawing with no BoC row"><b>${c.u}</b><span><i class="mini-u">U</i>no row</span></div>
      <div class="stat stat-p" title="P: a 4X style callout whose row records fewer places"><b>${c.p}</b><span><i class="mini-p">P</i>places short</span></div>
    </div>
    <div class="review ${allReviewed ? "done" : ""}">
      <div class="review-top">${allReviewed ? `${icon("check")}<span>All ${c.total} reviewed</span>` : `<span><b>${c.reviewed}</b> of ${c.total} reviewed</span>`}</div>
      <div class="review-bar"><i style="width:${revPct}%"></i></div>
    </div>`;
  renderNoticesLink();
  $("#sum-covered").onclick = () => { setPanel("covered"); setShowCovered(true); $("#covered-search")?.focus({ preventScroll: true }); };
  if (allReviewed && !state.exportPulsed) {
    state.exportPulsed = true;
    const b = $("#btn-export");
    b.classList.remove("pulse-once");
    void b.offsetWidth;
    b.classList.add("pulse-once");
  }
}

// Run notices (warnings under the summary). Each can be dismissed; dismissals are kept per run in sessionStorage.
function noticeStoreKey() { return `tallymark.notices.${state.runId}`; }
function dismissedNotices() {
  try { return new Set(JSON.parse(sessionStorage.getItem(noticeStoreKey()) || "[]")); } catch (e) { return new Set(state.noticesHidden || []); }
}
function saveDismissedNotices(set) {
  state.noticesHidden = [...set];
  try { sessionStorage.setItem(noticeStoreKey(), JSON.stringify([...set])); } catch (e) { /* storage blocked: kept for this page only */ }
}
function runNotices() {
  const out = [];
  const st = state.run.stats || {};
  for (const w of state.run.warnings || []) {
    if (w.type === "no_bbox") out.push({ key: "no_bbox", tone: "amber", text: "This BoC has no location columns. Rows were located by matching text, so a few matches may be off." });
    if (w.type === "rows_not_located") out.push({ key: "rows_not_located", tone: "amber", text: `${w.char_nos.length} row${w.char_nos.length === 1 ? "" : "s"} could not be placed on the drawing (CN ${w.char_nos.slice(0, 8).join(", ")}${w.char_nos.length > 8 ? ", ..." : ""}). Anything they cover may show as unaccounted.` });
    if (w.type === "sheet_without_rows") out.push({ key: `sheet_without_rows:${w.sheet}`, tone: "amber", text: `Sheet ${w.sheet} has no BoC rows, so every requirement on it shows as unaccounted.` });
  }
  const llmMsg = { no_credential: "no API key is set", auth_failed: "the API key was rejected", permission_denied: "the key can't use this model", model_not_found: "the model was not found for this key", rate_limited: "the API rate limited the request", connection_failed: "the model API could not be reached", disabled: "it is turned off" };
  if (st.ambiguous && st.llm_error) {
    const why = llmMsg[st.llm_error] || st.llm_error;
    out.push({ key: "llm", tone: "blue", text: `The model check did not run (${why}). ${st.ambiguous} ambiguous leftover${st.ambiguous === 1 ? " is" : "s are"} flagged with low confidence for you to review.` });
  }
  return out;
}
function renderBanners() {
  const all = runNotices();
  const hidden = dismissedNotices();
  const shown = all.filter((n) => !hidden.has(n.key));
  state.noticesHiddenCount = all.length - shown.length;
  $("#result-banners").innerHTML = shown.map((n) => `
    <div class="banner banner-${n.tone} notice" data-key="${esc(n.key)}" role="status">
      <span class="banner-i" aria-hidden="true">${n.tone === "blue" ? "i" : "!"}</span>
      <button type="button" class="notice-text" aria-expanded="false" title="${esc(n.text)}">${esc(n.text)}</button>
      <button type="button" class="notice-x" aria-label="Dismiss this notice" title="Dismiss this notice">${icon("x")}</button>
    </div>`).join("");
  $$("#result-banners .notice").forEach((el) => {
    const txt = $(".notice-text", el);
    txt.onclick = () => { const on = !el.classList.contains("expanded"); el.classList.toggle("expanded", on); txt.setAttribute("aria-expanded", String(on)); };
    $(".notice-x", el).onclick = () => {
      const set = dismissedNotices();
      set.add(el.dataset.key);
      saveDismissedNotices(set);
      renderBanners();
      requestAnimationFrame(() => { if (!state.selected && !state.locate) fitView(false); });
    };
  });
  renderNoticesLink();
}
function renderNoticesLink() {
  const slot = $("#notices-slot");
  if (!slot) return;
  const n = state.noticesHiddenCount || 0;
  slot.innerHTML = n ? `<button type="button" class="notices-link" id="notices-show">${n} notice${n === 1 ? "" : "s"} hidden · <u>Show</u></button>` : "";
  if (n) $("#notices-show").onclick = () => { saveDismissedNotices(new Set()); renderBanners(); requestAnimationFrame(() => { if (!state.selected && !state.locate) fitView(false); }); };
}

function renderThumbs() {
  const sheets = state.run.sheets_checked || [1];
  const byS = (n) => state.flags.filter((f) => f.sheet === n && f.disposition !== "dismissed").length;
  const openS = (n) => state.flags.filter((f) => f.sheet === n && f.disposition === "open").length;
  $("#rail-count").textContent = String(sheets.length);
  $("#thumbs").innerHTML = sheets.map((n) => {
    const k = byS(n), o = openS(n);
    const pill = k
      ? `<span class="count-pill ${o ? "hot" : "done"}">${o ? "" : icon("check")}${plural(k, "flag")}</span>`
      : `<span class="count-pill">No flags</span>`;
    return `<button class="thumb ${n === state.sheet ? "active" : ""}" data-sheet="${n}" aria-pressed="${n === state.sheet}" aria-label="Sheet ${n}, ${plural(k, "flag")}${k ? `, ${o} open` : ""}">
      <span class="thumb-img"><img alt="" loading="lazy" src="/runs/${state.runId}/sheets/${n}.png"></span>
      <span class="thumb-foot"><span class="thumb-name">Sheet ${n}</span>${pill}</span></button>`;
  }).join("");
  $$("#thumbs .thumb").forEach((b) => (b.onclick = () => setSheet(+b.dataset.sheet, { fit: true })));
  $("#sheet-select").innerHTML = sheets.map((n) => `<option value="${n}" ${n === state.sheet ? "selected" : ""}>Sheet ${n} · ${plural(byS(n), "flag")}</option>`).join("");
}

function visibleFlags() {
  return state.flags.filter((f) => {
    if (state.filter === "u") return f.class === "unaccounted";
    if (state.filter === "p") return f.class === "places";
    if (state.filter === "open") return f.disposition === "open";
    return true;
  }).sort((a, b) => (a.disposition === "dismissed") - (b.disposition === "dismissed"));
}

function renderChips() {
  const c = counts();
  const chips = [["all", "All", c.total, "All flags (A)"], ["u", "", c.u, "Unaccounted: no BoC row (U)"], ["p", "", c.p, "Places short (P)"], ["open", "Open", c.open, "Not reviewed yet"]];
  $("#chips").innerHTML = chips.map(([k, l, n, t]) => {
    const lab = k === "u" ? `<i class="mini-u">U</i>No row` : k === "p" ? `<i class="mini-p">P</i>Places` : l;
    return `<button class="chip ${state.filter === k ? "on" : ""}" data-f="${k}" aria-pressed="${state.filter === k}" title="${t}">${lab}<span class="n">${n}</span></button>`;
  }).join("");
  $$("#chips .chip").forEach((b) => (b.onclick = () => { state.filter = b.dataset.f; renderChips(); renderList(); }));
}

function flagColor(f) {
  if (f.disposition !== "open") return DISP_HEX[f.disposition];
  return f.class === "places" ? "#B7791F" : "#C8372D";
}

function flagVar(f) {
  if (f.disposition !== "open") return DISP_COLOR[f.disposition];
  return f.class === "places" ? "var(--amber)" : "var(--red)";
}

function shortText(f) {
  return f.transcription || f.drawing_text || "";
}

function statusPill(f) {
  return `<span class="status-pill s-${f.disposition}">${f.disposition === "confirmed" ? icon("check") : ""}${DISP_SHORT[f.disposition]}</span>`;
}

function renderList() {
  const list = visibleFlags();
  if (!state.flags.length) {
    $("#flag-list").innerHTML = `<div class="empty"><h3>Nothing left on the drawing</h3><p>Every requirement-like region is covered by a BoC row, after exclusions. Review your exclusions if this looks too clean.</p><button class="btn btn-sm" id="empty-excl">${icon("sliders")}View exclusions</button></div>`;
    $("#empty-excl").onclick = openSettings;
    return;
  }
  if (!list.length) {
    $("#flag-list").innerHTML = `<div class="empty"><p>No flags match this filter.</p></div>`;
    return;
  }
  $("#flag-list").innerHTML = list.map((f) => {
    const isP = f.class === "places";
    const cls = isP ? "Places shortfall" : "Unaccounted requirement";
    const aria = `${f.title}. ${cls}, ${f.flag_id}, sheet ${f.sheet} zone ${f.zone}, ${DISP_LABEL[f.disposition].toLowerCase()}`;
    const txt = shortText(f);
    return `<div class="flag-row ${state.selected === f.flag_id ? "selected" : ""} ${f.disposition}" role="option" tabindex="0" aria-selected="${state.selected === f.flag_id}" aria-label="${esc(aria)}" data-id="${f.flag_id}" style="--fc:${flagVar(f)}">
      <span class="flag-badge ${isP ? "p" : "u"}" aria-hidden="true">${isP ? "P" : "U"}</span>
      <div class="flag-body">
        <div class="flag-title">${esc(f.title)}</div>
        <div class="flag-meta"><span class="fid">${f.flag_id}</span><span class="sep">·</span><span>Sheet ${f.sheet}</span><span class="sep">·</span><span>Zone ${f.zone}</span></div>
        ${txt ? `<div class="flag-text" title="Text read on the drawing">${esc(txt)}</div>` : ""}
        <div class="flag-foot">${statusPill(f)}${f.disposition_note ? `<span class="flag-note">${esc(f.disposition_note)}</span>` : ""}</div>
      </div></div>`;
  }).join("");
  $$("#flag-list .flag-row").forEach((r) => {
    r.onclick = () => selectFlag(r.dataset.id);
    r.onkeydown = (e) => { if (e.key === "Enter") selectFlag(r.dataset.id); };
  });
}

/* ------------------------------------------------------------------ covered regions (read-only) */
// Every covered text region of every checked sheet, with the BoC rows that cover it. Built from
// /runs/{id}/sheets/{n}/coverage.json; ids are "<sheet>-<index>" so they stay stable for a run.
async function loadCoverageAll() {
  const sheets = state.run.sheets_checked || [1];
  await Promise.all(sheets.map(async (n) => {
    if (state.coverage[n]) return;
    try { state.coverage[n] = await api(`/runs/${state.runId}/sheets/${n}/coverage.json`); } catch (e) { state.coverage[n] = { covered: [], excluded: [] }; }
  }));
  state.coveredItems = sheets.flatMap((n) => (state.coverage[n].covered || []).map((r, i) => {
    const rows = (r.char_nos || []).map((cn) => state.rows.find((x) => x.char_no === cn && x.sheet === n) || state.rows.find((x) => x.char_no === cn)).filter(Boolean);
    return { id: `${n}-${i}`, sheet: n, poly: r.poly, bbox: r.bbox, text: r.text || "", char_nos: r.char_nos || [], rows };
  }));
}

function coveredTip(item) {
  return item.rows.length ? `Covered by ${item.rows.map((x) => `CN ${x.char_no}: ${x.requirement}`).join("; ")}` : "Covered by a BoC row";
}

function setPanel(panel) {
  state.panel = panel;
  const cov = panel === "covered";
  $("#tab-flags").setAttribute("aria-selected", String(!cov));
  $("#tab-covered").setAttribute("aria-selected", String(cov));
  $("#tab-flags").tabIndex = cov ? -1 : 0;
  $("#tab-covered").tabIndex = cov ? 0 : -1;
  $("#flags-tools").hidden = cov;
  $("#covered-tools").hidden = !cov;
  $("#flag-list").hidden = cov;
  $("#covered-list").hidden = !cov;
  if (cov) renderCoveredList();
}

function renderTabs() {
  $("#tab-flags-n").textContent = String(state.flags.length);
  $("#tab-covered-n").textContent = String(state.coveredItems.length);
}

function renderCoveredList() {
  const q = state.coveredQuery.trim().toLowerCase();
  const items = state.coveredItems.filter((it) => !q || [it.text, ...it.char_nos.map((c) => `cn ${c}`), ...it.rows.map((r) => r.requirement)].join(" ").toLowerCase().includes(q));
  const el = $("#covered-list");
  if (!state.coveredItems.length) { el.innerHTML = `<div class="empty"><p>No text region on the drawing is covered by a BoC row yet.</p></div>`; return; }
  if (!items.length) { el.innerHTML = `<div class="empty"><p>Nothing covered matches "${esc(state.coveredQuery.trim())}".</p></div>`; return; }
  const sheets = [...new Set(items.map((it) => it.sheet))];
  el.innerHTML = sheets.map((n) => {
    const group = items.filter((it) => it.sheet === n);
    return `<div class="cov-group"><div class="cov-group-h">Sheet ${n}<span>${group.length} covered</span></div>${group.map((it) => {
      const req = it.rows.map((r) => r.requirement).join(" | ") || it.text;
      const zone = it.rows[0]?.zone;
      const cns = it.char_nos.length ? it.char_nos.map((c) => `CN ${esc(c)}`).join(", ") : "CN ?";
      return `<button class="cov-row ${state.coveredSel === it.id ? "selected" : ""}" data-cid="${it.id}" aria-pressed="${state.coveredSel === it.id}" title="Show on the drawing">
        <span class="cov-cn">${cns}</span>
        <span class="cov-body"><span class="cov-req">${esc(req)}</span>
          <span class="cov-meta">Sheet ${it.sheet}${zone ? ` · Zone ${esc(zone)}` : ""}</span>
          <span class="cov-ocr"><span class="ocr-k">OCR</span>${esc(it.text)}</span></span></button>`;
    }).join("")}</div>`;
  }).join("");
  $$("#covered-list .cov-row").forEach((b) => (b.onclick = () => selectCovered(b.dataset.cid)));
}

async function selectCovered(id, { fromCanvas = false } = {}) {
  const it = state.coveredItems.find((x) => x.id === id);
  if (!it) return;
  state.coveredSel = id;
  state.selected = null;
  state.locate = { sheet: it.sheet, poly: it.poly, bbox: it.bbox, kind: "covered", label: it.char_nos.length ? `CN ${it.char_nos.join(", ")}` : "Covered" };
  if (state.panel !== "covered") setPanel("covered"); else renderCoveredList();
  if (it.sheet !== state.sheet) await setSheet(it.sheet);
  renderList();
  renderDetail();
  renderOverlay();
  requestAnimationFrame(() => zoomToBox(it.bbox));
  $(`#covered-list .cov-row[data-cid="${id}"]`)?.scrollIntoView({ block: "nearest" });
}

function setShowCovered(on) {
  state.showCovered = on;
  $("#tb-covered").setAttribute("aria-pressed", String(on));
  renderOverlay();
}

/* ------------------------------------------------------------------ canvas */
async function setSheet(n, { fit = false } = {}) {
  const changed = n !== state.sheet || !$("#stage img.sheet-img");
  state.sheet = n;
  const aspect = state.run.page ? state.run.page.height_pt / state.run.page.width_pt : 612 / 792;
  const stage = $("#stage");
  if (changed) {
    $$("img.sheet-img", stage).forEach((i) => i.remove());
    const img = new Image();
    img.className = "sheet-img";
    img.alt = `Drawing sheet ${n}`;
    img.src = `/runs/${state.runId}/sheets/${n}.png`;
    img.style.cssText = "position:absolute;inset:0;width:100%;height:100%;display:block;";
    img.draggable = false;
    stage.prepend(img);
  }
  state.view.aspect = aspect;
  if (!state.coverage[n]) {
    try { state.coverage[n] = await api(`/runs/${state.runId}/sheets/${n}/coverage.json`); } catch (e) { state.coverage[n] = { covered: [], excluded: [] }; }
  }
  $$("#thumbs .thumb").forEach((t) => { const on = +t.dataset.sheet === n; t.classList.toggle("active", on); t.setAttribute("aria-pressed", String(on)); });
  $("#sheet-select").value = String(n);
  if (fit) fitView(false);
  renderOverlay();
  updateViewLabels();
}

function updateViewLabels() {
  const total = state.run.drawing.sheets || (state.run.sheets_checked || [1]).length;
  $("#tb-page").textContent = `Sheet ${state.sheet} of ${total}`;
  $("#tb-zoom").textContent = `${Math.round(state.view.s * 100)}%`;
}

// The viewport carries data-ui-scale-exempt, so inside it visual px == local px and these rects can be used as is.
function layoutBase() {
  const vp = $("#viewport").getBoundingClientRect();
  const pad = 18;
  const aspect = state.view.aspect || 612 / 792;
  let W = vp.width - pad * 2;
  let H = W * aspect;
  if (H > vp.height - pad * 2) { H = vp.height - pad * 2; W = H / aspect; }
  W = Math.max(W, 100); H = Math.max(H, 100);
  const stage = $("#stage");
  stage.style.width = `${W}px`;
  stage.style.height = `${H}px`;
  state.view.W = W; state.view.H = H;
  return { vw: vp.width, vh: vp.height, W, H };
}

function applyView(animate) {
  const stage = $("#stage");
  stage.classList.toggle("animate", !!animate && !reducedMotion);
  const { s, tx, ty } = state.view;
  stage.style.transform = `translate(${tx}px, ${ty}px) scale(${s})`;
  $$("#tags .tag").forEach((t) => (t.style.transform = `scale(${1 / s})`));
  updateViewLabels();
}

function fitView(animate = true) {
  if (state.screen !== "results" || !state.run) return;
  const { vw, vh, W, H } = layoutBase();
  state.view.s = 1;
  state.view.tx = (vw - W) / 2;
  state.view.ty = (vh - H) / 2;
  applyView(animate);
}

function zoomBy(factor) {
  const vp = $("#viewport").getBoundingClientRect();
  const { s, tx, ty } = state.view;
  const ns = Math.min(8, Math.max(0.5, s * factor));
  const cx = vp.width / 2, cy = vp.height / 2;
  state.view.tx = cx - ((cx - tx) / s) * ns;
  state.view.ty = cy - ((cy - ty) / s) * ns;
  state.view.s = ns;
  applyView(true);
}

// Centre a normalized box (0-1 page coords) in the part of the viewport that is not under the BoC panel.
function zoomToBox(b) {
  if (!b) return;
  const { vw, vh, W, H } = layoutBase();
  let left = 0, right = vw;
  const panel = window.TallyBocView?.isOpen?.() && document.querySelector(".tbv-panel");
  if (panel) {
    const vr = $("#viewport").getBoundingClientRect(), pr = panel.getBoundingClientRect();
    if (pr.left > vr.left + 160 && pr.left < vr.right) right = pr.left - vr.left - 12;
  }
  const avail = right - left;
  const padW = b.w * W * 1.6 + 40, padH = b.h * H * 1.6 + 40;
  const s = Math.min(4, Math.max(1, Math.min(avail / padW, vh / padH)));
  const cx = (b.x + b.w / 2) * W, cy = (b.y + b.h / 2) * H;
  state.view.s = s;
  state.view.tx = left + avail / 2 - s * cx;
  state.view.ty = vh / 2 - s * cy;
  applyView(true);
}
function zoomToFlag(f) { zoomToBox(f.bbox); }

function polyPoints(poly, W, H) {
  return poly.map(([x, y]) => `${(x * W).toFixed(1)},${(y * H).toFixed(1)}`).join(" ");
}
function boxPoly(b) { return [[b.x, b.y], [b.x + b.w, b.y], [b.x + b.w, b.y + b.h], [b.x, b.y + b.h]]; }

const COVER_HEX = "#0E8C7F";   // covered outlines on the white drawing paper
const LOCATE_HEX = "#2F5D8A";  // a BoC row picked in the BoC viewer

function renderOverlay() {
  const W = 1000, H = 1000 * (state.view.aspect || 612 / 792);
  const svg = $("#overlay");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "none");
  const covItems = state.coveredItems.filter((it) => it.sheet === state.sheet);
  const flags = state.flags.filter((f) => f.sheet === state.sheet);
  const lit = flags.filter((f) => f.disposition !== "dismissed");
  const loc = state.locate && state.locate.sheet === state.sheet ? state.locate : null;
  const locPoly = loc ? (loc.poly || (loc.bbox ? boxPoly(loc.bbox) : null)) : null;
  const litPolys = lit.map((f) => expandPoly(f.poly, 0.004));
  if (locPoly) litPolys.push(expandPoly(locPoly, 0.004));
  const holes = litPolys.map((p) => `<polygon points="${polyPoints(p, W, H)}" fill="black"/>`).join("");
  const veil = state.faded
    ? `<defs><mask id="veil-mask"><rect x="0" y="0" width="${W}" height="${H}" fill="white"/>${holes}</mask></defs>
       <rect x="0" y="0" width="${W}" height="${H}" fill="#fff" fill-opacity="0.72" mask="url(#veil-mask)"/>`
    : "";
  const showCov = state.showCovered;
  const covered = (state.faded || showCov) ? covItems.map((it) => {
    const sel = state.coveredSel === it.id;
    return `<polygon class="covered-shape ${showCov ? "on" : ""} ${sel ? "selected" : ""}" data-cid="${it.id}" data-tip="${esc(coveredTip(it))}" points="${polyPoints(it.poly, W, H)}" vector-effect="non-scaling-stroke" stroke-width="${showCov ? 1.6 : 1}"/>`;
  }).join("") : "";
  const shapes = flags.map((f) => {
    const color = flagColor(f);
    const dash = f.disposition === "excluded" ? "2 3" : f.class === "places" && f.disposition === "open" ? "7 4" : "none";
    const fill = f.disposition === "dismissed" ? "#9AA3AE" : color;
    const width = f.disposition === "confirmed" ? 3 : 2;
    const tip = `${f.flag_id} · ${f.title} · click to review`;
    return `<polygon class="flag-shape ${state.selected === f.flag_id ? "selected" : ""}" data-id="${f.flag_id}" data-tip="${esc(tip)}"
      points="${polyPoints(expandPoly(f.poly, 0.004), W, H)}" fill="${fill}" fill-opacity="${f.disposition === "dismissed" ? 0.08 : 0.12}"
      stroke="${color}" stroke-width="${width}" stroke-dasharray="${dash}" vector-effect="non-scaling-stroke"/>`;
  }).join("");
  const locShape = locPoly
    ? `<polygon class="locate-shape ${loc.kind}" points="${polyPoints(expandPoly(locPoly, 0.006), W, H)}" fill="none" stroke="${loc.kind === "covered" ? COVER_HEX : LOCATE_HEX}" stroke-width="3" vector-effect="non-scaling-stroke"/>`
    : "";
  // under the veil the drawing is desaturated; flagged ink is re-drawn at full colour through the holes
  const clip = state.faded && litPolys.length
    ? `<defs><clipPath id="lit-clip">${litPolys.map((p) => `<polygon points="${polyPoints(p, W, H)}"/>`).join("")}</clipPath></defs>
       <image href="/runs/${state.runId}/sheets/${state.sheet}.png" x="0" y="0" width="${W}" height="${H}" preserveAspectRatio="none" clip-path="url(#lit-clip)"/>`
    : "";
  svg.innerHTML = `<g>${veil}</g><g>${clip}</g><g>${covered}</g><g>${shapes}</g><g>${locShape}</g>`;
  // grayscale the drawing only while faded, so lit flags keep their ink colour via the hole in the veil
  const img = $("#stage img.sheet-img");
  if (img) img.style.filter = state.faded ? "saturate(0.35)" : "none";

  const inv = `scale(${1 / state.view.s})`;
  const topLeft = (poly) => [Math.min(...poly.map((p) => p[0])), Math.min(...poly.map((p) => p[1]))];
  const covTags = showCov ? covItems.map((it) => {
    const [x, y] = topLeft(it.poly);
    const label = it.char_nos.length ? `CN ${it.char_nos.join(",")}` : "CN";
    return `<span class="tag cvtag ${state.coveredSel === it.id ? "on" : ""}" data-cid="${it.id}" style="left:${x * 100}%;top:calc(${y * 100}% - 15px);transform:${inv}">${esc(label)}</span>`;
  }).join("") : "";
  const flagTags = flags.map((f) => {
    const [x, y] = topLeft(f.poly);
    const kind = f.disposition !== "open" ? f.disposition : f.class === "places" ? "p" : "u";
    return `<span class="tag ${kind}" data-id="${f.flag_id}" style="left:${x * 100}%;top:calc(${y * 100}% - 17px);transform:${inv}">${f.flag_id}</span>`;
  }).join("");
  const locTag = loc && loc.kind === "row" && locPoly
    ? (() => { const [x, y] = topLeft(locPoly); return `<span class="tag loc" style="left:${x * 100}%;top:calc(${y * 100}% - 17px);transform:${inv}">${esc(loc.label)}</span>`; })()
    : "";
  $("#tags").innerHTML = covTags + flagTags + locTag;
  $$("#tags .tag[data-id]").forEach((t) => (t.onclick = (e) => { e.stopPropagation(); selectFlag(t.dataset.id); }));
  $$("#tags .tag[data-cid]").forEach((t) => (t.onclick = (e) => { e.stopPropagation(); selectCovered(t.dataset.cid, { fromCanvas: true }); }));
  $$("#overlay .flag-shape").forEach((p) => (p.onclick = (e) => { e.stopPropagation(); if (!state.dragMoved) selectFlag(p.dataset.id); }));
  $$("#overlay .covered-shape.on").forEach((p) => (p.onclick = (e) => { e.stopPropagation(); if (!state.dragMoved) selectCovered(p.dataset.cid, { fromCanvas: true }); }));
  $("#tb-faded").setAttribute("aria-pressed", String(state.faded));
  $("#tb-original").setAttribute("aria-pressed", String(!state.faded));
  $("#tb-covered").setAttribute("aria-pressed", String(state.showCovered));
}

function expandPoly(poly, d) {
  const cx = poly.reduce((a, p) => a + p[0], 0) / poly.length;
  const cy = poly.reduce((a, p) => a + p[1], 0) / poly.length;
  return poly.map(([x, y]) => {
    const dx = x - cx, dy = y - cy;
    const len = Math.hypot(dx, dy) || 1;
    return [x + (dx / len) * d, y + (dy / len) * d];
  });
}

function wireViewport() {
  const vp = $("#viewport");
  const tip = $("#hover-tip");
  let drag = null;
  vp.addEventListener("mousedown", (e) => {
    if (e.button !== 0 || e.target.closest(".flag-shape, .tag")) return;
    state.dragMoved = false;
    drag = { x: e.clientX, y: e.clientY, tx: state.view.tx, ty: state.view.ty };
    vp.classList.add("dragging");
  });
  window.addEventListener("mousemove", (e) => {
    if (drag) {
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) state.dragMoved = true;
      state.view.tx = drag.tx + dx;
      state.view.ty = drag.ty + dy;
      applyView(false);
      return;
    }
    const target = e.target.closest?.("[data-tip]");
    if (target && vp.contains(target)) {
      const r = vp.getBoundingClientRect();
      tip.textContent = target.dataset.tip;
      tip.hidden = false;
      tip.style.left = `${Math.max(8, Math.min(e.clientX - r.left + 12, r.width - tip.offsetWidth - 8))}px`;
      tip.style.top = `${e.clientY - r.top + 14}px`;
    } else {
      tip.hidden = true;
    }
  });
  window.addEventListener("mouseup", () => { drag = null; vp.classList.remove("dragging"); setTimeout(() => (state.dragMoved = false), 0); });
  vp.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = vp.getBoundingClientRect();
    const { s, tx, ty } = state.view;
    const ns = Math.min(8, Math.max(0.5, s * (e.deltaY < 0 ? 1.12 : 1 / 1.12)));
    const px = e.clientX - r.left, py = e.clientY - r.top;
    state.view.tx = px - ((px - tx) / s) * ns;
    state.view.ty = py - ((py - ty) / s) * ns;
    state.view.s = ns;
    applyView(false);
  }, { passive: false });
  window.addEventListener("resize", () => { if (state.screen === "results") fitView(false); });
}

/* ------------------------------------------------------------------ BoC viewer (boc-view.js) */
function openBoc(opts = {}) {
  if (state.screen !== "results" || !window.TallyBocView) return;
  const f = state.flags.find((x) => x.flag_id === state.selected);
  const cov = state.coveredItems.find((x) => x.id === state.coveredSel);
  const activeCharNo = f ? (f.char_no || f.nearest_char_no) : cov?.char_nos[0];
  closeHelpMenu();
  window.TallyBocView.open({ runId: state.runId, activeCharNo: activeCharNo ?? undefined, onLocate: locateRow, filter: opts.filter });
}

async function locateRow({ sheet, bbox, char_no }) {
  const row = state.rows.find((r) => r.char_no === char_no && r.sheet === sheet);
  state.selected = null;
  state.coveredSel = null;
  state.locate = bbox ? { sheet, bbox, kind: "row", label: `CN ${char_no}` } : null;
  renderDetail();
  renderList();
  if (state.panel === "covered") renderCoveredList();
  await setSheet(sheet, { fit: !bbox });
  renderOverlay();
  if (bbox) requestAnimationFrame(() => zoomToBox(bbox));
  else toast(`CN ${char_no}${row ? "" : " (not on this sheet)"} has no location on sheet ${sheet}; showing the whole sheet.`);
}

/* ------------------------------------------------------------------ selection + detail */
async function selectFlag(id, { zoom = true } = {}) {
  const f = state.flags.find((x) => x.flag_id === id);
  if (!f) return;
  state.selected = id;
  state.coveredSel = null;
  state.locate = null;
  if (state.panel !== "flags") setPanel("flags");
  if (f.sheet !== state.sheet) await setSheet(f.sheet);
  renderList();
  renderOverlay();
  renderDetail();
  if (zoom) requestAnimationFrame(() => zoomToFlag(f));
  $(`#flag-list .flag-row[data-id="${id}"]`)?.scrollIntoView({ block: "nearest" });
}

function clearSelection({ refit = true } = {}) {
  state.selected = null;
  renderDetail();
  renderList();
  renderOverlay();
  if (refit) fitView();
}

function renderDetail() {
  const el = $("#detail");
  const f = state.flags.find((x) => x.flag_id === state.selected);
  if (!f) { el.hidden = true; closeRulePop(); return; }
  el.hidden = false;
  const isP = f.class === "places";
  const color = flagVar(f);
  let boc;
  if (isP) {
    const row = state.rows.find((r) => r.char_no === f.char_no);
    boc = `<div class="boc-quote">
        <div class="bq-head"><span class="cn-chip">CN ${esc(f.char_no)}</span><span class="muted">Sheet ${f.sheet}${row?.zone ? ` · Zone ${esc(row.zone)}` : ""}</span></div>
        <div class="bq-req">${esc(row?.requirement || f.nearest_requirement || f.extracted)}</div>
        <div class="bq-places">Places recorded: <b>${esc(row?.places ?? "?")}</b></div>
      </div>
      <p class="d-note">The callout on the drawing asks for more places than this row records.</p>`;
  } else {
    const dist = f.nearest_distance_mm != null ? `${f.nearest_distance_mm} mm away` : "";
    boc = `<div class="boc-none">${icon("x")}<span>No BoC row covers this text.</span></div>
      ${f.nearest_char_no ? `<div class="boc-quote near">
        <div class="bq-head"><span class="d-k">Nearest row</span><span class="cn-chip">CN ${esc(f.nearest_char_no)}</span><span class="muted">${dist}</span></div>
        <div class="bq-req">${esc(f.nearest_requirement)}</div>
      </div>` : ""}`;
  }
  const conf = f.confidence ? f.confidence[0].toUpperCase() + f.confidence.slice(1) : "";
  const transcribed = f.transcription || f.drawing_text;
  const source = f.transcription ? "Read by the model from the crop" : "Read by OCR";
  el.innerHTML = `
    <div class="detail-head">
      <span class="flag-badge ${isP ? "p" : "u"}" aria-hidden="true">${isP ? "P" : "U"}</span>
      <div class="dh-titles"><div class="dh-title">${f.flag_id} · ${isP ? "Places shortfall" : "Unaccounted requirement"}</div><div class="dh-sub">Sheet ${f.sheet} · Zone ${f.zone}</div></div>
      ${statusPill(f)}
      <button class="btn btn-sm btn-quiet btn-square" id="detail-close" aria-label="Close detail (Esc)" title="Close (Esc)">${icon("x")}</button></div>
    <div class="detail-grid">
      <section class="dcol">
        <h3 class="dcol-h">${icon("table")}What the BoC says</h3>
        ${boc}
      </section>
      <section class="dcol">
        <h3 class="dcol-h">${icon("sheet")}On the drawing</h3>
        <div class="crop-box" style="--fc:${color}"><img alt="Crop of the drawing around ${esc(f.flag_id)}" src="${f.crop_url}"></div>
        ${transcribed ? `<div class="ocr-line"><span class="ocr-k">${source}</span><span class="ocr-t">${esc(transcribed)}</span></div>` : ""}
        <div class="crop-cap">300 DPI crop · Sheet ${f.sheet} · Zone ${f.zone}</div>
      </section>
      <section class="dcol">
        <h3 class="dcol-h">${icon("alert")}Why it's flagged</h3>
        <div class="issue-title">${esc(f.title)}</div>
        <p class="issue-reason">${esc(f.reason)}</p>
        <div class="issue-meta">
          ${conf ? `<span class="conf-pill conf-${esc(f.confidence)}">${conf} confidence</span>` : ""}
          <span class="muted">${esc(f.confidence_basis || "")}</span>
          ${f.rule_ids?.length ? `<span class="rules">Rule ${esc(f.rule_ids.join(", "))}</span>` : ""}
        </div>
        ${f.disposition !== "open" ? `<div class="disp-note" style="--dc:${DISP_COLOR[f.disposition]}">${DISP_LABEL[f.disposition]}${f.disposition_note ? `: ${esc(f.disposition_note)}` : ""}</div>` : ""}
      </section>
    </div>
    <div class="actions" data-tour="decisions">
      <span class="actions-label">Your decision</span>
      <div class="decide" role="group" aria-label="Decision for ${f.flag_id}">
        <button class="btn btn-sm act-confirm ${f.disposition === "confirmed" ? "on" : ""}" id="act-c" aria-pressed="${f.disposition === "confirmed"}">${icon("check")}<span>Confirm miss</span><kbd>C</kbd></button>
        <button class="btn btn-sm act-exclude ${f.disposition === "excluded" ? "on" : ""}" id="act-e" aria-pressed="${f.disposition === "excluded"}">${icon("shield")}<span>Excluded by customer rule</span><kbd>E</kbd></button>
        <button class="btn btn-sm act-dismiss ${f.disposition === "dismissed" ? "on" : ""}" id="act-d" aria-pressed="${f.disposition === "dismissed"}">${icon("slash")}<span>Dismiss</span><kbd>D</kbd></button>
      </div>
      <div class="nav" role="group" aria-label="Move between flags"><button class="btn btn-sm" id="act-k" title="Previous flag (K)">${icon("chev-l")}<span>Prev</span><kbd>K</kbd></button><button class="btn btn-sm" id="act-j" title="Next flag (J)"><span>Next</span><kbd>J</kbd>${icon("chev-r")}</button></div>
    </div>`;
  $("#detail-close").onclick = () => clearSelection();
  $("#act-c").onclick = () => dispose("confirmed");
  $("#act-e").onclick = () => openRulePop();
  $("#act-d").onclick = () => dispose("dismissed");
  $("#act-k").onclick = () => step(-1);
  $("#act-j").onclick = () => step(1);
}

function step(dir) {
  const list = visibleFlags();
  if (!list.length) return;
  const i = list.findIndex((f) => f.flag_id === state.selected);
  const next = list[(i + dir + list.length) % list.length] || list[0];
  selectFlag(next.flag_id);
}

async function dispose(disposition, note = null, { record = true, quiet = false } = {}) {
  const f = state.flags.find((x) => x.flag_id === state.selected);
  if (!f) return;
  await setDisposition(f, disposition, note, { record, quiet });
}

async function setDisposition(f, disposition, note, { record = true, quiet = false } = {}) {
  const prev = { disposition: f.disposition, note: f.disposition_note };
  try {
    const updated = await api(`/runs/${state.runId}/flags/${f.flag_id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ disposition, note }),
    });
    Object.assign(f, updated);
  } catch (e) {
    toast(`Could not save: ${e.message}`);
    return;
  }
  if (record) state.undo.push({ id: f.flag_id, ...prev });
  const verb = { confirmed: "Confirmed", excluded: "Excluded", dismissed: "Dismissed", open: "Reopened" }[disposition];
  if (!quiet) toast(`${verb} ${f.flag_id}.`, record);
  renderSummary();
  renderThumbs();
  renderChips();
  renderList();
  renderOverlay();
  renderDetail();
}

async function undoLast() {
  const last = state.undo.pop();
  if (!last) return;
  const f = state.flags.find((x) => x.flag_id === last.id);
  if (!f) return;
  await setDisposition(f, last.disposition, last.note, { record: false, quiet: true });
  toast(`Undid ${f.flag_id}.`, false);
}

/* rule popover */
let ruleChoice = null;
function openRulePop() {
  const pop = $("#rule-pop");
  const btn = $("#act-e");
  if (!btn) return;
  const f = state.flags.find((x) => x.flag_id === state.selected);
  ruleChoice = f?.disposition === "excluded" ? f.disposition_note : null;
  $("#rule-presets").innerHTML = RULE_PRESETS.map((p) => `<button data-p="${esc(p)}" class="${ruleChoice === p ? "on" : ""}">${esc(p)}</button>`).join("");
  $("#rule-note").value = ruleChoice && !RULE_PRESETS.includes(ruleChoice) ? ruleChoice : "";
  $$("#rule-presets button").forEach((b) => (b.onclick = () => {
    if (b.dataset.p === "Other...") { $("#rule-note").focus(); ruleChoice = null; }
    else { ruleChoice = b.dataset.p; saveRule(); }
  }));
  pop.hidden = false;
  // fixed popover: convert the button's zoomed rect and the window size into local CSS px (UI scale)
  const r = localRect(btn);
  const vw = window.innerWidth / uiFactor();
  pop.style.left = `${Math.max(12, Math.min(r.left, vw - pop.offsetWidth - 12))}px`;
  pop.style.top = `${Math.max(12, r.top - pop.offsetHeight - 8)}px`;
  $("#rule-presets button")?.focus();
}
function closeRulePop() { $("#rule-pop").hidden = true; }
function saveRule() {
  const note = $("#rule-note").value.trim() || ruleChoice || "Customer rule";
  closeRulePop();
  dispose("excluded", note);
}

/* ------------------------------------------------------------------ toast */
let toastTimer = null;
function toast(msg, undoable = false) {
  $("#toast-text").textContent = msg;
  $("#toast-undo").hidden = !undoable;
  $("#toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($("#toast").hidden = true), 4500);
}

/* ------------------------------------------------------------------ export */
function exportName() {
  const stem = (state.run.drawing.filename || "drawing").replace(/\.[^.]+$/, "");
  const short = /ftc_06/.test(stem) ? "ftc06" : stem;
  return `${short}_exceptions_${state.run.created_at.slice(0, 10)}.csv`;
}
function openExport() {
  if (state.screen !== "results") return;
  renderExportPreview();
  $("#export-modal").hidden = false;
  $("#export-download").focus();
}
function renderExportPreview() {
  const incl = $("#export-dismissed").checked;
  const rows = state.flags.filter((f) => incl || f.disposition !== "dismissed").slice(0, 3);
  const n = state.flags.filter((f) => incl || f.disposition !== "dismissed").length;
  $("#csv-preview").innerHTML = `<table><thead><tr><th>flag_id</th><th>class</th><th>sheet</th><th>zone</th><th>drawing_text</th><th>disposition</th></tr></thead><tbody>${
    rows.map((f) => `<tr><td>${f.flag_id}</td><td>${f.class}</td><td>${f.sheet}</td><td>${f.zone}</td><td title="${esc(shortText(f))}">${esc(shortText(f))}</td><td>${f.disposition}</td></tr>`).join("") ||
    `<tr><td colspan="6">No flags to export.</td></tr>`}</tbody></table>`;
  $("#export-name").textContent = `${exportName()} · ${n} row${n === 1 ? "" : "s"}`;
}
async function downloadCsv() {
  const incl = $("#export-dismissed").checked;
  const res = await fetch(`/runs/${state.runId}/exceptions.csv?include_dismissed=${incl}`);
  const blob = await res.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = exportName();
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  $("#export-modal").hidden = true;
  toast(`Downloaded ${exportName()}.`);
}

/* ------------------------------------------------------------------ settings */
async function loadSettings() {
  state.settings = await api("/settings");
  if (!Object.keys(state.exclusions).length) {
    for (const x of state.settings.exclusions) state.exclusions[x.key] = x.default;
  }
}
function openSettings() {
  const s = state.settings;
  $("#excl-toggles").innerHTML = s.exclusions.map((x) => `<div class="toggle-row"><span>${esc(x.label)}</span><button class="switch" role="switch" aria-label="${esc(x.label)}" aria-checked="${!!state.exclusions[x.key]}" data-k="${x.key}"></button></div>`).join("");
  $$("#excl-toggles .switch").forEach((b) => (b.onclick = () => {
    state.exclusions[b.dataset.k] = !state.exclusions[b.dataset.k];
    b.setAttribute("aria-checked", String(state.exclusions[b.dataset.k]));
    renderLoad();
  }));
  $("#excl-regex").value = state.extraRegex;
  renderProviderFields();
  $("#use-llm").checked = state.useLlm;
  renderKeyStatus();
  $("#settings").hidden = false;
  $(".drawer .switch")?.focus();
}
function renderProviderFields() {
  const s = state.settings;
  const prov = s.providers.find((p) => p.id === s.provider);
  $("#provider-select").innerHTML = s.providers.map((p) => `<option value="${p.id}" ${p.id === s.provider ? "selected" : ""}>${esc(p.label)}${p.credential_set ? " ✓" : ""}</option>`).join("");
  $("#model-select").innerHTML = s.models.map((m) => `<option ${m === s.model ? "selected" : ""}>${esc(m)}</option>`).join("");
  $("#api-key-label").textContent = prov.key_label;
  $("#api-key").placeholder = prov.placeholder;
}
function renderKeyStatus(msg, ok) {
  const s = state.settings;
  const el = $("#key-status");
  if (msg) { el.textContent = msg; el.className = `key-status ${ok ? "ok" : "bad"}`; return; }
  if (s.credential_set) {
    const kind = s.credential_kind === "oauth_token" ? "OAuth token" : "API key";
    el.textContent = `${kind} set (${s.credential_source}).`;
    el.className = "key-status ok";
  } else {
    el.textContent = "No key set.";
    el.className = "key-status";
  }
}
async function saveSettings(body) {
  state.settings = await api("/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  renderProviderFields();
  renderKeyStatus();
  renderModelLine();
}

/* ------------------------------------------------------------------ help */
function renderHelp() {
  $("#shortcuts").innerHTML = SHORTCUTS.map(([k, a]) => `<div><span class="k">${k}</span><span class="a">${a}</span></div>`).join("");
}

function openHelpModal() {
  closeHelpMenu();
  $("#help-tour").hidden = state.screen !== "results";
  $("#help").hidden = false;
}

// Help menu in the top bar: absolutely positioned under its button, so no rect math (and no UI-scale issue).
function openHelpMenu() {
  const onResults = state.screen === "results";
  const tour = $("#menu-tour");
  tour.disabled = !onResults;
  $("#menu-tour-sub").textContent = onResults ? "A short walk through the results" : "Available once a check has run";
  $("#help-menu").hidden = false;
  $("#btn-help").setAttribute("aria-expanded", "true");
  (onResults ? tour : $("#menu-keys")).focus();
}
function closeHelpMenu() {
  const m = $("#help-menu");
  if (!m || m.hidden) return false;
  m.hidden = true;
  $("#btn-help").setAttribute("aria-expanded", "false");
  return true;
}
function wireHelpMenu() {
  $("#btn-help").onclick = (e) => { e.stopPropagation(); if ($("#help-menu").hidden) openHelpMenu(); else closeHelpMenu(); };
  $("#menu-tour").onclick = () => { closeHelpMenu(); startTour(); };
  $("#menu-keys").onclick = () => openHelpModal();
  $("#help-menu").addEventListener("keydown", (e) => {
    const items = $$("#help-menu [role=menuitem]:not(:disabled)");
    const i = items.indexOf(document.activeElement);
    if (e.key === "ArrowDown") { e.preventDefault(); items[(i + 1) % items.length]?.focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); items[(i - 1 + items.length) % items.length]?.focus(); }
    else if (e.key === "Tab") closeHelpMenu();
  });
  document.addEventListener("pointerdown", (e) => { if (!e.target.closest(".menu-wrap")) closeHelpMenu(); });
}

function closeOverlays() {
  let closed = closeInfoPop() || closeHelpMenu();
  for (const id of ["#help", "#export-modal", "#settings", "#rule-pop"]) {
    if (!$(id).hidden) { $(id).hidden = true; closed = true; }
  }
  return closed;
}

/* ------------------------------------------------------------------ guided tour (tour.js) */
// The detail and decision steps need an open flag: their before() hook opens the first visible one
// and the tour's end event closes it again if the tour opened it.
let tourOpenedFlag = null;
function prepTourFlag() {
  if (state.panel !== "flags") setPanel("flags");
  if (state.selected) return;
  const first = visibleFlags()[0] || state.flags[0];
  if (!first) return;
  tourOpenedFlag = first.flag_id;
  return selectFlag(first.flag_id).then(() => new Promise((r) => setTimeout(r, 320)));
}
function wireTour() {
  const T = window.TallyTour;
  if (!T?.setSteps) return;
  const defaults = T.defaults?.() || [];
  const flagsText = defaults.find((d) => d.key === "flags")?.text;
  T.setSteps([
    "summary",
    "sheets",
    "canvas",
    { key: "flags", before: () => { if (state.panel !== "flags") setPanel("flags"); },
      text: (flagsText ? `${flagsText} ` : "") + "The <b>Covered</b> tab lists the text a BoC row already covers, read-only." },
    { key: "detail", before: prepTourFlag },
    { key: "decisions", before: prepTourFlag },
    "export",
  ]);
  document.addEventListener("tallytour:end", () => {
    if (tourOpenedFlag && state.selected === tourOpenedFlag && state.screen === "results") clearSelection();
    tourOpenedFlag = null;
  });
}
function startTour() {
  if (state.screen !== "results") return;
  closeOverlays();
  window.TallyTour?.start?.({ force: true });
}

/* ------------------------------------------------------------------ keyboard */
function onKey(e) {
  if (window.TallyBocView?.isOpen?.() || window.TallyTour?.isOpen?.()) return; // both modules handle their own keys
  const typing = e.target.closest?.("input, textarea, select");
  if (e.key === "Escape") {
    if (!closeOverlays() && state.screen === "results") {
      if (state.selected) clearSelection({ refit: false });
      else if (state.locate) { state.locate = null; state.coveredSel = null; renderOverlay(); if (state.panel === "covered") renderCoveredList(); }
    }
    return;
  }
  if (typing) {
    if (e.key === "Enter" && e.target.id === "rule-note") saveRule();
    return;
  }
  if (e.key === "?") { e.preventDefault(); if ($("#help").hidden) openHelpModal(); else $("#help").hidden = true; return; }
  if (state.screen !== "results") return;
  if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "e") { e.preventDefault(); openExport(); return; }
  if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "z") { e.preventDefault(); undoLast(); return; }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (!$("#export-modal").hidden || !$("#settings").hidden || !$("#help").hidden) return;
  const k = e.key.toLowerCase();
  const sheets = state.run.sheets_checked || [1];
  const filterTo = (f) => { if (state.panel !== "flags") setPanel("flags"); state.filter = f; renderChips(); renderList(); };
  if (k === "j") step(1);
  else if (k === "k") step(-1);
  else if (k === "c" && state.selected) dispose("confirmed");
  else if (k === "e" && state.selected) { e.preventDefault(); openRulePop(); }
  else if (k === "d" && state.selected) dispose("dismissed");
  else if (k === "f") fitView();
  else if (k === "+" || k === "=") zoomBy(1.25);
  else if (k === "-" || k === "_") zoomBy(0.8);
  else if (k === "o") { state.faded = !state.faded; renderOverlay(); }
  else if (k === "v") setShowCovered(!state.showCovered);
  else if (k === "b") { e.preventDefault(); openBoc(); }
  else if (/^[1-9]$/.test(k) && sheets.includes(+k)) setSheet(+k, { fit: true });
  else if (k === "u") filterTo("u");
  else if (k === "p") filterTo("p");
  else if (k === "a") filterTo("all");
}

/* ------------------------------------------------------------------ theme */
// Light by default. Dark only when picked with the toggle; the choice is kept in localStorage.
const THEME_KEY = "tallymark-theme";
function storedTheme() {
  try { const t = localStorage.getItem(THEME_KEY); return t === "light" || t === "dark" ? t : null; } catch (e) { return null; }
}
function applyTheme(t) {
  document.documentElement.setAttribute("data-theme", t);
  const next = t === "dark" ? "light" : "dark";
  const b = $("#btn-theme");
  b.setAttribute("aria-label", `Switch to ${next} mode`);
  b.title = `Switch to ${next} mode`;
}
function wireTheme() {
  applyTheme(storedTheme() || "light");
  $("#btn-theme").onclick = () => {
    const t = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    try { localStorage.setItem(THEME_KEY, t); } catch (e) { /* private mode: the choice lasts for this page only */ }
    applyTheme(t);
  };
}

/* ------------------------------------------------------------------ info popovers (load cards) */
// Opens on hover or keyboard focus, stays open on click/tap (pinned), closes on Escape, mouse leave or an outside click.
const infoPop = { id: null, pinned: false, openT: null, closeT: null, loopT: null };
function infoBtn(id) { return $(`.info-btn[data-pop="${id}"]`); }
function openInfoPop(id, { pin = false } = {}) {
  clearTimeout(infoPop.closeT);
  if (infoPop.id && infoPop.id !== id) closeInfoPop();
  const pop = $(`#${id}`);
  const btn = infoBtn(id);
  if (infoPop.id !== id) {
    pop.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    infoPop.id = id;
    positionInfoPop();
    runPopLoop(pop);
  }
  if (pin) infoPop.pinned = true;
}
function closeInfoPop() {
  clearTimeout(infoPop.openT); clearTimeout(infoPop.closeT); clearTimeout(infoPop.loopT);
  if (!infoPop.id) return false;
  const pop = $(`#${infoPop.id}`);
  pop.hidden = true;
  pop.classList.remove("on");
  infoBtn(infoPop.id)?.setAttribute("aria-expanded", "false");
  infoPop.id = null;
  infoPop.pinned = false;
  return true;
}
function scheduleCloseInfoPop() {
  if (infoPop.pinned) return;
  clearTimeout(infoPop.openT);
  clearTimeout(infoPop.closeT);
  infoPop.closeT = setTimeout(closeInfoPop, 220);
}
function positionInfoPop() {
  if (!infoPop.id) return;
  const pop = $(`#${infoPop.id}`);
  // fixed popover: the button's rect and the window size are converted to local CSS px (UI scale)
  const r = localRect(infoBtn(infoPop.id));
  const vw = window.innerWidth / uiFactor(), vh = window.innerHeight / uiFactor();
  const w = pop.offsetWidth, h = pop.offsetHeight;
  const left = Math.max(16, Math.min(r.left - 28, vw - w - 16));
  let top = r.bottom + 10;
  if (top + h > vh - 16 && r.top - h - 10 > 16) top = r.top - h - 10;
  pop.style.left = `${left}px`;
  pop.style.top = `${Math.max(16, Math.min(top, vh - h - 16))}px`;
}
// The sample inside each popover (balloons popping in, BoC rows sliding in) replays on a loop while it is open.
function runPopLoop(pop) {
  clearTimeout(infoPop.loopT);
  pop.classList.remove("on");
  if (reducedMotion) { pop.classList.add("on"); return; }
  void pop.offsetWidth;
  infoPop.loopT = setTimeout(() => {
    pop.classList.add("on");
    infoPop.loopT = setTimeout(() => { if (!pop.hidden) runPopLoop(pop); }, 5200);
  }, 120);
}
function wireInfoPops() {
  $$(".info-btn").forEach((btn) => {
    const id = btn.dataset.pop;
    const pop = $(`#${id}`);
    btn.addEventListener("mouseenter", () => {
      clearTimeout(infoPop.closeT);
      clearTimeout(infoPop.openT);
      infoPop.openT = setTimeout(() => openInfoPop(id), 140);
    });
    btn.addEventListener("mouseleave", scheduleCloseInfoPop);
    btn.addEventListener("focus", () => { if (btn.matches(":focus-visible")) openInfoPop(id); });
    btn.addEventListener("blur", (e) => { if (!pop.contains(e.relatedTarget)) scheduleCloseInfoPop(); });
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      if (infoPop.id === id && infoPop.pinned) closeInfoPop();
      else openInfoPop(id, { pin: true });
    });
    pop.addEventListener("mouseenter", () => clearTimeout(infoPop.closeT));
    pop.addEventListener("mouseleave", scheduleCloseInfoPop);
    pop.addEventListener("focusout", (e) => { if (!pop.contains(e.relatedTarget) && e.relatedTarget !== btn) scheduleCloseInfoPop(); });
    $(".ip-close", pop).onclick = () => { closeInfoPop(); btn.focus(); };
  });
  document.addEventListener("pointerdown", (e) => {
    if (infoPop.id && !e.target.closest(".info-pop, .info-btn")) closeInfoPop();
  });
  window.addEventListener("resize", positionInfoPop);
  $("#screen-load").addEventListener("scroll", positionInfoPop, { passive: true });
}

/* ------------------------------------------------------------------ sneak peek (load screen) */
// Six captioned steps of a miniature results screen. Each step sets data-step plus cumulative at-N classes;
// app.css does the motion. Reduced motion shows the finished state with every caption, and steps stay clickable.
const PEEK_MS = [2600, 3000, 3400, 3400, 3800, 3200];
const peek = { step: 1, timer: null, paused: false };
function setPeekStep(n) {
  const el = $("#peek");
  peek.step = n;
  el.dataset.step = String(n);
  for (let i = 1; i <= 6; i++) el.classList.toggle(`at-${i}`, i <= n);
  el.style.setProperty("--dur", `${PEEK_MS[n - 1]}ms`);
  $$("#peek-steps button").forEach((b) => {
    const k = +b.dataset.step;
    b.classList.toggle("on", k === n);
    b.classList.toggle("done", k < n);
    if (k === n) b.setAttribute("aria-current", "step"); else b.removeAttribute("aria-current");
  });
}
function schedulePeek() {
  clearTimeout(peek.timer);
  if (peek.paused || reducedMotion) return;
  peek.timer = setTimeout(() => {
    // hold the loop while another screen is up or the tab is hidden, so it never runs unseen
    if (state.screen !== "load" || document.hidden) return schedulePeek();
    setPeekStep(peek.step >= 6 ? 1 : peek.step + 1);
    schedulePeek();
  }, PEEK_MS[peek.step - 1]);
}
function setPeekPaused(p) {
  peek.paused = p;
  $("#peek").classList.toggle("paused", p);
  $("#peek-toggle").setAttribute("aria-pressed", String(p));
  $("#peek-toggle-label").textContent = p ? "Play" : "Pause";
  if (p) clearTimeout(peek.timer);
  else { setPeekStep(peek.step); schedulePeek(); }
}
function wirePeek() {
  $$("#peek-steps button").forEach((b) => (b.onclick = () => { setPeekStep(+b.dataset.step); schedulePeek(); }));
  if (reducedMotion) {
    $("#peek").classList.add("static");
    $("#peek-toggle").hidden = true;
    setPeekStep(6);
    return;
  }
  $("#peek-toggle").onclick = () => setPeekPaused(!peek.paused);
  setPeekStep(1);
  schedulePeek();
}

/* ------------------------------------------------------------------ layout tools (layout-tools.js) */
// Resizable side panes and the interface size control. Without the module the app keeps its fixed layout.
function wireLayout() {
  const L = window.TallyLayout;
  if (!L) return;
  const refit = () => { if (state.screen === "results" && !state.selected && !state.locate) fitView(false); };
  const center = $("#canvas-col");
  L.makeResizable($("#rail"), { side: "left", min: 96, max: 320, defaultWidth: 176, storageKey: "tallymark.pane.rail", onResize: refit, center, label: "Resize the sheets panel" });
  L.makeResizable($("#flags-col"), { side: "right", min: 240, max: 560, defaultWidth: 340, storageKey: "tallymark.pane.flags", onResize: refit, center, label: "Resize the flags panel" });
  L.uiScale.mountControl($("#scale-slot-top"));
  L.uiScale.mountControl($("#scale-slot-drawer"), { shortcuts: false });
}

/* ------------------------------------------------------------------ boot */
async function boot() {
  wireDropCard("drawing");
  wireDropCard("boc");
  wireViewport();
  wireTheme();
  wireInfoPops();
  wirePeek();
  renderHelp();
  $("#btn-sample").onclick = useSample;
  $("#btn-run").onclick = () => startRun(false);
  $("#mismatch-continue").onclick = () => startRun(true);
  $("#mismatch-cancel").onclick = () => ($("#banner-mismatch").hidden = true);
  $("#btn-edit-excl").onclick = openSettings;
  $("#btn-settings").onclick = openSettings;
  wireHelpMenu();
  $("#help").onclick = () => ($("#help").hidden = true);
  $("#help-tour").onclick = (e) => { e.stopPropagation(); $("#help").hidden = true; startTour(); };
  $("#btn-boc").onclick = () => openBoc();
  $("#tb-covered").onclick = () => setShowCovered(!state.showCovered);
  $("#tab-flags").onclick = () => setPanel("flags");
  $("#tab-covered").onclick = () => setPanel("covered");
  $$(".panel-tabs [role=tab]").forEach((t) => t.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const other = t.id === "tab-flags" ? "covered" : "flags";
    setPanel(other);
    $(`#tab-${other}`).focus();
  }));
  $("#covered-search").oninput = (e) => { state.coveredQuery = e.target.value; renderCoveredList(); };
  $("#covered-boc").onclick = () => openBoc({ filter: "covered" });
  $("#btn-export").onclick = openExport;
  $("#btn-rerun").onclick = () => { clearTimeout(procTimer); state.skipSheets = []; show("load"); renderLoad(); };
  const cancel = () => { clearTimeout(procTimer); state.runId = null; show("load"); renderLoad(); };
  $("#btn-cancel").onclick = cancel;
  $("#proc-cancel").onclick = cancel;
  $("#err-back").onclick = cancel;
  $("#err-retry").onclick = () => startRun(true);
  $("#toast-undo").onclick = undoLast;
  $("#tb-fit").onclick = () => fitView();
  $("#tb-in").onclick = () => zoomBy(1.25);
  $("#tb-out").onclick = () => zoomBy(0.8);
  $("#tb-faded").onclick = () => { state.faded = true; renderOverlay(); };
  $("#tb-original").onclick = () => { state.faded = false; renderOverlay(); };
  $("#sheet-select").onchange = (e) => setSheet(+e.target.value, { fit: true });
  $("#rule-save").onclick = saveRule;
  $("#rule-cancel").onclick = closeRulePop;
  $("#export-dismissed").onchange = renderExportPreview;
  $("#export-download").onclick = downloadCsv;
  $$("[data-close]").forEach((b) => (b.onclick = () => closeOverlays()));
  $("#settings").onclick = (e) => { if (e.target.id === "settings") closeOverlays(); };
  $("#export-modal").onclick = (e) => { if (e.target.id === "export-modal") closeOverlays(); };
  $("#excl-regex").oninput = (e) => { state.extraRegex = e.target.value.trim(); renderLoad(); };
  $("#use-llm").onchange = (e) => { state.useLlm = e.target.checked; renderModelLine(); };
  $("#provider-select").onchange = (e) => saveSettings({ provider: e.target.value });
  $("#model-select").onchange = (e) => saveSettings({ model: e.target.value });
  $("#key-save").onclick = async () => {
    const v = $("#api-key").value.trim();
    if (!v) return renderKeyStatus("Paste a key first.", false);
    await saveSettings({ credential: v });
    $("#api-key").value = "";
  };
  $("#key-clear").onclick = () => saveSettings({ clear_credential: true });
  $("#key-test").onclick = async () => {
    renderKeyStatus("Testing...", true);
    const r = await api("/settings/test", { method: "POST" });
    renderKeyStatus(r.message, r.ok);
  };
  document.addEventListener("keydown", onKey);
  wireLayout();
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wireTour, { once: true }); else wireTour();
  document.addEventListener("mousedown", (e) => { if (!$("#rule-pop").hidden && !e.target.closest("#rule-pop, #act-e")) closeRulePop(); });
  await loadSettings();
  show("load");
  renderLoad();
}

boot();
