"""Persistence for uploads, run docs, rendered sheets, crops, the OCR cache and decision traces.

Two layers:

* Local (always): files under one data directory plus a SQLite index of run docs.
  The directory is TALLYMARK_DATA_DIR, else /tmp/tallymark on Vercel (only /tmp is writable
  there), else ./data. Tests point it at a temporary directory with configure().
* Durable (optional): Vercel Blob. Enabled when BLOB_READ_WRITE_TOKEN (or a connected store's
  BLOB_STORE_ID) is set and TALLYMARK_BLOB is not "0". Every artifact written locally is also
  written to Blob under the same relative path, and a read that misses locally fetches it from
  Blob and caches it. This is what lets a serverless deployment answer a request on an instance
  that did not run the check. Blob errors never fail a request: the store falls back to local
  files and stops calling Blob for a minute.

Callers use materialize(path) before reading a file and persist(path) after writing one, so the
rest of the app keeps working with plain paths.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path

log = logging.getLogger("tallymark.store")

ROOT = Path(__file__).resolve().parent.parent
SEED_CACHE = ROOT / "samples" / "cache"  # precomputed OCR for the bundled sample (read-only)
DATA = UPLOADS = RUNS = CACHE = DB_PATH = None  # set by configure()


def default_data_dir() -> Path:
    """TALLYMARK_DATA_DIR if set, /tmp/tallymark on Vercel (only /tmp is writable), else ./data."""
    env = os.environ.get("TALLYMARK_DATA_DIR")
    if env:
        return Path(env)
    if os.environ.get("VERCEL"):
        return Path("/tmp/tallymark")
    return ROOT / "data"


def configure(base: str | Path | None = None) -> Path:
    """Point every store path at `base` (tests use a temporary directory). Returns the base."""
    global DATA, UPLOADS, RUNS, CACHE, DB_PATH
    DATA = Path(base) if base else default_data_dir()
    UPLOADS, RUNS, CACHE, DB_PATH = DATA / "uploads", DATA / "runs", DATA / "cache", DATA / "runs.sqlite3"
    for d in (UPLOADS, RUNS, CACHE):
        d.mkdir(parents=True, exist_ok=True)
    return DATA


configure()

_lock = threading.Lock()


class UploadMissing(FileNotFoundError):
    """An upload id this instance (and the durable store) does not know: expired or never uploaded."""


# --------------------------------------------------------------------------- durable layer (Vercel Blob)
_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="blob")


def _off_loop(fn, *args, **kwargs):
    """The Blob SDK's sync API must not run on a thread with an active event loop (async routes)."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return fn(*args, **kwargs)
    return _POOL.submit(fn, *args, **kwargs).result()


class BlobStore:
    """Thin wrapper over the official Vercel Blob Python SDK (vercel-blob >= 0.5, sync API)."""

    def __init__(self) -> None:
        self.access = os.environ.get("TALLYMARK_BLOB_ACCESS", "private")
        self.prefix = os.environ.get("TALLYMARK_BLOB_PREFIX", "tallymark/")
        self.enabled = (os.environ.get("TALLYMARK_BLOB", "1") != "0"
                        and bool(os.environ.get("BLOB_READ_WRITE_TOKEN") or os.environ.get("BLOB_STORE_ID")))
        self.last_error: str | None = None
        self._down_until = 0.0

    @property
    def available(self) -> bool:
        return self.enabled and time.monotonic() >= self._down_until

    def _fail(self, op: str, key: str, exc: Exception) -> None:
        self.last_error = f"{op} {key}: {type(exc).__name__}"
        self._down_until = time.monotonic() + 60
        log.warning("blob %s failed for %s (%s); using local files for 60 s", op, key, type(exc).__name__)

    def put(self, key: str, data: bytes, content_type: str | None = None, mutable: bool = False) -> bool:
        if not self.available:
            return False
        try:
            from vercel.blob import sync as vb

            _off_loop(vb.put_blob, self.prefix + key, data, access=self.access, content_type=content_type,
                      allow_overwrite=True, cache_control_max_age=60 if mutable else None)
            return True
        except Exception as exc:  # noqa: BLE001
            self._fail("put", key, exc)
            return False

    def get(self, key: str) -> bytes | None:
        if not self.available:
            return None
        try:
            from vercel.blob import sync as vb

            return _off_loop(vb.get_blob, self.prefix + key, access=self.access).body
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "BlobNotFoundError":
                return None
            self._fail("get", key, exc)
            return None

    def list(self, prefix: str) -> list[tuple[str, float]]:
        """(key, uploaded_at timestamp) under prefix."""
        if not self.available:
            return []
        try:
            from vercel.blob import sync as vb

            def run():
                return [(b.pathname[len(self.prefix):], b.uploaded_at.timestamp())
                        for b in vb.query_blobs(prefix=self.prefix + prefix)]

            return _off_loop(run)
        except Exception as exc:  # noqa: BLE001
            self._fail("list", prefix, exc)
            return []

    def delete_prefix(self, prefix: str) -> int:
        keys = [k for k, _ in self.list(prefix)]
        if not keys or not self.available:
            return 0
        try:
            from vercel.blob import sync as vb

            for i in range(0, len(keys), 1000):
                _off_loop(vb.delete_blob, [self.prefix + k for k in keys[i:i + 1000]])
            return len(keys)
        except Exception as exc:  # noqa: BLE001
            self._fail("delete", prefix, exc)
            return 0


blob = BlobStore()


def storage_status() -> dict:
    return {"data_dir_kind": "tmp" if str(DATA).startswith("/tmp") else "local", "durable": blob.enabled,
            "durable_available": blob.available, "durable_last_error": blob.last_error}


def _key(path: Path) -> str | None:
    try:
        return Path(path).resolve().relative_to(DATA.resolve()).as_posix()
    except ValueError:
        return None


_TYPES = {".json": "application/json", ".png": "image/png", ".csv": "text/csv", ".pdf": "application/pdf"}


def persist(path: Path, mutable: bool = False) -> None:
    """Copy a file written under the data dir to the durable store (no-op without one)."""
    key = _key(path)
    if key is None or not blob.enabled:
        return
    try:
        data = Path(path).read_bytes()
    except OSError:
        return
    blob.put(key, data, _TYPES.get(Path(path).suffix.lower(), "application/octet-stream"), mutable=mutable)


def materialize(path: Path) -> bool:
    """True when the file is available locally, fetching it from the bundled seed cache or the
    durable store on a miss."""
    path = Path(path)
    if path.exists():
        return True
    key = _key(path)
    if key is None:
        return False
    if key.startswith("cache/"):
        seed = SEED_CACHE / key[len("cache/"):]
        if seed.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(seed, path)
            return True
    data = blob.get(key) if blob.enabled else None
    if data is None:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex[:6]}.part")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return True


def write_file(path: Path, data: bytes | str, mutable: bool = False) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)
    persist(path, mutable=mutable)


# --------------------------------------------------------------------------- uploads
def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, created_at TEXT, status TEXT, doc TEXT)"
    )
    return conn


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_upload(data: bytes, filename: str, kind: str, upload_id: str | None = None) -> dict:
    upload_id = upload_id or f"up_{uuid.uuid4().hex[:12]}"
    folder = UPLOADS / upload_id
    folder.mkdir(parents=True, exist_ok=True)
    safe = Path(filename).name or f"{kind}.bin"
    write_file(folder / safe, data)
    meta = {"upload_id": upload_id, "filename": safe, "kind": kind, "size": len(data), "sha256": sha256(data)}
    write_file(folder / "meta.json", json.dumps(meta))
    return meta


def load_upload(upload_id: str) -> tuple[bytes, dict]:
    folder = UPLOADS / Path(upload_id).name
    if not materialize(folder / "meta.json"):
        raise UploadMissing(upload_id)
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    if not materialize(folder / meta["filename"]):
        raise UploadMissing(upload_id)
    return (folder / meta["filename"]).read_bytes(), meta


# --------------------------------------------------------------------------- run docs
# SQLite is a per-instance index. With a durable store, finished run docs (complete / error, and
# every later change such as a flag disposition) are also written to runs/<id>/run.json, and a run
# this instance has not seen is loaded from there on demand.
_TERMINAL = ("complete", "error")


def run_dir(run_id: str) -> Path:
    p = RUNS / Path(run_id).name
    p.mkdir(parents=True, exist_ok=True)
    return p


def _run_key(run_id: str) -> str:
    return f"runs/{Path(run_id).name}/run.json"


def _persist_doc(doc: dict) -> None:
    if blob.enabled and doc.get("status") in _TERMINAL:
        blob.put(_run_key(doc["run_id"]), json.dumps(doc).encode("utf-8"), "application/json", mutable=True)


def _index(doc: dict) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, created_at, status, doc) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET status=excluded.status, doc=excluded.doc",
            (doc["run_id"], doc["created_at"], doc["status"], json.dumps(doc)),
        )


def put_run(doc: dict) -> None:
    _index(doc)
    _persist_doc(doc)


def _load_durable(run_id: str) -> dict | None:
    data = blob.get(_run_key(run_id)) if blob.enabled else None
    if data is None:
        return None
    doc = json.loads(data)
    _index(doc)
    return doc


def get_run(run_id: str) -> dict | None:
    with _lock, _conn() as conn:
        row = conn.execute("SELECT doc FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row:
        return json.loads(row[0])
    return _load_durable(run_id)


def update_run(run_id: str, fn) -> dict | None:
    """Read-modify-write under the lock. fn mutates the doc in place."""
    with _lock, _conn() as conn:
        found = conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if not found and _load_durable(run_id) is None:
        return None
    with _lock, _conn() as conn:
        row = conn.execute("SELECT doc FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            return None
        doc = json.loads(row[0])
        fn(doc)
        conn.execute("UPDATE runs SET status = ?, doc = ? WHERE run_id = ?", (doc["status"], json.dumps(doc), run_id))
    _persist_doc(doc)
    return doc


def list_runs(limit: int = 20) -> list[dict]:
    if blob.enabled:
        # pull the newest finished runs other instances wrote into this instance's index
        with _lock, _conn() as conn:
            known = {r for (r,) in conn.execute("SELECT run_id FROM runs")}
        docs = sorted(((k, t) for k, t in blob.list("runs/") if k.endswith("/run.json")), key=lambda x: -x[1])
        for k, _ in docs[:limit]:
            rid = k.split("/")[1]
            if rid not in known:
                _load_durable(rid)
    with _lock, _conn() as conn:
        rows = conn.execute("SELECT doc FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for (d,) in rows:
        doc = json.loads(d)
        out.append({k: doc.get(k) for k in ("run_id", "created_at", "status", "drawing", "boc", "stats")})
    return out


def run_file(run_id: str, name: str) -> Path | None:
    """Path of a file in the run folder, fetched from the durable store if needed; None if missing."""
    p = run_dir(run_id) / name
    return p if materialize(p) else None


# --------------------------------------------------------------------------- decision trace
# The trace is one JSON document plus small binary artifacts (model crops) per run.
TRACE_FILE = "trace.json"


def put_trace(run_id: str, trace: dict) -> None:
    write_file(run_dir(run_id) / TRACE_FILE, json.dumps(trace, ensure_ascii=False))


def get_trace(run_id: str) -> dict | None:
    p = RUNS / Path(run_id).name / TRACE_FILE
    return json.loads(p.read_text(encoding="utf-8")) if materialize(p) else None


def put_trace_artifact(run_id: str, name: str, data: bytes) -> None:
    """name is a relative path such as 'llm/L1/0.png'."""
    write_file(run_dir(run_id) / "trace" / Path(name), data)


def get_trace_artifact(run_id: str, name: str) -> bytes | None:
    base = (RUNS / Path(run_id).name / "trace").resolve()
    p = (base / name).resolve()
    if base not in p.parents or not materialize(p) or not p.is_file():
        return None
    return p.read_bytes()


def delete_run(run_id: str) -> bool:
    with _lock, _conn() as conn:
        n = conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,)).rowcount
    shutil.rmtree(RUNS / Path(run_id).name, ignore_errors=True)
    if blob.enabled:
        n += blob.delete_prefix(f"runs/{Path(run_id).name}/")
    return n > 0
