"""Tallymark API + static frontend."""
from __future__ import annotations

import asyncio
import csv
import datetime as dt
import io
import json
import os
import threading
import uuid
from pathlib import Path

import cv2
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel

from . import store
from .pipeline.boc import BocError, parse_boc
from .pipeline.exclusions import DEFAULT_EXCLUSIONS, EXCLUSION_LABELS
from .pipeline.geometry import Box
from .pipeline.llm import (DEFAULT_PROVIDER, PROVIDERS, crop_with_outline, detect_provider, ping,
                           resolve_credential)
from .pipeline.regions import open_drawing
from .pipeline.run import RunOptions, execute, new_run_doc
from .pipeline.trace import CATALOG as TRACE_CATALOG
from .pipeline.trace import summary as trace_summary

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
SAMPLES = ROOT / "samples"
MAX_BYTES = 50 * 1024 * 1024
MAX_SHEETS = 20

app = FastAPI(title="Tallymark", version="1.0")


def sync_runs() -> bool:
    """On Vercel (or with TALLYMARK_SYNC=1) a run executes inside the POST /runs request, because a
    serverless function may not keep working after it has sent its response."""
    return bool(os.environ.get("VERCEL")) or os.environ.get("TALLYMARK_SYNC") == "1"


EXPIRED = ("This run's files are no longer on the server (on this deployment results are kept only for a short "
           "time). Run the check again.")
UPLOAD_EXPIRED = "That uploaded file is no longer on the server. Load the file again."
SAMPLE_FILES = {"drawing": "nist_ftc_06_asme1_rd.pdf", "boc": "ftc06_boc_simulated.csv"}

# Keys live in server memory (or .env / environment), one per provider. They are never written to run docs.
_settings = {"provider": DEFAULT_PROVIDER,
             "credentials": {p: None for p in PROVIDERS},
             "models": {p: cfg["models"][0] for p, cfg in PROVIDERS.items()}}


def _load_env_file() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))


_load_env_file()
if os.environ.get("UINK_MODEL"):
    _settings["models"][_settings["provider"]] = os.environ["UINK_MODEL"]
if not os.environ.get("UINK_PROVIDER") and not resolve_credential(_settings["provider"], None)[0]:
    # start on whichever provider already has a key in the environment
    _settings["provider"] = next((p for p in PROVIDERS if resolve_credential(p, None)[0]), _settings["provider"])


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- inspection / uploads
def _inspect_drawing(data: bytes, filename: str) -> dict:
    name = filename.lower()
    if not name.endswith((".pdf", ".tif", ".tiff")):
        raise HTTPException(422, "This file isn't a PDF or TIFF.")
    if len(data) > MAX_BYTES:
        raise HTTPException(422, "File is over 50 MB.")
    try:
        doc = open_drawing(data, filename)
    except Exception:  # noqa: BLE001
        raise HTTPException(422, "This file isn't a readable PDF or TIFF.")
    if doc.needs_pass or doc.is_encrypted:
        raise HTTPException(422, "PDF is password protected. Remove protection and try again.")
    if doc.page_count > MAX_SHEETS:
        raise HTTPException(422, f"This drawing has {doc.page_count} sheets. The demo checks up to {MAX_SHEETS}.")
    return {"sheets": doc.page_count}


def _inspect_boc(data: bytes, filename: str) -> dict:
    if len(data) > MAX_BYTES:
        raise HTTPException(422, "File is over 50 MB.")
    try:
        boc = parse_boc(data, filename)
    except BocError as e:
        raise HTTPException(422, str(e))
    return {"rows": len(boc.rows), "columns": len(boc.columns), "has_bbox": boc.has_bbox,
            "sheets": sorted({r.sheet for r in boc.rows}), "warnings": boc.warnings}


@app.post("/uploads")
async def upload(file: UploadFile = File(...), kind: str = Form(...)):
    data = await file.read()
    info = _inspect_drawing(data, file.filename or "") if kind == "drawing" else _inspect_boc(data, file.filename or "")
    meta = store.save_upload(data, file.filename or kind, kind)
    return {**meta, **info}


def _sample_upload_id(kind: str) -> str:
    """Stable id per bundled sample file, so any server instance can recreate it from the repo."""
    return f"up_sample_{kind}_{store.sha256((SAMPLES / SAMPLE_FILES[kind]).read_bytes())[:10]}"


def _load_upload(upload_id: str) -> tuple[bytes, dict]:
    for kind in SAMPLE_FILES:
        if upload_id == _sample_upload_id(kind):
            try:
                return store.load_upload(upload_id)
            except store.UploadMissing:
                fname = SAMPLE_FILES[kind]
                store.save_upload((SAMPLES / fname).read_bytes(), fname, kind, upload_id=upload_id)
    try:
        return store.load_upload(upload_id)
    except store.UploadMissing:
        raise HTTPException(410, UPLOAD_EXPIRED)


@app.get("/samples")
def samples():
    """Bundled sample: public NIST FTC-06 drawing + simulated BoC."""
    return _sample_payload({kind: _sample_upload_id(kind) for kind in SAMPLE_FILES})


def _sample_payload(ids: dict) -> dict:
    d_bytes, d_meta = _load_upload(ids["drawing"])
    b_bytes, b_meta = _load_upload(ids["boc"])
    return {
        "drawing": {**d_meta, **_inspect_drawing(d_bytes, d_meta["filename"])},
        "boc": {**b_meta, **_inspect_boc(b_bytes, b_meta["filename"]), "simulated": True},
        "acknowledgment": "Drawing: NIST MBE PMI test case FTC-06, used with acknowledgment.",
    }


@app.post("/precheck")
def precheck(body: dict):
    """Non-blocking schema warnings before a run (sheets with no BoC rows)."""
    d_bytes, d_meta = _load_upload(body["drawing_upload_id"])
    b_bytes, b_meta = _load_upload(body["boc_upload_id"])
    sheets = _inspect_drawing(d_bytes, d_meta["filename"])["sheets"]
    boc = _inspect_boc(b_bytes, b_meta["filename"])
    empty = [n for n in range(1, sheets + 1) if n not in boc["sheets"]]
    return {"sheets": sheets, "sheets_without_rows": empty, "has_bbox": boc["has_bbox"]}


# --------------------------------------------------------------------------- settings
class SettingsIn(BaseModel):
    provider: str | None = None
    credential: str | None = None
    model: str | None = None
    clear_credential: bool = False


def _provider_status(p: str) -> dict:
    cred, source = resolve_credential(p, _settings["credentials"][p])
    cfg = PROVIDERS[p]
    return {
        "id": p, "label": cfg["label"], "key_label": cfg["key_label"], "placeholder": cfg["placeholder"],
        "models": cfg["models"], "model": _settings["models"][p],
        "credential_set": bool(cred), "credential_source": source,
        "credential_kind": ("oauth_token" if cred and cred.startswith("sk-ant-oat") else "api_key") if cred else None,
    }


@app.get("/settings")
def get_settings():
    p = _settings["provider"]
    active = _provider_status(p)
    return {
        "provider": p,
        "providers": [_provider_status(x) for x in PROVIDERS],
        "model": active["model"],
        "models": active["models"],
        "credential_set": active["credential_set"],
        "credential_kind": active["credential_kind"],
        "credential_source": active["credential_source"],
        "exclusions": [{"key": k, "label": v, "default": k in DEFAULT_EXCLUSIONS} for k, v in EXCLUSION_LABELS.items()],
    }


@app.put("/settings")
def put_settings(body: SettingsIn):
    if body.provider:
        if body.provider not in PROVIDERS:
            raise HTTPException(422, f"Unknown provider {body.provider}.")
        _settings["provider"] = body.provider
    if body.credential:
        key = body.credential.strip()
        # a pasted key that clearly belongs to another provider switches to that provider
        _settings["provider"] = detect_provider(key) or _settings["provider"]
        _settings["credentials"][_settings["provider"]] = key
    p = _settings["provider"]
    if body.clear_credential:
        _settings["credentials"][p] = None
    if body.model:
        _settings["models"][p] = body.model
    return get_settings()


@app.post("/settings/test")
def test_credential(x_tallymark_key: str | None = Header(None), x_tallymark_provider: str | None = Header(None),
                    x_tallymark_model: str | None = Header(None)):
    """One tiny request to confirm the key works with the selected provider and model.
    A key sent in X-Tallymark-Key is used for this request only and never stored."""
    p, cred, model = _request_llm(x_tallymark_key, x_tallymark_provider, x_tallymark_model)
    ok, message = ping(p, cred, model)
    return {"ok": ok, "message": message}


def _request_llm(key: str | None, provider: str | None, model: str | None) -> tuple[str, str | None, str]:
    """Provider, credential and model for one request. Header values (sent by a browser that keeps the
    key itself, as on a serverless deployment) win over the server's in-memory settings, and are never
    stored or logged."""
    key = (key or "").strip() or None
    p = provider if provider in PROVIDERS else ((detect_provider(key) if key else None) or _settings["provider"])
    cred = key or _settings["credentials"][p]
    return p, cred, (model or _settings["models"][p])


# --------------------------------------------------------------------------- runs
@app.post("/runs", status_code=202)
async def create_run(
    drawing: UploadFile | None = File(None),
    boc: UploadFile | None = File(None),
    drawing_upload_id: str | None = Form(None),
    boc_upload_id: str | None = Form(None),
    exclusions: str | None = Form(None),
    extra_regex: str | None = Form(None),
    use_llm: bool = Form(True),
    skip_sheets: str | None = Form(None),
    simulated: bool | None = Form(None),
    x_tallymark_key: str | None = Header(None),
    x_tallymark_provider: str | None = Header(None),
    x_tallymark_model: str | None = Header(None),
):
    if drawing is not None:
        d_bytes = await drawing.read()
        _inspect_drawing(d_bytes, drawing.filename or "")
        d_meta = store.save_upload(d_bytes, drawing.filename or "drawing.pdf", "drawing")
    elif drawing_upload_id:
        d_bytes, d_meta = _load_upload(drawing_upload_id)
    else:
        raise HTTPException(422, "Load a drawing and a BoC first.")
    if boc is not None:
        b_bytes = await boc.read()
        _inspect_boc(b_bytes, boc.filename or "")
        b_meta = store.save_upload(b_bytes, boc.filename or "boc.csv", "boc")
    elif boc_upload_id:
        b_bytes, b_meta = _load_upload(boc_upload_id)
    else:
        raise HTTPException(422, "Load a drawing and a BoC first.")

    llm_provider, llm_cred, llm_model = _request_llm(x_tallymark_key, x_tallymark_provider, x_tallymark_model)
    opts = RunOptions(
        exclusions=json.loads(exclusions) if exclusions else list(DEFAULT_EXCLUSIONS),
        extra_regex=extra_regex or None,
        use_llm=use_llm,
        model=llm_model,
        provider=llm_provider,
        credential=llm_cred,
        skip_sheets=json.loads(skip_sheets) if skip_sheets else [],
        simulated=simulated if simulated is not None else "simulated" in b_meta["filename"].lower(),
    )
    run_id = f"run_{dt.datetime.now().strftime('%Y-%m-%d')}_{uuid.uuid4().hex[:6]}"
    doc = new_run_doc(run_id, _now(), d_meta, b_meta, opts)
    store.put_run(doc)
    args = (run_id, d_bytes, d_meta["filename"], b_bytes, b_meta["filename"], opts)
    if sync_runs():
        # serverless: finish the whole check inside this request (worker thread keeps the event loop free)
        await run_in_threadpool(execute, *args)
        return JSONResponse({"run_id": run_id, "status": "done", "mode": "sync", "run": _run_summary(_run_or_404(run_id))},
                            status_code=200)
    threading.Thread(target=execute, args=args, daemon=True).start()
    return {"run_id": run_id, "status": "queued", "mode": "async"}


def _run_or_404(run_id: str) -> dict:
    doc = store.get_run(run_id)
    if doc is None:
        raise HTTPException(404, "Run not found. It may have expired: run the check again.")
    return doc


def _run_summary(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k not in ("flags", "rows")} | {"flag_ids": [f["flag_id"] for f in doc["flags"]]}


@app.get("/runs")
def runs():
    return store.list_runs()


@app.get("/runs/{run_id}")
def get_run(run_id: str):
    return _run_summary(_run_or_404(run_id))


@app.get("/runs/{run_id}/events")
async def run_events(run_id: str):
    """Server-sent events with stage progress until the run completes or fails."""
    _run_or_404(run_id)

    # In sync mode the run is already finished (or runs on another instance), so send the current
    # state once and close. Locally, stream until the run ends, with a ceiling so a lost run can't
    # hold a connection forever.
    deadline = asyncio.get_running_loop().time() + (0 if sync_runs() else 900)

    async def gen():
        last = None
        while True:
            doc = await run_in_threadpool(store.get_run, run_id)
            if doc is None:
                yield f"data: {json.dumps({'status': 'error', 'stages': [], 'error': {'message': EXPIRED}})}\n\n"
                break
            payload = json.dumps({"status": doc["status"], "stages": doc["stages"], "error": doc["error"]})
            if payload != last:
                yield f"data: {payload}\n\n"
                last = payload
            if doc["status"] in ("complete", "error") or asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.15)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/runs/{run_id}/flags")
def get_flags(run_id: str, cls: str | None = None, disposition: str | None = None, sheet: int | None = None):
    flags = _run_or_404(run_id)["flags"]
    if cls:
        flags = [f for f in flags if f["class"] == cls]
    if disposition:
        flags = [f for f in flags if f["disposition"] == disposition]
    if sheet:
        flags = [f for f in flags if f["sheet"] == sheet]
    return flags


@app.get("/runs/{run_id}/rows")
def get_rows(run_id: str):
    return _run_or_404(run_id)["rows"]


class DispositionIn(BaseModel):
    disposition: str
    note: str | None = None


@app.patch("/runs/{run_id}/flags/{flag_id}")
def patch_flag(run_id: str, flag_id: str, body: DispositionIn):
    if body.disposition not in ("open", "confirmed", "excluded", "dismissed"):
        raise HTTPException(422, "disposition must be open, confirmed, excluded or dismissed")
    updated = {}

    def fn(doc):
        for f in doc["flags"]:
            if f["flag_id"] == flag_id:
                f["disposition"] = body.disposition
                f["disposition_note"] = body.note if body.disposition != "open" else None
                f["disposed_at"] = _now() if body.disposition != "open" else None
                updated.update(f)

    if store.update_run(run_id, fn) is None:
        raise HTTPException(404, "Run not found.")
    if not updated:
        raise HTTPException(404, "Flag not found.")
    return updated


def _sheet_path(doc: dict, n: int, kind: str) -> Path:
    cache = doc["drawing"].get("cache")
    if not cache:
        raise HTTPException(404, "Sheet not rendered yet.")
    p = store.CACHE / cache / f"sheet_{n}_{kind}.png"
    if not store.materialize(p):
        if doc["status"] in ("complete", "error"):
            raise HTTPException(410, EXPIRED)
        raise HTTPException(404, "Sheet not rendered yet.")
    return p


@app.get("/runs/{run_id}/sheets/{n}.png")
def sheet_png(run_id: str, n: int):
    return FileResponse(_sheet_path(_run_or_404(run_id), n, "view"), media_type="image/png",
                        headers={"Cache-Control": "max-age=3600"})


@app.get("/runs/{run_id}/sheets/{n}/coverage.json")
def sheet_coverage(run_id: str, n: int):
    doc = _run_or_404(run_id)
    path = store.run_file(run_id, "coverage.json")
    if path is None:
        raise HTTPException(410 if doc["status"] == "complete" else 404,
                            EXPIRED if doc["status"] == "complete" else "Coverage not ready.")
    overlay = json.loads(path.read_text(encoding="utf-8")).get(str(n), {"covered": [], "excluded": []})
    return {**overlay, "flags": [f for f in doc["flags"] if f["sheet"] == n]}


@app.get("/runs/{run_id}/flags/{flag_id}/crop.png")
def flag_crop(run_id: str, flag_id: str):
    doc = _run_or_404(run_id)
    flag = next((f for f in doc["flags"] if f["flag_id"] == flag_id), None)
    if flag is None:
        raise HTTPException(404, "Flag not found.")
    out = store.run_dir(run_id) / f"crop_{flag_id}.png"
    if not store.materialize(out):
        work = cv2.imread(str(_sheet_path(doc, flag["sheet"], "work")))[:, :, ::-1].copy()
        im = crop_with_outline(work, [tuple(p) for p in flag["poly"]], Box.from_dict(flag["bbox"]), pad_ratio=0.45)
        im.save(out)
        store.persist(out)
    return FileResponse(out, media_type="image/png")


EXPORT_COLUMNS = ["flag_id", "class", "sheet", "zone", "bbox_x", "bbox_y", "bbox_w", "bbox_h", "drawing_text",
                  "char_no", "nearest_char_no", "reason", "confidence", "disposition", "note", "audit_ref"]


@app.get("/runs/{run_id}/exceptions.csv")
def exceptions_csv(run_id: str, include_dismissed: bool = False):
    doc = _run_or_404(run_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(EXPORT_COLUMNS)
    for f in doc["flags"]:
        if f["disposition"] == "dismissed" and not include_dismissed:
            continue
        b = f["bbox"]
        w.writerow([f["flag_id"], f["class"], f["sheet"], f["zone"], b["x"], b["y"], b["w"], b["h"],
                    f.get("transcription") or f["drawing_text"], f["char_no"] or "", f["nearest_char_no"] or "",
                    f["reason"], f["confidence"], f["disposition"], f["disposition_note"] or "",
                    f"/runs/{run_id}/trace/flags/{f['flag_id']}" if (doc.get("trace") or {}).get("available") else ""])
    stem = Path(doc["drawing"]["filename"]).stem
    short = "ftc06" if "ftc_06" in stem else stem
    name = f"{short}_exceptions_{doc['created_at'][:10]}.csv"
    return Response(buf.getvalue(), media_type="text/csv",
                     headers={"Content-Disposition": f'attachment; filename="{name}"'})


# --------------------------------------------------------------------------- decision trace (read-only)
# Schema: docs/TRACE.md. The trace is written when a run finishes (partial on a failed run).
def _trace_or_404(run_id: str) -> dict:
    doc = _run_or_404(run_id)
    trace = store.get_trace(run_id)
    if trace is None:
        if doc["status"] in ("queued", "running"):
            raise HTTPException(409, "The trace is written when the run finishes.")
        raise HTTPException(404, "No trace for this run (it ran before tracing was added, or it expired).")
    return trace


@app.get("/runs/{run_id}/trace")
def get_trace(run_id: str):
    return trace_summary(_trace_or_404(run_id))


@app.get("/runs/{run_id}/trace/catalog")
def get_trace_catalog(run_id: str):
    trace = _trace_or_404(run_id)
    return {**TRACE_CATALOG, "trace_catalog_version": trace.get("catalog_version")}


@app.get("/runs/{run_id}/trace/regions")
def get_trace_regions(run_id: str, sheet: int | None = None, outcome: str | None = None, q: str | None = None,
                      offset: int = 0, limit: int = 200):
    regions = _trace_or_404(run_id)["regions"]
    if sheet is not None:
        regions = [r for r in regions if r["sheet"] == sheet]
    if outcome:
        wanted = set(outcome.split(","))
        regions = [r for r in regions if r["final"]["outcome"] in wanted]
    if q:
        regions = [r for r in regions if q.lower() in (r["text"] or "").lower()]
    limit = max(1, min(limit, 1000))
    return {"total": len(regions), "offset": offset, "limit": limit, "items": regions[offset:offset + limit]}


@app.get("/runs/{run_id}/trace/regions/{region_id}")
def get_trace_region(run_id: str, region_id: str):
    reg = next((r for r in _trace_or_404(run_id)["regions"] if r["region_id"] == region_id), None)
    if reg is None:
        raise HTTPException(404, "Region not found.")
    return reg


@app.get("/runs/{run_id}/trace/flags/{flag_id}")
def get_trace_flag(run_id: str, flag_id: str):
    rec = _trace_or_404(run_id)["flags"].get(flag_id)
    if rec is None:
        raise HTTPException(404, "Flag not found.")
    return rec


@app.get("/runs/{run_id}/trace/llm/{call_id}")
def get_trace_llm_call(run_id: str, call_id: str):
    call = next((c for c in _trace_or_404(run_id)["llm_calls"] if c["call_id"] == call_id), None)
    if call is None:
        raise HTTPException(404, "Model call not found.")
    return call


@app.get("/runs/{run_id}/trace/llm/{call_id}/crops/{i}.png")
def get_trace_llm_crop(run_id: str, call_id: str, i: int):
    _run_or_404(run_id)
    data = store.get_trace_artifact(run_id, f"llm/{Path(call_id).name}/{int(i)}.png")
    if data is None:
        raise HTTPException(404, "Crop not found.")
    return Response(data, media_type="image/png", headers={"Cache-Control": "max-age=3600"})


@app.get("/runs/{run_id}/audit.json")
def get_audit_json(run_id: str):
    trace = _trace_or_404(run_id)
    doc = _run_or_404(run_id)
    body = {**trace, "catalog": TRACE_CATALOG,
            "flags_as_reported": doc["flags"], "stats": doc.get("stats"), "warnings": doc.get("warnings")}
    name = f"{Path(doc['drawing']['filename']).stem}_audit_{run_id}.json"
    return Response(json.dumps(body, ensure_ascii=False, indent=1), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.delete("/runs/{run_id}")
def delete_run(run_id: str):
    if not store.delete_run(run_id):
        raise HTTPException(404, "Run not found.")
    return {"deleted": run_id}


@app.get("/health")
def health():
    return {"ok": True, "run_mode": "sync" if sync_runs() else "async", "storage": store.storage_status()}


@app.exception_handler(HTTPException)
async def http_error(_, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
