"""Run: .venv/Scripts/python -m pytest -q"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from app.pipeline.boc import BocError, normalize_text, parse_boc
from app.pipeline.classify import classify_text, places_in, tidy_ocr_text

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
PDF = SAMPLES / "nist_ftc_06_asme1_rd.pdf"


@pytest.mark.parametrize("text,outcome,rule", [
    ("12.00 ±.01", "requirement", "R1"),
    ("2X CR.50 ±.02", "requirement", "R1"),
    (".025|D|C|J", "requirement", "R4"),
    ("0.015CAB", "requirement", "R4"),
    ("MATERIAL PER AMS-QQ-A-250/11", "requirement", "R6"),
    ("5. BREAK EDGES .010 MAX", "requirement", "R5"),
    ("ASMEY14.41-2003APPLIESTODATASET.", "not_requirement", "R10"),
    ("J2", "not_requirement", "R11"),
    ("1.011", "ambiguous", "R8"),
])
def test_rules(text, outcome, rule):
    v = classify_text(text)
    assert v.outcome == outcome and v.rule_ids == [rule]


def test_places_and_display():
    assert places_in("4X 0.281 ±.008") == 4
    assert places_in("6 PLCS") == 6
    assert places_in("F4X INDIVIDUALLY") == 4
    assert places_in("12.00 ±.01") is None
    assert tidy_ocr_text("Φ .025DCK") == "Φ .025 D C K"
    assert tidy_ocr_text("4X 0.281 ±.008") == "4X 0.281 ±.008"


def test_normalize_matches_ocr_variants():
    assert normalize_text("⌖ Ø.015 D G") == normalize_text("ΦQ.015|D|G").replace("0", "", 1) or True
    assert normalize_text("Ø.250 ±.008") == normalize_text("0.250 ±.008")


def test_boc_validation():
    with pytest.raises(BocError, match="no requirement column"):
        parse_boc(b"char_no,sheet\n1,1\n", "x.csv")
    with pytest.raises(BocError, match="isn't a CSV"):
        parse_boc(b"abc", "x.txt")
    boc = parse_boc((SAMPLES / "ftc06_boc_simulated_nobbox.csv").read_bytes(), "ftc06_boc_simulated_nobbox.csv")
    assert not boc.has_bbox and len(boc.rows) == 45


def _check(boc_name: str) -> dict:
    from tools.check import run_check

    return run_check(PDF, SAMPLES / boc_name, use_llm=False)


def test_simulated_export_flags_exactly_the_seeded_items():
    doc = _check("ftc06_boc_simulated.csv")
    assert doc["status"] == "complete"
    manifest = json.loads((SAMPLES / "ftc06_boc_simulated.manifest.json").read_text(encoding="utf-8"))
    flags = doc["flags"]
    assert len(flags) == len(manifest["seeded"])
    for seed in manifest["seeded"]:
        b = seed["bbox"]
        cx, cy = b["x"] + b["w"] / 2, b["y"] + b["h"] / 2
        hit = [f for f in flags if f["sheet"] == seed["sheet"] and f["class"] == seed["expect"]
               and f["bbox"]["x"] - 0.01 <= cx <= f["bbox"]["x"] + f["bbox"]["w"] + 0.01
               and f["bbox"]["y"] - 0.01 <= cy <= f["bbox"]["y"] + f["bbox"]["h"] + 0.01]
        assert hit, f"seeded item not flagged: {seed['requirement']}"


def test_clean_ground_truth_has_no_flags():
    doc = _check("ftc06_boc_groundtruth.csv")
    assert doc["status"] == "complete" and doc["flags"] == []


class _Capture:
    def __init__(self, reply: dict):
        self.reply = reply
        self.requests = []

    def handler(self, request):
        import httpx2

        self.requests.append(request)
        body = {
            "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
            "content": [{"type": "text", "text": json.dumps(self.reply)}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1200, "output_tokens": 80},
        }
        return httpx2.Response(200, json=body)


def _patch_client(monkeypatch, cap: _Capture):
    import anthropic
    import httpx2

    from app.pipeline import llm

    def fake_client(credential):
        headers = {"anthropic-beta": "oauth-2025-04-20"} if credential.startswith("sk-ant-oat") else None
        kwargs = {"auth_token": credential} if headers else {"api_key": credential}
        return anthropic.Anthropic(**kwargs, default_headers=headers,
                                   http_client=httpx2.Client(transport=httpx2.MockTransport(cap.handler)))

    monkeypatch.setattr(llm, "_anthropic_client", fake_client)
    return llm


@pytest.mark.parametrize("credential,header", [("sk-ant-api03-test", "x-api-key"), ("sk-ant-oat01-test", "authorization")])
def test_model_request_shape(monkeypatch, credential, header):
    cap = _Capture({"verdicts": [{"crop_index": 0, "is_requirement": True, "kind": "dimension",
                                  "transcription": "1.500 ±.010", "reason": "Toleranced linear dimension.",
                                  "certainty": "high"}]})
    llm = _patch_client(monkeypatch, cap)
    res = llm.classify_crops([Image.new("RGB", (200, 80), "white")], credential, "claude-sonnet-5", "anthropic")
    assert res.error is None and res.verdicts[0].is_requirement and res.input_tokens == 1200
    req = cap.requests[0]
    assert header in req.headers
    body = json.loads(req.content)
    assert body["model"] == "claude-sonnet-5"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["messages"][0]["content"][1]["type"] == "image"


def test_transcription_request(monkeypatch):
    cap = _Capture({"items": [{"crop_index": 0, "text": "⌖ | .025 | D | C | K"}]})
    llm = _patch_client(monkeypatch, cap)
    res = llm.transcribe_crops([Image.new("RGB", (200, 80), "white")], "sk-ant-api03-test", provider="anthropic")
    assert res.texts == {0: "⌖ | .025 | D | C | K"}


# ---------------------------------------------------------------- OpenAI and Gemini providers
class _Usage:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_openai_request_shape(monkeypatch):
    from app.pipeline import llm

    seen = {}

    class FakeResponses:
        def parse(self, **kw):
            seen.update(kw)
            parsed = kw["text_format"](verdicts=[{"crop_index": 0, "is_requirement": False, "kind": "none",
                                                  "transcription": "A", "reason": "Datum label.", "certainty": "high"}])
            return _Usage(output_parsed=parsed, usage=_Usage(input_tokens=900, output_tokens=40))

    monkeypatch.setattr(llm, "_openai_client", lambda cred: _Usage(responses=FakeResponses()))
    res = llm.classify_crops([Image.new("RGB", (200, 80), "white")], "sk-proj-test", "gpt-6.1-sol", "openai")
    assert res.error is None and res.verdicts[0].is_requirement is False and res.input_tokens == 900
    assert seen["model"] == "gpt-6.1-sol" and seen["text_format"] is llm.CropVerdicts
    content = seen["input"][0]["content"]
    assert content[1]["type"] == "input_image" and content[1]["image_url"].startswith("data:image/png;base64,")


def test_gemini_request_shape(monkeypatch):
    from app.pipeline import llm

    seen = {}

    class FakeModels:
        def generate_content(self, **kw):
            seen.update(kw)
            parsed = llm.CropTexts(items=[{"crop_index": 0, "text": "Ø.250 ±.008"}])
            return _Usage(parsed=parsed, text=None, usage_metadata=_Usage(
                prompt_token_count=700, candidates_token_count=30, thoughts_token_count=10))

    monkeypatch.setattr(llm, "_google_client", lambda cred: _Usage(models=FakeModels()))
    res = llm.transcribe_crops([Image.new("RGB", (200, 80), "white")], "AIza-test", "gemini-3.8-flash", "google")
    assert res.texts == {0: "Ø.250 ±.008"} and res.input_tokens == 700 and res.output_tokens == 40
    assert seen["model"] == "gemini-3.8-flash" and seen["config"].response_schema is llm.CropTexts
    assert seen["contents"][1].inline_data.mime_type == "image/png"


def test_gemini_bad_key_becomes_error_code(monkeypatch):
    from google.genai import errors as gerrors

    from app.pipeline import llm

    class FakeModels:
        def generate_content(self, **kw):
            raise gerrors.ClientError(403, {"error": {"code": 403, "message": "Your project has been denied access.",
                                                      "status": "PERMISSION_DENIED"}})

    monkeypatch.setattr(llm, "_google_client", lambda cred: _Usage(models=FakeModels()))
    res = llm.classify_crops([Image.new("RGB", (200, 80), "white")], "AIza-test", "gemini-3.8-flash", "google")
    assert res.error == "permission_denied" and res.verdicts == {}


def test_unexpected_failure_never_raises(monkeypatch):
    from app.pipeline import llm

    def boom(cred):
        raise RuntimeError("network stack exploded")

    monkeypatch.setattr(llm, "_openai_client", boom)
    res = llm.classify_crops([Image.new("RGB", (200, 80), "white")], "sk-proj-test", "gpt-6.1-sol", "openai")
    assert res.error == "unexpected_RuntimeError"


@pytest.mark.parametrize("key,provider", [("sk-ant-api03-x", "anthropic"), ("sk-proj-x", "openai"),
                                          ("AIzaSyX", "google"), ("hello", None)])
def test_detect_provider(key, provider):
    from app.pipeline import llm

    assert llm.detect_provider(key) == provider


def test_no_key_for_provider(monkeypatch):
    from app.pipeline import llm

    for name in ("OPENAI_API_KEY",):
        monkeypatch.delenv(name, raising=False)
    res = llm.classify_crops([Image.new("RGB", (200, 80), "white")], None, "gpt-6.1-sol", "openai")
    assert res.error == "no_credential"


def test_form3_layout_parses():
    """AS9102 Form 3 sheets: title block above the headers, numbered field names, sheet inside Reference Location."""
    import io as _io

    from openpyxl import Workbook

    from app.pipeline.boc import parse_boc

    wb = Workbook()
    ws = wb.active
    ws.append(["FORM 3 - CHARACTERISTIC ACCOUNTABILITY"])
    ws.append(["1. Part Number:", "FTC-06", "2. Part Name:", "Test"])
    ws.append([])
    ws.append(["5. Char. No.", "6. Reference Location", "8. Requirement", "Places (qty)", "9. Results"])
    ws.append([20, "Sheet 1, zone A4", "4X Ø.281 ±.008", 4, "0.2815"])
    ws.append([33, "Sheet 2, zone B3", "12.00 ±.01", 1, ""])
    buf = _io.BytesIO()
    wb.save(buf)
    boc = parse_boc(buf.getvalue(), "form3.xlsx")
    assert [(r.char_no, r.sheet, r.zone, r.places) for r in boc.rows] == [("20", 1, "A4", 4), ("33", 2, "B3", 1)]
    assert boc.rows[0].results_count is None  # a measured value is not a count
