"""Orchestrates one check: drawing + BoC in, flags out. Reports stage progress to the store."""
from __future__ import annotations

import concurrent.futures
import json
import os
import threading
import time
import traceback
from dataclasses import dataclass, field

import cv2
import numpy as np

from .. import store
from ..pipeline.boc import Characteristic, parse_boc
from .classify import classify_text, places_in, tidy_ocr_text
from .coverage import (decimals, locate_rows_without_bbox, match_score, nearest_row, numbers_compatible,
                       subtract_coverage, text_similarity)
from .exclusions import DEFAULT_EXCLUSIONS, apply_exclusions, pick_template
from .geometry import Box, iou, union_box, union_poly, zone_label
from .llm import (DEFAULT_MODEL, DEFAULT_PROVIDER, classify_crops, cost_usd, crop_with_outline, resolve_credential,
                  transcribe_crops)
from .trace import RunTrace
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
# Investigator (bounded tool loop) for ambiguous items. Total wall time per run is capped so a run fits a
# serverless request; items it can't finish fall back to the single-shot check.
INVESTIGATOR_MAX_ITEMS = 15
INVESTIGATOR_WORKERS = 4
INVESTIGATOR_TOTAL_S = float(os.environ.get("TALLYMARK_INVESTIGATOR_BUDGET_S", "60"))
INVESTIGATOR_FATAL = {"auth_failed", "permission_denied", "model_not_found", "no_credential"}
LINK_MIN = 70  # match_score a claimed covering row's requirement must reach against the item's OCR text


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
    investigator: bool | None = None  # None = on whenever the model check runs with a key


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
        "investigator": _investigator_enabled(opts) if opts.use_llm else None,
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
    tr = RunTrace(run_id, opts, drawing, drawing_name, boc_bytes, boc_name, PIPELINE_VERSION)
    try:
        _execute(run_id, drawing, drawing_name, boc_bytes, boc_name, opts, tr)
    except StageError as e:
        _fail(run_id, e.stage, e.sheet, e.message)
        _save_partial_trace(run_id, tr, {"stage": e.stage, "sheet": e.sheet, "message": e.message})
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        _fail(run_id, None, None, f"{type(e).__name__}: {e}")
        _save_partial_trace(run_id, tr, {"stage": None, "sheet": None, "message": f"{type(e).__name__}: {e}"})


def _save_partial_trace(run_id: str, tr: RunTrace, error: dict) -> None:
    try:
        store.put_trace(run_id, tr.partial(error))
        store.update_run(run_id, lambda d: d.update(trace={"available": True, "partial": True}))
    except Exception:  # noqa: BLE001  the trace never changes how a run ends
        traceback.print_exc()


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


def _execute(run_id, drawing, drawing_name, boc_bytes, boc_name, opts: RunOptions, tr: RunTrace) -> None:
    rdir = store.run_dir(run_id)
    sha = store.sha256(drawing)
    cache = _cache_dir(sha)
    with tr.stage("parse_boc") as st:
        boc = parse_boc(boc_bytes, boc_name)
        st["out"] = {"rows": len(boc.rows), "skipped_warnings": len(boc.warnings)}
    rows = boc.rows

    # 1. render ---------------------------------------------------------------
    t0 = time.perf_counter()
    _stage(run_id, "render", "running")
    st_render = tr.stage("render")
    st = st_render.__enter__()
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
            if store.materialize(wpath) and store.materialize(vpath):
                work[n] = cv2.imread(str(wpath))[:, :, ::-1].copy()
            else:
                img = render_page(doc[n - 1], WORK_DPI)
                cv2.imwrite(str(wpath), img[:, :, ::-1])
                cv2.imwrite(str(vpath), render_page(doc[n - 1], VIEW_DPI)[:, :, ::-1])
                store.persist(wpath)
                store.persist(vpath)
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
    st.update({"in": {"sheets": doc.page_count, "skipped": list(opts.skip_sheets)}, "out": {"rendered": len(sheets)},
               "detail": {"work_dpi": WORK_DPI, "view_dpi": VIEW_DPI, "cache": cache.name}})
    st_render.__exit__(None, None, None)
    tr.set_page(page.width, page.height, template.name, doc.page_count, sheets, boc)

    # 2. text regions -----------------------------------------------------------
    t0 = time.perf_counter()
    _stage(run_id, "text_regions", "running")
    regions: list[Region] = []
    ocr_sheets: list[dict] = []
    st_ocr = tr.stage("ocr", sheets=ocr_sheets)
    st_ocr.__enter__()
    for n in sheets:
        rpath = cache / f"sheet_{n}_regions.json"
        ts = time.perf_counter()
        if store.materialize(rpath):
            regions += [Region.from_dict(r) for r in json.loads(rpath.read_text(encoding="utf-8"))]
            ocr_sheets.append(_sheet_read(n, True, ts, [r for r in regions if r.sheet == n]))
            continue
        _stage(run_id, "text_regions", "running", detail=f"sheet {n}")
        try:
            sheet_regions = merge_regions(
                ocr_regions(work[n], n), text_layer_regions(doc[n - 1], n), detect_fcf_grids(work[n]), n
            )
        except Exception as exc:  # noqa: BLE001
            raise StageError("text_regions", n, f"The page may be a low-resolution scan ({exc}).") from exc
        store.write_file(rpath, json.dumps([r.to_dict() for r in sheet_regions]))
        regions += sheet_regions
        ocr_sheets.append(_sheet_read(n, False, ts, sheet_regions))
    _stage(run_id, "text_regions", "done", count=f"{len(regions)} found", ms=_ms(t0))
    st_ocr.__exit__(None, None, None)
    tr.stages[-1].update({"in": {"sheets": len(sheets)}, "out": {"regions": len(regions)}})

    # 3. match rows -------------------------------------------------------------
    t0 = time.perf_counter()
    _stage(run_id, "match", "running")
    checked_rows = [r for r in rows if r.sheet in sheets]
    with tr.stage("match") as st:
        relocated = locate_rows_without_bbox(checked_rows, regions, tr.placements, aspect)
        subtract_coverage(checked_rows, regions, aspect, tr.coverage)
        located = sum(1 for r in checked_rows if r.bbox is not None)
        st["in"] = {"rows": len(checked_rows), "regions": len(regions)}
        st["out"] = {"rows_located": located, "rows_relocated_by_text": relocated,
                     "regions_covered": sum(1 for r in regions if r.covered_by)}
    _stage(run_id, "match", "done", count=f"{located} of {len(checked_rows)} rows", ms=_ms(t0))

    # 4. exclusions -------------------------------------------------------------
    t0 = time.perf_counter()
    _stage(run_id, "exclusions", "running")
    with tr.stage("exclusions") as st:
        n_excl = apply_exclusions(regions, template, opts.exclusions, opts.extra_regex)
        st["in"] = {"uncovered_regions": sum(1 for r in regions if not r.covered_by)}
        st["out"] = {"excluded": n_excl}
        st["detail"] = {"template": template.name, "enabled": list(opts.exclusions)}
    _stage(run_id, "exclusions", "done", count=f"{n_excl} hidden", ms=_ms(t0))

    # 5. classify leftovers -----------------------------------------------------
    t0 = time.perf_counter()
    _stage(run_id, "classify", "running")
    leftovers = [r for r in regions if not r.covered_by and not r.excluded_by]
    candidates = []
    with tr.stage("rules") as st:
        for n in sheets:
            for group in group_line_fragments([r for r in leftovers if r.sheet == n], aspect):
                text = _join_fragments(group)
                verdict = classify_text(text, max(g.fcf_cells for g in group))
                gid = tr.add_group(group, text, verdict)
                if verdict.outcome == "not_requirement":
                    continue
                candidates.append({"group": group, "text": text, "verdict": verdict, "gid": gid})
        st["in"] = {"leftover_regions": len(leftovers), "groups": len(tr.groups)}
        st["out"] = {o: sum(1 for g in tr.groups if g["outcome"] == o)
                     for o in ("requirement", "ambiguous", "not_requirement")}

    ambiguous = [c for c in candidates if c["verdict"].outcome == "ambiguous"]
    llm_stats = {"llm_calls": 0, "llm_input_tokens": 0, "llm_output_tokens": 0, "cost_usd": 0.0,
                 "llm_error": None, "ambiguous": len(ambiguous), "model_rejected": 0}
    st_model = tr.stage("model_check")
    st = st_model.__enter__()
    st["in"] = {"ambiguous_groups": len(ambiguous)}
    if ambiguous and opts.use_llm:
        _stage(run_id, "classify", "running", detail=f"model on {len(ambiguous)} crops")
        crops = []
        for c in ambiguous:
            g = c["group"]
            poly, box = _group_outline(g)
            crops.append(crop_with_outline(work[g[0].sheet], poly, box))
        refs = [{"group_id": c["gid"], "region_ids": [x.region_id for x in c["group"]]} for c in ambiguous]
        for c in ambiguous:
            c["llm"] = None
        investigations: dict[int, tuple] = {}
        fatal = None
        if _investigator_enabled(opts):
            _stage(run_id, "classify", "running",
                   detail=f"investigating {min(len(ambiguous), INVESTIGATOR_MAX_ITEMS)} items")
            done, fatal, inv_info = _investigate_all(ambiguous, work, rows, regions, page, opts)
            for i, (ctx, ires) in sorted(done.items()):
                llm_stats["llm_calls"] += ires.calls
                llm_stats["llm_input_tokens"] += ires.input_tokens
                llm_stats["llm_output_tokens"] += ires.output_tokens
                if ires.verdict is not None:
                    ambiguous[i]["llm"] = ires.verdict
                investigations[i] = (ires, tr.add_investigation(ires, ctx, crops[i], refs[i]))
            llm_stats["investigator"] = inv_info
        # single shot for everything the investigator did not settle (all of it when the investigator is off)
        pending = [i for i, c in enumerate(ambiguous) if c["llm"] is None]
        raw_calls: list[dict] = []
        call_ids: list[str] = []
        res = None
        if pending and not fatal:
            sub = [crops[i] for i in pending]
            res = classify_crops(sub, opts.credential, opts.model, opts.provider, trace_calls=raw_calls)
            llm_stats["llm_calls"] += res.calls
            llm_stats["llm_input_tokens"] += res.input_tokens
            llm_stats["llm_output_tokens"] += res.output_tokens
            for k, i in enumerate(pending):
                ambiguous[i]["llm"] = res.verdicts.get(k)
            call_ids = tr.add_llm_calls(raw_calls, sub, [refs[i] for i in pending])
        llm_stats["cost_usd"] = cost_usd(opts.model, llm_stats["llm_input_tokens"], llm_stats["llm_output_tokens"])
        llm_stats["llm_error"] = fatal or (res.error if res is not None else None)
        for i, c in enumerate(ambiguous):
            v = c["llm"]
            inv = investigations.get(i)
            rec = {"provider": opts.provider, "model": opts.model}
            if inv is not None and inv[0].verdict is not None:
                ires, inv_id = inv
                rec.update(status="verdict", source="investigator", call_id=inv_id, crop_index=0,
                           verdict=v.model_dump(), error=None, steps=ires.steps, evidence=ires.evidence)
            else:
                k = pending.index(i) if i in pending else None
                call = None
                if k is not None:
                    call = next((tr.llm_calls[[x["call_id"] for x in tr.llm_calls].index(cid)]
                                 for cid, rc in zip(call_ids, raw_calls)
                                 if rc["crop_offset"] <= k < rc["crop_offset"] + rc["n_crops"]), None)
                rec.update(status="verdict" if v is not None else ("no_verdict" if call is not None else "not_sent"),
                           source="single_shot", call_id=call["call_id"] if call else None,
                           crop_index=k - call["crop_offset"] if call else None,
                           verdict=v.model_dump() if v is not None else None,
                           error=None if v is not None else ((call or {}).get("error") or fatal or (res.error if res else None)))
                if inv is not None:
                    rec["investigation"] = {"call_id": inv[1], "error": inv[0].error, "steps": inv[0].steps}
            claim = [str(x) for x in (getattr(v, "covered_by", None) or [])]
            if v is not None and v.is_requirement and claim:
                c["link"] = _check_link(claim, c["text"], c["group"][0].sheet, rows)
                rec["link"] = c["link"]
            tr.note_model(c["gid"], rec)
        st["out"] = {"calls": len(call_ids) + len(investigations), "investigated": len(investigations),
                     "answered": sum(1 for c in ambiguous if c["llm"] is not None),
                     "said_not_requirement": sum(1 for c in ambiguous if c["llm"] is not None and not c["llm"].is_requirement),
                     "linked_to_rows": sum(1 for c in ambiguous if (c.get("link") or {}).get("ok"))}
        st["detail"] = {"error": llm_stats["llm_error"]}
    elif ambiguous:
        llm_stats["llm_error"] = "disabled"
    if ambiguous and not opts.use_llm:
        for c in ambiguous:
            tr.note_model(c["gid"], {"status": "not_sent", "call_id": None, "crop_index": None, "provider": None,
                                     "model": None, "verdict": None, "error": "disabled"})
        st["status_override"] = "skipped"
        st["detail"] = {"error": "disabled"}
    elif not ambiguous:
        st["status_override"] = "skipped"
    st_model.__exit__(None, None, None)
    _stage(run_id, "classify", "done", count=f"{len(candidates)} candidates", ms=_ms(t0))

    # 6. places ----------------------------------------------------------------
    t0 = time.perf_counter()
    _stage(run_id, "places", "running")
    st_places = tr.stage("places")
    st = st_places.__enter__()
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
                tr.note_places({"region_id": reg.region_id, "char_no": row.char_no, "callout_places": n_places,
                                "recorded": None, "shortfall": None, "note": "row already flagged by another callout"})
                continue
            recorded = row.results_count if row.results_count is not None else row.places
            if recorded < n_places:
                seen_rows.add(row.char_no)
                places_flags.append({"region": reg, "row": row, "n": n_places, "recorded": recorded})
            else:
                tr.note_places({"region_id": reg.region_id, "char_no": row.char_no, "callout_places": n_places,
                                "recorded": recorded, "recorded_field": "results_count" if row.results_count is not None
                                else "places", "shortfall": False})
    _stage(run_id, "places", "done", count=f"{len(places_flags)} shortfall", ms=_ms(t0))
    st.update({"in": {"covered_regions": sum(1 for r in regions if r.covered_by)},
               "out": {"checks": len(tr.places_checks) + len(places_flags), "shortfalls": len(places_flags)}})
    st_places.__exit__(None, None, None)

    # scoring, flags ------------------------------------------------------------
    st_score = tr.stage("scoring")
    st = st_score.__enter__()
    flags = []
    linked = []  # ambiguous items the investigator tied to an existing row, and the check agreed
    no_bbox = not boc.has_bbox
    for c in candidates:
        g, v = c["group"], c["verdict"]
        poly, box = _group_outline(g)
        near, dist = nearest_row(checked_rows, g[0].sheet, box, aspect)
        locate_factor = 0.8 if (no_bbox or (near is not None and near.bbox_source == "text_match")) else 1.0
        llm = c.get("llm")
        rule_ids = list(v.rule_ids)
        title, reason, kind = v.title, v.reason, v.kind
        srec = {"base": v.base, "locate_factor": locate_factor,
                "locate_reason": "BoC has no locations" if no_bbox else
                ("nearest row was placed by text" if locate_factor < 1 else "row locations from the BoC"),
                "nearest_char_no": near.char_no if near is not None else None,
                "llm_factor": 1.0, "llm_reason": "a rule decided", "threshold": FLAG_THRESHOLD}
        if v.outcome == "ambiguous":
            link = c.get("link")
            if llm is not None and link and link["ok"]:
                tr.note_score(c["gid"], {**srec, "llm_factor": None, "product": None, "band": "none",
                                         "llm_reason": f"AI linked it to CN {', '.join(link['char_nos'])}",
                                         "basis": "model judged", "result": "covered_by_investigator"}, None)
                linked.append(c)
                continue
            if llm is not None:
                if not llm.is_requirement:
                    llm_stats["model_rejected"] += 1
                    tr.note_score(c["gid"], {**srec, "llm_factor": None, "llm_reason": "AI said not a requirement",
                                             "product": None, "band": "none", "basis": "model judged",
                                             "result": "dropped_by_model"}, None)
                    continue
                srec.update(llm_factor=LLM_FACTOR[llm.certainty], llm_reason=f"AI certainty {llm.certainty}")
                score = v.base * LLM_FACTOR[llm.certainty] * locate_factor
                basis = "model judged"
                n_tools = sum(1 for x in (c.get("investigation_steps") or []) if x.get("tool") != "submit_verdict")
                if n_tools > 1:
                    basis = f"model judged after {n_tools} tool steps"
                reason = f"Looks like a requirement: {llm.reason.rstrip('.')}. No row covers it."
                if link and not link["ok"]:
                    reason = f"{link['note']} {reason}"
                kind = llm.kind if llm.kind != "none" else kind
                title = {"dimension": "Dimension, no row", "gdt": "Geometric tolerance, no characteristic",
                         "note": "Note, no characteristic", "spec": "Specification reference, no row"}.get(kind, title)
            else:
                srec.update(llm_factor=0.85, llm_reason="AI check did not run or answer")
                score = v.base * 0.85 * locate_factor
                basis = "model check skipped"
                reason = v.reason + " The model check did not run, so review it."
        else:
            score = v.base * locate_factor
            basis = {"R1": "tolerance pattern", "R2": "limit pattern", "R3": "size callout pattern",
                     "R4": "frame pattern", "R5": "note pattern", "R6": "spec pattern",
                     "R7": "finish pattern"}.get(rule_ids[0], "rule")
        srec.update(product=score, basis=basis)
        if score < FLAG_THRESHOLD:
            tr.note_score(c["gid"], {**srec, "band": "none", "result": "below_threshold"}, None)
            continue
        flags.append(_flag_doc("unaccounted", g[0].sheet, box, poly, g[0].angle, c["text"], None, None,
                               near, dist, rule_ids, llm, title, reason, score, basis, kind, [x.region_id for x in g]))
        tr.note_score(c["gid"], {**srec, "band": flags[-1]["confidence"], "result": "flagged"}, flags[-1])

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
        tr.note_places({"region_id": reg.region_id, "char_no": row.char_no, "callout_places": p["n"],
                        "recorded": p["recorded"], "recorded_field": "results_count" if row.results_count is not None
                        else "places", "shortfall": True,
                        "score": {"base": 0.9, "locate_factor": near_factor,
                                  "locate_reason": "row placed by text" if near_factor < 1 else "row location from the BoC",
                                  "llm_factor": 1.0, "llm_reason": "a rule decided", "threshold": FLAG_THRESHOLD,
                                  "product": score, "basis": "nX parsed", "band": flags[-1]["confidence"],
                                  "result": "flagged"}}, flags[-1])

    pre_merge = list(flags)
    flags = _merge_overlapping(flags)
    tr.note_merge(pre_merge, flags)
    flags.sort(key=lambda f: (f["sheet"], round(f["bbox"]["y"] * 12), f["bbox"]["x"]))
    counters = {"unaccounted": 0, "places": 0}
    for f in flags:
        counters[f["class"]] += 1
        f["flag_id"] = f"{'U' if f['class'] == 'unaccounted' else 'P'}-{counters[f['class']]}"
        f["crop_url"] = f"/runs/{run_id}/flags/{f['flag_id']}/crop.png"
        f["run_id"] = run_id
    st.update({"in": {"candidates": len(candidates), "places_shortfalls": len(places_flags)},
               "out": {"flags": len(flags), "merged_away": len(pre_merge) - len(flags)}})
    st_score.__exit__(None, None, None)

    # display text: the model transcribes each flagged crop when it is available; otherwise tidied OCR
    for f in flags:
        f["drawing_text"] = tidy_ocr_text(f["drawing_text"])
    st_tr = tr.stage("transcription")
    st = st_tr.__enter__()
    if flags and opts.use_llm:
        crops = [crop_with_outline(work[f["sheet"]], [tuple(p) for p in f["poly"]], Box.from_dict(f["bbox"]))
                 for f in flags]
        raw_calls = []
        tres = transcribe_crops(crops, opts.credential, opts.model, opts.provider, trace_calls=raw_calls)
        for i, f in enumerate(flags):
            if tres.texts.get(i):
                f["transcription"] = tres.texts[i]
        llm_stats["llm_calls"] += tres.calls
        llm_stats["llm_input_tokens"] += tres.input_tokens
        llm_stats["llm_output_tokens"] += tres.output_tokens
        llm_stats["cost_usd"] = cost_usd(opts.model, llm_stats["llm_input_tokens"], llm_stats["llm_output_tokens"])
        llm_stats["transcription_error"] = tres.error
        tr.add_llm_calls(raw_calls, crops, [{"flag_id": f["flag_id"], "region_ids": f["region_ids"], "_flag": f}
                                            for f in flags])
        st.update({"in": {"flags": len(flags)}, "out": {"transcribed": len(tres.texts)}, "detail": {"error": tres.error}})
    else:
        st.update({"in": {"flags": len(flags)}, "status_override": "skipped",
                   "detail": {"error": "disabled" if not opts.use_llm else None}})
    st_tr.__exit__(None, None, None)

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
    if linked:
        stats["linked_by_ai"] = len(linked)
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
            # linked by the AI investigator to an existing row (not flagged); the UI should mark these "check"
            "linked": [{"poly": [[round(p[0], 5), round(p[1], 5)] for p in _group_outline(c["group"])[0]],
                        "bbox": _group_outline(c["group"])[1].to_dict(), "text": c["text"],
                        "char_nos": c["link"]["char_nos"], "group_id": c["gid"],
                        "region_ids": [x.region_id for x in c["group"]], "marker": "linked_by_ai"}
                       for c in linked if c["group"][0].sheet == n],
        }
    store.write_file(rdir / "coverage.json", json.dumps(overlay))

    trace_ref = {"available": False}
    try:
        trace = tr.finalize(regions, rows, checked_rows, flags, template, opts, aspect, llm_stats)
        store.put_trace(run_id, trace)
        trace_ref = {"available": True, "partial": False, "url": f"/runs/{run_id}/trace",
                     "funnel": {s["key"]: s["count"] for s in trace["funnel"]}}
    except Exception as exc:  # noqa: BLE001  the trace never changes a run's outcome
        traceback.print_exc()
        trace_ref = {"available": False, "error": f"{type(exc).__name__}: {exc}"}

    def finish(doc):
        doc["status"] = "complete"
        doc["flags"] = flags
        doc["stats"] = stats
        doc["warnings"] = warnings
        doc["rows"] = [r.to_dict() for r in rows]
        doc["trace"] = trace_ref

    store.update_run(run_id, finish)


def _sheet_read(n: int, cache_hit: bool, t0: float, regs: list[Region]) -> dict:
    """Per-sheet text reading record for the trace (counts by where each region came from)."""
    origins: dict[str, int] = {}
    for r in regs:
        tag = r.region_id.split("-", 1)[1][:1]
        key = {"o": "ocr", "r": "ocr_rotated", "t": "text_layer", "g": "fcf_grid"}.get(tag, "ocr")
        origins[key] = origins.get(key, 0) + 1
    return {"sheet": n, "cache_hit": cache_hit, "ms": _ms(t0), "regions": len(regs), "by_origin": origins,
            "with_text_layer": sum(1 for r in regs if "text_layer" in r.sources),
            "with_frame": sum(1 for r in regs if r.fcf_cells)}


def _investigator_enabled(opts: RunOptions) -> bool:
    """On by default when the model check runs and a key is available; an explicit False turns it off."""
    if not opts.use_llm or opts.investigator is False:
        return False
    return resolve_credential(opts.provider, opts.credential)[0] is not None


def _investigate_all(ambiguous: list[dict], work: dict, rows, regions, page, opts: RunOptions):
    """Run the investigator on up to INVESTIGATOR_MAX_ITEMS ambiguous items, a few at a time, inside a total
    time budget. Returns ({index: (ctx, result)}, fatal_error_code | None, info). Never raises."""
    from .investigate import context_for_group, investigate

    page_mm = (page.width / 72 * 25.4, page.height / 72 * 25.4)
    deadline = time.perf_counter() + INVESTIGATOR_TOTAL_S
    stop = threading.Event()
    fatal: list[str] = []
    done: dict[int, tuple] = {}

    def one(i: int):
        remaining = deadline - time.perf_counter()
        if stop.is_set() or remaining < 5:
            return None
        c = ambiguous[i]
        g = c["group"]
        poly, box = _group_outline(g)
        ctx = context_for_group(c["gid"], g, c["text"], c["verdict"].reason, poly, box, work[g[0].sheet], rows,
                                regions, page_mm, zone_label(box.center))
        res = investigate(ctx, opts.provider, opts.credential, opts.model, timeout_s=min(90.0, remaining))
        if res.error in INVESTIGATOR_FATAL:
            fatal.append(res.error)
            stop.set()
        return ctx, res

    t0 = time.perf_counter()
    targets = list(range(min(len(ambiguous), INVESTIGATOR_MAX_ITEMS)))
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=INVESTIGATOR_WORKERS) as pool:
            for i, out in zip(targets, pool.map(one, targets)):
                if out is not None:
                    done[i] = out
                    ambiguous[i]["investigation_steps"] = out[1].steps
    except Exception:  # noqa: BLE001  the investigator never fails a run; the single shot takes over
        traceback.print_exc()
    info = {"items": len(targets), "investigated": len(done),
            "verdicts": sum(1 for _, r in done.values() if r.verdict is not None),
            "skipped_over_budget": len(targets) - len(done) if not fatal else 0,
            "not_investigated": max(0, len(ambiguous) - len(targets)),
            "ms": int((time.perf_counter() - t0) * 1000), "budget_s": INVESTIGATOR_TOTAL_S,
            "errors": sorted({r.error for _, r in done.values() if r.error})}
    return done, (fatal[0] if fatal else None), info


def _check_link(claim: list[str], text: str, sheet: int, rows: list[Characteristic]) -> dict:
    """Check the investigator's claim that BoC rows already account for this text: the row must exist, be on
    the same sheet, carry compatible values and read like the text (match_score >= LINK_MIN)."""
    by_cn = {r.char_no: r for r in rows}
    checks = []
    for cn in claim:
        row = by_cn.get(cn)
        if row is None:
            checks.append({"char_no": cn, "ok": False, "why": "no such row in the BoC"})
            continue
        compat = numbers_compatible(decimals(row.requirement), text)
        score = round(match_score(row.requirement, text), 1)
        sheet_ok = row.sheet == sheet
        why = None if (sheet_ok and compat and score >= LINK_MIN) else (
            f"row is on sheet {row.sheet}" if not sheet_ok else
            "values differ" if not compat else f"text only {score:.0f}% alike (needs {LINK_MIN}%)")
        checks.append({"char_no": cn, "requirement": row.requirement, "sheet": row.sheet, "sheet_ok": sheet_ok,
                       "numbers_compatible": compat, "text_score": score, "threshold": LINK_MIN,
                       "ok": why is None, "why": why})
    ok = [x["char_no"] for x in checks if x["ok"]]
    note = None
    if not ok:
        reasons = "; ".join(f"CN {x['char_no']}: {x['why']}" for x in checks)
        note = f"Model thinks CN {', '.join(claim)} covers this; text didn't match ({reasons})."
    return {"claimed": claim, "checks": checks, "ok": bool(ok), "char_nos": ok, "note": note}


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
