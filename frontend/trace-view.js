/* Tallymark decision-trace views. Self-contained renderer, no build step, no dependencies.
 *
 * Renders what the backend recorded in the decision trace (docs/TRACE.md) as plain-language UI:
 *   stepsHTML(steps, ctx)     vertical timeline for a flag chain (/trace/flags/{id}) or a region record
 *   regionSteps(reg, ctx)     builds the same step list from one region record (/trace/regions/{id})
 *   outcome(reg, ctx)         the region's final outcome as a sentence {tone, title, sub}
 *   callHTML(call, ctx)       one AI request verbatim (/trace/llm/{call_id})
 *   reportHTML(t, ctx)        the run report (funnel, stages, AI calls, settings)
 *   wire(root, ctx)           copy buttons, expanders, lazy "See the exact request" loaders
 * ctx = { runId, catalog, regionsById?, focusCrop? }. Step kinds: measured | rule | ai | score | user.
 * Investigator runs (docs/INVESTIGATOR.md) are shown as tool sub-steps inside the AI step.
 */
(function () {
  "use strict";

  var esc = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };
  var icon = function (n) { return '<svg class="i" aria-hidden="true"><use href="#i-' + n + '"/></svg>'; };
  var num = function (n) { return n == null ? "–" : Number(n).toLocaleString("en-US"); };
  var ms = function (v) { return v == null ? "–" : v < 1000 ? Math.round(v) + " ms" : (v / 1000).toFixed(v < 10000 ? 1 : 0) + " s"; };
  var usd = function (v) { return v == null ? "not priced" : v === 0 ? "$0" : "$" + (v < 0.01 ? v.toFixed(4) : v.toFixed(3)); };
  var pct = function (v) { return v == null ? "–" : Math.round(v * 100) + "%"; };
  var json = function (v) { try { return JSON.stringify(v, null, 2); } catch (e) { return String(v); } };
  var human = function (k) { return String(k == null ? "" : k).replace(/_/g, " ").replace(/^\w/, function (c) { return c.toUpperCase(); }); };

  var KIND = {
    measured: { label: "Measured", icon: "ruler", hint: "Read or measured from the drawing or the BoC by code." },
    rule: { label: "Rule", icon: "rule", hint: "A fixed rule or exclusion decided this. No AI involved." },
    ai: { label: "AI", icon: "sparkle", hint: "A vision model was asked. Its exact request and answer are recorded." },
    score: { label: "Score", icon: "gauge", hint: "The confidence arithmetic." },
    user: { label: "You", icon: "user", hint: "A decision you made." },
  };
  var TOOLS = {
    zoom: "Zoom in", nearby_rows: "Nearby BoC rows", ocr_context: "Text nearby",
    search_boc: "Search the BoC", submit_verdict: "Submit the verdict",
  };
  var OUTCOME_TONE = {
    covered: "teal", covered_by_investigator: "ai-link", excluded: "slate", not_requirement: "slate",
    dropped_by_model: "violet", below_threshold: "amber", merged: "slate",
    flagged_unaccounted: "red", flagged_places: "amber",
  };

  function cat(ctx, section, key) {
    var c = ctx && ctx.catalog && ctx.catalog[section];
    if (!c || key == null) return null;
    if (c[key]) return c[key];
    if (section === "llm_errors") {
      var m = /^(api_error|unexpected)_(.+)$/.exec(key);
      if (m && c[m[1]]) return { label: c[m[1]].label + " (" + m[2] + ")", explain: c[m[1]].explain };
    }
    return null;
  }
  function label(ctx, section, key) { var e = cat(ctx, section, key); return e ? e.label : human(key); }

  /* ---------------------------------------------------------------- small blocks */
  function pre(text, opts) {
    opts = opts || {};
    var long = String(text || "").length > 600 || String(text || "").split("\n").length > 12;
    return '<div class="tv-pre-wrap' + (long ? " tv-long" : "") + '">' +
      '<div class="tv-pre-bar"><span class="tv-pre-label">' + esc(opts.label || "") + "</span>" +
      (long ? '<button type="button" class="tv-mini" data-tv-expand aria-expanded="false">' + icon("chev-d") + "<span>Show all</span></button>" : "") +
      '<button type="button" class="tv-mini" data-tv-copy title="Copy to clipboard">' + icon("copy") + "<span>Copy</span></button></div>" +
      '<pre class="tv-pre" tabindex="0">' + esc(text == null ? "(empty)" : text) + "</pre></div>";
  }
  function disclosure(title, inner, open) {
    return '<details class="tv-disc"' + (open ? " open" : "") + "><summary>" + icon("chev-r") + "<span>" + title + "</span></summary>" +
      '<div class="tv-disc-body">' + inner + "</div></details>";
  }
  function chip(text, cls, title) { return '<span class="tv-chip ' + (cls || "") + '"' + (title ? ' title="' + esc(title) + '"' : "") + ">" + text + "</span>"; }
  function cropImg(ctx, callId, i, alt) {
    if (!ctx.runId || !callId || i == null) return "";
    return '<img class="tv-crop" loading="lazy" alt="' + esc(alt || "Crop sent to the AI") + '" src="/runs/' + encodeURIComponent(ctx.runId) +
      "/trace/llm/" + encodeURIComponent(callId) + "/crops/" + encodeURIComponent(i) + '.png">';
  }

  /* ---------------------------------------------------------------- step bodies */
  function regionsTable(regions, ctx) {
    if (!regions || !regions.length) return "";
    return '<div class="tv-table-wrap"><table class="tv-table"><thead><tr><th scope="col">Text read</th><th scope="col">OCR confidence</th><th scope="col">Read by</th></tr></thead><tbody>' +
      regions.map(function (r) {
        return '<tr><td class="tv-mono">' + esc(r.text || "(no text)") + "</td><td>" + (r.ocr_conf == null ? '<span class="tv-muted">no OCR score</span>' : pct(r.ocr_conf)) +
          "</td><td>" + esc(r.source_label || label(ctx, "sources", r.origin)) + "</td></tr>";
      }).join("") + "</tbody></table></div>";
  }

  function candidatesTable(cands, ctx) {
    if (!cands || !cands.length) return "";
    return '<div class="tv-table-wrap"><table class="tv-table"><thead><tr><th scope="col">Row</th><th scope="col">Requirement</th><th scope="col">Distance</th><th scope="col">Text match</th><th scope="col">Values</th><th scope="col">Result</th></tr></thead><tbody>' +
      cands.map(function (c) {
        var test = c.passed_by ? cat(ctx, "coverage_tests", c.passed_by) : null;
        var res = c.passed
          ? '<span class="tv-pass" title="' + esc(test ? test.explain : "") + '">' + icon("check") + "Covers it" + (test ? ": " + esc(test.label) : "") + "</span>"
          : '<span class="tv-fail">' + icon("x") + "No</span>";
        return "<tr" + (c.passed ? ' class="tv-row-pass"' : "") + "><td><b>CN " + esc(c.char_no) + "</b></td><td class=\"tv-mono\">" + esc(c.requirement) +
          "</td><td>" + (c.distance_mm == null ? "–" : esc(c.distance_mm) + " mm") + "</td><td>" + (c.text_blank ? '<span class="tv-muted">no text</span>' : Math.round(c.text_similarity || 0) + '% <span class="tv-muted">/ 60%</span>') +
          "</td><td>" + (c.numbers_compatible ? "agree" : '<span class="tv-muted">differ</span>') + "</td><td>" + res + "</td></tr>";
      }).join("") + "</tbody></table></div>";
  }

  function checksList(checks, ctx) {
    if (!checks || !checks.length) return "";
    var item = function (c) {
      var e = cat(ctx, "rule_checks", c.check);
      var st = c.status === "fired" ? "Decided" : c.status === "matched_after" ? "Also matched" : "No match";
      return '<li class="tv-check tv-check-' + esc(c.status) + '"><span class="tv-check-st">' + (c.status === "fired" ? icon("check") : "") + st + "</span>" +
        '<span class="tv-check-t"><b>' + esc(e ? e.label : human(c.check)) + "</b>" + (e ? '<span class="tv-muted"> ' + esc(e.explain) + "</span>" : "") +
        (c.evidence ? '<span class="tv-ev">' + esc(c.evidence) + "</span>" : "") + "</span></li>";
    };
    var hits = checks.filter(function (c) { return c.status !== "no_match"; });
    var misses = checks.filter(function (c) { return c.status === "no_match"; });
    return '<ol class="tv-checks">' + hits.map(item).join("") + "</ol>" +
      (misses.length ? disclosure(misses.length + " other check" + (misses.length === 1 ? "" : "s") + " did not match (tried in this order)",
        '<ol class="tv-checks">' + misses.map(item).join("") + "</ol>") : "");
  }

  function verdictBox(v) {
    if (!v) return "";
    return '<dl class="tv-verdict">' +
      "<dt>Answer</dt><dd>" + (v.is_requirement ? '<b class="tv-yes">It is a requirement</b>' : '<b class="tv-no">Not a requirement</b>') + "</dd>" +
      (v.kind ? "<dt>Kind</dt><dd>" + esc(human(v.kind)) + "</dd>" : "") +
      (v.certainty ? "<dt>Certainty</dt><dd>" + esc(human(v.certainty)) + "</dd>" : "") +
      (v.reason ? "<dt>Reason</dt><dd>&ldquo;" + esc(v.reason) + "&rdquo;</dd>" : "") +
      (v.transcription ? '<dt>Read as</dt><dd class="tv-mono">' + esc(v.transcription) + "</dd>" : "") +
      (v.covered_by && v.covered_by.length ? "<dt>Says covered by</dt><dd>" + v.covered_by.map(function (c) { return "CN " + esc(c); }).join(", ") + "</dd>" : "") +
      (v.evidence && v.evidence.length ? "<dt>Evidence</dt><dd>" + v.evidence.map(function (r) { return chip(esc(r), "tv-ref"); }).join(" ") + "</dd>" : "") +
      "</dl>";
  }

  function linkBox(link) {
    if (!link) return "";
    var rows = (link.checks || []).map(function (c) {
      return "<tr" + (c.ok ? ' class="tv-row-pass"' : "") + "><td><b>CN " + esc(c.char_no) + '</b></td><td class="tv-mono">' + esc(c.requirement || "") + "</td><td>" +
        (c.sheet_ok ? "same sheet" : '<span class="tv-muted">other sheet</span>') + "</td><td>" + (c.numbers_compatible ? "agree" : '<span class="tv-muted">differ</span>') +
        "</td><td>" + (c.text_score == null ? "–" : Math.round(c.text_score) + '% <span class="tv-muted">/ ' + esc(c.threshold != null ? c.threshold : 70) + "%</span>") + "</td><td>" +
        (c.ok ? '<span class="tv-pass">' + icon("check") + "Accepted</span>" : '<span class="tv-fail">' + icon("x") + esc(c.why || "Rejected") + "</span>") + "</td></tr>";
    }).join("");
    var cns = (link.char_nos && link.char_nos.length ? link.char_nos : link.claimed || []).map(function (x) { return "CN " + x; }).join(", ");
    return '<div class="tv-link-box ' + (link.ok ? "ok" : "bad") + '"><b>' + (link.ok ? "Linked to " : "Claimed covered by ") + esc(cns) +
      (link.ok ? ". Code checked the claim and agreed, so it is not flagged. Please check it." : ". Code checked the claim and did not agree, so the flag stays.") + "</b>" +
      (link.note ? '<span class="tv-sub">' + esc(link.note) + "</span>" : "") +
      (rows ? '<div class="tv-table-wrap"><table class="tv-table"><thead><tr><th>Row</th><th>Requirement</th><th>Sheet</th><th>Values</th><th>Text match</th><th>Check</th></tr></thead><tbody>' + rows + "</tbody></table></div>" : "") + "</div>";
  }

  function exactRequestBtn(callId, cropIndex) {
    return '<div class="tv-req" data-tv-call="' + esc(callId) + '"' + (cropIndex != null ? ' data-tv-crop="' + esc(cropIndex) + '"' : "") + ">" +
      '<button type="button" class="tv-link" data-tv-load aria-expanded="false">' + icon("sparkle") + "<span>See the exact request and raw answer</span>" + icon("chev-d") + "</button>" +
      '<div class="tv-req-body" hidden></div></div>';
  }

  function toolArgs(args) {
    if (!args || typeof args !== "object") return "";
    return Object.keys(args).filter(function (k) { return k !== "evidence"; }).map(function (k) {
      var v = args[k];
      return chip('<span class="tv-muted">' + esc(human(k)) + "</span> " + esc(typeof v === "object" ? JSON.stringify(v) : v), "tv-arg");
    }).join(" ");
  }
  function toolLabel(ctx, tool) { var e = cat(ctx, "investigator_tools", tool); return e ? e.label : (TOOLS[tool] || human(tool)); }
  // Zoom images: an explicit url, the call record's crops (matched by ref), or the investigate call's
  // crop order (index 0 is the crop, then one per zoom step, in step order).
  function toolImage(s, data, ctx, idx) {
    var url = s.image_url || s.url || (data && data.images && s.image_ref && data.images[s.image_ref]) || null;
    if (!url && s.image_ref && data && data.crops) {
      var c = data.crops.filter(function (x) { return x.ref === s.image_ref; })[0];
      if (c) url = c.url;
    }
    if (!url && s.image_ref && idx && ctx.runId && data && data.call_id) url = "/runs/" + encodeURIComponent(ctx.runId) + "/trace/llm/" + encodeURIComponent(data.call_id) + "/crops/" + idx + ".png";
    return url ? '<img class="tv-crop tv-zoom" loading="lazy" alt="' + esc(toolLabel(ctx, s.tool) + ": the image the AI looked at") + '" src="' + esc(url) + '">' : "";
  }
  function toolSubsteps(steps, data, ctx) {
    var cited = (data && (data.evidence || (data.verdict && data.verdict.evidence))) || [];
    var zi = 0;
    return '<ol class="tv-tools">' + steps.map(function (s) {
      var idx = s.image_ref ? ++zi : null;
      var isCited = s.ref && cited.indexOf(s.ref) >= 0;
      return '<li class="tv-tool' + (s.ok === false ? " tv-tool-fail" : "") + '">' +
        '<div class="tv-tool-h">' + (s.ref ? chip(esc(s.ref), "tv-ref") : chip(String(s.n || ""), "tv-ref tv-ref-n")) +
        "<b>" + esc(toolLabel(ctx, s.tool)) + '</b><span class="tv-mono tv-muted">' + esc(s.tool || "") + "</span>" +
        (s.ok === false ? chip("failed", "tv-chip-bad") : "") + (isCited ? chip("cited as evidence", "tv-chip-ev") : "") +
        (s.ms != null ? '<span class="tv-muted tv-ms">' + ms(s.ms) + "</span>" : "") + "</div>" +
        (toolArgs(s.args) ? '<div class="tv-args">' + toolArgs(s.args) + "</div>" : "") +
        (s.result_summary ? '<div class="tv-tool-r">' + esc(s.result_summary) + "</div>" : "") +
        toolImage(s, data, ctx, idx) + "</li>";
    }).join("") + "</ol>";
  }

  function aiBody(s, ctx) {
    var d = s.data || {};
    var out = "";
    if (d.status === "not_needed") return "";
    var investigator = d.source === "investigator" || (Array.isArray(d.steps) && d.steps.length && d.steps[0].tool);
    if (d.tool && !investigator) {  // one investigator tool call, shown as its own timeline step
      var args = toolArgs(d.args);
      return '<div class="tv-meta">' + (d.ref ? chip(esc(d.ref), "tv-ref") : "") + chip(esc(toolLabel(ctx, d.tool)), "tv-chip-ai") +
        (d.ok === false ? chip("failed", "tv-chip-bad") : "") + (d.ms != null ? chip(ms(d.ms), "") : "") + "</div>" +
        (args ? '<div class="tv-args">' + args + "</div>" : "") + toolImage(d, d, ctx, null);
    }
    if (d.provider || d.model) out += '<div class="tv-meta">' + chip(icon("sparkle") + esc(d.provider || "") + " · " + esc(d.model || ""), "tv-chip-ai") +
      (d.call_id ? chip("request " + esc(d.call_id), "") : "") + (d.crop_index != null && d.call_id ? chip("crop " + esc(d.crop_index), "") : "") + "</div>";
    if (investigator && ctx.toolStepsSeparate) {  // the tool calls are already listed as their own steps above
      var nt = (d.steps || []).filter(function (x) { return x.tool !== "submit_verdict"; }).length;
      out += '<p class="tv-sub">The AI investigated with ' + nt + " read-only tool step" + (nt === 1 ? "" : "s") + " (listed above) before answering.</p>";
      out += '<div class="tv-ai-grid">' + (d.call_id ? cropImg(ctx, d.call_id, d.crop_index != null ? d.crop_index : 0, "The crop the AI was shown, region outlined in red") : "") +
        verdictBox(Object.assign({}, d.verdict, { evidence: d.evidence || (d.verdict || {}).evidence })) + "</div>";
      out += linkBox(d.link);
      if (d.call_id) out += exactRequestBtn(d.call_id, 0);
      return out;
    }
    if (investigator) {
      var stepsN = (d.steps || []).length;
      out += '<p class="tv-sub">The model could look around before answering: ' + stepsN + " tool step" + (stepsN === 1 ? "" : "s") +
        (d.calls != null ? ", " + d.calls + " request" + (d.calls === 1 ? "" : "s") : "") +
        (d.input_tokens != null ? ", " + num((d.input_tokens || 0) + (d.output_tokens || 0)) + " tokens, " + ms(d.latency_ms) + ", " + usd(d.cost_usd) : "") + ". Every tool is read-only.</p>";
      out += toolSubsteps(d.steps || [], d, ctx);
      if (d.verdict) out += verdictBox(Object.assign({}, d.verdict, { evidence: d.evidence || d.verdict.evidence }));
      out += linkBox(d.link);
      if (d.error) out += '<p class="tv-err">' + icon("alert") + esc(label(ctx, "llm_errors", d.error)) + "</p>";
      if (d.turns && d.turns.length) {
        out += disclosure("Requests (" + d.turns.length + ")", '<div class="tv-table-wrap"><table class="tv-table"><thead><tr><th>#</th><th>Final turn</th><th>Tokens in / out</th><th>Time</th><th>Stop</th><th>Tool calls</th></tr></thead><tbody>' +
          d.turns.map(function (t) {
            return "<tr><td>" + esc(t.n) + "</td><td>" + (t.final ? "yes" + (t.forced ? " (forced verdict)" : "") : "no") + "</td><td>" + num(t.input_tokens) + " / " + num(t.output_tokens) +
              "</td><td>" + ms(t.ms) + "</td><td>" + esc(t.stop || "") + '</td><td class="tv-mono">' + esc((t.tool_calls || []).join ? (t.tool_calls || []).join(", ") : t.tool_calls) + "</td></tr>";
          }).join("") + "</tbody></table></div>");
      }
      if (d.raw_messages) out += disclosure("Full transcript (verbatim, images shown as placeholders)", pre(json(d.raw_messages), { label: "transcript" }));
      if (d.call_id) out += exactRequestBtn(d.call_id, 0);
      return out;
    }
    if (d.investigation) {
      var ie = d.investigation;
      out += disclosure("The AI investigator tried first" + (ie.error ? " and stopped (" + esc(label(ctx, "llm_errors", ie.error)) + ")" : "") + ", then one direct question was asked",
        toolSubsteps(ie.steps || [], { call_id: ie.call_id }, ctx) + (ie.call_id ? exactRequestBtn(ie.call_id, 0) : ""));
    }
    if (d.status === "verdict") {
      out += '<div class="tv-ai-grid">' + (d.call_id ? cropImg(ctx, d.call_id, d.crop_index, "The crop the AI was shown, region outlined in red") : "") + verdictBox(d.verdict) + "</div>";
      out += linkBox(d.link);
    } else if (d.transcription !== undefined && d.call_id) {
      out += '<div class="tv-ai-grid">' + cropImg(ctx, d.call_id, d.crop_index) + '<dl class="tv-verdict"><dt>Shown as</dt><dd class="tv-mono">' + esc(d.transcription || "(nothing)") + "</dd><dt>Effect</dt><dd>Display only. It never changes a decision.</dd></dl></div>";
    } else if (d.status === "no_verdict" || d.status === "not_sent") {
      var e = cat(ctx, "llm_errors", d.error);
      out += '<p class="tv-err">' + icon("alert") + "<span><b>" + esc(label(ctx, "model_status", d.status)) + "</b>" + (d.error ? ": " + esc(e ? e.label + ". " + e.explain : d.error) : "") + "</span></p>";
    }
    if (d.call_id) out += exactRequestBtn(d.call_id, d.crop_index);
    return out;
  }

  function scoreBody(s, ctx) {
    var d = s.data || {};
    if (d.product == null && d.base == null) return "";
    var band = d.band || "none";
    var bandLabel = label(ctx, "bands", band);
    var fx = function (v) { return v == null ? "–" : Number.isInteger(Number(v)) ? Number(v).toFixed(1) : String(v); };
    var f = function (v, what, why) { return '<span class="tv-factor" title="' + esc(why || "") + '"><b>' + esc(fx(v)) + "</b><span>" + what + "</span></span>"; };
    var eq = d.product == null
      ? '<span class="tv-band tv-band-none">dropped</span>'
      : '<span class="tv-op">=</span><span class="tv-factor tv-product"><b>' + Number(d.product).toFixed(2) + "</b><span>score</span></span>" +
        '<span class="tv-op">→</span><span class="tv-band tv-band-' + esc(band) + '">' + esc(bandLabel) + "</span>";
    return '<div class="tv-formula" role="group" aria-label="Score arithmetic">' + f(d.base, "rule", cat(ctx, "factors", "base") && cat(ctx, "factors", "base").explain) +
      '<span class="tv-op">×</span>' + f(d.locate_factor, "location", d.locate_reason) + '<span class="tv-op">×</span>' + f(d.llm_factor, "AI", d.llm_reason) + eq + "</div>" +
      '<ul class="tv-reasons">' + (d.locate_reason ? "<li><b>Location:</b> " + esc(d.locate_reason) + "</li>" : "") +
      (d.llm_reason ? "<li><b>AI:</b> " + esc(d.llm_reason) + "</li>" : "") +
      "<li><b>Needs:</b> " + esc(d.threshold != null ? d.threshold : 0.5) + " to flag; High is 0.8+, Medium 0.6+.</li></ul>";
  }

  function stepBody(s, ctx) {
    var d = s.data || {};
    var out = "";
    if (s.kind === "measured") {
      out += regionsTable(d.regions, ctx);
      out += candidatesTable(d.candidates, ctx);
      if ((!d.candidates || !d.candidates.length) && d.nearest_row && !/Nearest row/.test(s.detail || "")) out += '<p class="tv-sub">Nearest row: <b>CN ' + esc(d.nearest_row.char_no) + "</b> <span class=\"tv-mono\">" + esc(d.nearest_row.requirement) + "</span>, " + esc(d.nearest_row.distance_mm) + " mm away.</p>";
      if (d.rows_placed_here && d.rows_placed_here.length) out += '<p class="tv-sub">' + d.rows_placed_here.map(function (p) { return "CN " + esc(p.char_no) + " had no location and was placed here by text match (score " + esc(Math.round(p.score)) + ")."; }).join(" ") + "</p>";
    } else if (s.kind === "rule") {
      if (d.rule_id) { var r = cat(ctx, "rules", d.rule_id); if (r) out += '<p class="tv-sub">' + esc(r.explain) + "</p>"; }
      if (d.applied) {
        var x = cat(ctx, "exclusions", d.applied);
        out += '<p class="tv-sub"><b>' + esc(x ? x.label : human(d.applied)) + ":</b> " + esc(x ? x.explain : "") +
          (d.detail && d.detail.zone ? " Zone: " + esc(d.detail.zone) + "." : "") + (d.detail && d.detail.pattern ? ' Pattern: <span class="tv-mono">' + esc(d.detail.pattern) + "</span>" : "") + "</p>";
      } else if (d.enabled && d.enabled.length && !/Checked:/.test(s.detail || "")) {
        out += '<div class="tv-chips">' + d.enabled.map(function (k) { return chip(esc(label(ctx, "exclusions", k)), ""); }).join("") +
          (d.extra_regex ? chip('pattern <span class="tv-mono">' + esc(d.extra_regex) + "</span>", "") : "") + "</div>";
      }
      if (d.check && !/callout says/i.test(s.detail || "")) {
        var c = d.check;
        out += '<p class="tv-sub">Callout says <b>' + esc(c.callout_places) + "</b> places; row CN " + esc(c.char_no) + " records <b>" + esc(c.recorded == null ? "?" : c.recorded) + "</b> (" + esc(human(c.recorded_field || "places")) + ").</p>";
      }
      if (d.checks) out += checksList(d.checks, ctx);
    } else if (s.kind === "ai") {
      out += aiBody(s, ctx);
    } else if (s.kind === "score") {
      out += scoreBody(s, ctx);
    } else if (s.kind === "user" && d.note) {
      out += '<p class="tv-sub">Your note: &ldquo;' + esc(d.note) + "&rdquo;</p>";
    }
    return out;
  }

  function stepHTML(s, ctx) {
    var kind = KIND[s.kind] ? s.kind : "measured";
    var k = KIND[kind];
    var hasData = s.data && Object.keys(s.data).length;
    return '<li class="tv-step tv-k-' + kind + '">' +
      '<span class="tv-node" aria-hidden="true">' + icon(k.icon) + "</span>" +
      '<div class="tv-card">' +
      '<div class="tv-head"><span class="tv-badge" title="' + esc(k.hint) + '">' + k.label + '</span><h4 class="tv-title">' + esc(s.title) + "</h4></div>" +
      (s.detail ? '<p class="tv-detail">' + esc(s.detail) + "</p>" : "") +
      stepBody(s, ctx) +
      (hasData && !ctx.noRaw ? disclosure("Show the recorded data", pre(json(s.data), { label: "step " + (s.n || "") + " data" })) : "") +
      "</div></li>";
  }

  function stepsHTML(steps, ctx) {
    ctx = Object.assign({}, ctx || {});
    if (!steps || !steps.length) return '<p class="tv-empty">Nothing was recorded for this item.</p>';
    ctx.toolStepsSeparate = steps.some(function (s) { return s.kind === "ai" && s.data && s.data.tool; });
    var kinds = {};
    steps.forEach(function (s) { kinds[KIND[s.kind] ? s.kind : "measured"] = 1; });
    var legend = '<div class="tv-legend" aria-label="Who decided each step">' + Object.keys(KIND).filter(function (k) { return kinds[k]; }).map(function (k) {
      return '<span class="tv-lg tv-k-' + k + '"><span class="tv-badge">' + KIND[k].label + "</span><span>" + esc(KIND[k].hint) + "</span></span>";
    }).join("") + "</div>";
    return legend + '<ol class="tv-steps">' + steps.map(function (s) { return stepHTML(s, ctx); }).join("") + "</ol>";
  }

  /* ---------------------------------------------------------------- region -> steps */
  function regionSteps(reg, ctx) {
    var steps = [];
    var add = function (kind, title, detail, data) { steps.push({ n: steps.length + 1, kind: kind, title: title, detail: detail, data: data || {} }); };
    var srcs = (reg.sources || []).map(function (s) { return label(ctx, "sources", s); });
    add("measured", "Read the text", 'Read "' + (reg.text || "(no text)") + '" on sheet ' + reg.sheet +
      (reg.ocr_conf != null ? ", OCR confidence " + pct(reg.ocr_conf) : "") + (srcs.length ? ". Seen by: " + srcs.join(", ") + "." : "."),
      { regions: [{ region_id: reg.region_id, text: reg.text, ocr_conf: reg.ocr_conf, origin: reg.origin }], fcf_cells: reg.fcf_cells, sources: reg.sources });
    var cov = reg.coverage || {};
    var cands = cov.candidates || [];
    var mdetail;
    if (cov.covered) mdetail = "Yes. Row" + (cov.covered_by.length > 1 ? "s " : " ") + cov.covered_by.map(function (c) { return "CN " + c; }).join(", ") + " cover" + (cov.covered_by.length > 1 ? "" : "s") + " this text.";
    else if (cands.length) {
      var best = cands.slice().sort(function (a, b) { return (b.text_similarity || 0) - (a.text_similarity || 0); })[0];
      mdetail = "No. " + cands.length + " row(s) sit here but none matched: closest is CN " + best.char_no + ", text " + Math.round(best.text_similarity || 0) + "% similar (needs 60%), values " + (best.numbers_compatible ? "agree" : "differ") + ".";
    } else if (cov.nearest_row) mdetail = "No. No row's location overlaps it. Nearest row is CN " + cov.nearest_row.char_no + ", " + cov.nearest_row.distance_mm + " mm away.";
    else mdetail = "No. No BoC row is located on this sheet.";
    add("measured", "Matched to a BoC row?", mdetail, { candidates: cands, nearest_row: cands.length ? null : cov.nearest_row, rows_placed_here: cov.rows_placed_here });
    var ex = reg.exclusion || {};
    if (!ex.checked) add("rule", "Excluded?", ex.note || "Not checked: text a row covers is never excluded.", { applied: null });
    else if (ex.applied) add("rule", "Excluded: " + (ex.label || label(ctx, "exclusions", ex.applied)), "This text is hidden by an exclusion, so it is not judged further.", { applied: ex.applied, detail: ex.detail });
    else add("rule", "Excluded?", "No exclusion applies.", { applied: null, enabled: ctx.enabledExclusions || [] });
    var c = reg.classification;
    if (c) {
      var fired = (c.checks || []).filter(function (x) { return x.status === "fired"; })[0];
      add("rule", c.rule_id + ": " + c.rule_label, c.reason + (fired && fired.evidence ? ' Matched: "' + fired.evidence + '".' : "") +
        ((c.region_ids || []).length > 1 ? ' Judged together with the text on the same line: "' + c.text + '".' : ""),
        { rule_id: c.rule_id, outcome: c.outcome, group_id: c.group_id, group_text: c.text, checks: c.checks });
    }
    var m = reg.model;
    if (m) {
      var v = m.verdict;
      if (m.source === "investigator" || (m.status === "verdict" && v)) {
        add("ai", v ? (v.is_requirement ? "AI check: it is a requirement" : "AI check: not a requirement") : "AI check did not answer",
          v ? (m.provider || "") + " " + (m.model || "") + " answered with " + v.certainty + ' certainty: "' + v.reason + '".' : "The AI could not reach a verdict.", m);
      } else {
        add("ai", "AI check did not answer", "The AI check did not run or did not answer, so the item was scored as unconfirmed.", m);
      }
    } else if (c && c.outcome === "ambiguous") {
      add("ai", "AI check not run", "Ambiguous, but the AI check was off or had no key for this run.", { status: "not_sent" });
    }
    (reg.places || []).forEach(function (p) {
      if (p.callout_places == null) return;
      add("rule", "P1: Places check", p.shortfall ? "The callout says " + p.callout_places + " places; row CN " + p.char_no + " records " + p.recorded + ". Flagged." : "The callout says " + p.callout_places + " places; row CN " + p.char_no + " records " + p.recorded + ". That is enough.", { rule_id: "P1", check: p });
    });
    if (reg.score) {
      var sc = reg.score;
      add("score", sc.product == null ? "Score: dropped" : "Score " + Number(sc.product).toFixed(2) + ": " + label(ctx, "bands", sc.band || "none"),
        sc.product == null ? "The AI said it is not a requirement, so no score was kept." :
          sc.base + " (rule) × " + sc.locate_factor + " (location) × " + sc.llm_factor + " (AI) = " + Number(sc.product).toFixed(3) + ".", sc);
    }
    if (reg.user && reg.user.restored_flag_id) add("user", "Restored by you", "You restored this as flag " + reg.user.restored_flag_id + ". The machine's decision above is kept unchanged.", reg.user);
    return steps;
  }

  function linkedCN(reg) {
    var f = reg.final || {};
    var m = reg.model || {};
    var v = m.verdict || {};
    var cn = f.char_no || (f.char_nos && f.char_nos[0]) || f.linked_char_no || (m.link && m.link.ok && (m.link.char_nos || [])[0]) ||
      (v.covered_by && v.covered_by[0]) || v.duplicate_of || m.duplicate_of || m.linked_char_no ||
      ((reg.coverage || {}).covered_by || [])[0];
    return cn == null ? null : String(cn);
  }

  function outcome(reg, ctx) {
    var f = reg.final || {};
    var o = f.outcome;
    var tone = OUTCOME_TONE[o] || "slate";
    var c = reg.classification || {};
    var cov = reg.coverage || {};
    var v = (reg.model || {}).verdict || {};
    var sc = reg.score || {};
    switch (o) {
      case "covered": return { tone: tone, title: "Covered by " + (cov.covered_by || []).map(function (x) { return "CN " + x; }).join(", "), sub: "A BoC row accounts for this text, so it is greyed out." };
      case "covered_by_investigator": return { tone: tone, title: "Linked by AI to " + (linkedCN(reg) ? "CN " + linkedCN(reg) : "a BoC row") + ". Please check.", sub: "No row matched by location and text; the AI said an existing row already states it, and code checked that row's sheet, values and text before accepting." };
      case "excluded": { var x = cat(ctx, "exclusions", (reg.exclusion || {}).applied); return { tone: tone, title: "Excluded: " + ((reg.exclusion || {}).label || (x && x.label) || "by an exclusion"), sub: x ? x.explain : "" }; }
      case "not_requirement": return { tone: tone, title: "Ruled not a requirement: " + c.rule_id + " " + c.rule_label, sub: c.reason };
      case "dropped_by_model": return { tone: tone, title: "Dropped by AI: the model said it is not a requirement", sub: v.reason ? "“" + v.reason + "” (" + v.certainty + " certainty)" : "" };
      case "below_threshold": return { tone: tone, title: "Not flagged: scored " + (sc.product == null ? "–" : Number(sc.product).toFixed(2)) + ", needs 0.5", sub: "It looked like a requirement but the evidence was too weak to flag." };
      case "merged": return { tone: tone, title: "Merged into " + (f.flag_ids || []).join(", "), sub: "It overlaps a stronger flag, so it is shown as part of that flag." };
      case "flagged_unaccounted": return { tone: tone, title: "Flagged: no BoC row (" + (f.flag_ids || []).join(", ") + ")", sub: c.title || "" };
      case "flagged_places": return { tone: tone, title: "Flagged: places shortfall (" + (f.flag_ids || []).join(", ") + ")", sub: "Covered, but the callout's nX count is more than the row records." };
      default: return { tone: tone, title: f.label || human(o), sub: "" };
    }
  }

  /* ---------------------------------------------------------------- one AI request */
  function callHTML(call, ctx) {
    ctx = ctx || {};
    var crops = call.crops || [];
    var req = call.request || {};
    var job = cat(ctx, "llm_jobs", call.job);
    var err = call.error ? cat(ctx, "llm_errors", call.error) : null;
    var focus = ctx.focusCrop != null ? Number(ctx.focusCrop) : null;
    return '<div class="tv-call">' +
      '<div class="tv-meta">' + chip(esc(call.call_id), "tv-chip-strong") + chip(esc(job ? job.label : human(call.job)), "") + chip(icon("sparkle") + esc(call.provider) + " · " + esc(call.model), "tv-chip-ai") +
      chip(num(call.input_tokens) + " in / " + num(call.output_tokens) + " out tokens", "") + chip(ms(call.latency_ms), "") + chip(usd(call.cost_usd), "") +
      (call.stop_reason ? chip("stop: " + esc(call.stop_reason), "") : "") + (call.error ? chip(esc(err ? err.label : call.error), "tv-chip-bad") : "") + "</div>" +
      (call.error_detail ? '<p class="tv-err">' + icon("alert") + "<span>" + esc(call.error_detail) + "</span></p>" : "") +
      '<h5 class="tv-h5">The exact question (system prompt)</h5>' + pre(call.system_prompt, { label: "system prompt" }) +
      '<h5 class="tv-h5">Sent with the images</h5>' + pre(call.instruction, { label: "instruction" }) +
      (crops.length ? '<h5 class="tv-h5">Images sent (' + crops.length + ")" + (focus != null ? ", this item is crop " + focus : "") + '</h5><div class="tv-crops">' + crops.map(function (c) {
        var img = ctx.runId ? cropImg(ctx, call.call_id, c.index, "Crop " + c.index) : c.url ? '<img class="tv-crop" loading="lazy" alt="Crop ' + esc(c.index) + '" src="' + esc(c.url) + '">' : "";
        return '<figure class="tv-cropfig' + (focus === c.index ? " tv-focus" : "") + '">' + img +
          "<figcaption>" + (c.ref && c.ref !== "crop" ? "Zoom " + esc(c.ref) : "Crop " + esc(c.index)) + (c.flag_id ? " · " + esc(c.flag_id) : "") + "</figcaption></figure>";
      }).join("") + "</div>" : "") +
      (call.steps ? '<h5 class="tv-h5">Tool calls, in order</h5>' + toolSubsteps(call.steps, call, ctx) : "") +
      (call.turns && call.turns.length ? '<h5 class="tv-h5">Each request, with the text the model wrote</h5>' + call.turns.map(function (t) {
        return '<div class="tv-meta">' + chip("request " + esc(t.n), "tv-chip-strong") + (t.final ? chip("final turn" + (t.forced ? ", verdict forced" : ""), "") : "") +
          chip(num(t.input_tokens) + " in / " + num(t.output_tokens) + " out", "") + chip(ms(t.ms), "") + (t.stop ? chip("stop: " + esc(t.stop), "") : "") +
          ((t.tool_calls || []).length ? chip("tools: " + esc([].concat(t.tool_calls).join(", ")), "tv-chip-ai") : "") + "</div>" +
          (t.text ? pre(t.text, { label: "text of request " + t.n }) : "");
      }).join("") : "") +
      (call.raw_response_text != null || !call.turns ? '<h5 class="tv-h5">Raw answer, exactly as returned</h5>' + pre(call.raw_response_text, { label: "raw response" }) : "") +
      (call.raw_messages ? disclosure("Full transcript (verbatim, images shown as placeholders)", pre(json(call.raw_messages), { label: "transcript" })) : "") +
      disclosure("Message layout (text and image order)", pre(json(call.user_content), { label: "user content" })) +
      disclosure("Parsed answer (JSON)", pre(json(call.parsed), { label: "parsed" })) +
      disclosure("Request settings" + (req.response_schema_name ? " and schema " + esc(req.response_schema_name) : ""),
        '<dl class="tv-dl"><dt>Max tokens</dt><dd>' + esc(req.max_tokens) + "</dd><dt>Reasoning effort</dt><dd>" + esc(req.reasoning_effort || "default") + "</dd>" +
        (call.response_id ? "<dt>Response id</dt><dd class=\"tv-mono\">" + esc(call.response_id) + "</dd>" : "") + "</dl>" +
        (req.response_schema ? pre(json(req.response_schema), { label: "response schema" }) : "")) +
      "</div>";
  }

  /* ---------------------------------------------------------------- run report */
  var FUNNEL_ORDER = ["read", "covered", "excluded", "leftover", "not_requirement", "requirement_by_rule", "ambiguous", "sent_to_model",
    "dropped_by_model", "covered_by_investigator", "below_threshold", "merged", "flagged_unaccounted", "flagged_places"];
  var FUNNEL_TONE = { read: "ink", covered: "teal", covered_by_investigator: "ai-link", excluded: "slate", leftover: "ink", not_requirement: "slate",
    requirement_by_rule: "ink", ambiguous: "violet", sent_to_model: "violet", dropped_by_model: "violet", below_threshold: "amber", merged: "slate",
    flagged_unaccounted: "red", flagged_places: "amber" };

  function funnelHTML(t) {
    var fun = (t.funnel || []).slice();
    var byKey = {};
    fun.forEach(function (s) { byKey[s.key] = s; });
    if (!byKey.covered_by_investigator && t.outcomes && t.outcomes.covered_by_investigator && t.outcomes.covered_by_investigator.count) {
      byKey.covered_by_investigator = { key: "covered_by_investigator", label: "Linked by AI to an existing row", parent: "ambiguous", count: t.outcomes.covered_by_investigator.count, region_ids: t.outcomes.covered_by_investigator.region_ids || null };
    }
    var keys = FUNNEL_ORDER.filter(function (k) { return byKey[k]; }).concat(Object.keys(byKey).filter(function (k) { return FUNNEL_ORDER.indexOf(k) < 0; }));
    var total = Math.max(1, (byKey.read || {}).count || 1);
    var depth = function (s) { var d = 0, p = s.parent; while (p && byKey[p] && d < 4) { d++; p = byKey[p].parent; } return d; };
    return '<ol class="tr-funnel">' + keys.map(function (k) {
      var s = byKey[k];
      var w = Math.max(s.count ? 0.8 : 0, (s.count / total) * 100);
      var flags = s.flag_ids && s.flag_ids.length ? " · " + s.flag_ids.slice(0, 6).join(", ") + (s.flag_ids.length > 6 ? " and " + (s.flag_ids.length - 6) + " more" : "") : "";
      var sub = s.groups != null ? " (" + s.groups + " lines)" : "";
      return '<li class="tr-fn tr-tone-' + (FUNNEL_TONE[k] || "ink") + '" style="--d:' + depth(s) + '">' +
        '<button type="button" class="tr-fn-btn" data-fn="' + esc(k) + '" aria-expanded="false"' + (s.count ? "" : " disabled") + ">" +
        '<span class="tr-fn-label">' + esc(s.label) + '<span class="tv-muted">' + esc(sub + flags) + "</span></span>" +
        '<span class="tr-fn-bar" aria-hidden="true"><i style="width:' + w.toFixed(2) + '%"></i></span>' +
        '<span class="tr-fn-n">' + num(s.count) + "</span>" + (s.count ? icon("chev-d") : "") + "</button>" +
        '<div class="tr-fn-list" hidden></div></li>';
    }).join("") + "</ol>";
  }

  function regionListHTML(ids, ctx) {
    var R = ctx.regionsById || {};
    var shown = ids.slice(0, 300);
    return '<ul class="tr-items">' + shown.map(function (id) {
      var r = R[id];
      if (!r) return '<li><span class="tv-mono tv-muted">' + esc(id) + "</span></li>";
      return '<li><button type="button" class="tr-item" data-region="' + esc(id) + '" title="Show on the drawing and inspect">' +
        '<span class="tv-mono">' + esc(r.text || "(no text)") + '</span><span class="tv-muted">Sheet ' + esc(r.sheet) + " · " + esc((r.final || {}).label || "") + "</span>" + icon("inspect") + "</button></li>";
    }).join("") + "</ul>" + (ids.length > shown.length ? '<p class="tv-muted">' + (ids.length - shown.length) + " more not listed; the audit JSON has them all.</p>" : "");
  }

  function fmtIO(o) {
    if (o == null) return "–";
    if (typeof o !== "object") return esc(o);
    return Object.keys(o).map(function (k) {
      var v = o[k];
      if (Array.isArray(v)) v = v.length ? v.join(", ") : "none";
      else if (v && typeof v === "object") v = JSON.stringify(v);
      return '<span class="tr-io"><span class="tv-muted">' + esc(human(k)) + "</span> " + esc(v) + "</span>";
    }).join(" ");
  }

  function stagesHTML(t, ctx) {
    var st = t.stages || [];
    if (!st.length) return '<p class="tv-empty">No stages were recorded.</p>';
    var t0 = Math.min.apply(null, st.map(function (s) { return Date.parse(s.started_at) || Infinity; }));
    var end = Math.max.apply(null, st.map(function (s) { return (Date.parse(s.started_at) || t0) + (s.ms || 0); }));
    var total = Math.max(1, (t.run && t.run.duration_ms) || (end - t0) || 1, end - t0);
    return '<ol class="tr-stages">' + st.map(function (s) {
      var start = (Date.parse(s.started_at) || t0) - t0;
      var left = Math.max(0, Math.min(99, (start / total) * 100));
      var width = Math.max(0.7, Math.min(100 - left, ((s.ms || 0) / total) * 100));
      var skipped = s.status === "skipped";
      var why = s.detail && s.detail.error ? " (" + label(ctx, "llm_errors", s.detail.error) + ")" : "";
      var sheets = (s.sheets || []).map(function (x) {
        return '<li><span>Sheet ' + esc(x.sheet) + "</span><span>" + num(x.regions) + " regions · " + ms(x.ms) + "</span>" + (x.cache_hit ? chip("from cache", "tv-chip-cache", "Text came from the OCR cache of an earlier run of the same file, so this is the cache read time.") : "") + "</li>";
      }).join("");
      return '<li class="tr-stage' + (skipped ? " tr-skipped" : "") + '">' +
        '<div class="tr-st-h"><b>' + esc(s.label || label(ctx, "stages", s.key)) + "</b><span class=\"tr-st-ms\">" + (skipped ? "skipped" + esc(why) : ms(s.ms)) + "</span></div>" +
        '<div class="tr-gantt" aria-hidden="true"><i style="left:' + left.toFixed(2) + "%;width:" + width.toFixed(2) + '%"></i></div>' +
        '<div class="tr-st-io"><span class="tr-io-k">In</span> ' + fmtIO(s.in) + ' <span class="tr-io-k">Out</span> ' + fmtIO(s.out) + "</div>" +
        (sheets ? '<ul class="tr-sheets">' + sheets + "</ul>" : "") + "</li>";
    }).join("") + "</ol>";
  }

  function callsHTML(t, ctx) {
    var calls = t.llm_calls || [];
    var llm = t.llm || {};
    if (!calls.length) {
      var why = llm.classify_error && llm.classify_error !== "disabled" ? " The AI check could not run (" + label(ctx, "llm_errors", llm.classify_error) + ")." : "";
      return '<div class="tr-empty">' + icon("rule") + "<div><b>No AI calls in this run: rules decided everything.</b><p>" + "Add a key in Settings to enable the model check." + esc(why) + "</p></div></div>";
    }
    return '<p class="tv-sub">' + calls.length + " request" + (calls.length === 1 ? "" : "s") + ", " + num(llm.input_tokens) + " input and " + num(llm.output_tokens) + " output tokens, " + usd(llm.cost_usd) + " in total. Open a row to see the exact prompt, the images and the raw answer.</p>" +
      '<div class="tr-calls" role="table" aria-label="AI calls">' +
      '<div class="tr-call-row tr-call-head" role="row"><span role="columnheader">Request</span><span role="columnheader">Job</span><span role="columnheader">Provider · model</span><span role="columnheader">Items</span><span role="columnheader">Tokens in / out</span><span role="columnheader">Time</span><span role="columnheader">Cost</span><span role="columnheader">Error</span></div>' +
      calls.map(function (c) {
        return '<div class="tr-call" role="rowgroup" data-tv-call="' + esc(c.call_id) + '">' +
          '<button type="button" class="tr-call-row" role="row" data-tv-load aria-expanded="false">' +
          '<span role="cell" data-l="Request"><b>' + esc(c.call_id) + "</b>" + icon("chev-d") + "</span>" +
          '<span role="cell" data-l="Job">' + esc(label(ctx, "llm_jobs", c.job)) + "</span>" +
          '<span role="cell" data-l="Model" class="tv-mono">' + esc(c.provider) + " · " + esc(c.model) + "</span>" +
          '<span role="cell" data-l="Items">' + num(c.n_crops) + "</span>" +
          '<span role="cell" data-l="Tokens">' + num(c.input_tokens) + " / " + num(c.output_tokens) + "</span>" +
          '<span role="cell" data-l="Time">' + ms(c.latency_ms) + "</span>" +
          '<span role="cell" data-l="Cost">' + usd(c.cost_usd) + "</span>" +
          '<span role="cell" data-l="Error">' + (c.error ? '<span class="tv-chip tv-chip-bad">' + esc(label(ctx, "llm_errors", c.error)) + "</span>" : '<span class="tv-muted">none</span>') + "</span>" +
          '</button><div class="tv-req-body" hidden></div></div>';
      }).join("") + "</div>";
  }

  function settingsHTML(t, ctx) {
    var r = t.run || {};
    var s = r.settings || {};
    var app = r.app || {};
    var inp = r.inputs || {};
    var v = app.versions || {};
    var file = function (f, what) {
      if (!f) return "";
      return "<dt>" + what + "</dt><dd><b>" + esc(f.filename) + "</b> · " + num(f.bytes) + " bytes" + (f.rows != null ? " · " + f.rows + " rows" : "") +
        '<span class="tv-mono tr-sha">sha256 ' + esc(f.sha256) + "</span></dd>";
    };
    return '<dl class="tv-dl tr-dl">' +
      "<dt>Exclusions</dt><dd>" + ((s.exclusions || []).length ? s.exclusions.map(function (k) { return chip(esc(label(ctx, "exclusions", k)), ""); }).join(" ") : "none") +
      (s.extra_regex ? ' and pattern <span class="tv-mono">' + esc(s.extra_regex) + "</span>" : "") + "</dd>" +
      "<dt>AI check</dt><dd>" + (s.use_llm ? "On" + (s.provider ? ": " + esc(s.provider) + " · <span class=\"tv-mono\">" + esc(s.model) + "</span>" : "") +
        (s.credential_source ? " (key from " + esc(s.credential_source === "session" ? "this server's memory" : s.credential_source) + ", never recorded)" : " (no key)") : "Off") + "</dd>" +
      (s.investigator != null ? "<dt>Investigator</dt><dd>" + (s.investigator ? "On" : "Off") + "</dd>" : "") +
      "<dt>Thresholds</dt><dd>Flag at " + esc(s.flag_threshold) + ", coverage pad " + esc(s.coverage_pad) + ", place rows by text at score " + esc(s.relocate_min_score) + "</dd>" +
      (s.skip_sheets && s.skip_sheets.length ? "<dt>Skipped sheets</dt><dd>" + esc(s.skip_sheets.join(", ")) + "</dd>" : "") +
      (s.simulated_boc ? "<dt>BoC</dt><dd>Simulated export (prepared for the demo)</dd>" : "") +
      file(inp.drawing, "Drawing") + file(inp.boc, "BoC") +
      (r.page ? "<dt>Page</dt><dd>" + esc(r.page.width_mm) + " mm wide, template " + esc(r.page.template) + ", sheets " + esc((r.page.sheets_checked || []).join(", ")) + " of " + esc(r.page.sheets_total) + "</dd>" : "") +
      "<dt>App</dt><dd>" + esc(app.name || "Tallymark") + " · pipeline " + esc(app.pipeline_version) + " · commit <span class=\"tv-mono\">" + esc(app.git_commit || "unknown") + "</span>" + (app.git_dirty ? " (with local changes)" : "") + "</dd>" +
      "<dt>Libraries</dt><dd>" + Object.keys(v).map(function (k) { return chip(esc(k) + " " + esc(v[k]), "tv-chip-mono"); }).join(" ") + "</dd>" +
      "<dt>Trace</dt><dd>schema " + esc(t.schema_version) + " · wording " + esc(t.catalog_version) + "</dd>" +
      "</dl>";
  }

  function reportHTML(t, ctx) {
    var r = t.run || {};
    var inv = t.invariants || {};
    var invHTML = Object.keys(inv).length ? '<ul class="tr-inv">' + Object.keys(inv).map(function (k) {
      return '<li class="' + (inv[k] ? "ok" : "bad") + '">' + icon(inv[k] ? "check" : "x") + "<span>" + esc(k) + "</span></li>";
    }).join("") + "</ul>" : "";
    return '<section class="tr-sec" aria-labelledby="tr-h-funnel"><h3 id="tr-h-funnel">' + icon("route") + "Where every piece of text ended up</h3>" +
      '<p class="tv-sub">Counts are text regions (one callout can be several pieces). Open a bar to list them; pick one to see it on the drawing with its full record.</p>' +
      funnelHTML(t) + invHTML + "</section>" +
      '<section class="tr-sec" aria-labelledby="tr-h-stages"><h3 id="tr-h-stages">' + icon("gauge") + "Stages, in order (" + ms(r.duration_ms) + " in total)</h3>" + stagesHTML(t, ctx) + "</section>" +
      '<section class="tr-sec" aria-labelledby="tr-h-ai"><h3 id="tr-h-ai">' + icon("sparkle") + "Every AI call</h3>" + callsHTML(t, ctx) + "</section>" +
      '<section class="tr-sec" aria-labelledby="tr-h-set"><h3 id="tr-h-set">' + icon("sliders") + "Settings and versions used</h3>" + settingsHTML(t, ctx) + "</section>";
  }

  /* ---------------------------------------------------------------- behaviour */
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise(function (resolve) {
      var ta = document.createElement("textarea");
      ta.value = text; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); } catch (e) { /* ignore */ }
      ta.remove(); resolve();
    });
  }

  var callCache = {};
  function fetchCall(runId, callId) {
    var key = runId + "|" + callId;
    if (!callCache[key]) {
      callCache[key] = fetch("/runs/" + encodeURIComponent(runId) + "/trace/llm/" + encodeURIComponent(callId)).then(function (r) {
        if (!r.ok) throw new Error(r.status === 404 ? "This request is not in the trace." : "The server answered " + r.status + ".");
        return r.json();
      });
      callCache[key].catch(function () { delete callCache[key]; });
    }
    return callCache[key];
  }

  function wire(root, ctx) {
    if (!root || root.__tvWired) return;
    root.__tvWired = true;
    root.addEventListener("click", function (e) {
      var copy = e.target.closest("[data-tv-copy]");
      if (copy && root.contains(copy)) {
        var p = copy.closest(".tv-pre-wrap").querySelector(".tv-pre");
        copyText(p.textContent).then(function () {
          var span = copy.querySelector("span");
          span.textContent = "Copied";
          setTimeout(function () { span.textContent = "Copy"; }, 1400);
        });
        return;
      }
      var exp = e.target.closest("[data-tv-expand]");
      if (exp && root.contains(exp)) {
        var w = exp.closest(".tv-pre-wrap");
        var on = !w.classList.contains("tv-open");
        w.classList.toggle("tv-open", on);
        exp.setAttribute("aria-expanded", String(on));
        exp.querySelector("span").textContent = on ? "Show less" : "Show all";
        return;
      }
      var load = e.target.closest("[data-tv-load]");
      if (load && root.contains(load)) {
        var host = load.closest("[data-tv-call]");
        var body = host.querySelector(".tv-req-body");
        var open = load.getAttribute("aria-expanded") !== "true";
        load.setAttribute("aria-expanded", String(open));
        host.classList.toggle("tv-expanded", open);
        body.hidden = !open;
        if (open && !body.dataset.loaded) {
          var c = ctx();
          body.innerHTML = '<p class="tv-muted">Loading the recorded request...</p>';
          fetchCall(c.runId, host.dataset.tvCall).then(function (call) {
            body.dataset.loaded = "1";
            body.innerHTML = callHTML(call, Object.assign({}, c, { focusCrop: host.dataset.tvCrop }));
          }, function (err) { body.innerHTML = '<p class="tv-err">' + icon("alert") + "<span>" + esc(err.message) + "</span></p>"; });
        }
      }
    });
  }

  window.TallyTrace = {
    KIND: KIND, esc: esc,
    stepsHTML: stepsHTML, regionSteps: regionSteps, outcome: outcome, linkedCN: linkedCN,
    callHTML: callHTML, reportHTML: reportHTML, regionListHTML: regionListHTML,
    wire: function (root, ctxOrFn) { wire(root, typeof ctxOrFn === "function" ? ctxOrFn : function () { return ctxOrFn; }); },
    copyText: copyText, fmt: { ms: ms, usd: usd, num: num, pct: pct },
  };
})();
