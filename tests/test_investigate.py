"""Bounded investigator: tools on a synthetic sheet, and the tool loop on all three providers.

Everything is mocked: Anthropic and OpenAI run their real SDK clients against an httpx2
MockTransport (so the request bodies are the real wire shapes); Gemini runs a fake client
that returns real google-genai response types. No network, no key.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from app.pipeline import investigate as inv
from app.pipeline import llm
from app.pipeline.boc import Characteristic
from app.pipeline.geometry import Box, box_poly

PAGE_MM = (279.4, 215.9)  # letter landscape
ITEM = Box(0.50, 0.50, 0.04, 0.02)
CRED = "sk-ant-api03-SECRET-test-key"


def _rows() -> list[Characteristic]:
    return [
        Characteristic("12", 1, "1.250 ±.005", bbox=Box(0.55, 0.50, 0.05, 0.02), bbox_source="export"),
        Characteristic("13", 1, "Ø.281 ±.008", bbox=Box(0.50, 0.40, 0.04, 0.02), bbox_source="text_match"),
        Characteristic("14", 1, "2.500", bbox=Box(0.10, 0.10, 0.04, 0.02)),
        Characteristic("15", 1, "6.000"),  # no location
        Characteristic("16", 2, "1.250", bbox=Box(0.52, 0.50, 0.04, 0.02)),  # other sheet
    ]


def _regions() -> list[inv.OcrRegion]:
    return [
        inv.OcrRegion("R1", 1, "1.250", ITEM),  # the item itself
        inv.OcrRegion("R2", 1, "(REF)", Box(0.545, 0.50, 0.02, 0.02)),
        inv.OcrRegion("R3", 1, "A", Box(0.50, 0.53, 0.02, 0.02)),
        inv.OcrRegion("R4", 1, "", Box(0.51, 0.47, 0.02, 0.02)),  # blank frame cell
        inv.OcrRegion("R5", 2, "NEAR BUT OTHER SHEET", Box(0.50, 0.50, 0.04, 0.02)),
        inv.OcrRegion("R6", 1, "1.25O", Box(0.501, 0.501, 0.038, 0.018)),  # duplicate read of the item
        inv.OcrRegion("R7", 1, "FAR AWAY", Box(0.05, 0.9, 0.05, 0.02)),
    ]


def _ctx(**kw) -> inv.InvestigationContext:
    img = np.full((850, 1100, 3), 255, np.uint8)
    img[425:442, 552:592] = 0  # some ink inside the item box
    base = dict(item_id="G7", sheet=1, bbox=ITEM, poly=box_poly(ITEM), ocr_text="1.250",
                rule_reason="A number with no tolerance and no frame.", sheet_image=img, page_size_mm=PAGE_MM,
                rows=_rows(), regions=_regions(), region_ids=["R1"], zone="B4")
    base.update(kw)
    return inv.InvestigationContext(**base)


# ---------------------------------------------------------------------------------------- tools
def test_nearby_rows_distances_directions_and_unlocated():
    out = inv.tool_nearby_rows(_ctx(), 20)
    assert [(r["char_no"], r["distance_mm"], r["direction"]) for r in out["rows"]] == [
        ("12", 2.8, "right"), ("13", 17.3, "above")]
    assert out["rows"][1]["located_by"] == "text_match"
    assert out["rows_without_location"] == 1 and "search_boc" in out["note"]
    assert [r["char_no"] for r in inv.tool_nearby_rows(_ctx(), 5)["rows"]] == ["12"]
    assert inv.tool_nearby_rows(_ctx(), 500)["radius_mm"] == 40.0  # capped
    assert inv.tool_nearby_rows(_ctx(), "junk")["radius_mm"] == 15.0


def test_ocr_context_excludes_self_blank_other_sheets():
    out = inv.tool_ocr_context(_ctx(), 10)
    assert [(r["region_id"], r["text"], r["distance_mm"], r["direction"]) for r in out["regions"]] == [
        ("R2", "(REF)", 1.4, "right"), ("R3", "A", 2.2, "below")]


def test_direction_diagonal_and_overlap():
    a = Box(0.5, 0.5, 0.02, 0.02)
    assert inv.direction(a, Box(0.6, 0.4, 0.02, 0.02), PAGE_MM) == "above-right"
    assert inv.direction(a, Box(0.3, 0.5, 0.02, 0.02), PAGE_MM) == "left"
    assert inv.direction(a, Box(0.51, 0.51, 0.02, 0.02), PAGE_MM) == "overlapping"


def test_page_size_falls_back_to_image_aspect():
    assert inv.page_mm(_ctx(page_size_mm=None)) == pytest.approx((279.4, 279.4 * 850 / 1100))


def test_search_boc_ranking_and_limit():
    out = inv.tool_search_boc(_ctx(), "1.250", 5)
    assert [r["char_no"] for r in out["rows"][:2]] == ["16", "12"]
    scores = [r["score"] for r in out["rows"]]
    assert scores == sorted(scores, reverse=True) and scores[0] == 100.0
    assert out["rows"][0]["sheet"] == 2 and out["rows"][1]["located"] is True
    assert len(inv.tool_search_boc(_ctx(), "1.250", 1)["rows"]) == 1
    assert len(inv.tool_search_boc(_ctx(), "1.250", 99)["rows"]) <= 5
    assert inv.tool_search_boc(_ctx(), "Ø.281", 5)["rows"][0]["char_no"] == "13"


def test_zoom_returns_larger_outlined_crop():
    data, im = inv.tool_zoom(_ctx(), 3, "small")
    assert (data["factor"], data["pad"]) == (3, "small")
    assert im.width == 68 * 3 and im.height > im.width / 3
    arr = np.asarray(im)
    assert ((arr[:, :, 0] > 200) & (arr[:, :, 1] < 60)).any()  # red outline drawn
    big, im2 = inv.tool_zoom(_ctx(), 4, "large")
    assert max(im2.size) <= inv.ZOOM_MAX_PX
    assert inv.tool_zoom(_ctx(), 9, "huge")[0] == {**inv.tool_zoom(_ctx(), 4, "medium")[0]}


def test_tools_are_read_only():
    ctx = _ctx()
    before = ([r.to_dict() for r in ctx.rows], [(r.region_id, r.text, r.bbox) for r in ctx.regions], ctx.sheet_image.copy())
    for name, args in [("zoom", {"factor": 2, "pad": "large"}), ("nearby_rows", {"radius_mm": 40}),
                       ("ocr_context", {"radius_mm": 40}), ("search_boc", {"query": "1.250", "limit": 5})]:
        inv.run_tool(ctx, name, args)
    assert [r.to_dict() for r in ctx.rows] == before[0]
    assert [(r.region_id, r.text, r.bbox) for r in ctx.regions] == before[1]
    assert np.array_equal(ctx.sheet_image, before[2])


# ---------------------------------------------------------------------------------- fake models
VERDICT = {"is_requirement": False, "kind": "none", "transcription": "1.250",
           "reason": "Same value as CN 12 next to it, marked (REF).", "certainty": "high", "evidence": ["S1", "S2"]}
SCRIPT = [("nearby_rows", {"radius_mm": 20}), ("ocr_context", {"radius_mm": 10}), ("submit_verdict", VERDICT)]


class _Anthropic:
    """Scripted Claude behind the real anthropic SDK (httpx2 MockTransport)."""

    def __init__(self, script, status: int | None = None, error_body: dict | None = None, raise_exc=None):
        self.script, self.status, self.error_body, self.raise_exc = list(script), status, error_body, raise_exc
        self.bodies: list[dict] = []
        self.headers = []

    def handler(self, request):
        import httpx2

        if self.raise_exc:
            raise self.raise_exc
        body = json.loads(request.content)
        self.bodies.append(body)
        self.headers.append(request.headers)
        if self.status:
            return httpx2.Response(self.status, json=self.error_body or {
                "type": "error", "error": {"type": "api_error", "message": "boom"}})
        name, args = self.script.pop(0)
        n = len(self.bodies)
        content = [{"type": "tool_use", "id": f"toolu_{n}", "name": name, "input": args}] if name else \
            [{"type": "text", "text": args}]
        return httpx2.Response(200, json={
            "id": f"msg_{n}", "type": "message", "role": "assistant", "model": body["model"], "content": content,
            "stop_reason": "tool_use" if name else "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1000 * n, "output_tokens": 50}})


def _patch_anthropic(monkeypatch, fake):
    import anthropic
    import httpx2

    monkeypatch.setattr(llm, "_anthropic_client", lambda cred: anthropic.Anthropic(
        api_key=cred, max_retries=0, http_client=httpx2.Client(transport=httpx2.MockTransport(fake.handler))))


class _OpenAI:
    def __init__(self, script, status: int | None = None):
        self.script, self.status = list(script), status
        self.bodies: list[dict] = []

    def handler(self, request):
        import httpx2

        body = json.loads(request.content)
        self.bodies.append(body)
        if self.status:
            return httpx2.Response(self.status, json={"error": {"message": "nope", "type": "x", "code": None}})
        name, args = self.script.pop(0)
        n = len(self.bodies)
        output = [{"type": "reasoning", "id": f"rs_{n}", "summary": []}]
        if name:
            output.append({"type": "function_call", "id": f"fc_{n}", "call_id": f"call_{n}", "name": name,
                           "arguments": args if isinstance(args, str) else json.dumps(args), "status": "completed"})
        else:
            output.append({"type": "message", "id": f"msg_{n}", "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": args, "annotations": []}]})
        return httpx2.Response(200, json={
            "id": f"resp_{n}", "object": "response", "created_at": 1, "model": body["model"], "status": "completed",
            "output": output, "parallel_tool_calls": True, "tool_choice": body.get("tool_choice", "auto"),
            "tools": [], "usage": {"input_tokens": 900 * n, "output_tokens": 40, "total_tokens": 900 * n + 40,
                                   "input_tokens_details": {"cached_tokens": 0},
                                   "output_tokens_details": {"reasoning_tokens": 0}}})


def _patch_openai(monkeypatch, fake):
    import httpx2
    import openai

    monkeypatch.setattr(llm, "_openai_client", lambda cred: openai.OpenAI(
        api_key=cred, max_retries=0, http_client=httpx2.Client(transport=httpx2.MockTransport(fake.handler))))


class _Gemini:
    def __init__(self, script, exc=None):
        self.script, self.exc = list(script), exc
        self.calls: list[dict] = []

    def generate_content(self, **kw):
        from google.genai import types

        self.calls.append({"model": kw["model"], "config": kw["config"], "n_contents": len(kw["contents"]),
                           "contents": list(kw["contents"])})
        if self.exc:
            raise self.exc
        name, args = self.script.pop(0)
        n = len(self.calls)
        part = {"function_call": {"id": f"fc{n}", "name": name, "args": args}, "thought_signature": b"sig" * 50} \
            if name else {"text": args}
        return types.GenerateContentResponse.model_validate({
            "candidates": [{"content": {"role": "model", "parts": [part]}, "finish_reason": "STOP"}],
            "usage_metadata": {"prompt_token_count": 700 * n, "candidates_token_count": 30,
                               "thoughts_token_count": 10}})


def _patch_gemini(monkeypatch, fake):
    class Client:
        models = fake

    monkeypatch.setattr(llm, "_google_client", lambda cred: Client())


def _assert_clean_transcript(res, secret):
    dumped = json.dumps(res.raw_messages, ensure_ascii=False)
    assert secret not in dumped
    assert "[image crop]" in dumped

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str):
            assert len(o) < 3000, "a long (base64?) payload leaked into raw_messages"
            assert not o.startswith(("iVBOR", "data:image")), "image payload leaked into raw_messages"
    walk(res.raw_messages)


def _assert_scripted_result(res, tin, tout):
    assert res.error is None, res.error
    assert isinstance(res.verdict, llm.CropVerdict)
    assert res.verdict.is_requirement is False and res.verdict.certainty == "high" and res.verdict.crop_index == 0
    assert [s["tool"] for s in res.steps] == ["nearby_rows", "ocr_context", "submit_verdict"]
    assert [s["ref"] for s in res.steps] == ["S1", "S2", None]
    assert "CN 12 '1.250 ±.005' 2.8 mm right" in res.steps[0]["result_summary"]
    assert "'(REF)' 1.4 mm right" in res.steps[1]["result_summary"]
    assert res.evidence == ["S1", "S2"]
    assert (res.calls, res.input_tokens, res.output_tokens) == (3, tin, tout)
    assert len(res.turns) == 3 and not any(t["final"] for t in res.turns)


# ----------------------------------------------------------------------------- provider loops
def test_anthropic_loop(monkeypatch):
    fake = _Anthropic(SCRIPT)
    _patch_anthropic(monkeypatch, fake)
    res = inv.investigate(_ctx(), "anthropic", CRED, "claude-sonnet-5")
    _assert_scripted_result(res, 1000 + 2000 + 3000, 150)
    first, second = fake.bodies[0], fake.bodies[1]
    assert [t["name"] for t in first["tools"]] == ["zoom", "nearby_rows", "ocr_context", "search_boc",
                                                   "submit_verdict"]
    assert all(t["strict"] is True for t in first["tools"]) and "tool_choice" not in first
    assert first["messages"][0]["content"][0]["type"] == "image"
    tr = second["messages"][2]["content"][0]
    assert tr["type"] == "tool_result" and tr["tool_use_id"] == "toolu_1"
    assert json.loads(tr["content"][0]["text"].split("] ", 1)[1])["rows"][0]["char_no"] == "12"
    assert second["messages"][1]["content"][0]["type"] == "tool_use"  # assistant turn appended unchanged
    _assert_clean_transcript(res, CRED)


def test_anthropic_zoom_image_goes_inside_tool_result(monkeypatch):
    fake = _Anthropic([("zoom", {"factor": 2, "pad": "medium"}),
                       ("submit_verdict", {**VERDICT, "evidence": ["crop", "S1"]})])
    _patch_anthropic(monkeypatch, fake)
    res = inv.investigate(_ctx(), "anthropic", CRED, "claude-sonnet-5")
    assert res.error is None and res.steps[0]["image_ref"] == "S1"
    inner = fake.bodies[1]["messages"][2]["content"][0]["content"]
    assert [b["type"] for b in inner] == ["text", "image"] and inner[1]["source"]["media_type"] == "image/png"
    dumped = json.dumps(res.raw_messages)
    assert "[image S1]" in dumped
    _assert_clean_transcript(res, CRED)


def test_openai_loop(monkeypatch):
    fake = _OpenAI(SCRIPT)
    _patch_openai(monkeypatch, fake)
    res = inv.investigate(_ctx(), "openai", "sk-proj-SECRET", "gpt-6.1-sol")
    _assert_scripted_result(res, 900 + 1800 + 2700, 120)
    first, second = fake.bodies[0], fake.bodies[1]
    assert first["tool_choice"] == "auto" and first["tools"][0]["type"] == "function"
    assert first["input"][0]["content"][0]["image_url"].startswith("data:image/png;base64,")
    types_ = [i.get("type") for i in second["input"]]
    assert types_[1:] == ["reasoning", "function_call", "function_call_output"]  # reasoning passed back
    assert second["input"][-1]["call_id"] == "call_1"
    _assert_clean_transcript(res, "sk-proj-SECRET")


def test_openai_zoom_image_in_function_output(monkeypatch):
    fake = _OpenAI([("zoom", {"factor": 2, "pad": "small"}), ("submit_verdict", {**VERDICT, "evidence": ["S1"]})])
    _patch_openai(monkeypatch, fake)
    res = inv.investigate(_ctx(), "openai", "sk-proj-SECRET", "gpt-6.1-sol")
    assert res.error is None
    out = fake.bodies[1]["input"][-1]["output"]
    assert [o["type"] for o in out] == ["input_text", "input_image"]
    _assert_clean_transcript(res, "sk-proj-SECRET")


def test_gemini_loop(monkeypatch):
    fake = _Gemini(SCRIPT)
    _patch_gemini(monkeypatch, fake)
    res = inv.investigate(_ctx(), "google", "AIza-SECRET", "gemini-3.8-flash")
    _assert_scripted_result(res, 700 + 1400 + 2100, 120)
    cfg = fake.calls[0]["config"]
    assert cfg.automatic_function_calling.disable is True
    assert cfg.tool_config.function_calling_config.mode == "AUTO"
    assert [f.name for f in cfg.tools[0].function_declarations][-1] == "submit_verdict"
    # turn 2 sees: user (crop + text), model (function_call with its thought signature), user (function_response)
    c2 = fake.calls[1]["contents"]
    assert [c.role for c in c2] == ["user", "model", "user"]
    assert c2[1].parts[0].thought_signature == b"sig" * 50
    fr = c2[2].parts[0].function_response
    assert fr.name == "nearby_rows" and fr.id == "fc1" and "output" in fr.response
    dumped = json.dumps(res.raw_messages)
    assert "sigsig" not in dumped
    _assert_clean_transcript(res, "AIza-SECRET")


def test_gemini_zoom_is_a_multimodal_function_response(monkeypatch):
    fake = _Gemini([("zoom", {"factor": 3, "pad": "medium"}), ("submit_verdict", {**VERDICT, "evidence": ["S1"]})])
    _patch_gemini(monkeypatch, fake)
    res = inv.investigate(_ctx(), "google", "AIza-SECRET", "gemini-3.8-flash")
    assert res.error is None
    fr = fake.calls[1]["contents"][2].parts[0].function_response
    assert fr.parts[0].inline_data.mime_type == "image/png"
    _assert_clean_transcript(res, "AIza-SECRET")


# ----------------------------------------------------------------------------------- the bounds
def test_step_cap_forces_submission(monkeypatch):
    fake = _Anthropic([("search_boc", {"query": "1.250", "limit": 3})] * 2 + [("submit_verdict", VERDICT)])
    _patch_anthropic(monkeypatch, fake)
    res = inv.investigate(_ctx(), "anthropic", CRED, "claude-sonnet-5", max_steps=2)
    assert res.error is None and res.verdict is not None
    assert "tool_choice" not in fake.bodies[1]
    assert fake.bodies[2]["tool_choice"] == {"type": "tool", "name": "submit_verdict"}
    assert fake.bodies[2]["messages"][-1]["content"][-1]["text"] == inv.FINAL_NOTE
    assert res.turns[-1]["final"] and res.turns[-1]["forced"]


def test_step_cap_reached_when_model_keeps_calling_tools(monkeypatch):
    fake = _Anthropic([("ocr_context", {"radius_mm": 10})] * 10)
    _patch_anthropic(monkeypatch, fake)
    res = inv.investigate(_ctx(), "anthropic", CRED, "claude-sonnet-5", max_steps=2)
    assert res.error == "step_cap_reached" and res.verdict is None
    assert res.calls == 3  # 2 tool turns + 1 forced final turn, then stop
    executed = [s for s in res.steps if s["ok"]]
    assert len(executed) == 2 and res.steps[-1]["result_summary"] == "not run: tool budget used"


def test_final_turn_without_forced_tool_choice_on_models_that_reject_it(monkeypatch):
    fake = _Anthropic([("nearby_rows", {"radius_mm": 10}), ("submit_verdict", {**VERDICT, "evidence": ["S1"]})])
    _patch_anthropic(monkeypatch, fake)
    res = inv.investigate(_ctx(), "anthropic", CRED, "claude-sonnet-5-5", max_steps=1)
    assert res.error is None
    assert "tool_choice" not in fake.bodies[1]
    assert fake.bodies[1]["messages"][-1]["content"][-1]["text"] == inv.FINAL_NOTE
    assert res.turns[-1]["final"] and not res.turns[-1]["forced"]


def test_forced_tool_choice_400_falls_back_to_auto(monkeypatch):
    import httpx2

    fake = _Anthropic([("nearby_rows", {"radius_mm": 10}), ("submit_verdict", {**VERDICT, "evidence": ["S1"]})])
    orig = fake.handler

    def handler(request):
        if "tool_choice" in json.loads(request.content):
            return httpx2.Response(400, json={"type": "error", "error": {
                "type": "invalid_request_error", "message": "tool_choice: type tool is not supported for this model."}})
        return orig(request)

    fake.handler = handler
    _patch_anthropic(monkeypatch, fake)
    res = inv.investigate(_ctx(), "anthropic", CRED, "claude-sonnet-5", max_steps=1)
    assert res.error is None and res.verdict is not None and not res.turns[-1]["forced"]


def test_prose_answer_gets_one_forced_turn(monkeypatch):
    fake = _OpenAI([(None, "It is probably a reference value."), (None, "Still prose.")])
    _patch_openai(monkeypatch, fake)
    res = inv.investigate(_ctx(), "openai", "sk-proj-x", "gpt-6.1-sol")
    assert res.error == "step_cap_reached" and res.calls == 2
    assert fake.bodies[1]["tool_choice"] == {"type": "function", "name": "submit_verdict"}


def test_token_budget_moves_to_final_turn(monkeypatch):
    fake = _Gemini([("nearby_rows", {"radius_mm": 10}), ("submit_verdict", {**VERDICT, "evidence": ["S1"]})])
    _patch_gemini(monkeypatch, fake)
    res = inv.investigate(_ctx(), "google", "AIza-x", "gemini-3.8-flash", budget_tokens=500)
    assert res.error is None and res.calls == 2
    assert fake.calls[1]["config"].tool_config.function_calling_config.mode == "ANY"
    assert fake.calls[1]["config"].tool_config.function_calling_config.allowed_function_names == ["submit_verdict"]


def test_zero_steps_is_a_single_forced_turn(monkeypatch):
    fake = _OpenAI([("submit_verdict", {**VERDICT, "evidence": ["crop"]})])
    _patch_openai(monkeypatch, fake)
    res = inv.investigate(_ctx(), "openai", "sk-proj-x", "gpt-6.1-sol", max_steps=0)
    assert res.error is None and res.calls == 1 and fake.bodies[0]["tool_choice"]["name"] == "submit_verdict"


# ------------------------------------------------------------------------------ invalid verdicts
def test_invalid_verdict_json_is_no_verdict(monkeypatch):
    fake = _OpenAI([("submit_verdict", "{not json"), ("submit_verdict", "{still: not json}")])
    _patch_openai(monkeypatch, fake)
    res = inv.investigate(_ctx(), "openai", "sk-proj-x", "gpt-6.1-sol")
    assert res.error == "no_verdict" and res.verdict is None and res.calls == 2
    assert [s["ok"] for s in res.steps] == [False, False]
    assert "not valid JSON" in res.steps[0]["result_summary"]


def test_invalid_verdict_gets_one_repair(monkeypatch):
    bad = {**VERDICT, "certainty": "very", "evidence": ["crop"]}
    fake = _Gemini([("submit_verdict", bad), ("submit_verdict", {**VERDICT, "evidence": ["crop"]})])
    _patch_gemini(monkeypatch, fake)
    res = inv.investigate(_ctx(), "google", "AIza-x", "gemini-3.8-flash")
    assert res.error is None and res.verdict.certainty == "high"
    assert "certainty" in res.steps[0]["result_summary"]


def test_evidence_must_cite_real_steps(monkeypatch):
    fake = _Anthropic([("submit_verdict", {**VERDICT, "evidence": ["S9"]})] * 2)
    _patch_anthropic(monkeypatch, fake)
    res = inv.investigate(_ctx(), "anthropic", CRED, "claude-sonnet-5")
    assert res.error == "no_verdict" and "S9" in res.steps[0]["result_summary"]


# ----------------------------------------------------------------------------------- failures
@pytest.mark.parametrize("status,code", [(401, "auth_failed"), (403, "permission_denied"), (429, "rate_limited"),
                                         (404, "model_not_found"), (500, "api_error_500")])
def test_anthropic_status_codes_map(monkeypatch, status, code):
    fake = _Anthropic([], status=status)
    _patch_anthropic(monkeypatch, fake)
    res = inv.investigate(_ctx(), "anthropic", CRED, "claude-sonnet-5")
    assert res.error == code and res.verdict is None
    assert CRED not in json.dumps(res.raw_messages)


@pytest.mark.parametrize("status,code", [(401, "auth_failed"), (403, "permission_denied"), (429, "rate_limited")])
def test_openai_status_codes_map(monkeypatch, status, code):
    _patch_openai(monkeypatch, _OpenAI([], status=status))
    assert inv.investigate(_ctx(), "openai", "sk-proj-x", "gpt-6.1-sol").error == code


def test_network_failure_maps(monkeypatch):
    import httpx2

    _patch_anthropic(monkeypatch, _Anthropic([], raise_exc=httpx2.ConnectError("no route")))
    assert inv.investigate(_ctx(), "anthropic", CRED, "claude-sonnet-5").error == "connection_failed"


def test_gemini_errors_map(monkeypatch):
    from google.genai import errors as gerrors

    exc = gerrors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})
    _patch_gemini(monkeypatch, _Gemini([], exc=exc))
    assert inv.investigate(_ctx(), "google", "AIza-x", "gemini-3.8-flash").error == "rate_limited"


def test_unexpected_failure_never_raises(monkeypatch):
    def boom(cred):
        raise RuntimeError("stack exploded")

    monkeypatch.setattr(llm, "_openai_client", boom)
    res = inv.investigate(_ctx(), "openai", "sk-proj-x", "gpt-6.1-sol")
    assert res.error == "unexpected_RuntimeError" and res.verdict is None


def test_no_credential_and_unknown_provider(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert inv.investigate(_ctx(), "openai", None, "gpt-6.1-sol").error == "no_credential"
    assert inv.investigate(_ctx(), "acme", "k", "m").error == "unknown_provider_acme"


def test_timeout(monkeypatch):
    fake = _OpenAI(SCRIPT)
    _patch_openai(monkeypatch, fake)
    res = inv.investigate(_ctx(), "openai", "sk-proj-x", "gpt-6.1-sol", timeout_s=0)
    assert res.error == "timeout" and res.calls == 0


def test_context_for_group_and_result_dict(monkeypatch):
    from app.pipeline.regions import Region

    regs = [Region("r1", 1, "1.250", ITEM, box_poly(ITEM)), Region("r2", 1, "(REF)", Box(0.545, 0.5, 0.02, 0.02),
                                                                   box_poly(ITEM)),
            Region("r9", 2, "x", ITEM, box_poly(ITEM))]
    ctx = inv.context_for_group("G1", regs[:1], "1.250", "why", box_poly(ITEM), ITEM,
                                np.full((850, 1100, 3), 255, np.uint8), _rows(), regs, PAGE_MM, "B4")
    assert ctx.region_ids == ["r1"] and [r.region_id for r in ctx.regions] == ["r1", "r2"]
    assert [r["region_id"] for r in inv.tool_ocr_context(ctx, 5)["regions"]] == ["r2"]
    fake = _Anthropic(SCRIPT)
    _patch_anthropic(monkeypatch, fake)
    d = inv.investigate(_ctx(), "anthropic", CRED, "claude-sonnet-5").to_dict()
    json.dumps(d)  # serializable for the trace
    assert d["verdict"]["kind"] == "none" and d["cost_usd"] is not None
