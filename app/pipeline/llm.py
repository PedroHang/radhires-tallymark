"""The two narrow jobs the vision model does.

1. classify_crops: for ambiguous leftovers only, answer "is the outlined text an
   inspectable requirement?". High-confidence flags come from deterministic rules.
2. transcribe_crops: for flags already raised, read the outlined text with proper
   engineering symbols so the review panel is legible. Display only.

The model never reads values into the BoC and never decides coverage.

Three providers do the same two jobs with the same prompts and the same output schema:
Anthropic (Claude), OpenAI (GPT) and Google (Gemini). Every provider error comes back
as a short code on the result; nothing here raises into the pipeline, so a bad key or an
outage degrades to the rules-only path instead of failing the run.
"""
from __future__ import annotations

import base64
import io
import os
from dataclasses import dataclass
from typing import Literal

import cv2
import numpy as np
from PIL import Image
from pydantic import BaseModel, Field

from .geometry import Box

PROVIDERS = {
    "anthropic": {"label": "Anthropic (Claude)", "key_label": "Anthropic API key", "placeholder": "sk-ant-...",
                  "env": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
                  "models": ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5", "claude-sonnet-5"]},
    "openai": {"label": "OpenAI (GPT)", "key_label": "OpenAI API key", "placeholder": "sk-...",
               "env": ("OPENAI_API_KEY",),
               "models": ["gpt-6.1-sol", "gpt-6-luna", "gpt-6-astra"]},
    "google": {"label": "Google (Gemini)", "key_label": "Gemini API key", "placeholder": "AIza...",
               "env": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
               "models": ["gemini-3.8-flash", "gemini-3.5-flash-lite"]},
}
DEFAULT_PROVIDER = os.environ.get("UINK_PROVIDER", "anthropic")
DEFAULT_MODEL = os.environ.get("UINK_MODEL", PROVIDERS.get(DEFAULT_PROVIDER, PROVIDERS["anthropic"])["models"][0])
MAX_CROPS_PER_REQUEST = 20

SYSTEM = (
    "You are checking crops from an engineering drawing for a first article inspection. "
    "Each crop has one text region outlined in red. Decide whether the outlined text is an "
    "inspectable requirement (a dimension, a tolerance, a GD&T control, a thread or surface finish "
    "callout, or a note that imposes a requirement) or not (a label, a reference value, a view or "
    "datum name, a coordinate axis, or an annotation). Answer only from what is visible in the crop. "
    "If the text is unreadable, say so and use low certainty."
)


class CropVerdict(BaseModel):
    crop_index: int = Field(description="0-based index of the crop, in the order the crops were given")
    is_requirement: bool
    kind: Literal["dimension", "gdt", "note", "spec", "other", "none"]
    transcription: str = Field(description="The outlined text as it appears on the drawing")
    reason: str = Field(description="At most 20 words, plain English")
    certainty: Literal["high", "medium", "low"]


class CropVerdicts(BaseModel):
    verdicts: list[CropVerdict]


@dataclass
class LlmResult:
    verdicts: dict[int, CropVerdict]
    calls: int
    input_tokens: int
    output_tokens: int
    error: str | None = None


def detect_provider(key: str) -> str | None:
    """Guess the provider from the key's prefix so a pasted key just works."""
    key = (key or "").strip()
    if key.startswith("sk-ant-"):
        return "anthropic"
    if key.startswith(("AIza", "AQ.")):  # Gemini API keys, older and newer formats
        return "google"
    if key.startswith("sk-"):
        return "openai"
    return None


def resolve_credential(provider: str, session_credential: str | None) -> tuple[str | None, str | None]:
    """(credential, source). A key pasted in Settings wins over the environment / .env."""
    if session_credential and session_credential.strip():
        return session_credential.strip(), "session"
    for name in PROVIDERS[provider]["env"]:
        val = os.environ.get(name, "").strip()
        if val:
            return val, "environment"
    return None, None


class LlmError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _status_code(code: int | None, message: str = "") -> str:
    if code == 400 and "api key" in message.lower():
        return "auth_failed"  # Gemini answers 400 API_KEY_INVALID for a bad key
    return {401: "auth_failed", 403: "permission_denied", 404: "model_not_found", 429: "rate_limited"}.get(
        code, f"api_error_{code}" if code else "api_error")


def _error_code(provider: str, exc: Exception) -> str:
    """Map any provider SDK exception to one short code the UI can explain."""
    if provider in ("anthropic", "openai"):
        sdk = __import__(provider)
        if isinstance(exc, sdk.APIConnectionError):
            return "connection_failed"
        if isinstance(exc, sdk.APIStatusError):
            return _status_code(exc.status_code, str(exc))
    if provider == "google":
        from google.genai import errors as gerrors

        if isinstance(exc, gerrors.APIError):
            return _status_code(getattr(exc, "code", None), str(exc))
    if type(exc).__name__ in ("ConnectError", "ConnectTimeout", "ReadTimeout", "TimeoutException"):
        return "connection_failed"
    return f"unexpected_{type(exc).__name__}"


def _anthropic_client(cred: str):
    """API keys (sk-ant-api...) go on x-api-key. OAuth access tokens go on
    Authorization: Bearer with the oauth beta header. Anthropic intends Claude
    subscription (Pro/Max) tokens for Claude Code, so the API may refuse them here;
    a Console API key is the supported path for this app."""
    import anthropic

    if cred.startswith("sk-ant-oat"):
        return anthropic.Anthropic(auth_token=cred, default_headers={"anthropic-beta": "oauth-2025-04-20"})
    return anthropic.Anthropic(api_key=cred)


def _openai_client(cred: str):
    import openai

    return openai.OpenAI(api_key=cred)


def _google_client(cred: str):
    from google import genai

    return genai.Client(api_key=cred)


def structured_call(provider: str, cred: str, model: str, system: str, images: list[Image.Image],
                    instruction: str, schema: type[BaseModel], max_tokens: int):
    """One request: numbered crops + an instruction in, a parsed `schema` instance out.
    Returns (parsed_or_None, input_tokens, output_tokens). Raises LlmError on failure."""
    try:
        if provider == "anthropic":
            content: list[dict] = []
            for i, im in enumerate(images):
                content.append({"type": "text", "text": f"Crop {i}:"})
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                            "data": _b64png(im)}})
            content.append({"type": "text", "text": instruction})
            resp = _anthropic_client(cred).messages.parse(
                model=model, max_tokens=max_tokens, system=system, output_config={"effort": "low"},
                messages=[{"role": "user", "content": content}], output_format=schema)
            parsed = None if resp.stop_reason == "refusal" else resp.parsed_output
            return parsed, resp.usage.input_tokens, resp.usage.output_tokens

        if provider == "openai":
            content = []
            for i, im in enumerate(images):
                content.append({"type": "input_text", "text": f"Crop {i}:"})
                content.append({"type": "input_image", "image_url": f"data:image/png;base64,{_b64png(im)}"})
            content.append({"type": "input_text", "text": instruction})
            resp = _openai_client(cred).responses.parse(
                model=model, instructions=system, input=[{"role": "user", "content": content}],
                text_format=schema, reasoning={"effort": "low"}, max_output_tokens=max_tokens)
            usage = resp.usage
            return resp.output_parsed, (usage.input_tokens if usage else 0), (usage.output_tokens if usage else 0)

        if provider == "google":
            from google.genai import types

            parts: list = []
            for i, im in enumerate(images):
                parts.append(types.Part.from_text(text=f"Crop {i}:"))
                parts.append(types.Part.from_bytes(data=base64.standard_b64decode(_b64png(im)), mime_type="image/png"))
            parts.append(types.Part.from_text(text=instruction))
            client = _google_client(cred)  # keep a reference: the SDK closes an unreferenced client mid-request
            resp = client.models.generate_content(
                model=model, contents=parts,
                config=types.GenerateContentConfig(system_instruction=system, response_mime_type="application/json",
                                                   response_schema=schema, max_output_tokens=max_tokens))
            um = resp.usage_metadata
            tin = (um.prompt_token_count or 0) if um else 0
            tout = ((um.candidates_token_count or 0) + (um.thoughts_token_count or 0)) if um else 0
            parsed = resp.parsed
            if parsed is None and resp.text:
                parsed = schema.model_validate_json(resp.text)
            return parsed, tin, tout
    except LlmError:
        raise
    except Exception as exc:  # noqa: BLE001  any SDK / network / parse failure becomes a code
        raise LlmError(_error_code(provider, exc)) from exc
    raise LlmError(f"unknown_provider_{provider}")


def ping(provider: str, session_credential: str | None, model: str) -> tuple[bool, str]:
    """One tiny request to confirm the key works with the selected model (Settings > Test)."""
    cred, _ = resolve_credential(provider, session_credential)
    if not cred:
        return False, "No API key set for this provider."
    blank = Image.new("RGB", (32, 32), "white")
    try:
        structured_call(provider, cred, model, "Answer the question.", [blank],
                        "Is crop 0 blank? Return one item.", CropTexts, 2000)
        return True, f"Connected to {model}."
    except LlmError as e:
        msg = ERROR_TEXT.get(e.code, e.code)
        return False, msg[0].upper() + msg[1:] + "."


ERROR_TEXT = {
    "no_credential": "no API key is set",
    "auth_failed": "the API key was rejected",
    "permission_denied": "the key can't use this model",
    "model_not_found": "the model was not found for this key",
    "rate_limited": "the API rate limited the request",
    "connection_failed": "the API could not be reached",
}


def crop_with_outline(img_rgb: np.ndarray, poly: list[tuple[float, float]], box: Box, pad_ratio: float = 0.3,
                      min_px: int = 160) -> Image.Image:
    h, w = img_rgb.shape[:2]
    px, py = box.w * w, box.h * h
    padx = max(pad_ratio * px, 40)
    pady = max(pad_ratio * py, 40, (min_px - py) / 2)
    x0 = int(max(0, box.x * w - padx))
    y0 = int(max(0, box.y * h - pady))
    x1 = int(min(w, box.x2 * w + padx))
    y1 = int(min(h, box.y2 * h + pady))
    crop = img_rgb[y0:y1, x0:x1].copy()
    pts = np.array([[p[0] * w - x0, p[1] * h - y0] for p in poly], np.int32)
    cv2.polylines(crop, [pts], True, (220, 30, 30), 3)
    im = Image.fromarray(crop)
    if max(im.size) > 1000:
        im.thumbnail((1000, 1000))
    return im


def _b64png(im: Image.Image) -> str:
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")


def classify_crops(crops: list[Image.Image], credential: str | None, model: str = DEFAULT_MODEL,
                   provider: str = DEFAULT_PROVIDER) -> LlmResult:
    cred, _ = resolve_credential(provider, credential)
    if not cred:
        return LlmResult({}, 0, 0, 0, error="no_credential")
    verdicts: dict[int, CropVerdict] = {}
    calls = tin = tout = 0
    for start in range(0, len(crops), MAX_CROPS_PER_REQUEST):
        batch = crops[start:start + MAX_CROPS_PER_REQUEST]
        try:
            parsed, i_tok, o_tok = structured_call(
                provider, cred, model, SYSTEM, batch,
                f"Return exactly one verdict for each of the {len(batch)} crops.", CropVerdicts, 16000)
        except LlmError as e:
            return LlmResult(verdicts, calls, tin, tout, error=e.code)
        calls += 1
        tin += i_tok
        tout += o_tok
        if parsed is None:
            continue
        for v in parsed.verdicts:
            if 0 <= v.crop_index < len(batch):
                verdicts[start + v.crop_index] = v
    return LlmResult(verdicts, calls, tin, tout)


TRANSCRIBE_SYSTEM = (
    "You transcribe text from crops of an engineering drawing. Each crop has one region outlined in red. "
    "Transcribe only what is inside the red outline, exactly as drawn. Use engineering symbols where they are "
    "visible: Ø diameter, ± plus-minus, ⌖ position, ⌓ profile of a surface, ⌒ profile of a line, ▱ flatness, "
    "⊥ perpendicularity, ⌴ counterbore, ↧ depth, Ⓜ maximum material condition. Separate feature control frame "
    "cells with ' | '. Do not guess at text you cannot read; write [?] for it."
)


class CropText(BaseModel):
    crop_index: int
    text: str


class CropTexts(BaseModel):
    items: list[CropText]


@dataclass
class TranscribeResult:
    texts: dict[int, str]
    calls: int
    input_tokens: int
    output_tokens: int
    error: str | None = None


def transcribe_crops(crops: list[Image.Image], credential: str | None, model: str = DEFAULT_MODEL,
                     provider: str = DEFAULT_PROVIDER) -> TranscribeResult:
    """Display-only transcription of flagged crops. Flag decisions never depend on it."""
    cred, _ = resolve_credential(provider, credential)
    if not cred:
        return TranscribeResult({}, 0, 0, 0, error="no_credential")
    texts: dict[int, str] = {}
    calls = tin = tout = 0
    for start in range(0, len(crops), MAX_CROPS_PER_REQUEST):
        batch = crops[start:start + MAX_CROPS_PER_REQUEST]
        try:
            parsed, i_tok, o_tok = structured_call(
                provider, cred, model, TRANSCRIBE_SYSTEM, batch,
                f"Return one transcription for each of the {len(batch)} crops.", CropTexts, 4000)
        except LlmError as e:
            return TranscribeResult(texts, calls, tin, tout, error=e.code)
        calls += 1
        tin += i_tok
        tout += o_tok
        if parsed is None:
            continue
        for item in parsed.items:
            if 0 <= item.crop_index < len(batch) and item.text.strip():
                texts[start + item.crop_index] = item.text.strip()
    return TranscribeResult(texts, calls, tin, tout)


# $ per million tokens (input, output). Only prices checked against a first-party source are listed;
# any other model reports tokens without a dollar figure rather than a guessed one.
# Anthropic: claude-api skill cache 2026-06-24. OpenAI: developers.openai.com/api/docs/models, 2026-10-19.
PRICING = {"claude-sonnet-5": (2.0, 10.0), "claude-opus-5": (5.0, 25.0), "claude-haiku-4-5": (1.0, 5.0),
           "gpt-6.1-sol": (2.0, 10.0), "gpt-6-luna": (0.1, 0.5)}


def cost_usd(model: str, tin: int, tout: int) -> float | None:
    if model not in PRICING:
        return None
    pin, pout = PRICING[model]
    return round(tin / 1e6 * pin + tout / 1e6 * pout, 5)
