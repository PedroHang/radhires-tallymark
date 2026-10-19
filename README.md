# Tallymark

Tallymark is a fictional demo brand (every mark on the drawing gets tallied). The repo folder keeps
its working name, `unaccounted-ink`.

A prototype that answers one question after an AI extraction tool has built a
Bill of Characteristics (BoC) from an engineering drawing:

> What requirement-looking ink is still on the drawing that no BoC row covers?

It fades every region a row already covers, leaves lit only the unaccounted text, and flags
repeated-feature callouts ("4X") whose row records fewer places. A quality engineer confirms,
excludes or dismisses each flag and exports an exceptions CSV.


## Run it

```bash
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m uvicorn app.main:app --port 8031
```

Open http://localhost:8031, click **Use sample drawing**, then **Run check**.

The first run on a drawing reads every sheet with OCR (about 10-30 s per sheet on CPU). Results
are cached per drawing, so the next run takes about a second.

### Model key (optional)

Rules decide every high-confidence flag. A vision model is used for two narrow jobs:

1. Ambiguous leftovers only (a bare number with no tolerance and no frame): "is this an inspectable requirement?"
2. Flagged crops only: transcribe the outlined text with proper symbols, for the review panel.

Any of three providers works, with the same prompts and the same structured output schema:

| Provider | Key | Env var | Default model (others selectable in Settings) |
| --- | --- | --- | --- |
| Anthropic | `sk-ant-...` | `ANTHROPIC_API_KEY` | `claude-sonnet-5-5` |
| OpenAI | `sk-...` | `OPENAI_API_KEY` | `gpt-6.1-sol` |
| Google Gemini | `AIza...` / `AQ....` | `GEMINI_API_KEY` or `GOOGLE_API_KEY` | `gemini-3.8-flash` |

Paste a key in **Settings > Model check** (the provider is picked from the key's prefix), or put the
env var in a `.env` file next to this README. **Test** sends one tiny request and says plainly whether
the key and model work. `UINK_PROVIDER` / `UINK_MODEL` override the defaults.

Without a key, or when the provider fails (bad key, 403, rate limit, outage), the run never crashes:
the error comes back as a short code, ambiguous leftovers are flagged with low confidence, a banner
says why the model check did not run, and the panel shows OCR text. Cost in dollars is reported only
for models with a verified price; others report tokens only.

About Claude Pro/Max tokens (`claude setup-token`, `sk-ant-oat...`): the app sends them as an OAuth
bearer token, but Anthropic intends subscription tokens for Claude Code, so the API may refuse them
from this app. A Console API key (`sk-ant-api...`) is the reliable option. **Test** in Settings tells
you right away.

## Frontend

Plain HTML, CSS and JS in `frontend/` (no build step), served by FastAPI. Light and dark themes follow
the system setting until the header toggle is used (the choice is kept in `localStorage`). The start
screen has a looping "sneak peek" of a run (`#peek` in `index.html`, `wirePeek()` in `app.js`) and info
popovers on the two load cards (`.info-pop`, `wireInfoPops()`). Both respect `prefers-reduced-motion`.
Preview images in `frontend/assets/` are rendered from the public NIST FTC-06 sample.

## How it works

| Stage | What happens | Where |
| --- | --- | --- |
| Rendering sheets | PDF/TIFF pages rendered at 300 DPI (work) and 150 DPI (view) | `app/pipeline/regions.py` |
| Reading text regions | RapidOCR (PaddleOCR models on ONNX) at 0° plus straightened ±30° passes for isometric callouts; PDF text layer merged in; feature control frame grids found with OpenCV | `regions.py` |
| Matching to BoC rows | Each row covers regions near its bbox **whose text is compatible** with its requirement. Rows with no bbox are placed by fuzzy text match | `coverage.py` |
| Applying exclusions | Title band, caption, border strip, revision tag (template zones) plus rule excludes: reference dims in parentheses, view labels, scale notes, custom regex | `exclusions.py` |
| Classifying leftovers | Deterministic rules R1-R11 (tolerance, frame, size callout, numbered note, spec, finish...). Only ambiguous ones go to the model | `classify.py`, `llm.py` |
| Checking repeated features | "4X ..." callout covered by a row whose Places (or result count) is lower | `run.py` |

Scoring follows BRD 7.3: `score = base(rule) x locate_factor x llm_factor`, flag at >= 0.5, High >= 0.8, Medium >= 0.6.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/uploads` | Upload a drawing or BoC (`file`, `kind`), returns an `upload_id` and validation info |
| GET | `/samples` | Bundled NIST FTC-06 drawing + simulated BoC |
| POST | `/precheck` | Sheets with no BoC rows (non-blocking warning) |
| POST | `/runs` | Start a check (multipart files or upload ids, `exclusions`, `extra_regex`, `use_llm`, `skip_sheets`), 202 |
| GET | `/runs/{id}` | Status, stages, stats, warnings |
| GET | `/runs/{id}/events` | Server-sent events with stage progress |
| GET | `/runs/{id}/flags` | Flags (`cls`, `disposition`, `sheet` filters) |
| PATCH | `/runs/{id}/flags/{flag_id}` | Disposition: `open`, `confirmed`, `excluded`, `dismissed` + `note` |
| GET | `/runs/{id}/sheets/{n}.png` | Rendered sheet |
| GET | `/runs/{id}/sheets/{n}/coverage.json` | Covered / excluded regions + flags for the overlay |
| GET | `/runs/{id}/flags/{flag_id}/crop.png` | Evidence crop |
| GET | `/runs/{id}/exceptions.csv` | Export (`include_dismissed`) |
| GET/PUT | `/settings`, POST `/settings/test` | Model, credential (kept in memory only), connection test |
| DELETE | `/runs/{id}` | Delete a run and its files |

## BoC input

CSV or XLSX. Required: `char_no`, `sheet`, `requirement`. Optional: `zone`, `char_type`, `designator`,
`places`, `results_count`, `units`, `bbox_x`, `bbox_y`, `bbox_w`, `bbox_h` (normalized 0-1 page
coordinates of the requirement text). This schema is ours; real extraction tools export to Excel but their
columns are not public.

## Samples and eval

- `samples/nist_ftc_06_asme1_rd.pdf`: NIST MBE PMI test case FTC-06 (public, used with acknowledgment, no NIST logo).
- `samples/ftc06_boc_groundtruth.csv`: 48 rows hand-transcribed from the rendered sheets (`tools/make_samples.py`).
- `samples/ftc06_boc_simulated.csv`: ground truth with 4 seeded changes (note 4 dropped, 4X places reduced to 1,
  12.00 ±.01 dropped on sheet 2, one position frame dropped on sheet 3). Manifest alongside.
- `samples/eval/`: the other 10 NIST FTC/CTC drawings.
- `python -m tools.eval` writes `eval/REPORT.md` and `eval/results.json`.
- `python -m pytest -q` runs rules, end-to-end sample and model-request-shape tests (the model calls are mocked).

## Tools

- `python -m tools.check <drawing> <boc.csv> [--no-llm]` runs a check in the terminal.
- `python -m tools.dump_regions <drawing>` writes region overlays to `scratch/` for debugging.
