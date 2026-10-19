"""Decision trace: recording never changes outcomes, and the record is complete and consistent.

Run: .venv/Scripts/python -m pytest -q tests/test_trace.py
"""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

import pytest
from PIL import Image

from app import store
from app.pipeline import classify, llm, run, trace

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
PDF = SAMPLES / "nist_ftc_06_asme1_rd.pdf"


def _check(boc: Path, use_llm: bool = False, credential: str | None = None, provider: str = "anthropic",
           model: str = "claude-sonnet-5") -> dict:
    from tools.check import run_check

    if credential is None:
        return run_check(PDF, boc, use_llm=use_llm)
    import datetime as dt
    import uuid

    d, b = PDF.read_bytes(), boc.read_bytes()
    opts = run.RunOptions(use_llm=use_llm, credential=credential, provider=provider, model=model)
    run_id = f"test_{uuid.uuid4().hex[:8]}"
    doc = run.new_run_doc(run_id, dt.datetime.now(dt.timezone.utc).isoformat(),
                          {"filename": PDF.name, "sha256": store.sha256(d)},
                          {"filename": boc.name, "sha256": store.sha256(b)}, opts)
    store.put_run(doc)
    run.execute(run_id, d, PDF.name, b, boc.name, opts)
    return store.get_run(run_id)


@pytest.fixture(scope="module")
def sample():
    doc = _check(SAMPLES / "ftc06_boc_simulated.csv")
    return doc, store.get_trace(doc["run_id"])


@pytest.fixture(scope="module")
def one_row_boc(tmp_path_factory):
    """One located row: most of the drawing is left over, including ambiguous bare numbers."""
    with (SAMPLES / "ftc06_boc_groundtruth.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    p = tmp_path_factory.mktemp("boc") / "one_row.csv"
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerow(rows[1])
    return p


def _assert_consistent(doc: dict, t: dict) -> None:
    f = {s["key"]: s for s in t["funnel"]}
    assert all(t["invariants"].values()), t["invariants"]
    assert f["read"]["count"] == f["covered"]["count"] + f["excluded"]["count"] + f["leftover"]["count"]
    assert f["leftover"]["count"] == (f["not_requirement"]["count"] + f["requirement_by_rule"]["count"]
                                      + f["ambiguous"]["count"])
    # every region has exactly one final outcome, and the outcome partition covers all regions
    ids = [r["region_id"] for r in t["regions"]]
    assert len(ids) == len(set(ids)) == f["read"]["count"] == doc["stats"]["all_regions"]
    assert sum(o["count"] for o in t["outcomes"].values()) == len(ids)
    seen = [rid for o in t["outcomes"].values() for rid in o["region_ids"]]
    assert sorted(seen) == sorted(ids)
    for r in t["regions"]:
        assert r["final"]["outcome"] in trace.CATALOG["outcomes"]
    # leftovers are grouped exactly once
    grouped = [rid for g in t["groups"] for rid in g["region_ids"]]
    assert sorted(grouped) == sorted(f["leftover"]["region_ids"])
    # funnel agrees with the run's own stats
    assert f["covered"]["count"] == doc["stats"]["covered"]
    assert f["excluded"]["count"] == doc["stats"]["excluded"]
    assert len(f["flagged_unaccounted"]["flag_ids"]) == doc["stats"]["unaccounted"]
    assert len(f["flagged_places"]["flag_ids"]) == doc["stats"]["places"]
    assert set(t["flags"]) == {fl["flag_id"] for fl in doc["flags"]}
    # each flag's chain ends in the score step, in the same band as the flag
    for fl in doc["flags"]:
        rec = t["flags"][fl["flag_id"]]
        steps = rec["steps"]
        assert [s["n"] for s in steps] == list(range(1, len(steps) + 1))
        assert {s["kind"] for s in steps} <= set(trace.CATALOG["step_kinds"])
        assert steps[0]["kind"] == "measured" and steps[-1]["kind"] == "score"
        assert steps[-1]["data"]["band"] == fl["confidence"]
        assert abs(steps[-1]["data"]["product"] - fl["score"]) < 1e-3
        for rid in fl["region_ids"]:
            reg = next(r for r in t["regions"] if r["region_id"] == rid)
            assert fl["flag_id"] in reg["final"]["flag_ids"]


def test_sample_trace_is_consistent(sample):
    doc, t = sample
    assert doc["trace"]["available"] and t is not None
    _assert_consistent(doc, t)
    assert t["run"]["settings"]["use_llm"] is False and "credential" not in json.dumps(t["run"]["settings"]).replace(
        "credential_source", "")
    assert t["run"]["inputs"]["drawing"]["sha256"] == store.sha256(PDF.read_bytes())
    assert [s["key"] for s in t["stages"]] == ["parse_boc", "render", "ocr", "match", "exclusions", "rules",
                                                "model_check", "places", "scoring", "transcription"]
    assert len(t["stages"][2]["sheets"]) == 3
    # covered regions name the row that covered them, with the measured values behind it
    covered = [r for r in t["regions"] if r["coverage"]["covered"]]
    assert covered and all(any(c["passed"] and c["char_no"] in r["coverage"]["covered_by"]
                               for c in r["coverage"]["candidates"]) for r in covered)
    excluded = [r for r in t["regions"] if r["final"]["outcome"] == "excluded"]
    assert excluded and all(r["exclusion"]["label"] for r in excluded)
    assert len(json.dumps(t)) < 2_000_000  # reasonably sized for a 3-sheet drawing


def test_trace_does_not_change_outcomes(sample):
    """The trace is recorded on every run; the flags must be the ones the seeded manifest expects."""
    doc, _ = sample
    manifest = json.loads((SAMPLES / "ftc06_boc_simulated.manifest.json").read_text(encoding="utf-8"))
    assert len(doc["flags"]) == len(manifest["seeded"])


def test_flag_threshold_mirrors_run():
    assert trace.FLAG_THRESHOLD == run.FLAG_THRESHOLD


def test_rule_explanation_matches_classifier(sample):
    _, t = sample
    texts = {g["text"]: g["fcf_cells"] for g in t["groups"]}
    texts.update({r["text"]: r["fcf_cells"] for r in t["regions"]})
    texts.update({x: 0 for x in ["12.00 ±.01", "2X CR.50 ±.02", ".025|D|C|J", "0.015CAB", "MATERIAL PER AMS-QQ-A-250/11",
                                 "5. BREAK EDGES .010 MAX", "J2", "1.011", "1.248-1.252", "63 Ra", "4X Ø.281", "", "A"]})
    texts.update({"": 3, ".05": 4, "ABC": 3})
    for text, cells in texts.items():
        v = classify.classify_text(text, cells)
        checks = trace.explain_rules(text, cells)
        fired = [c for c in checks if c["status"] == "fired"]
        assert len(fired) == 1 and fired[0]["rule_id"] == v.rule_ids[0], (text, cells, fired, v)


def test_catalog_covers_every_rule_id():
    src = (ROOT / "app" / "pipeline" / "classify.py").read_text(encoding="utf-8")
    emitted = set(re.findall(r'\["(R\d+)"\]', src))
    run_src = (ROOT / "app" / "pipeline" / "run.py").read_text(encoding="utf-8")
    emitted |= set(re.findall(r'\["(P\d+)"\]', run_src))
    assert emitted >= {"R1", "R4", "R8", "R10", "R11", "P1"}
    assert emitted <= set(trace.CATALOG["rules"])
    assert {f"R{i}" for i in range(1, 12)} <= set(trace.CATALOG["rules"])
    for key in list(trace.CATALOG["exclusions"]):
        assert trace.CATALOG["exclusions"][key]["label"] and trace.CATALOG["exclusions"][key]["explain"]
    from app.pipeline.exclusions import EXCLUSION_LABELS

    assert set(EXCLUSION_LABELS) | {"custom_regex"} == set(trace.CATALOG["exclusions"])
    assert set(trace.CATALOG["bands"]) == {"high", "medium", "low", "none"}
    for section in ("rules", "rule_checks", "outcomes", "bands", "sources", "coverage_tests", "stages"):
        for k, v in trace.CATALOG[section].items():
            assert v["label"] and v["explain"], (section, k)


# ---------------------------------------------------------------- model calls: raw text captured verbatim
class _Capture:
    """Anthropic Messages API stand-in. Answers each request from `reply(body)`."""

    def __init__(self, reply):
        self.reply = reply
        self.requests = []
        self.raw = []

    def handler(self, request):
        import httpx2

        body = json.loads(request.content)
        self.requests.append(body)
        text = json.dumps(self.reply(body))
        self.raw.append(text)
        return httpx2.Response(200, json={
            "id": f"msg_{len(self.requests)}", "type": "message", "role": "assistant", "model": body["model"],
            "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 50}})


def _patch_anthropic(monkeypatch, cap: _Capture):
    import anthropic
    import httpx2

    monkeypatch.setattr(llm, "_anthropic_client", lambda cred: anthropic.Anthropic(
        api_key=cred, http_client=httpx2.Client(transport=httpx2.MockTransport(cap.handler))))


def _n_crops(body) -> int:
    return sum(1 for part in body["messages"][0]["content"] if part["type"] == "image")


def test_anthropic_call_record_has_raw_text(monkeypatch):
    cap = _Capture(lambda body: {"verdicts": [{"crop_index": 0, "is_requirement": True, "kind": "dimension",
                                               "transcription": "1.011", "reason": "Basic size.", "certainty": "high"}]})
    _patch_anthropic(monkeypatch, cap)
    calls: list = []
    res = llm.classify_crops([Image.new("RGB", (200, 80), "white")], "sk-ant-api03-test", "claude-sonnet-5",
                             "anthropic", trace_calls=calls)
    assert res.error is None and len(calls) == 1
    c = calls[0]
    assert c["raw_response_text"] == cap.raw[0]
    assert c["parsed"]["verdicts"][0]["transcription"] == "1.011"
    assert c["system_prompt"] == llm.SYSTEM and c["instruction"].startswith("Return exactly one verdict")
    assert c["input_tokens"] == 1000 and c["output_tokens"] == 50 and c["latency_ms"] is not None
    assert c["response_id"] == "msg_1" and c["stop_reason"] == "end_turn" and c["error"] is None
    assert c["user_content"][0] == {"type": "text", "text": "Crop 0:"}
    assert "sk-ant" not in json.dumps(c)


def test_openai_call_record_has_raw_text(monkeypatch):
    raw = '{"verdicts":[{"crop_index":0,"is_requirement":false,"kind":"none","transcription":"A","reason":"Datum.","certainty":"high"}]}'

    class Obj:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class FakeResponses:
        def parse(self, **kw):
            return Obj(id="resp_1", output_text=raw, output_parsed=kw["text_format"].model_validate_json(raw),
                       usage=Obj(input_tokens=900, output_tokens=40))

    monkeypatch.setattr(llm, "_openai_client", lambda cred: Obj(responses=FakeResponses()))
    calls: list = []
    res = llm.classify_crops([Image.new("RGB", (200, 80), "white")], "sk-proj-test", "gpt-6.1-sol", "openai",
                             trace_calls=calls)
    assert res.verdicts[0].is_requirement is False
    assert calls[0]["raw_response_text"] == raw and calls[0]["response_id"] == "resp_1"
    assert calls[0]["request"]["reasoning_effort"] == "low"


def test_gemini_call_record_has_raw_text(monkeypatch):
    raw = '{"items": [{"crop_index": 0, "text": "Ø.250 ±.008"}]}'

    class Obj:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class FakeModels:
        def generate_content(self, **kw):
            return Obj(parsed=None, text=raw, response_id="g-1", usage_metadata=Obj(
                prompt_token_count=700, candidates_token_count=30, thoughts_token_count=10))

    monkeypatch.setattr(llm, "_google_client", lambda cred: Obj(models=FakeModels()))
    calls: list = []
    res = llm.transcribe_crops([Image.new("RGB", (200, 80), "white")], "AIza-test", "gemini-3.8-flash", "google",
                               trace_calls=calls)
    assert res.texts == {0: "Ø.250 ±.008"}
    assert calls[0]["raw_response_text"] == raw and calls[0]["job"] == "transcribe"
    assert calls[0]["output_tokens"] == 40 and calls[0]["parsed"] == {"items": [{"crop_index": 0, "text": "Ø.250 ±.008"}]}


def test_failed_call_records_error_code(monkeypatch):
    from google.genai import errors as gerrors

    class Obj:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class FakeModels:
        def generate_content(self, **kw):
            raise gerrors.ClientError(403, {"error": {"code": 403, "message": "denied", "status": "PERMISSION_DENIED"}})

    monkeypatch.setattr(llm, "_google_client", lambda cred: Obj(models=FakeModels()))
    calls: list = []
    res = llm.classify_crops([Image.new("RGB", (200, 80), "white")], "AIza-test", "gemini-3.8-flash", "google",
                             trace_calls=calls)
    assert res.error == "permission_denied"
    assert calls[0]["error"] == "permission_denied" and calls[0]["raw_response_text"] is None
    assert calls[0]["latency_ms"] is not None and "ClientError" in calls[0]["error_detail"]

    def boom(cred):
        raise RuntimeError("network stack exploded")

    monkeypatch.setattr(llm, "_openai_client", boom)
    calls = []
    llm.classify_crops([Image.new("RGB", (200, 80), "white")], "sk-proj-test", "gpt-6.1-sol", "openai", trace_calls=calls)
    assert calls[0]["error"] == "unexpected_RuntimeError"


def test_structured_call_unchanged_without_meta(monkeypatch):
    cap = _Capture(lambda body: {"items": [{"crop_index": 0, "text": "X"}]})
    _patch_anthropic(monkeypatch, cap)
    out = llm.structured_call("anthropic", "sk-ant-api03-test", "claude-sonnet-5", "s", [Image.new("RGB", (8, 8))],
                              "i", llm.CropTexts, 100)
    assert isinstance(out, tuple) and len(out) == 3 and out[1:] == (1000, 50)


# ---------------------------------------------------------------- whole run with the model check on (mocked)
def _verdict_reply(body):
    if "transcribe" in body["system"]:
        return {"items": [{"crop_index": i, "text": f"T{i}"} for i in range(_n_crops(body))]}
    # even crops are requirements (high), odd crops are not
    return {"verdicts": [{"crop_index": i, "is_requirement": i % 2 == 0, "kind": "dimension" if i % 2 == 0 else "none",
                          "transcription": f"c{i}", "reason": "Test verdict.", "certainty": "high"}
                         for i in range(_n_crops(body))]}


def test_run_with_model_records_every_call(monkeypatch, one_row_boc):
    cap = _Capture(_verdict_reply)
    _patch_anthropic(monkeypatch, cap)
    doc = _check(one_row_boc, use_llm=True, credential="sk-ant-api03-test")
    assert doc["status"] == "complete"
    t = store.get_trace(doc["run_id"])
    _assert_consistent(doc, t)
    jobs = [c["job"] for c in t["llm_calls"]]
    assert jobs.count("classify") >= 1 and jobs.count("transcribe") >= 1
    assert len(t["llm_calls"]) == len(cap.requests) == doc["stats"]["llm_calls"]
    for c, raw in zip(t["llm_calls"], cap.raw):
        assert c["raw_response_text"] == raw
        for crop in c["crops"]:
            png = store.get_trace_artifact(doc["run_id"], f"llm/{c['call_id']}/{crop['index']}.png")
            assert png and png[:4] == b"\x89PNG"
    f = {s["key"]: s for s in t["funnel"]}
    assert f["ambiguous"]["count"] > 0 and f["sent_to_model"]["count"] == f["ambiguous"]["count"]
    dropped_groups = {r["group_id"] for r in t["regions"] if r["final"]["outcome"] == "dropped_by_model"}
    assert len(dropped_groups) == doc["stats"]["model_rejected"] > 0
    ai_flags = [fl for fl in doc["flags"] if fl["rule_ids"] == ["R8"]]
    assert ai_flags
    for fl in ai_flags:
        steps = t["flags"][fl["flag_id"]]["steps"]
        ai = [s for s in steps if s["kind"] == "ai"]
        assert ai[0]["data"]["status"] == "verdict" and ai[0]["data"]["call_id"].startswith("L")
        assert ai[-1]["title"].startswith("AI transcription")


def test_run_with_failing_model_records_error(monkeypatch, one_row_boc):
    import httpx2

    def handler(request):
        return httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "bad"}})

    import anthropic

    monkeypatch.setattr(llm, "_anthropic_client", lambda cred: anthropic.Anthropic(
        api_key=cred, max_retries=0, http_client=httpx2.Client(transport=httpx2.MockTransport(handler))))
    doc = _check(one_row_boc, use_llm=True, credential="sk-ant-api03-bad")
    t = store.get_trace(doc["run_id"])
    _assert_consistent(doc, t)
    assert t["llm_calls"][0]["error"] == "auth_failed"
    models = [r["model"] for r in t["regions"] if r["model"]]
    assert models and all(m["status"] in ("no_verdict", "not_sent") and m["error"] == "auth_failed" for m in models)
    assert t["llm"]["classify_error"] == "auth_failed"


# ---------------------------------------------------------------- API
def test_trace_api(sample):
    from fastapi.testclient import TestClient

    from app.main import app

    doc, t = sample
    rid = doc["run_id"]
    c = TestClient(app)
    s = c.get(f"/runs/{rid}/trace").json()
    assert s["run"]["run_id"] == rid and s["funnel"][0]["key"] == "read" and "regions" not in s
    regs = c.get(f"/runs/{rid}/trace/regions", params={"outcome": "flagged_unaccounted"}).json()
    assert regs["total"] == len(t["outcomes"]["flagged_unaccounted"]["region_ids"]) > 0
    one = c.get(f"/runs/{rid}/trace/regions/{regs['items'][0]['region_id']}").json()
    assert one["final"]["outcome"] == "flagged_unaccounted"
    assert c.get(f"/runs/{rid}/trace/regions", params={"sheet": 2, "limit": 5}).json()["limit"] == 5
    fid = doc["flags"][0]["flag_id"]
    chain = c.get(f"/runs/{rid}/trace/flags/{fid}").json()
    assert chain["steps"][-1]["kind"] == "score"
    cat = c.get(f"/runs/{rid}/trace/catalog").json()
    assert "R4" in cat["rules"] and cat["version"] == trace.CATALOG_VERSION
    audit = c.get(f"/runs/{rid}/audit.json")
    assert "attachment" in audit.headers["content-disposition"] and audit.json()["regions"]
    assert c.get(f"/runs/{rid}/trace/llm/L9").status_code == 404
    assert c.get(f"/runs/{rid}/trace/llm/L1/crops/0.png").status_code == 404
    exp = list(csv.reader(io.StringIO(c.get(f"/runs/{rid}/exceptions.csv").text)))
    assert exp[0][-1] == "audit_ref" and exp[1][-1] == f"/runs/{rid}/trace/flags/{exp[1][0]}"
    assert c.get("/runs/nope/trace").status_code == 404
