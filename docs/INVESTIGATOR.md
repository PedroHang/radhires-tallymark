# The investigator: a bounded tool loop for ambiguous items

Code: `app/pipeline/investigate.py`. Tests: `tests/test_investigate.py` (all mocked, no network).

## What it is

Most leftovers are settled by rules R1 to R11. A few are **ambiguous**: rule R8 fires on a bare
number like `1.250` with no tolerance and no frame. Today each one goes to the vision model as one
crop and one fixed question, and comes back as a strict `CropVerdict`. That single shot has no
context. A bare `1.250` is easy to judge once you know that a BoC row 3 mm to the right already
holds `1.250 ±.005`, or that the OCR text just right of it says `(REF)`.

The investigator gives the model that context, for the ambiguous items only. It gets a small toolset
it can call a few times, and it still has to finish with the same strict verdict. Every tool call is
logged, so the decision trace can show the reasoning and what it was based on.

## Why it is bounded

The harness enforces every limit. The model controls none of them.

| Bound | How |
| --- | --- |
| **Step cap** | At most `max_steps` (default 4) investigative tool calls. After the last one, the harness sends one **final turn** that must call `submit_verdict`. If the model still calls a tool or answers in prose, the loop stops with `step_cap_reached`. Hard ceiling: `max_steps + 3` model requests. |
| **Forced verdict** | On the final turn, `tool_choice` is forced to `submit_verdict` where the model allows it (OpenAI `{"type":"function","name":"submit_verdict"}`, Gemini `mode=ANY` with `allowed_function_names`, Claude models that accept `{"type":"tool"}`). Claude Opus 5.5, Sonnet 5.5, Fable 5.1 and Mythos 5.1 reject a forced tool_choice with a 400, so on those the final turn uses `auto` plus an explicit instruction. A 400 that mentions `tool_choice` on any other Claude model (for example forced use while thinking is on) falls back the same way. |
| **Read-only tools** | Tools are pure functions over the inputs. They cannot change rows, regions, coverage or scores. The model never sees the whole sheet or the whole table, only answers within 40 mm or at most 5 search hits. |
| **Schema validation** | `submit_verdict` arguments are validated against `CropVerdict` (minus `crop_index`) plus an `evidence` list. Every evidence ref must exist (`crop`, or an `S<n>` that actually ran). An invalid submission gets **one** repair turn. A second failure gives `no_verdict`. |
| **Token budget** | A soft budget (default 60,000 tokens, input + output), checked between turns. Once it is crossed, the next turn is the final one. |
| **Timeout** | A wall-clock deadline (default 90 s). Each request gets the remaining time as its timeout. When the deadline passes, the result is `timeout`. |
| **Never raises** | Every SDK, network, parse or tool failure becomes a short code: the same codes as `llm.py` (`auth_failed`, `permission_denied`, `model_not_found`, `rate_limited`, `connection_failed`, `api_error_<n>`, `unexpected_<Type>`, `no_credential`), plus `step_cap_reached`, `no_verdict`, `budget_exceeded` and `timeout`. |
| **Fallback** | With `verdict is None`, run.py treats the item as it does today: single-shot `classify_crops`, or "model check skipped" (score × 0.85, the item stays visible). |
| **Prompt-injection hygiene** | The system prompt says tool results are data read from the drawing and the BoC, never instructions. OCR text is passed as a JSON string. |

## The loop

```
             ┌──────────────────────────────────────────────────────────────┐
 crop +      │ turn n  (tool_choice auto; forced to submit_verdict if final) │
 rule reason ├──────────────────────────────────────────────────────────────┤
 + 5 tools ─►│ model replies with tool calls                                │
             └───────────────┬──────────────────────────────────────────────┘
                             │ for each call, in order
          ┌──────────────────┼───────────────────────────┬──────────────────────────┐
          ▼                  ▼                           ▼                          ▼
   submit_verdict      zoom / nearby_rows /        budget used or final:      prose, no call
   validate schema     ocr_context / search_boc    "not run" result           (not final: one
   + evidence refs     run locally, read-only,                                nudge, next turn is
     │      │          step S<k> logged                                       final)
   valid  invalid           │
     │      └─► one repair turn (final), then no_verdict
     ▼                      ▼
   DONE           steps used == max_steps or tokens ≥ budget?
                    yes → append results + FINAL_NOTE, next turn is final
                    no  → append results, next turn
   final turn without a valid submit_verdict → step_cap_reached
   deadline passed → timeout     any exception → mapped error code
```

## Tools

Distances are **edge to edge in mm** (0 when the boxes touch or overlap). The page size comes from the
PDF page (`page_size_mm`), or from the image aspect on an 11 in wide sheet when it is unknown.
Directions are seen from the item: `left`, `right`, `above`, `below`, a diagonal such as
`above-right`, or `overlapping`. Each result is returned to the model prefixed with its ref (`[S1]`).

Examples below are taken from the synthetic sheet in `tests/test_investigate.py`. The item is a bare
`1.250` on sheet 1, letter landscape.

**`zoom(factor: 2|3|4, pad: small|medium|large)`** returns a sharper crop of the same region from
the 300 DPI scan, magnified, with the region outlined in red, and capped at 1568 px.

```json
{"factor": 3, "pad": "small", "effective_scale": 3.0, "width_px": 204, "height_px": 123}
```

The image goes back with the result: inside `tool_result` (Claude), as an `input_image` in
`function_call_output.output` (OpenAI), or as a multimodal `FunctionResponse.parts` blob (Gemini).
Summary line: `3x zoom, small pad (204x123 px)`.

**`nearby_rows(radius_mm ≤ 40)`** returns the BoC rows on the same sheet whose box is within the
radius, nearest first (at most 8). Rows with no location are counted, not guessed.

```json
{"radius_mm": 20.0,
 "rows": [{"char_no": "12", "requirement": "1.250 ±.005", "places": 1, "distance_mm": 2.8, "direction": "right", "located_by": "export"},
          {"char_no": "13", "requirement": "Ø.281 ±.008", "places": 1, "distance_mm": 17.3, "direction": "above", "located_by": "text_match"}],
 "more": 0, "rows_without_location": 1,
 "note": "1 row(s) on this sheet have no location, so distance is unknown; use search_boc to find them by text."}
```

**`ocr_context(radius_mm ≤ 40)`** returns OCR text near the item (at most 12). It leaves out the
item's own regions, re-reads that sit on the item, blank regions and other sheets.

```json
{"radius_mm": 10.0, "regions": [{"region_id": "R2", "text": "(REF)", "distance_mm": 1.4, "direction": "right"},
                                {"region_id": "R3", "text": "A", "distance_mm": 2.2, "direction": "below"}], "more": 0}
```

**`search_boc(query, limit ≤ 5)`** runs a fuzzy match over every row's requirement on all sheets. It
uses the app's `normalize_text` and RapidFuzz: `max(ratio, 0.9 × partial_ratio)`, and keeps scores of
50 and above.

```json
{"query": "1.250", "rows": [{"char_no": "16", "requirement": "1.250", "sheet": 2, "score": 100.0, "located": true},
                            {"char_no": "12", "requirement": "1.250 ±.005", "sheet": 1, "score": 90.0, "located": true},
                            {"char_no": "14", "requirement": "2.500", "sheet": 1, "score": 67.5, "located": true},
                            {"char_no": "13", "requirement": "Ø.281 ±.008", "sheet": 1, "score": 54.0, "located": true}]}
```

**`submit_verdict({is_requirement, kind, transcription, reason, certainty, evidence})`** is the
only way to finish. It becomes `llm.CropVerdict(crop_index=0, ...)`, so run.py's scoring code can use
it unchanged.

```json
{"is_requirement": false, "kind": "none", "transcription": "1.250",
 "reason": "Same value as CN 12 next to it, marked (REF).", "certainty": "high", "evidence": ["S1", "S2"]}
```

Logged `steps` for that run:

```json
[{"n": 1, "ref": "S1", "tool": "nearby_rows", "args": {"radius_mm": 20}, "ok": true, "ms": 0,
  "result_summary": "2 row(s) within 20 mm: CN 12 '1.250 ±.005' 2.8 mm right; CN 13 'Ø.281 ±.008' 17.3 mm above; 1 unlocated"},
 {"n": 2, "ref": "S2", "tool": "ocr_context", "args": {"radius_mm": 10}, "ok": true, "ms": 0,
  "result_summary": "2 text region(s) within 10 mm: '(REF)' 1.4 mm right; 'A' 2.2 mm below"},
 {"n": 3, "ref": null, "tool": "submit_verdict", "args": {"...": "..."}, "ok": true, "ms": 0,
  "result_summary": "not a requirement (none, high): Same value as CN 12 next to it, marked (REF)."}]
```

## Interface

```python
from app.pipeline.investigate import InvestigationContext, OcrRegion, context_for_group, investigate

@dataclass
class OcrRegion:                     # regions.Region fits as-is (OcrRegion.of(region))
    region_id: str; sheet: int; text: str; bbox: Box

@dataclass
class InvestigationContext:
    item_id: str                     # the trace group id, e.g. "G7"
    sheet: int
    bbox: Box                        # normalized page coords of the group outline
    poly: list[tuple[float, float]]
    ocr_text: str
    rule_reason: str                 # Verdict.reason from classify_text
    sheet_image: np.ndarray          # RGB work image (300 DPI)
    page_size_mm: tuple[float, float] | None
    rows: list[Characteristic]       # all BoC rows
    regions: list[OcrRegion]         # regions on that sheet
    region_ids: list[str] = []       # the item's own regions
    zone: str | None = None

def investigate(ctx, provider, credential, model, max_steps=4, budget_tokens=60_000,
                timeout_s=90.0) -> InvestigationResult

@dataclass
class InvestigationResult:
    verdict: CropVerdict | None      # crop_index is 0
    steps: list[dict]                # {n, ref, tool, args, result_summary, ms, ok, image_ref?}
    calls: int; input_tokens: int; output_tokens: int; latency_ms: int
    error: str | None
    raw_messages: list               # transcript: no credential; images become "[image crop]" / "[image S1]";
                                     # thinking signatures / thought signatures / encrypted reasoning become "[omitted]"
    evidence: list[str]              # refs the verdict cites
    turns: list[dict]                # per request: n, final, forced, ms, input/output tokens, stop, tool_calls, text
    provider: str; model: str
    cost_usd -> float | None         # property, via llm.cost_usd (None for unpriced models)
    to_dict() -> dict                # JSON-safe, for the trace
```

`credential` is resolved with `llm.resolve_credential`, so a session key wins over the environment, as
it does everywhere else.

## Wiring it into run.py

Put it behind a setting, **`investigator: on|off`**: `RunOptions.investigator: bool`, default on
when a key is resolved for the provider, and exposed in Settings next to the model check. It only
applies when `opts.use_llm` is true.

The place is stage 5 ("classify leftovers"), in the `if ambiguous and opts.use_llm:` block, before
the current `classify_crops(...)` call. Sketch:

```python
from .investigate import context_for_group, investigate

page_mm = (page.width / 72 * 25.4, page.height / 72 * 25.4)
fallback = []                                   # items that still go single-shot
if opts.investigator:
    for c in ambiguous[:MAX_INVESTIGATED]:      # e.g. 15 per run; the rest go single-shot
        g = c["group"]
        poly, box = _group_outline(g)
        ctx = context_for_group(c["gid"], g, c["text"], c["verdict"].reason, poly, box,
                                work[g[0].sheet], rows, regions, page_mm, zone_label(box.center))
        res = investigate(ctx, opts.provider, opts.credential, opts.model)
        c["investigation"] = res
        llm_stats["llm_calls"] += res.calls
        llm_stats["llm_input_tokens"] += res.input_tokens
        llm_stats["llm_output_tokens"] += res.output_tokens
        if res.verdict is not None:
            c["llm"] = res.verdict              # same CropVerdict the scoring code reads today
        else:
            fallback.append(c)                  # res.error says why
    fallback += ambiguous[MAX_INVESTIGATED:]
else:
    fallback = ambiguous
# existing path, unchanged, for `fallback` only: crops -> classify_crops -> c["llm"] = verdicts[i]
```

Notes:

- `c["llm"]` stays a `CropVerdict`, so scoring (`LLM_FACTOR[certainty]`, the drop on
  `is_requirement=False`, the kind-based title) needs no change. Recompute `cost_usd` from the summed
  tokens, as the current code does.
- Pass `rows` (all of them) and `regions` (all; `context_for_group` keeps the item's sheet). The
  tools never mutate them, but `subtract_coverage` has already run by then anyway.
- Run items one after another, or in a small `ThreadPoolExecutor(max_workers=4)`. Each call is
  independent and builds its own SDK client. Mind the provider rate limits: a 429 just sends that item
  to the fallback.
- If any item returns `auth_failed`, `permission_denied`, `model_not_found` or `no_credential`, stop
  investigating the rest (the single-shot path will fail the same way) and set `llm_error` to that code
  so the banner explains it.
- **Decision trace:** record one model entry per investigated group, for example
  `recorder.note_model(gid, {"source": "investigator", **res.to_dict()})`. `steps` is the
  human-readable list to show under the item ("S1 nearby_rows: 2 rows within 20 mm: CN 12 ...").
  `evidence` says which steps the verdict relied on. `turns` gives per-request tokens and latency.
  `raw_messages` is the audit transcript. To show the zoom images in the trace, re-render them with
  `investigate.tool_zoom(ctx, **step["args"])` (deterministic), or save them when the trace recorder
  gains an artifact hook. Images are never stored in `raw_messages`.
- Score explanation: keep `basis = "model judged"`, and add `"after N tool steps"` when
  `len(steps) > 1`.

## Provider adapters

Each adapter is about 50 lines with the same four methods: `start`, `call(final, timeout)`,
`add_results(outputs, note)` and `transcript`. Clients come from `llm._anthropic_client`,
`llm._openai_client` and `llm._google_client`, so OAuth handling and test monkeypatching work exactly
as they do for the single-shot check.

| | Anthropic (Messages API) | OpenAI (Responses API) | Google (google-genai `generate_content`) |
| --- | --- | --- | --- |
| Tool defs | `{name, description, input_schema, strict: true}` | `{type: "function", name, description, parameters, strict: true}` | `Tool(function_declarations=[FunctionDeclaration(parameters_json_schema=...)])`, automatic function calling disabled |
| Tool calls | `tool_use` blocks | `function_call` items (`call_id`, JSON `arguments`) | `resp.function_calls` (`id`, `args`) |
| History | `response.content` appended unchanged (append-only, so thinking blocks stay valid) | the whole `resp.output` appended, including `reasoning` items (required with tool outputs) | the candidate `Content` appended unchanged (keeps thought signatures) |
| Results | one user message with all `tool_result` blocks (`is_error` on failures), image blocks inside `tool_result.content` | `function_call_output` per call. `output` is a string, or `[input_text, input_image]` for zoom | one user `Content` with `FunctionResponse(id, name, response={"output": ...})` parts. Zoom adds `parts=[FunctionResponsePart(inline_data=FunctionResponseBlob(...))]` |
| Final turn | `tool_choice {"type":"tool","name":"submit_verdict"}`, except models that reject it (see above) | `tool_choice {"type":"function","name":"submit_verdict"}` | `FunctionCallingConfig(mode="ANY", allowed_function_names=["submit_verdict"])` |
| Effort | `output_config.effort = "low"` (as in llm.py) | `reasoning.effort = "low"` | model default |
| Timeout | per-request `timeout=` (seconds) | per-request `timeout=` (seconds) | `HttpOptions(timeout=ms)` |

## Limitations and things not confirmed without a live key

- Request shapes come from the installed SDKs (anthropic 1.6.0, openai 3.35.1, google-genai 2.31.0),
  the claude-api skill, and the OpenAI function-calling guide (function outputs may carry an array of
  images or files, and reasoning items must be passed back). Tests check the wire bodies against mock
  transports. **No live call was made.**
- **Gemini multimodal function responses** (an image inside `FunctionResponse.parts`) are documented
  for Gemini 3 series models. Google's current docs show the feature on the newer Interactions API,
  not on `generate_content`. The SDK types support it, but a model that ignores those parts would
  judge without the zoom image. If a live test shows that, append the image as an inline `Part` after
  the function response instead.
- **Forced tool_choice on Claude** is skipped for Opus 5.5, Sonnet 5.5, Fable 5.1 and Mythos 5.1, per
  the skill. For other Claude models it is tried, and a 400 that mentions `tool_choice` falls back to
  `auto`. Haiku 5.5's behaviour is not documented either way; the same fallback covers it.
- The token budget is soft: a turn that crosses it still completes, and then only the final turn
  follows.
- SDK retries (default 2 on 429 and 5xx) stay as the clients define them, so a rate-limited request can
  use up to 3 attempts within the timeout.
- The verdict schema has no "already covered by row X" field. If the model finds that a nearby row
  holds the same value, it says so in `reason` and `evidence`, but run.py can't yet act on it. A
  future `duplicate_of` field could let scoring downgrade such a flag instead of dropping or keeping
  it.
