"""Local persistence: SQLite for runs, plain files for uploads, renders and OCR cache."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UPLOADS = DATA / "uploads"
RUNS = DATA / "runs"
CACHE = DATA / "cache"
DB_PATH = DATA / "runs.sqlite3"

for d in (UPLOADS, RUNS, CACHE):
    d.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, created_at TEXT, status TEXT, doc TEXT)"
    )
    return conn


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_upload(data: bytes, filename: str, kind: str) -> dict:
    upload_id = f"up_{uuid.uuid4().hex[:12]}"
    folder = UPLOADS / upload_id
    folder.mkdir(parents=True)
    safe = Path(filename).name or f"{kind}.bin"
    (folder / safe).write_bytes(data)
    meta = {"upload_id": upload_id, "filename": safe, "kind": kind, "size": len(data), "sha256": sha256(data)}
    (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return meta


def load_upload(upload_id: str) -> tuple[bytes, dict]:
    folder = UPLOADS / Path(upload_id).name
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    return (folder / meta["filename"]).read_bytes(), meta


def run_dir(run_id: str) -> Path:
    p = RUNS / Path(run_id).name
    p.mkdir(parents=True, exist_ok=True)
    return p


def put_run(doc: dict) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, created_at, status, doc) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET status=excluded.status, doc=excluded.doc",
            (doc["run_id"], doc["created_at"], doc["status"], json.dumps(doc)),
        )


def get_run(run_id: str) -> dict | None:
    with _lock, _conn() as conn:
        row = conn.execute("SELECT doc FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return json.loads(row[0]) if row else None


def update_run(run_id: str, fn) -> dict | None:
    """Read-modify-write under the lock. fn mutates the doc in place."""
    with _lock, _conn() as conn:
        row = conn.execute("SELECT doc FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            return None
        doc = json.loads(row[0])
        fn(doc)
        conn.execute("UPDATE runs SET status = ?, doc = ? WHERE run_id = ?", (doc["status"], json.dumps(doc), run_id))
    return doc


def list_runs(limit: int = 20) -> list[dict]:
    with _lock, _conn() as conn:
        rows = conn.execute("SELECT doc FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for (d,) in rows:
        doc = json.loads(d)
        out.append({k: doc.get(k) for k in ("run_id", "created_at", "status", "drawing", "boc", "stats")})
    return out


def delete_run(run_id: str) -> bool:
    import shutil

    with _lock, _conn() as conn:
        n = conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,)).rowcount
    shutil.rmtree(RUNS / Path(run_id).name, ignore_errors=True)
    return n > 0
