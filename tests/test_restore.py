"""User override: restore an item the AI dropped as a normal open flag (app/restore.py).

Runs on the throwaway data dir from conftest.py, with the model provider mocked (no network).
Run: .venv/Scripts/python -m pytest -q tests/test_restore.py
"""
from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from app import store
from app.main import app
from tests.test_trace import SAMPLES, _Capture, _check, _patch_anthropic, _verdict_reply


@pytest.fixture()
def dropped_run(monkeypatch, tmp_path):
    with (SAMPLES / "ftc06_boc_groundtruth.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    boc = tmp_path / "one_row.csv"
    with boc.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerow(rows[1])
    _patch_anthropic(monkeypatch, _Capture(_verdict_reply))  # odd crops: "not a requirement"
    doc = _check(boc, use_llm=True, credential="sk-ant-api03-test")
    assert doc["status"] == "complete"
    return doc


def test_restore_dropped_item(dropped_run):
    rid = dropped_run["run_id"]
    c = TestClient(app)
    dropped = c.get(f"/runs/{rid}/trace/regions", params={"outcome": "dropped_by_model"}).json()["items"]
    assert dropped
    reg = dropped[0]
    n_flags = len(c.get(f"/runs/{rid}/flags").json())

    r = c.post(f"/runs/{rid}/trace/regions/{reg['region_id']}/restore", json={"note": "Looks like a size to me"})
    assert r.status_code == 201, r.text
    flag = r.json()
    assert flag["flag_id"] == "U-R1" and flag["origin"] == "restored_by_user"
    assert flag["disposition"] == "open" and flag["class"] == "unaccounted"
    assert reg["region_id"] in flag["region_ids"] and flag["restored_from"]["outcome"] == "dropped_by_model"

    flags = c.get(f"/runs/{rid}/flags").json()
    assert len(flags) == n_flags + 1 and flags[-1]["flag_id"] == "U-R1"
    # it behaves like any other flag: disposition, crop, export
    assert c.patch(f"/runs/{rid}/flags/U-R1", json={"disposition": "confirmed"}).json()["disposition"] == "confirmed"
    assert c.get(flag["crop_url"]).headers["content-type"] == "image/png"
    exp = list(csv.reader(io.StringIO(c.get(f"/runs/{rid}/exceptions.csv").text)))
    assert any(row[0] == "U-R1" and row[-1] == f"/runs/{rid}/trace/flags/U-R1" for row in exp[1:])

    # recorded in the trace; the machine's own outcome is left as it was
    t = store.get_trace(rid)
    assert t["user_actions"][-1]["flag_id"] == "U-R1" and t["user_actions"][-1]["note"] == "Looks like a size to me"
    again = next(x for x in t["regions"] if x["region_id"] == reg["region_id"])
    assert again["final"]["outcome"] == "dropped_by_model" and again["user"]["restored_flag_id"] == "U-R1"
    chain = c.get(f"/runs/{rid}/trace/flags/U-R1").json()
    assert chain["steps"][-1]["kind"] == "user" and any(s["kind"] == "ai" for s in chain["steps"])

    # idempotent: restoring again returns the same flag
    r2 = c.post(f"/runs/{rid}/trace/regions/{reg['region_id']}/restore")
    assert r2.status_code == 200 and r2.json()["flag_id"] == "U-R1"
    assert len(c.get(f"/runs/{rid}/flags").json()) == n_flags + 1


def test_restore_refuses_other_outcomes(dropped_run):
    rid = dropped_run["run_id"]
    c = TestClient(app)
    covered = c.get(f"/runs/{rid}/trace/regions", params={"outcome": "covered"}).json()["items"][0]
    assert c.post(f"/runs/{rid}/trace/regions/{covered['region_id']}/restore").status_code == 409
    assert c.post(f"/runs/{rid}/trace/regions/nope/restore").status_code == 404
    assert c.post("/runs/nope/trace/regions/s1-o1/restore").status_code == 404
