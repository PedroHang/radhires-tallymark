"""User override: restore an item the machine set aside (the AI dropped it, or it scored under 0.5)
as a normal open flag, so the engineer can decide on it like any other.

The machine's record is never rewritten. The region keeps its `final` outcome; the trace gains a
`user_actions` entry, a `user` note on each region of the item, and a flag chain for the new flag
whose last step says the user restored it. The flag itself is appended to the run's flags with
`origin: "restored_by_user"`, so review, export and the audit trail all see it.

    POST /runs/{run_id}/trace/regions/{region_id}/restore   body (optional): {"note": "..."}
    201 -> the new flag. 200 -> the item was already restored (same flag returned).
    404 run / trace / region missing; 409 run still running, or the outcome can't be restored.
"""
from __future__ import annotations

import datetime as dt
import threading

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import store
from .pipeline.geometry import zone_label

router = APIRouter()

RESTORABLE = {"dropped_by_model": "the AI check said it is not a requirement",
              "below_threshold": "it scored under the 0.5 flag threshold"}
ORIGIN = "restored_by_user"
_lock = threading.Lock()  # trace.json is a plain file: serialize the read-modify-write


class RestoreIn(BaseModel):
    note: str | None = Field(default=None, max_length=300)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _union_box(regs: list[dict]) -> dict:
    x0 = min(r["bbox"]["x"] for r in regs)
    y0 = min(r["bbox"]["y"] for r in regs)
    x1 = max(r["bbox"]["x"] + r["bbox"]["w"] for r in regs)
    y1 = max(r["bbox"]["y"] + r["bbox"]["h"] for r in regs)
    return {"x": round(x0, 5), "y": round(y0, 5), "w": round(x1 - x0, 5), "h": round(y1 - y0, 5)}


def _chain(flag: dict, regs: list[dict], prev: str, at: str, note: str | None) -> dict:
    """Flag chain in the /trace/flags shape: the machine's steps for this item, then the user's."""
    first = regs[0]
    steps: list[dict] = []

    def add(kind, title, detail, data=None):
        steps.append({"n": len(steps) + 1, "kind": kind, "title": title, "detail": detail, "data": data or {}})

    add("measured", "Read the text", f"Read \"{flag['drawing_text']}\" on sheet {flag['sheet']}, zone {flag['zone']}"
        + (f", from {len(regs)} text pieces." if len(regs) > 1 else "."),
        {"regions": [{"region_id": r["region_id"], "text": r["text"], "ocr_conf": r["ocr_conf"], "origin": r["origin"]}
                     for r in regs]})
    near = first["coverage"].get("nearest_row")
    add("measured", "Matched to a BoC row?",
        f"No. Nearest row is CN {near['char_no']} ({near['requirement']}), {near['distance_mm']} mm away." if near
        else "No. No BoC row is located near it.",
        {"candidates": [c for r in regs for c in r["coverage"]["candidates"]], "nearest_row": near})
    add("rule", "Excluded?", "No exclusion applies.", {"applied": None})
    cls = first.get("classification")
    if cls:
        add("rule", f"{cls['rule_id']}: {cls['rule_label']}", cls["reason"],
            {"rule_id": cls["rule_id"], "outcome": cls["outcome"], "group_id": cls["group_id"],
             "group_text": cls["text"], "checks": cls["checks"]})
    model = first.get("model")
    if model and model.get("status") == "verdict" and model.get("verdict"):
        v = model["verdict"]
        add("ai", "AI check: it is a requirement" if v["is_requirement"] else "AI check: not a requirement",
            f"{model.get('provider')} {model.get('model')} answered with {v['certainty']} certainty: \"{v['reason']}\".",
            model)
    elif model:
        add("ai", "AI check did not answer", "The AI check did not run or did not answer.", model)
    sc = dict(first.get("score") or {})
    if sc:
        prod = sc.get("product")
        add("score", "Score: not flagged" if prod is None else f"Score {prod:.2f}: not flagged",
            ("The AI said it is not a requirement, so no score was kept." if prev == "dropped_by_model"
             else f"{sc.get('base')} (rule) × {sc.get('locate_factor')} (location) × {sc.get('llm_factor')} (AI) = "
                  f"{(prod or 0):.3f}, under the 0.5 needed to flag."), sc)
    add("user", "Restored by you",
        f"You restored this as a flag ({RESTORABLE[prev]} first). The machine's decision above is kept as it was; "
        "this flag is now yours to confirm, exclude or dismiss." + (f" Note: {note}" if note else ""),
        {"action": "restore", "previous_outcome": prev, "at": at, "note": note})
    return {"flag_id": flag["flag_id"], "class": flag["class"], "sheet": flag["sheet"],
            "region_ids": flag["region_ids"], "group_id": first.get("group_id"), "confidence": flag["confidence"],
            "score": flag["score"], "origin": ORIGIN, "steps": steps}


@router.post("/runs/{run_id}/trace/regions/{region_id}/restore", status_code=201)
def restore_region(run_id: str, region_id: str, body: RestoreIn | None = None):
    note = (body.note.strip() or None) if body and body.note else None
    doc = store.get_run(run_id)
    if doc is None:
        raise HTTPException(404, "Run not found.")
    with _lock:
        trace = store.get_trace(run_id)
        if trace is None:
            if doc["status"] in ("queued", "running"):
                raise HTTPException(409, "The trace is written when the run finishes.")
            raise HTTPException(404, "No trace for this run.")
        reg = next((r for r in trace["regions"] if r["region_id"] == region_id), None)
        if reg is None:
            raise HTTPException(404, "Region not found.")
        done = (reg.get("user") or {}).get("restored_flag_id")
        if done:
            flag = next((f for f in doc["flags"] if f["flag_id"] == done), None)
            if flag is not None:
                return JSONResponse(flag, status_code=200)
        prev = reg["final"]["outcome"]
        if prev not in RESTORABLE:
            raise HTTPException(409, f"Only items the AI dropped or that scored under the threshold can be restored "
                                     f"(this one is '{reg['final']['label']}').")
        gid = reg.get("group_id")
        regs = [r for r in trace["regions"] if gid and r.get("group_id") == gid] or [reg]
        box = _union_box(regs)
        cls = reg.get("classification") or {}
        model = reg.get("model") or {}
        verdict = model.get("verdict") or {}
        near = reg["coverage"].get("nearest_row")
        at = _now()
        why = RESTORABLE[prev]
        flag = {
            "flag_id": None, "class": "unaccounted", "sheet": reg["sheet"],
            "zone": zone_label((box["x"] + box["w"] / 2, box["y"] + box["h"] / 2)), "bbox": box,
            "poly": reg["poly"] if len(regs) == 1 else [[box["x"], box["y"]], [box["x"] + box["w"], box["y"]],
                                                        [box["x"] + box["w"], box["y"] + box["h"]],
                                                        [box["x"], box["y"] + box["h"]]],
            "angle": reg.get("angle", 0.0), "char_no": None,
            "nearest_char_no": near["char_no"] if near else None,
            "nearest_requirement": near["requirement"] if near else None,
            "nearest_distance_mm": round(near["distance_mm"]) if near and near.get("distance_mm") is not None else None,
            "extracted": None, "drawing_text": cls.get("text") or reg["text"],
            "rule_ids": [cls["rule_id"]] if cls.get("rule_id") else [], "llm": verdict or None,
            "kind": cls.get("kind") or "other", "title": "Restored by you, no row",
            "reason": (f"You restored this item: {why}"
                       + (f" (\"{verdict['reason'].rstrip('.')}\")" if prev == "dropped_by_model" and verdict.get("reason") else "")
                       + ". No BoC row covers it."),
            "score": 0.0, "confidence": "low", "confidence_basis": "restored by you",
            "disposition": "open", "disposition_note": None, "disposed_at": None,
            "region_ids": [r["region_id"] for r in regs], "origin": ORIGIN,
            "restored_from": {"region_id": region_id, "group_id": gid, "outcome": prev, "at": at, "note": note},
            "run_id": run_id,
        }

        def add_flag(d):
            n = 1 + sum(1 for f in d["flags"] if f.get("origin") == ORIGIN)
            flag["flag_id"] = f"U-R{n}"
            flag["crop_url"] = f"/runs/{run_id}/flags/{flag['flag_id']}/crop.png"
            d["flags"].append(flag)

        if store.update_run(run_id, add_flag) is None:
            raise HTTPException(404, "Run not found.")
        for r in regs:
            r["user"] = {"restored_flag_id": flag["flag_id"], "at": at}
        trace.setdefault("user_actions", []).append(
            {"action": "restore", "at": at, "region_id": region_id, "group_id": gid,
             "region_ids": flag["region_ids"], "previous_outcome": prev, "flag_id": flag["flag_id"], "note": note})
        trace.setdefault("flags", {})[flag["flag_id"]] = _chain(flag, regs, prev, at, note)
        store.put_trace(run_id, trace)
    return flag
