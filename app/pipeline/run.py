"""Orchestrates one check: drawing + BoC in, flags out. Reports stage progress to the store."""
from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass, field

import cv2
import numpy as np

from .. import store
from ..pipeline.boc import Characteristic, parse_boc
from .classify import classify_text, places_in, tidy_ocr_text
from .coverage import decimals, locate_rows_without_bbox, nearest_row, subtract_coverage, text_similarity
from .exclusions import DEFAULT_EXCLUSIONS, apply_exclusions, pick_template
from .geometry import Box, iou, union_box, union_poly, zone_label
from .llm import DEFAULT_MODEL, DEFAULT_PROVIDER, classify_crops, cost_usd, crop_with_outline, transcribe_crops
from .regions import (
    VIEW_DPI,
    WORK_DPI,
    Region,
    detect_fcf_grids,
    group_line_fragments,
    merge_regions,
    ocr_regions,
    open_drawing,
    render_page,
    text_layer_regions,
)

PIPELINE_VERSION = "ocr3"
STAGES = [
    ("render", "Rendering sheets"),
    ("text_regions", "Reading text regions"),
    ("match", "Matching to BoC rows"),
    ("exclusions", "Applying exclusions"),
    ("classify", "Classifying leftovers"),
    ("places", "Checking repeated features"),
]
FLAG_THRESHOLD = 0.5
LLM_FACTOR = {"high": 1.0, "medium": 0.75, "low": 0.5}


@dataclass
class RunOptions:
    exclusions: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUSIONS))
    extra_regex: str | None = None
    use_llm: bool = True
    model: str = DEFAULT_MODEL
    provider: str = DEFAULT_PROVIDER
    credential: str | None = None
    skip_sheets: list[int] = field(default_factory=list)
    simulated: bool = False


class StageError(Exception):
    def __init__(self, stage: str, sheet: int | None, message: str):
        super().__init__(message)
        self.stage, self.sheet, self.message = stage, sheet, message


def new_run_doc(run_id: str, created_at: str, drawing_meta: dict, boc_meta: dict, opts: RunOptions) -> dict:
    return {
        "run_id": run_id,
        "created_at": created_at,
        "status": "queued",
        "drawing": {"filename": drawing_meta["filename"], "sha256": drawing_meta["sha256"], "sheets": None,
                    "upload_id": drawing_meta.get("upload_id")},
        "boc": {"filename": boc_meta["filename"], "rows": None, "has_bbox": None, "simulated": opts.simulated,
                "upload_id": boc_meta.get("upload_id")},
        "exclusions": opts.exclusions,
        "extra_regex": opts.extra_regex,
        "model": opts.model if opts.use_llm else None,
        "provider": opts.provider if opts.use_llm else None,
        "skip_sheets": opts.skip_sheets,
        "stages": [{"key": k, "name": n, "status": "pending", "count": None, "ms": None} for k, n in STAGES],
        "stats": {},
        "warnings": [],
        "error": None,
        "flags": [],
        "rows": [],
        "page": None,
    }


def _stage(run_id: str, key: str, status: str, count=None, ms=None, detail=None) -> None:
    def fn(doc):
        doc["status"] = "running"
        for s in doc["stages"]:
            if s["key"] == key:
                s["status"] = status
                if count is not None:
                    s["count"] = count
                if ms is not None:
                    s["ms"] = ms
                if detail is not None:
                    s["detail"] = detail

    store.update_run(run_id, fn)


def _cache_dir(sha: str):
    d = store.CACHE / f"{sha[:24]}_{PIPELINE_VERSION}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def execute(run_id: str, drawing: bytes, drawing_name: str, boc_bytes: bytes, boc_name: str, opts: RunOptions) -> None:
    try:
        _execute(run_id, drawing, drawing_name, boc_bytes, boc_name, opts)
    except StageError as e:
        _fail(run_id, e.stage, e.sheet, e.message)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        _fail(run_id, None, None, f"{type(e).__name__}: {e}")


def _fail(run_id: str, stage: str | None, sheet: int | None, message: str) -> None:
    def fn(doc):
        doc["status"] = "error"
        current = stage or next((s["key"] for s in doc["stages"] if s["status"] == "running"), None)
        for s in doc["stages"]:
            if s["key"] == current:
                s["status"] = "error"
        name = next((s["name"] for s in doc["stages"] if s["key"] == current), "the check")
        doc["error"] = {"stage": current, "stage_name": name, "sheet": sheet, "message": message}

    store.update_run(run_id, fn)


def _execute(run_id, drawing, drawing_name, boc_bytes, boc_name, opts: RunOptions) -> None:
    rdir = store.run_dir(run_id)
    sha = store.sha256(drawing)
    cache = _cache_dir(sha)
    boc = parse_boc(boc_bytes, boc_name)
    rows = boc.rows

    # 1. render ---------------------------------------------------------------
    t0 = time.perf_counter()
    _stage(run_id, "render", "running")
    doc = open_drawing(drawing, drawing_name)
    sheets = [i + 1 for i in range(doc.page_count) if (i + 1) not in opts.skip_sheets]
    page = doc[0].rect
    aspect = page.height / page.width
    first_text = doc[0].get_text("text")
    template = pick_template(first_text)
    work: dict[int, np.ndarray] = {}
    for n in sheets:
        wpath = cache / f"sheet_{n}_work.png"
        vpath = cache / f"sheet_{n}_view.png"
        try:
            if wpath.exists() and vpath.exists():
                work[n] = cv2.imread(str(wpath))[:, :, ::-1].copy()
            else:
                img = render_page(doc[n - 1], WORK_DPI)
                cv2.imwrite(str(wpath), img[:, :, ::-1])
                cv2.imwrite(str(vpath), render_page(doc[n - 1], VIEW_DPI)[:, :, ::-1])
                work[n] = img
        except Exception as exc:  # noqa: BLE001
            raise StageError("render", n, f"Sheet {n} could not be rendered ({exc}).") from exc
    store.update_run(run_id, lambda d: d.update({
        "drawing": {**d["drawing"], "sheets": doc.page_count, "cache": cache.name},
        "boc": {**d["boc"], "rows": len(rows), "has_bbox": boc.has_bbox},
        "page": {"width_pt": page.width, "height_pt": page.height, "template": template.name},
        "sheets_checked": sheets,
    }))
    _stage(run_id, "render", "done", count=f"{len(sheets)} of {doc.page_count}", ms=_ms(t0))

    # 2. text regions -----------------------------------------------------------
    t0 = time.perf_counter()
    _stage(run_id, "text_regions", "running")
    regions: list[Region] = []
    for n in sheets:
        rpath = cache / f"sheet_{n}_regions.json"
        if rpath.exists():
            regions += [Region.from_dict(r) for r in json.loads(rpath.read_text(encoding="utf-8"))]
            continue
        _stage(run_id, "text_regions", "running", detail=f"sheet {n}")
        try:
            sheet_regions = merge_regions(
                ocr_regions(work[n], n), text_layer_regions(doc[n - 1], n), detect_fcf_grids(work[n]), n
            )
        except Exception as exc:  # noqa: BLE001
            raise StageError("text_regions", n, f"The page may be a low-resolution scan ({exc}).") from exc
        rpath.write_text(json.dumps([r.to_dict() for r in sheet_regions]), encoding="utf-8")
        regions += sheet_regions
    _stage(run_id, "text_regions", "done", count=f"{len(regions)} found", ms=_ms(t0))

    # 3. match rows -------------------------------------------------------------
    t0 = time.perf_counter()
    _stage(run_id, "match", "running")
    checked_rows = [r for r in rows if r.sheet in sheets]
    relocated = locate_rows_without_bbox(checked_rows, regions)
    subtract_coverage(checked_rows, regions, aspect)
    located = sum(1 for r in checked_rows if r.bbox is not None)
    _stage(run_id, "match", "done", count=f"{located} of {len(checked_rows)} rows", ms=_ms(t0))

    # 4. exclusions -------------------------------------------------------------
    t0 = time.perf_counter()
    _stage(run_id, "exclusions", "running")
    n_excl = apply_exclusions(regions, template, opts.exclusions, opts.extra_regex)
    _stage(run_id, "exclusions", "done", count=f"{n_excl} hidden", ms=_ms(t0))

    # 5. classify leftovers -----------------------------------------------------
    t0 = time.perf_counter()
    _stage(run_id, "classify", "running")
    leftovers = [r for r in regions if not r.covered_by and not r.excluded_by]
    candidates = []
    for n in sheets:
        for group in group_line_fragments([r for r in leftovers if r.sheet == n], aspect):
            text = _join_fragments(group)
            verdict = classify_text(text, max(g.fcf_cells for g in group))
            if verdict.outcome == "not_requirement":
                continue
            candidates.append({"group": group, "text": text, "verdict": verdict})

    ambiguous = [c for c in candidates if c["verdict"].outcome == "ambiguous"]
    llm_stats = {"llm_calls": 0, "llm_input_tokens": 0, "llm_output_tokens": 0, "cost_usd": 0.0,
                 "llm_error": None, "ambiguous": len(ambiguous), "model_rejected": 0}
    if ambiguous and opts.use_llm:
        _stage(run_id, "classify", "running", detail=f"model on {len(ambiguous)} crops")
        crops = []
        for c in ambiguous:
            g = c["group"]
            poly, box = _group_outline(g)
            crops.append(crop_with_outline(work[g[0].sheet], poly, box))
        res = classify_crops(crops, opts.credential, opts.model, opts.provider)
        llm_stats.update(llm_calls=res.calls, llm_input_tokens=res.input_tokens, llm_output_tokens=res.output_tokens,
                         cost_usd=cost_usd(opts.model, res.input_tokens, res.output_tokens), llm_error=res.error)
        for i, c in enumerate(ambiguous):
            c["llm"] = res.verdicts.get(i)
    elif ambiguous:
        llm_stats["llm_error"] = "disabled"
    _stage(run_id, "classify", "done", count=f"{len(candidates)} candidates", ms=_ms(t0))

    # 6. places ----------------------------------------------------------------
    t0 = time.perf_counter()
    _stage(run_id, "places", "running")
    row_by_cn = {r.char_no: r for r in rows}
    places_flags = []
    seen_rows: set[str] = set()
    for reg in regions:
        if not reg.covered_by:
            continue
        n_places = places_in(reg.text)
        if not n_places:
            continue
        # every row whose values appear in this callout line shares its multiplier
        # (one line can carry a counterbore diameter row and a depth row)
        owners = [row_by_cn[cn] for cn in reg.covered_by if cn in row_by_cn]
        if not owners:
            continue
        sharing = [r for r in owners if decimals(r.requirement) & decimals(reg.text)]
        for row in sharing or [max(owners, key=lambda r: text_similarity(r.requirement, reg.text))]:
            if row.char_no in seen_rows:
                continue
            recorded = row.results_count if row.results_count is not None else row.places
            if recorded < n_places:
                seen_rows.add(row.char_no)
                places_flags.append({"region": reg, "row": row, "n": n_places, "recorded": recorded})
    _stage(run_id, "places", "done", count=f"{len(places_flags)} shortfall", ms=_ms(t0))

    # scoring, flags ------------------------------------------------------------
    flags = []
    no_bbox = not boc.has_bbox
    for c in candidates:
        g, v = c["group"], c["verdict"]
        poly, box = _group_outline(g)
        near, dist = nearest_row(checked_rows, g[0].sheet, box, aspect)
        locate_factor = 0.8 if (no_bbox or (near is not None and near.bbox_source == "text_match")) else 1.0
        llm = c.get("llm")
        rule_ids = list(v.rule_ids)
        title, reason, kind = v.title, v.reason, v.kind
        if v.outcome == "ambiguous":
            if llm is not None:
                if not llm.is_requirement:
                    llm_stats["model_rejected"] += 1
                    continue
                score = v.base * LLM_FACTOR[llm.certainty] * locate_factor
                basis = "model judged"
                reason = f"Looks like a requirement: {llm.reason.rstrip('.')}. No row covers it."
                kind = llm.kind if llm.kind != "none" else kind
                title = {"dimension": "Dimension, no row", "gdt": "Geometric tolerance, no characteristic",
                         "note": "Note, no characteristic", "spec": "Specification reference, no row"}.get(kind, title)
            else:
                score = v.base * 0.85 * locate_factor
                basis = "model check skipped"
                reason = v.reason + " The model check did not run, so review it."
        else:
            score = v.base * locate_factor
            basis = {"R1": "tolerance pattern", "R2": "limit pattern", "R3": "size callout pattern",
                     "R4": "frame pattern", "R5": "note pattern", "R6": "spec pattern",
                     "R7": "finish pattern"}.get(rule_ids[0], "rule")
        if score < FLAG_THRESHOLD:
            continue
        flags.append(_flag_doc("unaccounted", g[0].sheet, box, poly, g[0].angle, c["text"], None, None,
                               near, dist, rule_ids, llm, title, reason, score, basis, kind, [x.region_id for x in g]))

    for p in places_flags:
        reg, row = p["region"], p["row"]
        near_factor = 0.8 if row.bbox_source == "text_match" else 1.0
        score = 0.9 * near_factor
        what = "results" if row.results_count is not None else "Places"
        flags.append(_flag_doc(
            "places", reg.sheet, reg.bbox, [tuple(x) for x in reg.poly], reg.angle, reg.text, row.char_no,
            f"CN {row.char_no}: {row.requirement}, {what} = {p['recorded']}", row, 0.0, ["P1"], None,
            f"{p['n']}X callout, {what} {p['recorded']}",
            f"The callout says {p['n']} places; row CN {row.char_no} records {p['recorded']}.",
            score, "nX parsed", row.char_type, [reg.region_id],
        ))

    flags = _merge_overlapping(flags)
    flags.sort(key=lambda f: (f["sheet"], round(f["bbox"]["y"] * 12), f["bbox"]["x"]))
    counters = {"unaccounted": 0, "places": 0}
    for f in flags:
        counters[f["class"]] += 1
        f["flag_id"] = f"{'U' if f['class'] == 'unaccounted' else 'P'}-{counters[f['class']]}"
        f["crop_url"] = f"/runs/{run_id}/flags/{f['flag_id']}/crop.png"
        f["run_id"] = run_id

    # display text: the model transcribes each flagged crop when it is available; otherwise tidied OCR
    for f in flags:
        f["drawing_text"] = tidy_ocr_text(f["drawing_text"])
    if flags and opts.use_llm:
        crops = [crop_with_outline(work[f["sheet"]], [tuple(p) for p in f["poly"]], Box.from_dict(f["bbox"]))
                 for f in flags]
        tr = transcribe_crops(crops, opts.credential, opts.model, opts.provider)
        for i, f in enumerate(flags):
            if tr.texts.get(i):
                f["transcription"] = tr.texts[i]
        llm_stats["llm_calls"] += tr.calls
        llm_stats["llm_input_tokens"] += tr.input_tokens
        llm_stats["llm_output_tokens"] += tr.output_tokens
        llm_stats["cost_usd"] = cost_usd(opts.model, llm_stats["llm_input_tokens"], llm_stats["llm_output_tokens"])
        llm_stats["transcription_error"] = tr.error

    checked = [r for r in regions if not r.excluded_by]
    covered_regions = [r for r in regions if r.covered_by]
    stats = {
        "text_regions": len(checked),
        "all_regions": len(regions),
        "covered": len(covered_regions),
        "excluded": n_excl,
        "rows_located": located,
        "rows_relocated_by_text": relocated,
        "flags": len(flags),
        "unaccounted": counters["unaccounted"],
        "places": counters["places"],
        **llm_stats,
    }
    warnings = []
    sheets_with_rows = {r.sheet for r in rows}
    for n in sheets:
        if n not in sheets_with_rows:
            warnings.append({"type": "sheet_without_rows", "sheet": n})
    unlocated = [r.char_no for r in checked_rows if r.bbox is None]
    if unlocated:
        warnings.append({"type": "rows_not_located", "char_nos": unlocated})
    if no_bbox:
        warnings.append({"type": "no_bbox"})
    if boc.warnings:
        warnings.append({"type": "boc_rows_skipped", "messages": boc.warnings})

    overlay = {}
    for n in sheets:
        overlay[n] = {
            "covered": [{"poly": r.to_dict()["poly"], "bbox": r.bbox.to_dict(), "text": r.text,
                         "char_nos": r.covered_by} for r in regions if r.sheet == n and r.covered_by],
            "excluded": [{"poly": r.to_dict()["poly"], "bbox": r.bbox.to_dict(), "text": r.text,
                          "rule": r.excluded_by} for r in regions if r.sheet == n and r.excluded_by],
        }
    (rdir / "coverage.json").write_text(json.dumps(overlay), encoding="utf-8")

    def finish(doc):
        doc["status"] = "complete"
        doc["flags"] = flags
        doc["stats"] = stats
        doc["warnings"] = warnings
        doc["rows"] = [r.to_dict() for r in rows]

    store.update_run(run_id, finish)


def _join_fragments(group: list[Region]) -> str:
    """Join OCR fragments left to right, skipping a fragment the previous one already ends with
    (OCR often reads the last frame cell twice)."""
    parts: list[str] = []
    for g in group:
        t = g.text.strip()
        if not t:
            continue
        if parts and parts[-1].replace(" ", "").endswith(t.replace(" ", "")):
            continue
        parts.append(t)
    return " ".join(parts)


def _group_outline(group: list[Region]) -> tuple[list[tuple[float, float]], Box]:
    """Outline around a group of text fragments plus any feature control frame grid they sit in."""
    polys = [list(map(tuple, x.poly)) for x in group] + [list(map(tuple, gp)) for x in group for gp in x.grid_polys]
    poly = union_poly(polys, group[0].angle) if len(polys) > 1 else polys[0]
    return poly, Box.from_points(poly)


def _flag_doc(cls, sheet, box: Box, poly, angle, drawing_text, char_no, extracted, near: Characteristic | None,
              dist, rule_ids, llm, title, reason, score, basis, kind, region_ids) -> dict:
    confidence = "high" if score >= 0.8 else "medium" if score >= 0.6 else "low"
    mm = None
    if near is not None and dist:
        mm = round(dist * 11.0 * 25.4)  # page-width units -> mm on an 11 in wide sheet
    return {
        "flag_id": None,
        "class": cls,
        "sheet": sheet,
        "zone": zone_label(box.center),
        "bbox": box.to_dict(),
        "poly": [[round(p[0], 5), round(p[1], 5)] for p in poly],
        "angle": round(angle, 1),
        "char_no": char_no,
        "nearest_char_no": near.char_no if near is not None else None,
        "nearest_requirement": near.requirement if near is not None else None,
        "nearest_distance_mm": mm,
        "extracted": extracted,
        "drawing_text": drawing_text,
        "rule_ids": rule_ids,
        "llm": llm.model_dump() if llm is not None else None,
        "kind": kind,
        "title": title,
        "reason": reason,
        "score": round(score, 3),
        "confidence": confidence,
        "confidence_basis": basis,
        "disposition": "open",
        "disposition_note": None,
        "disposed_at": None,
        "region_ids": region_ids,
    }


def _merge_overlapping(flags: list[dict]) -> list[dict]:
    """One flag per requirement: drop an unaccounted flag that overlaps a stronger one by > 30%."""
    out: list[dict] = []
    for f in sorted(flags, key=lambda x: -x["score"]):
        fb = Box.from_dict(f["bbox"])
        if any(o["sheet"] == f["sheet"] and o["class"] == f["class"] and
               (iou(fb, Box.from_dict(o["bbox"])) > 0.3) for o in out):
            continue
        out.append(f)
    return out


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)
