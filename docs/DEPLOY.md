# Deploying Tallymark on Vercel

Tallymark runs on Vercel as one Python function (the FastAPI app) that also serves the static
frontend. This page covers what changes on a serverless host, the steps to deploy, the limits to
know about, and how to check a deployment locally before pushing.

## What is different on Vercel

| Locally (`uvicorn`) | On Vercel |
| --- | --- |
| `POST /runs` answers `202` at once; the check runs in a background thread and the UI follows it with server-sent events (`/runs/{id}/events`). | Nothing may keep running after a response is sent, so `POST /runs` runs the whole check inside the request and answers `200` with the finished run (`status: "done"`). `/runs/{id}/events` sends the final state once and closes. |
| Files live in `./data` (uploads, rendered sheets, OCR cache, run folders, traces) with a SQLite index. | Only `/tmp` is writable and it is not shared between instances, so the same files live in `/tmp/tallymark`. With a **Vercel Blob** store connected, every file is also written to Blob and read back on any instance that misses it. Without Blob, a request that lands on another instance answers "expired, run the check again" (`404`/`410`), never a 500. |
| API keys pasted in Settings stay in server memory. | Server memory is per instance and short-lived. Put keys in project environment variables, or let the browser send the key with each request (`X-Tallymark-Key`, used for that request only, never stored). |

Synchronous mode turns on automatically when the `VERCEL` environment variable is present (Vercel
sets it). `TALLYMARK_SYNC=1` turns it on anywhere, which is how to try it locally.

## Files involved

- `pyproject.toml` + `uv.lock`: the runtime dependencies Vercel installs, and
  `[tool.vercel] entrypoint = "app.main:app"`. The lock drops the GUI build of OpenCV that the OCR
  package asks for (`opencv-python-headless` provides the same `cv2`), which keeps the bundle under
  the size limit. `uvicorn` and `pytest` are a `dev` group and are not deployed.
- `.python-version`: `3.12`.
- `vercel.json`: `maxDuration: 300` and `excludeFiles` (tests, eval data, docs, local data, the venv).
- `.vercelignore`: the same folders, plus `requirements.txt` (kept for local `pip install`; Vercel uses
  the lock).
- `samples/cache/`: the precomputed OCR result (36 KB) for the bundled sample drawing, keyed exactly like
  the app's OCR cache. "Use sample drawing" then "Run check" skips OCR on a cold instance and finishes in
  about 1 to 4 seconds (measured locally with a fresh data directory).
- `app/store.py`: all persistence (local data dir, optional Blob layer).

## Deploy, step by step

1. **Import the repository.** In the Vercel dashboard: Add New, Project, import
   `PedroHang/radhires-tallymark`. Vercel detects FastAPI. Leave the root directory, build and output
   settings at their defaults.
2. **Environment variables** (Project, Settings, Environment Variables):

   | Variable | Needed | Value |
   | --- | --- | --- |
   | `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` or `GEMINI_API_KEY` | For the model check | A key for at least one provider. Without any key the app runs rules-only. |
   | `UINK_PROVIDER`, `UINK_MODEL` | Optional | Default provider (`anthropic`, `openai`, `google`) and model. Without them the app starts on whichever provider has a key. |
   | `BLOB_READ_WRITE_TOKEN` / `BLOB_STORE_ID` | Added for you in step 3 | |
   | `TALLYMARK_BLOB_ACCESS` | Only for a public store | `private` (default) or `public`; must match the store. |
   | `TALLYMARK_BLOB_PREFIX` | Optional | Path prefix inside the store, default `tallymark/`. |
   | `TALLYMARK_BLOB` | Optional | `0` turns the Blob layer off even if a store is connected. |
   | `TALLYMARK_INVESTIGATOR_BUDGET_S` | Optional | Total seconds per run for the AI investigator (default 60); items it can't reach fall back to the single-shot check. Keep it well under `maxDuration`. |

3. **Create a Blob store** (recommended; without it results only live on the instance that made them).
   Storage, Create, Blob, choose **Private** (drawings and BoCs are customer documents; private stores
   need a token to read, and the app streams files through its own endpoints), pick the region closest
   to the function (default `iad1`), then Connect to Project. Vercel adds `BLOB_STORE_ID` and OIDC
   credentials (and a `BLOB_READ_WRITE_TOKEN`), which the official `vercel-blob` SDK picks up without
   code changes. Redeploy after connecting.
4. **Deploy.** Push to `main` (or click Deploy). Then open `/health`: it should say
   `"run_mode": "sync"` and, with a store connected, `"storage": {"durable": true, "durable_available": true}`.
5. **Smoke test.** Open the site, Use sample drawing, Run check. Expect 4 flags (U-1, U-2, U-3, P-1).
6. **Protect it.** The app has no login: anyone with the URL can upload files and list recent runs
   (`GET /runs`). For a shared demo, turn on Vercel Deployment Protection (password or Vercel
   Authentication) under Project, Settings, Deployment Protection.

## Limits and expected timings

| Limit | Value | What it means here |
| --- | --- | --- |
| Bundle size (Python, uncompressed) | 500 MB | Dependencies measured from `uv.lock` for Linux x86_64 / CPython 3.12: **449 MB unpacked** (largest: opencv-python-headless 150 MB, onnxruntime 64 MB, pymupdf 62 MB, numpy 56 MB). App code and frontend add about 2 MB. If a future dependency pushes it over, new projects fall back to Large Functions (beta, up to 5 GB, needs Fluid compute); existing projects opt in with `VERCEL_SUPPORT_LARGE_FUNCTIONS=1`. |
| Max duration | 300 s on Hobby (set in `vercel.json`), up to 800 s on Pro | A whole check must finish in one request. |
| Memory / CPU | 2 GB, 1 vCPU (Hobby default and maximum); Pro can raise the default to 4 GB / 2 vCPU in the dashboard | OCR is CPU-bound. |
| Request body | 4.5 MB | **Drawings or BoCs over 4.5 MB can't be uploaded** on Vercel (`413 FUNCTION_PAYLOAD_TOO_LARGE`). Lifting this needs browser-to-Blob client uploads, which are not built. |
| `/tmp` | per instance, not shared, not durable | Covered by the Blob layer. |

Timings (measured on a Windows laptop; Vercel's 1 vCPU will differ):

- Cold start: importing the app, OpenCV, PyMuPDF and onnxruntime takes a few seconds on the first
  request to a new instance. The OCR models load on the first OCR call.
- Sample drawing on a fresh instance: about 1 to 4 s for the run (rendering the 3 sheets, no OCR thanks
  to `samples/cache/`).
- A new drawing: OCR costs about 6 to 10 s per sheet (eval: 5.8 to 8.8 s per sheet), so a 3-sheet
  drawing takes about 30 s and the 20-sheet maximum can approach the 300 s limit on Hobby. The OCR cache
  is keyed by the file's hash, so re-running the same drawing skips OCR (on any instance, with Blob).
- With the model check on, add the provider's latency (a few seconds per request of up to 20 crops).

## Storage on Vercel, in detail

- Everything is written under `/tmp/tallymark` first. With Blob, the same relative path is written to the
  store (`tallymark/uploads/...`, `tallymark/cache/...`, `tallymark/runs/<id>/run.json`, `trace.json`,
  `coverage.json`, crops). A miss in `/tmp` fetches from Blob and caches it locally.
- Run docs: SQLite in `/tmp` is a per-instance index. Finished runs (and every later change, such as a
  flag disposition) are written to `runs/<id>/run.json`; `GET /runs` merges the newest ones from Blob.
- Blob failures never fail a request: the store logs a warning, serves local files and stops calling Blob
  for 60 seconds. `/health` shows the last error code.
- Nothing expires automatically in Blob. `DELETE /runs/{id}` removes a run's folder from both places;
  uploads and the OCR cache stay until removed from the Blob dashboard.
- Overwritten run docs can take up to 60 s to propagate through the Blob CDN cache (the Python SDK has no
  consistent read yet), so a disposition saved on one instance may briefly show the old value on another.

## Frontend contract on Vercel

- `POST /runs` in sync mode answers `200` with `{"run_id", "status": "done", "mode": "sync", "run": {...}}`,
  where `run` is the same object as `GET /runs/{id}` (`run.status` is `complete` or `error`). Skip the
  event stream when `status` is `"done"`. Locally it still answers `202 {"run_id", "status": "queued", "mode": "async"}`.
- Send the drawing and the BoC as files with `POST /runs` (multipart `drawing`, `boc`), not only upload
  ids: an upload id from `POST /uploads` may have been stored on another instance. Upload ids still work
  when a Blob store is connected, and the bundled sample's ids (`up_sample_...`) work on any instance.
  An unknown upload id answers `410` with "That uploaded file is no longer on the server. Load the file again."
- A run, image or crop that is gone answers `404`/`410` with a "run the check again" message; show it
  instead of a broken image.
- Keys: the browser may send `X-Tallymark-Key` (and optionally `X-Tallymark-Provider`,
  `X-Tallymark-Model`) on `POST /runs` and `POST /settings/test`. The key is used for that request only.
- The sync request can take tens of seconds for a new drawing: show progress as indeterminate.

## Check it locally before deploying

```bash
# synchronous mode + a throwaway data dir, on a port other than the dev server's
TALLYMARK_SYNC=1 TALLYMARK_DATA_DIR=/tmp/tallymark-local .venv/Scripts/python -m uvicorn app.main:app --port 8041
curl -s localhost:8041/health
curl -s -F drawing=@samples/nist_ftc_06_asme1_rd.pdf -F boc=@samples/ftc06_boc_simulated.csv -F use_llm=false localhost:8041/runs
```

`VERCEL=1` does the same and also switches the data dir to `/tmp/tallymark`. To mimic a new instance,
restart with a different `TALLYMARK_DATA_DIR`. `tests/test_deploy.py` covers sync mode, the Blob layer
across two instances (with an in-memory store), expiry without Blob, an unreachable Blob endpoint, and
the per-request key header.

To rebuild the lock after changing dependencies: edit `pyproject.toml` and `requirements.txt` together
(a test checks they agree), then run `uv lock`.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Build fails: function pattern `app/main.py` matched nothing | Vercel resolved a different entrypoint. Check `[tool.vercel] entrypoint` in `pyproject.toml` and that the `functions` key in `vercel.json` is `app/main.py`. |
| Build fails: bundle over 500 MB | `opencv-python` got installed next to the headless build (the lock was not used). Make sure `uv.lock` is committed and `requirements.txt` is ignored by `.vercelignore`; or enable Large Functions. |
| `ImportError: libGL.so.1` | Same cause: the GUI OpenCV build was installed. |
| `504 FUNCTION_INVOCATION_TIMEOUT` on Run check | The drawing has too many sheets for 300 s of OCR. Skip sheets, use Pro (800 s), or re-run (the OCR cache of sheets already read is reused). |
| `413 FUNCTION_PAYLOAD_TOO_LARGE` | File over 4.5 MB (see Limits). |
| Results disappear or "run the check again" after a reload | No Blob store connected, or `/health` shows `durable_available: false` with an error (wrong access mode: set `TALLYMARK_BLOB_ACCESS=public` for a public store). |
| Model check says no API key | Add the provider key as an environment variable and redeploy, or send it from the browser with `X-Tallymark-Key`. |
