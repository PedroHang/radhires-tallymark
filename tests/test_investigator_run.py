"""The investigator inside a real run (mocked provider): links to existing rows, fallbacks, time budget.

Reproduces the case where coverage matching fails to tie a frame to the BoC row that states it: the
item is left over as ambiguous, the investigator finds the row with search_boc and says so in
covered_by, and run.py checks that claim before it changes anything.

Run: .venv/Scripts/python -m pytest -q tests/test_investigator_run.py
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from app import store
from app.pipeline import llm, run
from tests.test_trace import _assert_consistent, _check

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
TARGET = ".05/D/B/C 01D"  # an ambiguous leftover on sheet 2 of the sample when the BoC lacks its row
KEY = "sk-ant-api03-investigator-test"


def _boc(tmp_path: Path, requirement: str) -> Path:
    """One located row on sheet 1, plus a sheet-2 row that states the frame but sits in the wrong place,
    so coverage matching can't link it."""
    with (SAMPLES / "ftc06_boc_groundtruth.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    extra = dict(rows[1], char_no="50", sheet="2", zone="A8", char_type="gdt", requirement=requirement, places="1",
                 bbox_x="0.06", bbox_y="0.86", bbox_w="0.04", bbox_h="0.015")
    p = tmp_path / "boc_missing_link.csv"
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerow(rows[1])
        w.writerow(extra)
    return p


class _Claude:
    """Anthropic stand-in that answers the investigator, the single-shot check and transcription."""

    def __init__(self, claim: list[str] | None = None, status: int | None = None):
        self.claim = claim or []
        self.status = status
        self.requests: list[dict] = []
        self.keys: set[str] = set()

    def _reply(self, body, content, stop="tool_use"):
        import httpx2

        n = len(self.requests)
        return httpx2.Response(200, json={
            "id": f"msg_{n}", "type": "message", "role": "assistant", "model": body["model"], "content": content,
            "stop_reason": stop, "stop_sequence": None, "usage": {"input_tokens": 500, "output_tokens": 40}})

    def handler(self, request):
        import httpx2

        body = json.loads(request.content)
        self.requests.append(body)
        self.keys.add(request.headers.get("x-api-key"))
        if self.status:
            return httpx2.Response(self.status, json={"type": "error", "error": {"type": "authentication_error",
                                                                                 "message": "bad key"}})
        if "tools" in body:  # the investigator
            msgs = body["messages"]
            intro = msgs[0]["content"][1]["text"]
            searched = any(isinstance(m["content"], list) and any(b.get("type") == "tool_result" for b in m["content"])
                           for m in msgs if m["role"] == "user")
            if json.dumps(TARGET) in intro and not searched:
                return self._reply(body, [{"type": "tool_use", "id": "toolu_s", "name": "search_boc",
                                           "input": {"query": TARGET, "limit": 5}}])
            if json.dumps(TARGET) in intro:
                verdict = {"is_requirement": True, "kind": "gdt", "transcription": "⌓ .05 D B C",
                           "reason": "Profile frame; row 50 already states it.", "certainty": "high",
                           "evidence": ["crop", "S1"], "covered_by": self.claim}
            else:
                verdict = {"is_requirement": False, "kind": "none", "transcription": "x", "reason": "A label.",
                           "certainty": "high", "evidence": ["crop"], "covered_by": []}
            return self._reply(body, [{"type": "tool_use", "id": "toolu_v", "name": "submit_verdict", "input": verdict}])
        n = sum(1 for p in body["messages"][0]["content"] if p["type"] == "image")
        if "transcribe" in body["system"]:
            out = {"items": [{"crop_index": i, "text": f"T{i}"} for i in range(n)]}
        else:
            out = {"verdicts": [{"crop_index": i, "is_requirement": True, "kind": "dimension", "transcription": "c",
                                 "reason": "Single shot.", "certainty": "high"} for i in range(n)]}
        return self._reply(body, [{"type": "text", "text": json.dumps(out)}], "end_turn")


def _patch(monkeypatch, fake: _Claude):
    import anthropic
    import httpx2

    monkeypatch.setattr(llm, "_anthropic_client", lambda cred: anthropic.Anthropic(
        api_key=cred, max_retries=0, http_client=httpx2.Client(transport=httpx2.MockTransport(fake.handler))))


def _target_regions(t: dict) -> list[dict]:
    g = next(g for g in t["groups"] if g["text"] == TARGET)
    return [r for r in t["regions"] if r["region_id"] in g["region_ids"]]


def test_investigator_links_item_to_existing_row(monkeypatch, tmp_path):
    fake = _Claude(claim=["50"])
    _patch(monkeypatch, fake)
    doc = _check(_boc(tmp_path, "⌓ .05 D B C"), use_llm=True, credential=KEY, investigator=None)
    assert doc["status"] == "complete" and doc["investigator"] is True
    t = store.get_trace(doc["run_id"])
    _assert_consistent(doc, t)
    regs = _target_regions(t)
    assert regs and all(r["final"]["outcome"] == "covered_by_investigator" for r in regs)
    assert not any(set(f["region_ids"]) & {r["region_id"] for r in regs} for f in doc["flags"])
    assert doc["stats"]["linked_by_ai"] == 1 and doc["stats"]["investigator"]["verdicts"] >= 1
    model = regs[0]["model"]
    assert model["source"] == "investigator" and model["link"]["ok"] and model["link"]["char_nos"] == ["50"]
    check = model["link"]["checks"][0]
    assert check["numbers_compatible"] and check["text_score"] >= run.LINK_MIN
    assert [s["tool"] for s in model["steps"]] == ["search_boc", "submit_verdict"]
    # the investigation is one call record with its steps, transcript and images
    call = next(c for c in t["llm_calls"] if c["call_id"] == model["call_id"])
    assert call["job"] == "investigate" and call["evidence"] == ["crop", "S1"] and call["raw_messages"]
    assert KEY not in json.dumps(t)
    assert store.get_trace_artifact(doc["run_id"], f"llm/{call['call_id']}/0.png")[:4] == b"\x89PNG"
    f = {s["key"]: s["count"] for s in t["funnel"]}
    assert f["covered_by_investigator"] == len(regs)
    assert t["ai_links"][0]["char_nos"] == ["50"]
    # the coverage overlay shows it as linked by AI so the UI can mark it "check"
    overlay = json.loads((store.RUNS / doc["run_id"] / "coverage.json").read_text(encoding="utf-8"))["2"]
    assert overlay["linked"][0]["char_nos"] == ["50"] and overlay["linked"][0]["marker"] == "linked_by_ai"
    assert KEY in fake.keys and len(fake.requests) == doc["stats"]["llm_calls"]


def test_invalid_link_keeps_flag_with_explanation(monkeypatch, tmp_path):
    fake = _Claude(claim=["50"])
    _patch(monkeypatch, fake)
    doc = _check(_boc(tmp_path, "⌓ .07 D B C"), use_llm=True, credential=KEY, investigator=None)
    t = store.get_trace(doc["run_id"])
    _assert_consistent(doc, t)
    regs = _target_regions(t)
    assert all(r["final"]["outcome"] == "flagged_unaccounted" for r in regs)
    link = regs[0]["model"]["link"]
    assert not link["ok"] and link["checks"][0]["why"] == "values differ"
    flag = next(fl for fl in doc["flags"] if set(fl["region_ids"]) & {r["region_id"] for r in regs})
    assert flag["reason"].startswith("Model thinks CN 50 covers this; text didn't match")
    steps = t["flags"][flag["flag_id"]]["steps"]
    titles = [s["title"] for s in steps]
    assert "AI investigator S1: Searched the BoC" in titles
    assert any(s["kind"] == "ai" and s["title"].startswith("AI check: it is a requirement") for s in steps)
    assert steps[-1]["data"]["band"] == flag["confidence"]


def test_harness_rejects_claim_no_tool_returned(monkeypatch, tmp_path):
    """covered_by must name a row a cited step returned; otherwise one repair turn, then fallback."""
    fake = _Claude(claim=["999"])
    _patch(monkeypatch, fake)
    doc = _check(_boc(tmp_path, "⌓ .05 D B C"), use_llm=True, credential=KEY, investigator=None)
    t = store.get_trace(doc["run_id"])
    regs = _target_regions(t)
    model = regs[0]["model"]
    assert model["source"] == "single_shot" and model["investigation"]["error"] == "no_verdict"
    assert "link" not in model and regs[0]["final"]["outcome"] == "flagged_unaccounted"


def test_investigator_off_is_the_single_shot_path(monkeypatch, tmp_path):
    fake = _Claude(claim=["50"])
    _patch(monkeypatch, fake)
    doc = _check(_boc(tmp_path, "⌓ .05 D B C"), use_llm=True, credential=KEY, investigator=False)
    assert doc["investigator"] is False and "investigator" not in doc["stats"]
    assert not any("tools" in b for b in fake.requests)
    t = store.get_trace(doc["run_id"])
    assert {c["job"] for c in t["llm_calls"]} == {"classify", "transcribe"}


def test_fatal_error_stops_investigating_and_skips_single_shot(monkeypatch, tmp_path):
    fake = _Claude(status=401)
    _patch(monkeypatch, fake)
    doc = _check(_boc(tmp_path, "⌓ .05 D B C"), use_llm=True, credential=KEY, investigator=None)
    assert doc["status"] == "complete" and doc["stats"]["llm_error"] == "auth_failed"
    t = store.get_trace(doc["run_id"])
    _assert_consistent(doc, t)
    assert not [c for c in t["llm_calls"] if c["job"] == "classify"]


def test_time_budget_falls_back_to_single_shot(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "INVESTIGATOR_TOTAL_S", 0.0)
    fake = _Claude(claim=["50"])
    _patch(monkeypatch, fake)
    doc = _check(_boc(tmp_path, "⌓ .05 D B C"), use_llm=True, credential=KEY, investigator=None)
    info = doc["stats"]["investigator"]
    assert info["investigated"] == 0 and info["skipped_over_budget"] == info["items"] > 0
    t = store.get_trace(doc["run_id"])
    assert {c["job"] for c in t["llm_calls"]} == {"classify", "transcribe"}


def test_no_key_never_starts_investigator(monkeypatch, tmp_path):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    opts = run.RunOptions(use_llm=True, credential=None, provider="anthropic")
    assert run._investigator_enabled(opts) is False
    assert run._investigator_enabled(run.RunOptions(use_llm=True, credential=KEY, provider="anthropic")) is True
    assert run._investigator_enabled(run.RunOptions(use_llm=False, credential=KEY)) is False
