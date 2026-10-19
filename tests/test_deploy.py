"""Serverless (Vercel) readiness: config consistency, synchronous runs, durable storage, expiry.

Run: .venv/Scripts/python -m pytest -q tests/test_deploy.py
"""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import store

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
PDF = SAMPLES / "nist_ftc_06_asme1_rd.pdf"
BOC = SAMPLES / "ftc06_boc_simulated.csv"
DEV_ONLY = {"uvicorn", "pytest"}


def _name(req: str) -> str:
    return re.split(r"[\[=<>; ]", req.strip(), maxsplit=1)[0].lower().replace("_", "-")


def test_pyproject_matches_requirements():
    py = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    runtime = {d.lower() for d in py["project"]["dependencies"]}
    reqs = [l.strip() for l in (ROOT / "requirements.txt").read_text().splitlines() if l.strip() and not l.startswith("#")]
    assert runtime == {r.lower() for r in reqs if _name(r) not in DEV_ONLY}
    assert {_name(d) for d in py["dependency-groups"]["dev"]} == DEV_ONLY
    assert py["tool"]["vercel"]["entrypoint"] == "app.main:app"
    assert "opencv-python; sys_platform == 'never'" in py["tool"]["uv"]["override-dependencies"]
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    for d in py["project"]["dependencies"]:
        name, version = d.split("==")
        assert re.search(rf'name = "{re.escape(name.lower())}"\nversion = "{re.escape(version)}"', lock, re.I), d


def test_vercel_config():
    cfg = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    fn = cfg["functions"]["app/main.py"]
    assert fn["maxDuration"] == 300
    for pattern in ("tests/**", "samples/eval/**", "eval/**", "data/**", "docs/**", "frontend/dev/**", ".venv/**"):
        assert pattern in fn["excludeFiles"]
    assert (ROOT / ".python-version").read_text().strip() == "3.12"
    ignore = (ROOT / ".vercelignore").read_text().splitlines()
    assert "data/" in ignore and ".env" in ignore


def test_seed_cache_matches_pipeline_key():
    from app.pipeline.run import PIPELINE_VERSION

    key = f"{store.sha256(PDF.read_bytes())[:24]}_{PIPELINE_VERSION}"
    seed = store.SEED_CACHE / key
    assert [p.name for p in sorted(seed.glob("sheet_*_regions.json"))] == [f"sheet_{n}_regions.json" for n in (1, 2, 3)]
    assert sum(p.stat().st_size for p in seed.iterdir()) < 200_000  # small enough to ship


@pytest.fixture
def instance(tmp_path, monkeypatch):
    """A fresh 'server instance': empty data dir, empty SQLite index. Restores the session dir after."""
    previous = store.DATA

    def new(name: str):
        return store.configure(tmp_path / name)

    new("a")
    yield new
    store.configure(previous)


def _client():
    from app.main import app

    return TestClient(app)


def _post_run(c, headers=None, use_llm="false"):
    with PDF.open("rb") as d, BOC.open("rb") as b:
        return c.post("/runs", files={"drawing": (PDF.name, d, "application/pdf"), "boc": (BOC.name, b, "text/csv")},
                      data={"use_llm": use_llm}, headers=headers or {})


def test_sync_mode_returns_finished_run(instance, monkeypatch):
    monkeypatch.setenv("TALLYMARK_SYNC", "1")
    c = _client()
    r = _post_run(c)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done" and body["mode"] == "sync"
    assert body["run"]["status"] == "complete" and len(body["run"]["flag_ids"]) == 4
    # the events stream sends the final state once and closes
    ev = c.get(f"/runs/{body['run_id']}/events")
    events = [l for l in ev.text.splitlines() if l.startswith("data: ")]
    assert len(events) == 1 and json.loads(events[0][6:])["status"] == "complete"
    assert c.get("/health").json()["run_mode"] == "sync"


def test_async_mode_unchanged(instance, monkeypatch):
    monkeypatch.delenv("TALLYMARK_SYNC", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    c = _client()
    r = _post_run(c)
    assert r.status_code == 202 and r.json()["mode"] == "async"
    ev = c.get(f"/runs/{r.json()['run_id']}/events")  # streams until complete
    assert json.loads([l for l in ev.text.splitlines() if l.startswith("data: ")][-1][6:])["status"] == "complete"


class FakeBlob:
    """In-memory stand-in for the durable store (shared by every 'instance')."""

    enabled = available = True
    last_error = None

    def __init__(self):
        self.data: dict[str, bytes] = {}

    def put(self, key, data, content_type=None, mutable=False):
        self.data[key] = bytes(data)
        return True

    def get(self, key):
        return self.data.get(key)

    def list(self, prefix):
        return [(k, float(i)) for i, k in enumerate(sorted(self.data)) if k.startswith(prefix)]

    def delete_prefix(self, prefix):
        keys = [k for k in self.data if k.startswith(prefix)]
        for k in keys:
            del self.data[k]
        return len(keys)


ARTIFACTS = ["", "/flags", "/rows", "/sheets/1.png", "/sheets/2/coverage.json", "/flags/U-1/crop.png",
             "/exceptions.csv", "/trace", "/trace/flags/P-1", "/audit.json"]


def test_durable_store_serves_another_instance(instance, monkeypatch):
    monkeypatch.setenv("TALLYMARK_SYNC", "1")
    fake = FakeBlob()
    monkeypatch.setattr(store, "blob", fake)
    c = _client()
    run_id = _post_run(c).json()["run_id"]
    assert c.get(f"/runs/{run_id}/flags/U-1/crop.png").status_code == 200  # crop made on instance a
    assert f"runs/{run_id}/run.json" in fake.data and any(k.startswith("cache/") for k in fake.data)
    # a disposition on one instance is visible on another
    assert c.patch(f"/runs/{run_id}/flags/U-1", json={"disposition": "confirmed"}).status_code == 200

    instance("b")  # new instance: nothing local
    for path in ARTIFACTS:
        r = c.get(f"/runs/{run_id}{path}")
        assert r.status_code == 200, (path, r.status_code, r.text[:200])
    assert next(f for f in c.get(f"/runs/{run_id}/flags").json() if f["flag_id"] == "U-1")["disposition"] == "confirmed"
    assert run_id in [r["run_id"] for r in c.get("/runs").json()]
    assert c.delete(f"/runs/{run_id}").status_code == 200
    assert not [k for k in fake.data if k.startswith(f"runs/{run_id}/")]


def test_without_durable_store_a_new_instance_says_expired(instance, monkeypatch):
    monkeypatch.setenv("TALLYMARK_SYNC", "1")
    c = _client()
    run_id = _post_run(c).json()["run_id"]
    up = c.get("/samples").json()
    instance("b")
    r = c.get(f"/runs/{run_id}")
    assert r.status_code == 404 and "expired" in r.json()["detail"]
    r = c.post("/runs", data={"drawing_upload_id": "up_000000000000", "boc_upload_id": "up_000000000001"})
    assert r.status_code == 410 and "Load the file again" in r.json()["detail"]
    # the bundled sample's upload ids work on any instance
    r = c.post("/runs", data={"drawing_upload_id": up["drawing"]["upload_id"], "boc_upload_id": up["boc"]["upload_id"],
                              "use_llm": "false"})
    assert r.status_code == 200 and r.json()["run"]["status"] == "complete"


def test_missing_artifact_is_410_not_500(instance, monkeypatch):
    monkeypatch.setenv("TALLYMARK_SYNC", "1")
    import shutil

    c = _client()
    run_id = _post_run(c).json()["run_id"]
    shutil.rmtree(store.CACHE)
    shutil.rmtree(store.RUNS / run_id)
    store.CACHE.mkdir()
    assert c.get(f"/runs/{run_id}/sheets/1.png").status_code == 410
    assert c.get(f"/runs/{run_id}/sheets/1/coverage.json").status_code == 410
    assert c.get(f"/runs/{run_id}/flags/U-1/crop.png").status_code == 410
    assert c.get(f"/runs/{run_id}/trace").status_code == 404


def test_unreachable_blob_degrades_to_local(instance, monkeypatch):
    """A token that points nowhere: every Blob call fails fast, the run still completes."""
    pytest.importorskip("vercel.blob")
    monkeypatch.setenv("TALLYMARK_SYNC", "1")
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "vercel_blob_rw_fakestore123_fakesecret")
    monkeypatch.setenv("VERCEL_BLOB_API_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(store, "blob", store.BlobStore())
    c = _client()
    r = _post_run(c)
    assert r.status_code == 200 and r.json()["run"]["status"] == "complete"
    status = c.get("/health").json()["storage"]
    assert status["durable"] is True and status["durable_last_error"]
    assert c.get(f"/runs/{r.json()['run_id']}/sheets/1.png").status_code == 200


def test_key_header_is_used_and_never_stored(instance, monkeypatch):
    import anthropic
    import httpx2

    from app.pipeline import llm

    seen = []

    def handler(request):
        seen.append(request.headers.get("x-api-key"))
        body = json.loads(request.content)
        n = sum(1 for p in body["messages"][0]["content"] if p["type"] == "image")
        reply = {"items": [{"crop_index": i, "text": "T"} for i in range(n)]}
        return httpx2.Response(200, json={"id": "m", "type": "message", "role": "assistant", "model": body["model"],
                                          "content": [{"type": "text", "text": json.dumps(reply)}],
                                          "stop_reason": "end_turn", "stop_sequence": None,
                                          "usage": {"input_tokens": 1, "output_tokens": 1}})

    monkeypatch.setattr(llm, "_anthropic_client", lambda cred: anthropic.Anthropic(
        api_key=cred, http_client=httpx2.Client(transport=httpx2.MockTransport(handler))))
    monkeypatch.setenv("TALLYMARK_SYNC", "1")
    secret = "sk-ant-api03-header-only-test-key"
    c = _client()
    r = _post_run(c, headers={"X-Tallymark-Key": secret, "X-Tallymark-Model": "claude-sonnet-5"}, use_llm="true")
    assert r.status_code == 200 and seen and set(seen) == {secret}
    run_id = r.json()["run_id"]
    assert r.json()["run"]["provider"] == "anthropic"
    for path in ("", "/flags", "/audit.json"):
        assert secret not in c.get(f"/runs/{run_id}{path}").text
    assert secret not in json.dumps(c.get("/settings").json())


def test_pasted_key_is_never_stored_on_the_server(monkeypatch):
    """A key may only travel with the request that uses it; PUT /settings must refuse to keep one."""
    from fastapi.testclient import TestClient

    from app import main

    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    client = TestClient(main.app)
    r = client.put("/settings", json={"credential": "sk-ant-api03-SECRET-test-key"})
    assert r.status_code == 422
    s = client.get("/settings").json()
    assert not any(p["credential_set"] for p in s["providers"])
    assert "SECRET" not in client.get("/settings").text


def test_header_key_is_used_for_that_request_only(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    seen = []
    monkeypatch.setattr(main, "ping", lambda p, cred, model: (seen.append((p, cred, model)) or (True, "ok")))
    client = TestClient(main.app)
    client.post("/settings/test", headers={"X-Tallymark-Key": "sk-ant-api03-SECRET-a", "X-Tallymark-Model": "claude-sonnet-5-5"})
    client.post("/settings/test")
    assert seen[0] == ("anthropic", "sk-ant-api03-SECRET-a", "claude-sonnet-5-5")
    assert seen[1][1] in (None, "") or not seen[1][1].endswith("SECRET-a")  # the next request doesn't inherit it
