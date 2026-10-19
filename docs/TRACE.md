# Decision trace

Every run writes a **decision trace**: a complete, read-only record of what Tallymark read, measured
and decided, for every text region on the drawing (not only the flagged ones), plus every AI request
verbatim. The UI shows conclusions by default; the trace is what a user opens when they want to see
*how* a conclusion was reached.

Recording is passive. It never feeds back into a decision: `python -m tools.eval` and the sample
runs produce byte-identical flags and metrics with and without it.

- Code: `app/pipeline/trace.py` (recorder, catalog, rule explanation), hooks in `run.py`,
  `coverage.py`, `llm.py`; endpoints in `app/main.py`; storage through `app/store.py`
  (`put_trace` / `get_trace` / `put_trace_artifact` / `get_trace_artifact`).
- Storage: one JSON document per run (`trace.json`, about 250 KB for the 3-sheet sample) plus the
  PNG crops sent to the model (`trace/llm/<call_id>/<i>.png`). A failed run writes a partial trace
  (run-level data and the stages up to the failure).
- Tests: `tests/test_trace.py`.
- Versioning: `schema_version` (shape of the document, currently `"1"`) and `catalog_version`
  (the plain-language strings).

Example numbers below come from the bundled sample (`nist_ftc_06_asme1_rd.pdf` +
`ftc06_boc_simulated.csv`, AI check off), trimmed. The AI-call examples come from the test suite's
mocked provider, because no real key was used.

---

## Endpoints

All are `GET` and read-only. Old runs (before tracing) answer `404`; a run still in progress answers
`409` ("The trace is written when the run finishes.").

| Endpoint | Returns |
| --- | --- |
| `/runs/{id}/trace` | Run-level data, stages, funnel (with region id lists), outcome counts, invariants, AI-call summaries, flag index. No per-region records, no prompts. |
| `/runs/{id}/trace/regions?sheet=&outcome=&q=&offset=&limit=` | Region records, filtered. `outcome` accepts a comma list (`dropped_by_model,below_threshold`); `q` is a case-insensitive text search; `limit` defaults to 200 (max 1000). Response: `{total, offset, limit, items}`. |
| `/runs/{id}/trace/regions/{region_id}` | One full region record (the "inspect any text" payload). |
| `/runs/{id}/trace/flags/{flag_id}` | The decision chain for one flag, as ordered timeline steps. |
| `/runs/{id}/trace/llm/{call_id}` | One AI request verbatim: system prompt, instruction, content layout, request params, raw response text, parsed JSON, tokens, latency, cost, error. |
| `/runs/{id}/trace/llm/{call_id}/crops/{i}.png` | The i-th image that request sent (0-based, same order as the request). |
| `/runs/{id}/trace/catalog` | Plain-language labels for every code (rules, checks, exclusions, outcomes, bands, factors, sources, errors, stages). |
| `/runs/{id}/audit.json` | The whole trace + catalog + the flags as reported, as a download (`Content-Disposition: attachment`). |

The run document (`GET /runs/{id}`) also carries a small pointer:
`"trace": {"available": true, "partial": false, "url": "/runs/{id}/trace", "funnel": {"read": 104, ...}}`.
The exceptions export (`/runs/{id}/exceptions.csv`) has a new last column `audit_ref` with the
flag's trace path (`/runs/{id}/trace/flags/U-1`).

---

## `GET /runs/{id}/trace`

```json
{
 "schema_version": "1",
 "catalog_version": "2026-10-19.1",
 "run": {
  "run_id": "cli_821c6e86",
  "started_at": "2026-10-19T13:12:57.406+00:00",
  "finished_at": "2026-10-19T13:12:58.144+00:00",
  "duration_ms": 738,
  "status": "complete",
  "error": null,
  "app": {"name": "Tallymark", "pipeline_version": "ocr3", "git_commit": "5f0e63d", "git_dirty": true,
          "versions": {"python": "3.12.0", "rapidocr_onnxruntime": "1.4.4", "pymupdf": "1.28.2", "...": "..."}},
  "settings": {
   "exclusions": ["title_block", "border", "revision_block", "view_labels", "reference_dims", "scale_notes", "basic_dims"],
   "extra_regex": null, "use_llm": false, "provider": null, "model": null,
   "credential_source": null,
   "skip_sheets": [], "simulated_boc": true,
   "flag_threshold": 0.5, "coverage_pad": 0.006, "relocate_min_score": 80
  },
  "inputs": {
   "drawing": {"filename": "nist_ftc_06_asme1_rd.pdf", "sha256": "501a9760...893c", "bytes": 167608},
   "boc": {"filename": "ftc06_boc_simulated.csv", "sha256": "e72113f6...069d", "bytes": 2771, "rows": 45,
           "columns": ["char_no", "sheet", "zone", "char_type", "requirement", "places", "bbox_x", "bbox_y", "bbox_w", "bbox_h"],
           "has_bbox": true, "warnings": []}
  },
  "page": {"width_pt": 792.0, "height_pt": 612.0, "width_mm": 279.4, "template": "nist_pmi",
           "sheets_total": 3, "sheets_checked": [1, 2, 3]}
 },
 "stages": [
  {"key": "parse_boc", "label": "Reading the BoC", "started_at": "...", "ms": 1, "status": "done", "in": null, "out": {"rows": 45, "skipped_warnings": 0}, "detail": null},
  {"key": "render", "label": "Rendering sheets", "ms": 433, "status": "done", "in": {"sheets": 3, "skipped": []}, "out": {"rendered": 3},
   "detail": {"work_dpi": 300, "view_dpi": 150, "cache": "501a976013cb1e2c13bfc998_ocr3"}},
  {"key": "ocr", "label": "Reading text", "ms": 8, "status": "done", "in": {"sheets": 3}, "out": {"regions": 104},
   "sheets": [{"sheet": 1, "cache_hit": true, "ms": 1, "regions": 51, "by_origin": {"ocr": 49, "ocr_rotated": 2},
               "with_text_layer": 25, "with_frame": 2}, "..."]},
  {"key": "match", "label": "Matching to BoC rows", "ms": 6, "in": {"rows": 45, "regions": 104},
   "out": {"rows_located": 45, "rows_relocated_by_text": 0, "regions_covered": 46}},
  {"key": "exclusions", "ms": 0, "in": {"uncovered_regions": 58}, "out": {"excluded": 23}, "detail": {"template": "nist_pmi", "enabled": ["..."]}},
  {"key": "rules", "ms": 2, "in": {"leftover_regions": 35, "groups": 31}, "out": {"requirement": 3, "ambiguous": 0, "not_requirement": 28}},
  {"key": "model_check", "status": "skipped", "in": {"ambiguous_groups": 0}},
  {"key": "places", "ms": 38, "in": {"covered_regions": 46}, "out": {"checks": 10, "shortfalls": 1}},
  {"key": "scoring", "ms": 0, "in": {"candidates": 3, "places_shortfalls": 1}, "out": {"flags": 4, "merged_away": 0}},
  {"key": "transcription", "status": "skipped", "in": {"flags": 4}, "detail": {"error": "disabled"}}
 ],
 "funnel": [
  {"key": "read", "label": "Text regions read", "parent": null, "count": 104, "region_ids": ["s1-o0", "s1-o1", "..."]},
  {"key": "covered", "label": "Covered by a BoC row", "parent": "read", "count": 46, "region_ids": ["..."]},
  {"key": "excluded", "label": "Hidden by an exclusion", "parent": "read", "count": 23, "region_ids": ["..."]},
  {"key": "leftover", "label": "Left over (no row, not excluded)", "parent": "read", "count": 35, "region_ids": ["..."], "groups": 31},
  {"key": "not_requirement", "label": "Ruled not a requirement", "parent": "leftover", "count": 29, "region_ids": ["..."]},
  {"key": "requirement_by_rule", "label": "Ruled a requirement", "parent": "leftover", "count": 6, "region_ids": ["..."]},
  {"key": "ambiguous", "label": "Ambiguous (asked the AI)", "parent": "leftover", "count": 0, "region_ids": []},
  {"key": "sent_to_model", "label": "Sent to the AI check", "parent": "ambiguous", "count": 0, "region_ids": []},
  {"key": "dropped_by_model", "label": "Dropped by the AI check", "parent": "ambiguous", "count": 0, "region_ids": []},
  {"key": "below_threshold", "label": "Scored below 0.5", "parent": "leftover", "count": 0, "region_ids": []},
  {"key": "merged", "label": "Merged into a stronger flag", "parent": "leftover", "count": 0, "region_ids": []},
  {"key": "flagged_unaccounted", "label": "Flagged: no row (U)", "parent": "leftover", "count": 6, "region_ids": ["..."], "flag_ids": ["U-1", "U-2", "U-3"]},
  {"key": "flagged_places", "label": "Flagged: places shortfall (P)", "parent": "covered", "count": 1, "region_ids": ["s1-o44"], "flag_ids": ["P-1"]}
 ],
 "invariants": {
  "read = covered + excluded + leftover": true,
  "leftover = not_requirement + requirement_by_rule + ambiguous": true,
  "every region has one final outcome": true
 },
 "outcomes": {"covered": {"label": "Covered", "count": 45}, "excluded": {"label": "Excluded", "count": 23},
              "not_requirement": {"label": "Not a requirement", "count": 29}, "dropped_by_model": {"label": "Dropped by AI", "count": 0},
              "below_threshold": {"label": "Below threshold", "count": 0}, "merged": {"label": "Merged", "count": 0},
              "flagged_unaccounted": {"label": "Flagged: no row", "count": 6}, "flagged_places": {"label": "Flagged: places", "count": 1}},
 "llm": {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "classify_error": null, "transcription_error": null},
 "llm_calls": [],
 "flags": {"U-1": {"flag_id": "U-1", "class": "unaccounted", "sheet": 1, "region_ids": ["s1-o26", "s1-o27", "s1-o28"],
                   "group_id": "G16", "confidence": "high", "score": 0.9}, "...": {}},
 "groups_count": 31,
 "regions_count": 104
}
```

Notes:

- **Counts are regions, not flags.** OCR often splits one callout into pieces, and the rules judge a
  *group* of pieces on one line. `U-1` above is three regions (`.05ABC`, `C`, `APPLIESTOALL`) in one
  group. The funnel's `leftover.groups` and `flag_ids` lists give the group and flag counts.
- **Funnel vs outcomes.** The funnel is a tree (`parent`), showing how many regions reached each
  step. `outcomes` is a strict partition: every region has exactly one final outcome, so the
  outcome counts always sum to `read`. `flagged_places` regions are covered regions, so they count
  under `covered` in the funnel and under `flagged_places` (not `covered`) in `outcomes`.
- `credential_source` is `session`, `environment` or `null`. The key itself is never recorded.
- `ocr.sheets[].cache_hit: true` means the text regions came from the OCR cache of an earlier run
  of the same file (same sha256 and pipeline version); `ms` is then the cache read time, not OCR time.
- The full trace (and `audit.json`) also has `ai_links`: one entry per item the investigator tied to a
  row, `{group_id, region_ids, text, claimed, ok, char_nos, note, checks}`, accepted or not.
- In the summary, `llm_calls[]` has `call_id, job, provider, model, n_crops, input_tokens,
  output_tokens, latency_ms, cost_usd, error, stop_reason, url`.

---

## Region record (`/trace/regions`, `/trace/regions/{region_id}`)

One per text region on every checked sheet. A region flagged as a leftover (sample `s1-o26`):

```json
{
 "region_id": "s1-o26",
 "sheet": 1,
 "bbox": {"x": 0.10181, "y": 0.39909, "w": 0.05696, "h": 0.01562},
 "poly": [[0.10181, 0.39909], [0.15877, 0.39909], [0.15877, 0.41471], [0.10181, 0.41471]],
 "angle": 0.0,
 "text": ".05ABC",
 "ocr_conf": 0.885,
 "origin": "ocr",
 "sources": ["ocr", "text_layer", "fcf_grid"],
 "fcf_cells": 5,
 "coverage": {
  "covered": false, "covered_by": [], "candidates": [], "rows_placed_here": [],
  "nearest_row": {"char_no": "7", "requirement": "2X Ø.562 ±.008", "distance_mm": 38.9}
 },
 "exclusion": {"checked": true, "applied": null, "label": null, "detail": null},
 "group_id": "G16",
 "classification": {
  "group_id": "G16", "text": ".05ABC APPLIESTOALL", "region_ids": ["s1-o26", "s1-o27", "s1-o28"], "fcf_cells": 5,
  "outcome": "requirement", "rule_id": "R4", "rule_label": "Feature control frame",
  "title": "Feature control frame, no characteristic",
  "reason": "A feature control frame (tolerance box with datum references) that no row covers.",
  "kind": "gdt", "base": 0.9,
  "checks": [
   {"check": "R4_frame_grid", "rule_id": "R4", "matched": true, "status": "fired", "evidence": "5-cell frame, text '.05ABC APPLIESTOALL'"},
   {"check": "R1_tolerance", "rule_id": "R1", "matched": false, "status": "no_match", "evidence": null},
   "... every check, in order ...",
   {"check": "R10_prose", "rule_id": "R10", "matched": true, "status": "matched_after", "evidence": "15 letters, 2 digits, capitals run 'APPLIESTOALL'"}
  ]
 },
 "model": null,
 "places": null,
 "score": {"base": 0.9, "locate_factor": 1.0, "locate_reason": "row locations from the BoC", "nearest_char_no": "7",
           "llm_factor": 1.0, "llm_reason": "a rule decided", "threshold": 0.5, "product": 0.9,
           "basis": "frame pattern", "band": "high", "result": "flagged", "flag_id": "U-1"},
 "final": {"outcome": "flagged_unaccounted", "label": "Flagged: no row", "flag_ids": ["U-1"]}
}
```

A covered region (`s1-o11`) shows the candidate rows that were considered and the test that passed:

```json
"coverage": {
 "covered": true, "covered_by": ["1"],
 "candidates": [
  {"char_no": "1", "requirement": "2X CR.50 ±.02", "row_bbox_source": "export", "overlap": 1.0, "center_in_zone": true,
   "distance_pw": 5e-05, "distance_mm": 0.0, "text_similarity": 100.0, "text_blank": false,
   "numbers_compatible": true, "passed": true, "passed_by": "text_match"},
  {"char_no": "2", "requirement": "24X R.125 ±.020", "row_bbox_source": "export", "overlap": 0.714, "center_in_zone": true,
   "distance_pw": 0.0278, "distance_mm": 7.8, "text_similarity": 75.0, "text_blank": false,
   "numbers_compatible": false, "passed": false, "passed_by": null}
 ],
 "rows_placed_here": [],
 "nearest_row": {"char_no": "1", "requirement": "2X CR.50 ±.02", "distance_mm": 0.0}
},
"exclusion": {"checked": false, "applied": null, "label": null, "detail": null, "note": "Covered text is never excluded."},
"places": [{"region_id": "s1-o11", "char_no": "1", "callout_places": 2, "recorded": 2, "recorded_field": "places", "shortfall": false}],
"final": {"outcome": "covered", "label": "Covered", "flag_ids": []}
```

Field reference:

| Field | Meaning |
| --- | --- |
| `region_id` | `s{sheet}-{o\|r\|t\|g}{n}`: `o` OCR upright pass, `r` OCR ±30° pass, `t` PDF text layer, `g` frame found by shape with no text. Same ids as `flags[].region_ids`. |
| `bbox`, `poly` | Normalized 0-1 page coordinates (`poly` is the oriented quad, use it to draw). |
| `ocr_conf` | The OCR recognition score (0-1) for OCR regions; `null` for PDF text and frame-only regions (they have no OCR score). When the ±30° pass replaced an upright read, it is the winning read's score. |
| `origin` / `sources` | `origin`: which reader created the region. `sources`: everything that saw it (`text_layer` = the PDF text agreed; `fcf_grid` = a frame was attached). |
| `coverage.candidates[]` | Every BoC row whose (padded) box this region fell into. `passed_by` is the test that made the row cover it (see catalog `coverage_tests`), `null` when it failed. `distance_mm` is centre to centre. |
| `coverage.rows_placed_here[]` | Rows that had no location and were placed by text match on a callout that includes this region (`{char_no, region_id, region_ids, score, claim}`: `region_ids` is the whole callout, which can be several OCR fragments of one line; `claim` is 2+ when a row with places = N was also placed on a repeated copy). |
| `coverage.nearest_row` | Nearest located row on the sheet, for any region. |
| `exclusion` | `applied` is the exclusion key, `label` its plain name, `detail` the zone (`{template, zone, center}`) or the `pattern` that matched. Covered regions are never checked. |
| `classification` | The group this leftover belongs to, the rule outcome, and every rule check in evaluation order (`fired` / `matched_after` / `no_match`). `null` for covered or excluded regions. |
| `model` | Only for ambiguous groups: `{status: verdict\|no_verdict\|not_sent, source: investigator\|single_shot, call_id, crop_index, provider, model, verdict, error}`. With the investigator: `steps` and `evidence`; when the investigator failed and the single shot answered, `investigation: {call_id, error, steps}`; when the verdict named covering rows, `link: {claimed, ok, char_nos, note, checks: [{char_no, requirement, sheet, sheet_ok, numbers_compatible, text_score, threshold, ok, why}]}`. |
| `places` | nX checks run on this (covered) region: `{char_no, callout_places, recorded, recorded_field, shortfall, flag_id?, score?}`. |
| `score` | `{base, locate_factor, locate_reason, llm_factor, llm_reason, threshold, product, band, basis, result, flag_id?, merged_into?}`. `result`: `flagged`, `below_threshold`, `dropped_by_model` or `merged`. |
| `final` | `{outcome, label, flag_ids}`. `flag_ids` lists the flag this region is part of (or merged into). |

---

## Flag chain (`/trace/flags/{flag_id}`)

An ordered list of steps, ready to render as a timeline. Every step is
`{n, kind, title, detail, data}`, where `kind` is one of `measured`, `rule`, `ai`, `score`, `title`
and `detail` are plain-language and `data` holds the raw values. The chain always starts with
*Read the text* and ends with the *Score* step, whose `data.band` equals the flag's `confidence`.

Unaccounted flag `U-1` (sample, rule-decided):

```json
{
 "flag_id": "U-1", "class": "unaccounted", "sheet": 1, "region_ids": ["s1-o26", "s1-o27", "s1-o28"],
 "group_id": "G16", "confidence": "high", "score": 0.9,
 "steps": [
  {"n": 1, "kind": "measured", "title": "Read the text",
   "detail": "Read \".05 A B C APPLIESTOALL\" on sheet 1, zone C7, from 3 text pieces.",
   "data": {"regions": [{"region_id": "s1-o26", "text": ".05ABC", "ocr_conf": 0.885, "origin": "ocr", "source_label": "OCR"}, "..."]}},
  {"n": 2, "kind": "measured", "title": "Matched to a BoC row?",
   "detail": "No. No row's location overlaps it. Nearest row is CN 7 (2X Ø.562 ±.008), 38.9 mm away.",
   "data": {"candidates": [], "nearest_row": {"char_no": "7", "requirement": "2X Ø.562 ±.008", "distance_mm": 38.9}}},
  {"n": 3, "kind": "rule", "title": "Excluded?",
   "detail": "No exclusion applies. Checked: Title block, Border and zone labels, Revision block, View labels (SECTION A-A, DETAIL B), Reference dimensions (in parentheses), Scale notes, Basic dimensions (boxed numbers).",
   "data": {"enabled": ["title_block", "..."], "extra_regex": null, "applied": null}},
  {"n": 4, "kind": "rule", "title": "R4: Feature control frame",
   "detail": "A feature control frame (tolerance box with datum references) that no row covers. Matched: \"5-cell frame, text '.05ABC APPLIESTOALL'\".",
   "data": {"rule_id": "R4", "outcome": "requirement", "group_id": "G16", "group_text": ".05ABC APPLIESTOALL", "checks": ["..."]}},
  {"n": 5, "kind": "ai", "title": "AI check not needed", "detail": "A rule decided this, so the AI was not asked.",
   "data": {"status": "not_needed"}},
  {"n": 6, "kind": "score", "title": "Score 0.90: High confidence",
   "detail": "0.9 (rule) × 1.0 (location) × 1.0 (AI) = 0.900. Flags need 0.5; High is 0.8+, Medium 0.6+.",
   "data": {"base": 0.9, "locate_factor": 1.0, "llm_factor": 1.0, "product": 0.9, "threshold": 0.5, "band": "high",
            "basis": "frame pattern", "result": "flagged", "flag_id": "U-1", "...": "..."}}
 ]
}
```

Places flag `P-1` (sample): *Read the text* ("4X 0.281 ±.008") → *Matched to a BoC row?* ("Yes. Row
CN 19 covers this text.") → *Excluded?* ("Not checked: text a row covers is never excluded.") →
*P1: Places shortfall* ("The callout says 4 places; row CN 19 records 1.") → *AI check not needed*
→ *Score 0.90: High confidence*.

An ambiguous item the AI confirmed (mocked provider in the tests) has these AI and score steps:

```json
[
 {"n": 5, "kind": "ai", "title": "AI check: it is a requirement",
  "detail": "anthropic claude-sonnet-5 answered with high certainty: \"Test verdict.\" (read as \"c2\").",
  "data": {"status": "verdict", "call_id": "L1", "crop_index": 2, "provider": "anthropic", "model": "claude-sonnet-5",
           "verdict": {"crop_index": 2, "is_requirement": true, "kind": "dimension", "transcription": "c2",
                       "reason": "Test verdict.", "certainty": "high"}, "error": null}},
 {"n": 6, "kind": "ai", "title": "AI transcription (display only)",
  "detail": "Shown as \"T5\". This never changes the decision.",
  "data": {"call_id": "L2", "crop_index": 5, "transcription": "T5"}},
 {"n": 7, "kind": "score", "title": "Score 0.60: Medium confidence",
  "detail": "0.6 (rule) × 1.0 (location) × 1.0 (AI) = 0.600. Flags need 0.5; High is 0.8+, Medium 0.6+.",
  "data": {"base": 0.6, "locate_factor": 1.0, "llm_factor": 1.0, "llm_reason": "AI certainty high", "product": 0.6,
           "band": "medium", "basis": "model judged", "result": "flagged", "flag_id": "U-6"}}
]
```

When the investigator handled the item, one `ai` step per tool call comes before the verdict step,
titled `AI investigator S1: Searched the BoC` (label from `catalog.investigator_tools`), with the
tool's one-line `result_summary` as `detail` and `{tool, args, ref, ok, ms, call_id, image_url}` as
`data` (`image_url` is the zoom image for `zoom` steps). If the investigator named a covering row that
failed run.py's check, the verdict step's detail and the flag's `reason` start with
"Model thinks CN 41 covers this; text didn't match (CN 41: values differ)."

When the AI check could not answer, step 5 is *AI check did not answer* with the error label
(for example "Key rejected") and the score uses `llm_factor` 0.85.

Step titles by position: `Read the text` (measured) → `Matched to a BoC row?` (measured) →
`Excluded?` (rule) → `<rule id>: <rule label>` (rule) → `AI check ...` (ai) →
optional `AI transcription (display only)` (ai) → `Score <n>: <band> confidence` (score).

---

## AI call record (`/trace/llm/{call_id}`)

An `investigate` record covers one whole investigator loop for one item (several requests): it adds
`steps` (every tool call, `{n, ref, tool, args, result_summary, ms, ok, image_ref?}`), `turns` (per request:
tokens, latency, stop reason, tool calls), `evidence` (refs the verdict cites), `raw_messages` (the
sanitized transcript: no key, images as `[image crop]` / `[image S1]`) and `request.tool_schemas`.
`raw_response_text` is `null` there (see `turns[].text`). Its `crops` are the images the model saw:
index 0 is the crop (`ref: "crop"`), then one per zoom step (`ref: "S2"`...). `parsed` is the verdict,
including `covered_by`.

Call ids are `L1`, `L2`, ... in the order requests were sent; `job` is `classify` (is it a
requirement?) or `transcribe` (display text for raised flags). A request carries up to 20 crops.
From the mocked test run:

```json
{
 "call_id": "L1", "job": "classify", "provider": "anthropic", "model": "claude-sonnet-5",
 "system_prompt": "You are checking crops from an engineering drawing for a first article inspection. Each crop has one text region outlined in red. Decide whether ...",
 "instruction": "Return exactly one verdict for each of the 4 crops.",
 "user_content": [{"type": "text", "text": "Crop 0:"}, {"type": "image", "crop_index": 0},
                  {"type": "text", "text": "Crop 1:"}, {"type": "image", "crop_index": 1}, "...",
                  {"type": "text", "text": "Return exactly one verdict for each of the 4 crops."}],
 "request": {"max_tokens": 16000, "reasoning_effort": "low", "response_schema_name": "CropVerdicts", "response_schema": {"...": "JSON schema"}},
 "crop_offset": 0, "n_crops": 4,
 "raw_response_text": "{\"verdicts\": [{\"crop_index\": 0, \"is_requirement\": true, \"kind\": \"dimension\", \"transcription\": \"c0\", \"reason\": \"Test verdict.\", \"certainty\": \"high\"}, ...]}",
 "parsed": {"verdicts": [{"crop_index": 0, "is_requirement": true, "kind": "dimension", "transcription": "c0", "reason": "Test verdict.", "certainty": "high"}, "..."]},
 "input_tokens": 1000, "output_tokens": 50, "latency_ms": 310, "cost_usd": 0.0025,
 "response_id": "msg_1", "stop_reason": "end_turn", "error": null, "error_detail": null,
 "crops": [
  {"index": 0, "input_index": 0, "url": "/runs/test_4c566e98/trace/llm/L1/crops/0.png", "group_id": "G29", "region_ids": ["s2-o2"]},
  {"index": 1, "input_index": 1, "url": "/runs/test_4c566e98/trace/llm/L1/crops/1.png", "group_id": "G30", "region_ids": ["s2-o4", "s2-o3"]}
 ]
}
```

- `raw_response_text` is exactly what the provider returned: the Anthropic message's text blocks
  joined, OpenAI `output_text`, Gemini `response.text`.
- `user_content` is the exact layout of the user message; each `image` part is the crop at the same
  `index` under `crops` (served as PNG). Transcription crops carry `flag_id` instead of `group_id`.
- `cost_usd` is `null` for models without a verified price (tokens are still recorded).
- On failure: `error` is the short code (`auth_failed`, `permission_denied`, `model_not_found`,
  `rate_limited`, `connection_failed`, `api_error_<status>`, `unexpected_<Exception>`),
  `error_detail` the exception text (truncated to 500 chars), `raw_response_text` `null`. If no key
  is set, no request is sent: there is no call record, `trace.llm.classify_error` is `no_credential`
  and the affected regions have `model.status: "not_sent"`.

---

## Catalog (`/trace/catalog`)

A dictionary of every code the trace uses, each as `{label, explain}` (rules also carry `outcome`
and `base`; bands carry `min`; checks carry `rule`). The UI should never show a bare code.

| Section | Keys |
| --- | --- |
| `rules` | `R1`-`R11`, `P1` (R9 is listed as "Not used": no check emits it) |
| `rule_checks` | `R4_frame_grid`, `R1_tolerance`, `R4_frame_text`, `R2_limits`, `R6_spec`, `R5_note`, `R3_size`, `R7_finish`, `R11_short`, `R8_bare_number`, `R10_prose`, `R8_has_digits`, `R10_fallback` (in evaluation order; the first match decides) |
| `check_status` | `fired`, `matched_after`, `no_match` |
| `exclusions` | `title_block`, `border`, `revision_block`, `view_labels`, `reference_dims`, `scale_notes`, `basic_dims`, `custom_regex` |
| `coverage_tests` | `location_gate`, `text_match`, `tight_overlap`, `blank_in_zone`, `blank_frame_gdt`, `row_best_region`, `relocated_by_text` |
| `sources` | `ocr`, `ocr_rotated`, `text_layer`, `fcf_grid` |
| `outcomes` | `covered`, `excluded`, `not_requirement`, `dropped_by_model`, `covered_by_investigator`, `below_threshold`, `merged`, `flagged_unaccounted`, `flagged_places` |
| `bands` | `high` (≥ 0.8), `medium` (≥ 0.6), `low` (≥ 0.5), `none` (< 0.5, not flagged) |
| `factors` | `base`, `locate_factor`, `llm_factor` |
| `model_status` | `verdict`, `no_verdict`, `not_sent` |
| `llm_jobs` | `investigate`, `classify`, `transcribe` |
| `investigator_tools` | `zoom`, `nearby_rows`, `ocr_context`, `search_boc`, `submit_verdict` |
| `llm_errors` | `disabled`, `no_credential`, `auth_failed`, `permission_denied`, `model_not_found`, `rate_limited`, `connection_failed`, `api_error`, `unexpected` (match `api_error_*` / `unexpected_*` by prefix) |
| `step_kinds` | `measured`, `rule`, `ai`, `score` |
| `stages` | `parse_boc`, `render`, `ocr`, `match`, `exclusions`, `rules`, `model_check`, `places`, `scoring`, `transcription` |

Example: `rules.R4 = {"label": "Feature control frame", "explain": "A tolerance frame: a box with a tolerance value and datum letters.", "outcome": "requirement", "base": 0.9}`.

---

## Outcome vocabulary

| Outcome | When |
| --- | --- |
| `covered` | A BoC row covers the region (location gate passed and a coverage test passed). |
| `excluded` | Not covered, and an enabled exclusion matched. |
| `not_requirement` | Left over; the rules said label or prose (R10, R11). |
| `dropped_by_model` | Left over, ambiguous (R8), and the AI said not a requirement. |
| `below_threshold` | Left over and a requirement or unconfirmed ambiguous item, but the score was under 0.5 (for example R8 with no AI answer and no BoC locations: 0.6 × 0.85 × 0.8 = 0.41). |
| `merged` | Its flag overlapped a stronger flag of the same class by more than 30% IoU, so only the stronger one is shown (`final.flag_ids` names it). |
| `flagged_unaccounted` | Part of a U flag. |
| `flagged_places` | Covered, and the callout's nX count is larger than the row's places/results (a P flag). |
| `covered_by_investigator` | Ambiguous; the AI investigator named a BoC row that already states it (`covered_by`), and run.py's check agreed: the row exists, is on the same sheet, its values are compatible and its text reads like the item (`match_score` >= 70). Not flagged. Shown in `coverage.json` under `linked` with `"marker": "linked_by_ai"` so the UI can mark it "linked by AI, check". |

Score: `product = base × llm_factor × locate_factor` (computed in that order, exactly as the
pipeline does). `band` comes from `product` with the thresholds above.

---

## UI suggestions

**(a) Per-flag "How was this decided?" timeline.** Add a link in the flag review panel that fetches
`/runs/{id}/trace/flags/{flag_id}` and renders `steps` as a vertical timeline: one node per step,
icon by `kind` (measured = ruler, rule = gavel/checklist, ai = sparkle, score = gauge), `title` in
bold, `detail` as the body, and a "Show data" disclosure that pretty-prints `data`. For the rule
step, render `data.checks` as a checklist (the `fired` row highlighted, `matched_after` rows
greyed with "also matched"). For an `ai` step with a `call_id`, link "See the exact request" to the
AI-call view and show the crop thumbnail from
`/runs/{id}/trace/llm/{call_id}/crops/{crop_index}.png`. For the score step, show the three
factors as chips with their reasons (`locate_reason`, `llm_reason`) and the band.

**(b) "Inspect any text" mode on the canvas.** A toggle in the sheet toolbar. When on, fetch
`/runs/{id}/trace/regions?sheet=N&limit=1000` once per sheet and draw every `poly` faintly,
colored by `final.outcome` (legend from `catalog.outcomes`). Clicking a region opens a side panel
from `/trace/regions/{region_id}`: the OCR text and confidence (with `sources` as small badges),
the coverage table (candidate rows with distance, similarity, values agree, passed/failed and the
`coverage_tests` label), the exclusion applied and why (zone or pattern), the rule checklist, the
AI verdict if any, and the final outcome with its flag link. A search box can use `q=`.

**(c) "Dropped by AI" filter with user override.** A filter chip in the flags list that loads
`/trace/regions?outcome=dropped_by_model` and groups results by `group_id` (one card per group,
crop from the model call). Each card shows the AI's `reason` and `certainty`. An "It is a
requirement" button is the override. The trace API is read-only, so an override needs a write
endpoint that does not exist yet (suggested: `POST /runs/{id}/overrides {group_id, decision, note}`
that appends a manual flag, leaving the trace untouched as the record of what the machine decided).
Until then the UI can keep overrides client-side and include them in its own export. The same
filter works for `below_threshold`.

**(d) Run report page.** A page per run (`/runs/{id}/report` in the SPA) built from
`/runs/{id}/trace`:
- Funnel: horizontal bars from `funnel` (indent by `parent`); clicking a bar lists the regions.
  Show the `invariants` as green checks.
- Stage timeline: a Gantt-style row per `stages[]` entry with `ms`, `in` and `out`, and the per-sheet
  OCR rows with a "from cache" badge when `cache_hit`.
- Every AI call: a table from `llm_calls` (job, model, crops, tokens, latency, cost, error); each row
  expands to `/trace/llm/{call_id}` showing the system prompt, instruction, crop strip, raw response
  (monospace, verbatim) and parsed JSON side by side.
- Settings and versions: `run.settings`, `run.app` (git commit, library versions), `run.inputs`
  (file names, sizes, sha256).
- "Download audit JSON" button linking to `/runs/{id}/audit.json`.

---

## Limitations

- Region ids are only unique within a run; they are stable across runs of the same file because
  regions come from the OCR cache.
- When the ±30° OCR pass replaced the text of an upright read, the region keeps its `o` id and the
  winning read's text/confidence; the losing read is not kept.
- `distance_mm` assumes the page is drawn at full scale on its PDF page size.
- The trace does not record the exact base64 image bytes sent, but the crop PNG saved is the same
  image object that was encoded for the request.
- Overrides (UI suggestion c) need a new write endpoint.
