"""Tallymark API (BRD section 7.5) + static frontend."""
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
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import store
from .pipeline.boc import BocError, parse_boc
from .pipeline.exclusions import DEFAULT_EXCLUSIONS, EXCLUSION_LABELS
from .pipeline.geometry import Box
from .pipeline.llm import (DEFAULT_PROVIDER, PROVIDERS, crop_with_outline, detect_provider, ping,
                           resolve_credential)
from .pipeline.regions import open_drawing
from .pipeline.run import RunOptions, execute, new_run_doc

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
SAMPLES = ROOT / "samples"
MAX_BYTES = 50 * 1024 * 1024
MAX_SHEETS = 20

app = FastAPI(title="Tallymark", version="1.0")

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


@app.get("/samples")
def samples():
    """Bundled sample: public NIST FTC-06 drawing + simulated BoC (uploaded on first request)."""
    marker = store.UPLOADS / "samples.json"
    if marker.exists():
        ids = json.loads(marker.read_text(encoding="utf-8"))
        if all((store.UPLOADS / i).exists() for i in ids.values()):
            return _sample_payload(ids)
    ids = {}
    for kind, fname in (("drawing", "nist_ftc_06_asme1_rd.pdf"), ("boc", "ftc06_boc_simulated.csv")):
        ids[kind] = store.save_upload((SAMPLES / fname).read_bytes(), fname, kind)["upload_id"]
    marker.write_text(json.dumps(ids), encoding="utf-8")
    return _sample_payload(ids)


def _sample_payload(ids: dict) -> dict:
    d_bytes, d_meta = store.load_upload(ids["drawing"])
    b_bytes, b_meta = store.load_upload(ids["boc"])
    return {
        "drawing": {**d_meta, **_inspect_drawing(d_bytes, d_meta["filename"])},
        "boc": {**b_meta, **_inspect_boc(b_bytes, b_meta["filename"]), "simulated": True},
        "acknowledgment": "Drawing: NIST MBE PMI test case FTC-06, used with acknowledgment.",
    }


@app.post("/precheck")
def precheck(body: dict):
    """Non-blocking schema warnings before a run (sheets with no BoC rows)."""
    d_bytes, d_meta = store.load_upload(body["drawing_upload_id"])
    b_bytes, b_meta = store.load_upload(body["boc_upload_id"])
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
def test_credential():
    """One tiny request to confirm the key works with the selected provider and model."""
    p = _settings["provider"]
    ok, message = ping(p, _settings["credentials"][p], _settings["models"][p])
    return {"ok": ok, "message": message}


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
):
    if drawing is not None:
        d_bytes = await drawing.read()
        _inspect_drawing(d_bytes, drawing.filename or "")
        d_meta = store.save_upload(d_bytes, drawing.filename or "drawing.pdf", "drawing")
    elif drawing_upload_id:
        d_bytes, d_meta = store.load_upload(drawing_upload_id)
    else:
        raise HTTPException(422, "Load a drawing and a BoC first.")
    if boc is not None:
        b_bytes = await boc.read()
        _inspect_boc(b_bytes, boc.filename or "")
        b_meta = store.save_upload(b_bytes, boc.filename or "boc.csv", "boc")
    elif boc_upload_id:
        b_bytes, b_meta = store.load_upload(boc_upload_id)
    else:
        raise HTTPException(422, "Load a drawing and a BoC first.")

    opts = RunOptions(
        exclusions=json.loads(exclusions) if exclusions else list(DEFAULT_EXCLUSIONS),
        extra_regex=extra_regex or None,
        use_llm=use_llm,
        model=_settings["models"][_settings["provider"]],
        provider=_settings["provider"],
        credential=_settings["credentials"][_settings["provider"]],
        skip_sheets=json.loads(skip_sheets) if skip_sheets else [],
        simulated=simulated if simulated is not None else "simulated" in b_meta["filename"].lower(),
    )
    run_id = f"run_{dt.datetime.now().strftime('%Y-%m-%d')}_{uuid.uuid4().hex[:6]}"
    doc = new_run_doc(run_id, _now(), d_meta, b_meta, opts)
    store.put_run(doc)
    threading.Thread(target=execute, args=(run_id, d_bytes, d_meta["filename"], b_bytes, b_meta["filename"], opts),
                     daemon=True).start()
    return {"run_id": run_id}


def _run_or_404(run_id: str) -> dict:
    doc = store.get_run(run_id)
    if doc is None:
        raise HTTPException(404, "Run not found.")
    return doc


@app.get("/runs")
def runs():
    return store.list_runs()


@app.get("/runs/{run_id}")
def get_run(run_id: str):
    doc = _run_or_404(run_id)
    return {k: v for k, v in doc.items() if k not in ("flags", "rows")} | {"flag_ids": [f["flag_id"] for f in doc["flags"]]}


@app.get("/runs/{run_id}/events")
async def run_events(run_id: str):
    """Server-sent events with stage progress until the run completes or fails."""
    _run_or_404(run_id)

    async def gen():
        last = None
        while True:
            doc = store.get_run(run_id)
            payload = json.dumps({"status": doc["status"], "stages": doc["stages"], "error": doc["error"]})
            if payload != last:
                yield f"data: {payload}\n\n"
                last = payload
            if doc["status"] in ("complete", "error"):
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
    if not p.exists():
        raise HTTPException(404, "Sheet not rendered yet.")
    return p


@app.get("/runs/{run_id}/sheets/{n}.png")
def sheet_png(run_id: str, n: int):
    return FileResponse(_sheet_path(_run_or_404(run_id), n, "view"), media_type="image/png",
                        headers={"Cache-Control": "max-age=3600"})


@app.get("/runs/{run_id}/sheets/{n}/coverage.json")
def sheet_coverage(run_id: str, n: int):
    doc = _run_or_404(run_id)
    path = store.run_dir(run_id) / "coverage.json"
    if not path.exists():
        raise HTTPException(404, "Coverage not ready.")
    overlay = json.loads(path.read_text(encoding="utf-8")).get(str(n), {"covered": [], "excluded": []})
    return {**overlay, "flags": [f for f in doc["flags"] if f["sheet"] == n]}


@app.get("/runs/{run_id}/flags/{flag_id}/crop.png")
def flag_crop(run_id: str, flag_id: str):
    doc = _run_or_404(run_id)
    flag = next((f for f in doc["flags"] if f["flag_id"] == flag_id), None)
    if flag is None:
        raise HTTPException(404, "Flag not found.")
    out = store.run_dir(run_id) / f"crop_{flag_id}.png"
    if not out.exists():
        work = cv2.imread(str(_sheet_path(doc, flag["sheet"], "work")))[:, :, ::-1].copy()
        im = crop_with_outline(work, [tuple(p) for p in flag["poly"]], Box.from_dict(flag["bbox"]), pad_ratio=0.45)
        im.save(out)
    return FileResponse(out, media_type="image/png")


EXPORT_COLUMNS = ["flag_id", "class", "sheet", "zone", "bbox_x", "bbox_y", "bbox_w", "bbox_h", "drawing_text",
                  "char_no", "nearest_char_no", "reason", "confidence", "disposition", "note"]


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
                    f["reason"], f["confidence"], f["disposition"], f["disposition_note"] or ""])
    stem = Path(doc["drawing"]["filename"]).stem
    short = "ftc06" if "ftc_06" in stem else stem
    name = f"{short}_exceptions_{doc['created_at'][:10]}.csv"
    return Response(buf.getvalue(), media_type="text/csv",
                     headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.delete("/runs/{run_id}")
def delete_run(run_id: str):
    if not store.delete_run(run_id):
        raise HTTPException(404, "Run not found.")
    return {"deleted": run_id}


@app.get("/health")
def health():
    return {"ok": True}


@app.exception_handler(HTTPException)
async def http_error(_, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
