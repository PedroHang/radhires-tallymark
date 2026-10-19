"""Decision trace: a per-run audit trail of everything the pipeline read, measured and decided.

Recording is passive. Nothing here feeds back into a decision: the pipeline computes exactly
what it computed before, and this module only writes down the inputs, the measured values,
the rule that fired, every model request verbatim and the score arithmetic. The trace is
stored next to the run (store.put_trace) and served read-only by the /runs/{id}/trace API.
Schema and vocabulary are documented in docs/TRACE.md.
"""
from __future__ import annotations

import datetime as dt
import io
import platform
import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from .. import store
from .boc import normalize_text
from .classify import FCF_TEXT, LETTERS, R1, R2, R3, R5_NOTE, R5_WORDS, R6_SPEC, R7_FINISH, R8_BARE
from .coverage import MATCH_MIN, PAD, nearest_row
from .exclusions import EXCLUSION_LABELS, RE_BARE_NUMBER, RE_REFERENCE, RE_SCALE, RE_VIEW_LABEL

SCHEMA_VERSION = "1"
CATALOG_VERSION = "2026-10-19.1"
FLAG_THRESHOLD = 0.5  # mirrors run.FLAG_THRESHOLD (asserted equal in tests)
ROOT = Path(__file__).resolve().parent.parent.parent


def _e(label: str, explain: str, **extra) -> dict:
    return {"label": label, "explain": explain, **extra}


# --------------------------------------------------------------------------- plain-language catalog
CATALOG = {
    "version": CATALOG_VERSION,
    "rules": {
        "R1": _e("Toleranced dimension", "A number with a plus/minus tolerance, like .500 ±.008.", outcome="requirement", base=0.9),
        "R2": _e("Limit dimension", "An upper and a lower limit on one line, like 1.248-1.252.", outcome="requirement", base=0.9),
        "R3": _e("Size callout", "A diameter, radius or thread callout with a number, like Ø.250 or M8.", outcome="requirement", base=0.9),
        "R4": _e("Feature control frame", "A tolerance frame: a box with a tolerance value and datum letters.", outcome="requirement", base=0.9),
        "R5": _e("Numbered note", "A numbered note that imposes a requirement, like 5. BREAK EDGES .010 MAX.", outcome="requirement", base=0.9),
        "R6": _e("Specification reference", "A material or process specification is called out (AMS, ASTM, MIL, SAE, ISO).", outcome="requirement", base=0.9),
        "R7": _e("Surface finish", "Looks like a surface finish value, like 63 Ra. Starts at Medium confidence.", outcome="requirement", base=0.6),
        "R8": _e("Ambiguous number", "A number with no tolerance and no frame. It may be a requirement or a label, so the AI check is asked.", outcome="ambiguous", base=0.6),
        "R9": _e("Not used", "Reserved. No check in this version produces R9.", outcome=None, base=None),
        "R10": _e("Label or prose", "Mostly words, or no numbers, and no requirement pattern. Not a requirement.", outcome="not_requirement", base=0.0),
        "R11": _e("Short label", "Under 3 characters, like a datum letter or an axis label. Not a requirement.", outcome="not_requirement", base=0.0),
        "P1": _e("Places shortfall", "A callout says nX (for example 4X) but its BoC row records fewer places or results.", outcome="requirement", base=0.9),
    },
    # the individual checks behind the rules, in the order they are tried (first match decides)
    "rule_checks": {
        "R4_frame_grid": _e("Inside a tolerance frame", "Sits in a detected frame of 3 or more cells and has a value, datum letters, or no text at all.", rule="R4"),
        "R1_tolerance": _e("Plus/minus tolerance", "A number followed by a ± (or +/-) tolerance.", rule="R1"),
        "R4_frame_text": _e("Frame text", "A tolerance value followed by 1 to 3 datum letters, as OCR reads a frame.", rule="R4"),
        "R2_limits": _e("Limit pair", "Two decimal values joined by a dash.", rule="R2"),
        "R6_spec": _e("Specification", "Names a standard: AMS, ASTM, MIL-, SAE or ISO.", rule="R6"),
        "R5_note": _e("Numbered note", "Starts like '5. TEXT' and has a value or a requirement word (BREAK, MAX, PER...).", rule="R5"),
        "R3_size": _e("Size callout", "A diameter, radius, spherical or thread symbol with a number.", rule="R3"),
        "R7_finish": _e("Surface finish", "A 2-3 digit number followed by Ra, µ or MICRO.", rule="R7"),
        "R11_short": _e("Too short", "Fewer than 3 characters once spaces and symbols are folded.", rule="R11"),
        "R8_bare_number": _e("Bare number", "Only a number, nothing else.", rule="R8"),
        "R10_prose": _e("Mostly words", "At least 4 letters and twice as many letters as digits, or a long run of capitals.", rule="R10"),
        "R8_has_digits": _e("Numbers, no pattern", "Contains digits but matched none of the patterns above.", rule="R8"),
        "R10_fallback": _e("No numbers, no pattern", "Nothing above matched and there are no digits.", rule="R10"),
    },
    "check_status": {
        "fired": _e("Decided", "The first check that matched. It decides the outcome."),
        "matched_after": _e("Also matched", "Matched too, but an earlier check had already decided."),
        "no_match": _e("No match", "This check did not match."),
    },
    "exclusions": {
        **{k: _e(v, {
            "title_block": "Its centre is inside the title block zone of the sheet template.",
            "border": "Its centre is in the border strip or a zone label margin of the sheet template.",
            "revision_block": "Its centre is inside the revision block zone of the sheet template.",
            "view_labels": "Starts with SECTION, DETAIL or VIEW.",
            "reference_dims": "The whole text is in parentheses, which marks a reference dimension.",
            "scale_notes": "Mentions SCALE, SHEET n OF n or VIEW ROTATED.",
            "basic_dims": "A bare number inside a single box: a basic dimension, defined by a frame elsewhere.",
        }[k]) for k, v in EXCLUSION_LABELS.items()},
        "custom_regex": _e("Custom pattern", "Matched the extra regular expression set for this run."),
    },
    "coverage_tests": {
        "location_gate": _e("Near the row", "At least half the text box lies inside the row's box (padded about 5 pt), or its centre does."),
        "text_match": _e("Text matches", "Values agree and the text is at least 60% similar to the row's requirement."),
        "tight_overlap": _e("Tight overlap", "Values agree, the text box is 90% inside the row's box and at least 40% similar."),
        "blank_in_zone": _e("Blank region in the row's box", "No text was read, and it sits inside the row's box."),
        "blank_frame_gdt": _e("Blank frame", "A tolerance frame with no readable text inside a GD&T row's box."),
        "row_best_region": _e("Row's best match", "The row covers the best region at its location even if OCR garbled the text (half inside, at least 30% similar)."),
        "relocated_by_text": _e("Row placed by text", "The BoC row had no location, so it was placed on the region whose text matched best (score >= 80)."),
    },
    "sources": {
        "ocr": _e("OCR", "Read by the OCR engine (detection, angle classification, recognition) on the upright sheet."),
        "ocr_rotated": _e("OCR, rotated pass", "Read by a second OCR pass on the sheet turned ±30°, for slanted callouts."),
        "text_layer": _e("PDF text", "Taken from the PDF's own text layer."),
        "fcf_grid": _e("Frame detector", "A tolerance frame found by shape (a row of small boxes), even with no readable text."),
    },
    "outcomes": {
        "covered": _e("Covered", "A BoC row accounts for this text."),
        "excluded": _e("Excluded", "Hidden by an exclusion (title block, labels, reference dimensions...)."),
        "not_requirement": _e("Not a requirement", "A rule decided this text is a label or prose."),
        "dropped_by_model": _e("Dropped by AI", "Ambiguous, and the AI check said it is not a requirement."),
        "below_threshold": _e("Below threshold", "Looked like a requirement but scored under 0.5, so it was not flagged."),
        "merged": _e("Merged", "Overlaps a stronger flag, so it is shown as part of that flag."),
        "flagged_unaccounted": _e("Flagged: no row", "A requirement no BoC row covers (a U flag)."),
        "flagged_places": _e("Flagged: places", "Covered, but the callout's nX count is more than the row records (a P flag)."),
    },
    "bands": {
        "high": _e("High", "Score 0.8 or more.", min=0.8),
        "medium": _e("Medium", "Score from 0.6 to under 0.8.", min=0.6),
        "low": _e("Low", "Score from 0.5 to under 0.6.", min=0.5),
        "none": _e("Not flagged", "Score under 0.5.", min=0.0),
    },
    "factors": {
        "base": _e("Rule strength", "0.9 for a clear pattern, 0.6 for a surface finish or an ambiguous number."),
        "locate_factor": _e("Location certainty", "1.0 when BoC rows carry locations; 0.8 when the BoC has none or the nearest row was placed by text."),
        "llm_factor": _e("AI check", "1.0 / 0.75 / 0.5 for high / medium / low AI certainty; 0.85 when the AI check did not run; 1.0 when a rule decided."),
    },
    "model_status": {
        "verdict": _e("Answered", "The AI returned a verdict for this crop."),
        "no_verdict": _e("No answer", "The request was sent but no verdict came back for this crop."),
        "not_sent": _e("Not sent", "The AI check did not run for this crop."),
    },
    "llm_jobs": {
        "classify": _e("Is it a requirement?", "Ambiguous leftovers only. The answer can drop a candidate."),
        "transcribe": _e("Transcription", "Reads the text of raised flags with proper symbols. Display only, never decides."),
    },
    "llm_errors": {
        "disabled": _e("Off", "The AI check was turned off for this run."),
        "no_credential": _e("No key", "No API key was set for the provider."),
        "auth_failed": _e("Key rejected", "The API key was rejected."),
        "permission_denied": _e("Not allowed", "The key can't use this model."),
        "model_not_found": _e("Unknown model", "The model was not found for this key."),
        "rate_limited": _e("Rate limited", "The API rate limited the request."),
        "connection_failed": _e("No connection", "The API could not be reached."),
        "api_error": _e("API error", "The API returned an error (api_error_<status> carries the HTTP status)."),
        "unexpected": _e("Unexpected error", "Something else failed (unexpected_<ExceptionName>)."),
    },
    "step_kinds": {
        "measured": _e("Measured", "Something read or measured from the drawing or the BoC."),
        "rule": _e("Rule", "A deterministic rule or exclusion."),
        "ai": _e("AI", "A vision-model request."),
        "score": _e("Score", "The confidence arithmetic."),
    },
    "stages": {
        "parse_boc": _e("Reading the BoC", "Parse and validate the Bill of Characteristics."),
        "render": _e("Rendering sheets", "Turn each sheet into an image."),
        "ocr": _e("Reading text", "OCR, PDF text and frame detection on each sheet."),
        "match": _e("Matching to BoC rows", "Decide which text each row covers."),
        "exclusions": _e("Applying exclusions", "Hide sheet furniture and labels."),
        "rules": _e("Classifying leftovers", "Run rules R1-R11 on text no row covers."),
        "model_check": _e("AI check", "Ask the vision model about ambiguous leftovers."),
        "places": _e("Checking repeated features", "Compare nX callouts with the row's places."),
        "scoring": _e("Scoring", "Turn each candidate into a scored flag or drop it."),
        "transcription": _e("Transcribing flags", "AI transcription of flagged text, display only."),
    },
}


def band_for(score: float) -> str:
    """Same thresholds as run._flag_doc, plus 'none' under the flag threshold."""
    if score < FLAG_THRESHOLD:
        return "none"
    return "high" if score >= 0.8 else "medium" if score >= 0.6 else "low"


# --------------------------------------------------------------------------- rule explanation
def explain_rules(text: str, fcf_cells: int = 0) -> list[dict]:
    """Every check classify_text tries, in its order, with whether it matched and what matched.
    The first matched check is the one classify_text applies (asserted in tests)."""
    t = text.strip()
    up = t.upper()
    compact = normalize_text(t)
    letters = len(LETTERS.findall(t))
    digits = sum(ch.isdigit() for ch in t)

    def hit(rx, s, how="search"):
        m = getattr(rx, how)(s)
        return (m.group(0).strip() or "(empty)") if m else None

    fcf_hit = hit(FCF_TEXT, up)
    r5 = hit(R5_NOTE, t)
    r5w = hit(R5_WORDS, up)
    r3 = hit(R3, up)
    prose = re.search(r"[A-Z]{8,}", up)
    checks = [
        ("R4_frame_grid", fcf_cells >= 3 and (not t or fcf_hit or digits),
         f"{fcf_cells}-cell frame" + (f", text '{t}'" if t else ", no text")),
        ("R1_tolerance", hit(R1, up), None),
        ("R4_frame_text", fcf_hit if (fcf_hit and digits) else None, None),
        ("R2_limits", hit(R2, up), None),
        ("R6_spec", hit(R6_SPEC, up), None),
        ("R5_note", (r5 if (digits > 2 or r5w) else None) if r5 else None,
         f"note start '{r5}'" + (f", word {r5w}" if r5w else f", {digits} digits") if r5 else None),
        ("R3_size", r3 if (r3 and digits) else None, None),
        ("R7_finish", hit(R7_FINISH, up), None),
        ("R11_short", len(compact) < 3, f"{len(compact)} characters after folding"),
        ("R8_bare_number", hit(R8_BARE, t, "match"), None),
        ("R10_prose", letters >= max(4, digits * 2) or bool(prose),
         f"{letters} letters, {digits} digits" + (f", capitals run '{prose.group(0)}'" if prose else "")),
        ("R8_has_digits", digits > 0, f"{digits} digits"),
        ("R10_fallback", True, "last resort: always matches"),
    ]
    out, decided = [], False
    for key, matched, evidence in checks:
        ok = bool(matched)
        status = "no_match" if not ok else ("matched_after" if decided else "fired")
        if ok and not decided:
            decided = True
        ev = evidence if evidence is not None else (matched if isinstance(matched, str) else None)
        out.append({"check": key, "rule_id": CATALOG["rule_checks"][key]["rule"], "matched": ok, "status": status,
                    "evidence": ev if ok else None})
    return out


def exclusion_detail(region, template, key: str | None, extra_regex: str | None) -> dict | None:
    """Why an exclusion applied: the template zone that holds the centre, or the pattern that matched."""
    if not key:
        return None
    cx, cy = region.bbox.center
    if key in ("border", "title_block", "revision_block"):
        zone = next((z for z in template.zones.get(key, []) if z.contains((cx, cy))), None)
        return {"template": template.name, "zone": zone.to_dict() if zone else None,
                "center": [round(cx, 4), round(cy, 4)]}
    pattern = {"view_labels": RE_VIEW_LABEL, "scale_notes": RE_SCALE, "reference_dims": RE_REFERENCE,
               "basic_dims": RE_BARE_NUMBER}.get(key)
    if pattern is not None:
        d = {"pattern": pattern.pattern}
        if key == "basic_dims":
            d["fcf_cells"] = region.fcf_cells
        return d
    if key == "custom_regex":
        return {"pattern": extra_regex}
    return None


def _origin(region_id: str) -> str:
    tag = region_id.split("-", 1)[1][:1] if "-" in region_id else ""
    return {"o": "ocr", "r": "ocr_rotated", "t": "text_layer", "g": "fcf_grid"}.get(tag, "ocr")


_GIT: dict | None = None


def _git_info() -> dict:
    global _GIT
    if _GIT is None:
        _GIT = {"commit": None, "dirty": None}
        try:
            out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                                 text=True, timeout=3)
            if out.returncode == 0:
                _GIT["commit"] = out.stdout.strip() or None
                st = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no", "--", "app"],
                                    cwd=ROOT, capture_output=True, text=True, timeout=3)
                _GIT["dirty"] = bool(st.stdout.strip()) if st.returncode == 0 else None
        except Exception:  # noqa: BLE001  no git (e.g. a deployed bundle): versions stay null
            pass
    return _GIT


def _versions() -> dict:
    from importlib import metadata

    v = {"python": platform.python_version()}
    for pkg in ("rapidocr_onnxruntime", "opencv-python", "opencv-python-headless", "pymupdf", "rapidfuzz",
                "anthropic", "openai", "google-genai"):
        try:
            v[pkg] = metadata.version(pkg)
        except Exception:  # noqa: BLE001
            continue
    return v


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def _png(im) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------- recorder
class RunTrace:
    """Collects one run's trace. run.py calls the recording methods as it goes; finalize() builds
    the document. All recording is append-only bookkeeping on values the pipeline already computed."""

    def __init__(self, run_id: str, opts, drawing: bytes, drawing_name: str, boc_bytes: bytes, boc_name: str,
                 pipeline_version: str):
        from .llm import resolve_credential

        self.run_id = run_id
        self.started_at = _now()
        self._t0 = time.perf_counter()
        cred_source = resolve_credential(opts.provider, opts.credential)[1] if opts.use_llm else None
        git = _git_info()
        self.run = {
            "run_id": run_id,
            "started_at": self.started_at,
            "finished_at": None,
            "duration_ms": None,
            "status": "running",
            "error": None,
            "app": {"name": "Tallymark", "pipeline_version": pipeline_version, "git_commit": git["commit"],
                    "git_dirty": git["dirty"], "versions": _versions()},
            "settings": {
                "exclusions": list(opts.exclusions), "extra_regex": opts.extra_regex, "use_llm": opts.use_llm,
                "provider": opts.provider if opts.use_llm else None, "model": opts.model if opts.use_llm else None,
                "credential_source": cred_source,  # 'session' | 'environment' | None; the key itself is never recorded
                "skip_sheets": list(opts.skip_sheets), "simulated_boc": opts.simulated,
                "flag_threshold": FLAG_THRESHOLD, "coverage_pad": PAD, "relocate_min_score": MATCH_MIN,
            },
            "inputs": {
                "drawing": {"filename": drawing_name, "sha256": store.sha256(drawing), "bytes": len(drawing)},
                "boc": {"filename": boc_name, "sha256": store.sha256(boc_bytes), "bytes": len(boc_bytes)},
            },
            "page": None,
        }
        self.stages: list[dict] = []
        self.coverage: dict[str, list[dict]] = {}
        self.placements: list[dict] = []
        self.groups: list[dict] = []
        self._group_of: dict[str, str] = {}
        self.model: dict[str, dict] = {}  # group_id -> model involvement
        self.scores: dict[str, dict] = {}  # group_id -> score record
        self._score_flag: dict[str, dict] = {}  # group_id -> flag dict (not serialized)
        self.places_checks: list[dict] = []
        self._places_flag: list[tuple[dict, dict]] = []  # (check record, flag dict)
        self.llm_calls: list[dict] = []
        self._merged: list[tuple[dict, dict]] = []  # (dropped flag, surviving flag)
        self._transcribed: dict[int, tuple[str, int]] = {}  # id(flag) -> (call_id, crop_index)
        self.page_mm = 279.4

    # ---- stages
    @contextmanager
    def stage(self, key: str, **fields):
        rec = {"key": key, "label": CATALOG["stages"][key]["label"], "started_at": _now(), "ms": None,
               "status": "running", "in": None, "out": None, "detail": None, **fields}
        self.stages.append(rec)
        t0 = time.perf_counter()
        try:
            yield rec
            rec["status"] = rec.get("status_override") or "done"
            rec.pop("status_override", None)
        except BaseException:
            rec["status"] = "error"
            raise
        finally:
            rec["ms"] = int((time.perf_counter() - t0) * 1000)

    def set_page(self, width_pt: float, height_pt: float, template: str, sheets_total: int, sheets_checked: list[int],
                 boc) -> None:
        self.page_mm = width_pt / 72 * 25.4
        self.run["page"] = {"width_pt": width_pt, "height_pt": height_pt, "width_mm": round(self.page_mm, 1),
                            "template": template, "sheets_total": sheets_total, "sheets_checked": sheets_checked}
        self.run["inputs"]["boc"].update(rows=len(boc.rows), columns=boc.columns, has_bbox=boc.has_bbox,
                                         warnings=boc.warnings)

    # ---- classification
    def add_group(self, group, text: str, verdict) -> str:
        gid = f"G{len(self.groups) + 1}"
        fcf = max(g.fcf_cells for g in group)
        self.groups.append({
            "group_id": gid, "sheet": group[0].sheet, "region_ids": [g.region_id for g in group], "text": text,
            "fcf_cells": fcf, "outcome": verdict.outcome, "rule_id": verdict.rule_ids[0],
            "rule_label": CATALOG["rules"][verdict.rule_ids[0]]["label"], "title": verdict.title,
            "reason": verdict.reason, "kind": verdict.kind, "base": verdict.base,
            "checks": explain_rules(text, fcf)})
        for g in group:
            self._group_of[g.region_id] = gid
        return gid

    # ---- model calls
    def add_llm_calls(self, raw_calls: list[dict], crops: list, refs: list[dict]) -> list[str]:
        """Give each request an id, save the crops it sent, and map crop -> what it shows.
        refs[i] describes crops[i] (group_id / flag ref and region ids)."""
        ids = []
        for raw in raw_calls:
            cid = f"L{len(self.llm_calls) + 1}"
            ids.append(cid)
            crop_list = []
            for i in range(raw["n_crops"]):
                gi = raw["crop_offset"] + i
                name = f"llm/{cid}/{i}.png"
                try:
                    store.put_trace_artifact(self.run_id, name, _png(crops[gi]))
                    url = f"/runs/{self.run_id}/trace/llm/{cid}/crops/{i}.png"
                except Exception:  # noqa: BLE001  a crop that can't be saved is reported, never fatal
                    url = None
                ref = {k: v for k, v in refs[gi].items() if not k.startswith("_")}
                crop_list.append({"index": i, "input_index": gi, "url": url, **ref})
                if "_flag" in refs[gi]:
                    self._transcribed[id(refs[gi]["_flag"])] = (cid, i)
            self.llm_calls.append({"call_id": cid, **raw, "crops": crop_list})
        return ids

    def note_model(self, gid: str, rec: dict) -> None:
        self.model[gid] = rec

    # ---- scoring
    def note_score(self, gid: str, rec: dict, flag: dict | None) -> None:
        self.scores[gid] = rec
        if flag is not None:
            self._score_flag[gid] = flag

    def note_places(self, rec: dict, flag: dict | None = None) -> None:
        self.places_checks.append(rec)
        if flag is not None:
            self._places_flag.append((rec, flag))

    def note_merge(self, before: list[dict], after: list[dict]) -> None:
        """Mirror of run._merge_overlapping: for each dropped flag, the surviving flag it overlapped."""
        from .geometry import Box, iou

        kept = {id(f) for f in after}
        for f in before:
            if id(f) in kept:
                continue
            fb = Box.from_dict(f["bbox"])
            winner = next((o for o in after if o["sheet"] == f["sheet"] and o["class"] == f["class"]
                           and iou(fb, Box.from_dict(o["bbox"])) > 0.3), None)
            self._merged.append((f, winner))

    # ---- build
    def finalize(self, regions, rows, checked_rows, flags: list[dict], template, opts, aspect: float,
                 llm_stats: dict) -> dict:
        mm = self.page_mm
        flag_by_obj = {id(f): f for f in flags}
        merged_into = {id(f): w for f, w in self._merged}
        placed_at: dict[str, list[dict]] = {}
        for p in self.placements:
            placed_at.setdefault(p["region_id"], []).append(p)
        groups = {g["group_id"]: g for g in self.groups}
        places_by_region: dict[str, list[dict]] = {}
        for rec in self.places_checks:
            places_by_region.setdefault(rec["region_id"], []).append(rec)
        places_flag_ids: dict[str, list[str]] = {}
        places_merged: dict[str, str | None] = {}
        for rec, f in self._places_flag:
            if id(f) in flag_by_obj:
                rec["flag_id"] = f["flag_id"]
                places_flag_ids.setdefault(rec["region_id"], []).append(f["flag_id"])
            else:
                w = merged_into.get(id(f))
                rec["merged_into"] = w["flag_id"] if w else None
                places_merged[rec["region_id"]] = rec["merged_into"]

        # score records: resolve flag ids now that they are assigned
        for gid, rec in self.scores.items():
            f = self._score_flag.get(gid)
            if f is None:
                continue
            if id(f) in flag_by_obj:
                rec["flag_id"] = f["flag_id"]
            else:
                w = merged_into.get(id(f))
                rec["result"] = "merged"
                rec["merged_into"] = w["flag_id"] if w else None

        region_records = []
        for r in regions:
            rid = r.region_id
            origin = _origin(rid)
            cands = [{**c, "distance_mm": round(c["distance_pw"] * mm, 1)} for c in self.coverage.get(rid, [])]
            near, dist = nearest_row(checked_rows, r.sheet, r.bbox, aspect)
            gid = self._group_of.get(rid)
            g = groups.get(gid) if gid else None
            score = self.scores.get(gid) if gid else None
            # final outcome: exactly one per region
            flag_ids: list[str] = []
            merged_to = None
            if r.covered_by:
                if rid in places_flag_ids:
                    outcome, flag_ids = "flagged_places", places_flag_ids[rid]
                elif rid in places_merged:
                    outcome, merged_to = "merged", places_merged[rid]
                else:
                    outcome = "covered"
            elif r.excluded_by:
                outcome = "excluded"
            elif g is None:  # cannot happen: every leftover is grouped. Kept explicit for the audit.
                outcome = "not_requirement"
            elif g["outcome"] == "not_requirement":
                outcome = "not_requirement"
            else:
                res = score["result"] if score else "below_threshold"
                if res == "flagged":
                    outcome, flag_ids = "flagged_unaccounted", [score["flag_id"]]
                elif res == "merged":
                    outcome, merged_to = "merged", score.get("merged_into")
                else:
                    outcome = res  # dropped_by_model | below_threshold
            if merged_to:
                flag_ids = [merged_to]
            if outcome == "flagged_places":
                chk = next(p for p in places_by_region[rid] if p.get("flag_id") == flag_ids[0])
                score_rec = {**chk["score"], "flag_id": flag_ids[0]}
            else:
                score_rec = score
            region_records.append({
                "region_id": rid, "sheet": r.sheet, "bbox": r.bbox.to_dict(),
                "poly": [[round(p[0], 5), round(p[1], 5)] for p in r.poly], "angle": round(r.angle, 1),
                "text": r.text,
                "ocr_conf": round(r.conf, 4) if origin in ("ocr", "ocr_rotated") else None,
                "origin": origin, "sources": list(r.sources), "fcf_cells": r.fcf_cells,
                "coverage": {
                    "covered": bool(r.covered_by), "covered_by": list(r.covered_by), "candidates": cands,
                    "rows_placed_here": placed_at.get(rid, []),
                    "nearest_row": ({"char_no": near.char_no, "requirement": near.requirement,
                                     "distance_mm": round(dist * mm, 1)} if near is not None else None),
                },
                "exclusion": ({"checked": False, "applied": None, "label": None, "detail": None,
                               "note": "Covered text is never excluded."} if r.covered_by else
                              {"checked": True, "applied": r.excluded_by,
                               "label": CATALOG["exclusions"].get(r.excluded_by, {}).get("label") if r.excluded_by else None,
                               "detail": exclusion_detail(r, template, r.excluded_by, opts.extra_regex)}),
                "group_id": gid,
                "classification": ({k: g[k] for k in ("group_id", "text", "region_ids", "fcf_cells", "outcome",
                                                      "rule_id", "rule_label", "title", "reason", "kind", "base",
                                                      "checks")} if g else None),
                "model": self.model.get(gid) if gid else None,
                "places": places_by_region.get(rid),
                "score": score_rec,
                "final": {"outcome": outcome, "label": CATALOG["outcomes"][outcome]["label"], "flag_ids": flag_ids},
            })

        by_outcome: dict[str, list[str]] = {k: [] for k in CATALOG["outcomes"]}
        for rr in region_records:
            by_outcome[rr["final"]["outcome"]].append(rr["region_id"])
        ids = lambda pred: [rr["region_id"] for rr in region_records if pred(rr)]  # noqa: E731
        leftover = ids(lambda x: not x["coverage"]["covered"] and not x["exclusion"]["applied"])
        cls = lambda o: ids(lambda x: x["classification"] is not None and x["classification"]["outcome"] == o)  # noqa: E731
        sent = ids(lambda x: (x["model"] or {}).get("status") in ("verdict", "no_verdict"))

        def step(key, label, region_ids, parent=None, **extra):
            return {"key": key, "label": label, "parent": parent, "count": len(region_ids), "region_ids": region_ids,
                    **extra}

        u_flags = [f["flag_id"] for f in flags if f["class"] == "unaccounted"]
        p_flags = [f["flag_id"] for f in flags if f["class"] == "places"]
        funnel = [
            step("read", "Text regions read", [rr["region_id"] for rr in region_records]),
            step("covered", "Covered by a BoC row", ids(lambda x: x["coverage"]["covered"]), "read"),
            step("excluded", "Hidden by an exclusion", ids(lambda x: bool(x["exclusion"]["applied"])), "read"),
            step("leftover", "Left over (no row, not excluded)", leftover, "read",
                 groups=len(self.groups)),
            step("not_requirement", "Ruled not a requirement", cls("not_requirement"), "leftover"),
            step("requirement_by_rule", "Ruled a requirement", cls("requirement"), "leftover"),
            step("ambiguous", "Ambiguous (asked the AI)", cls("ambiguous"), "leftover"),
            step("sent_to_model", "Sent to the AI check", sent, "ambiguous"),
            step("dropped_by_model", "Dropped by the AI check", by_outcome["dropped_by_model"], "ambiguous"),
            step("below_threshold", "Scored below 0.5", by_outcome["below_threshold"], "leftover"),
            step("merged", "Merged into a stronger flag", by_outcome["merged"], "leftover"),
            step("flagged_unaccounted", "Flagged: no row (U)", by_outcome["flagged_unaccounted"], "leftover",
                 flag_ids=u_flags),
            step("flagged_places", "Flagged: places shortfall (P)", by_outcome["flagged_places"], "covered",
                 flag_ids=p_flags),
        ]
        f = {s["key"]: s["count"] for s in funnel}
        invariants = {
            "read = covered + excluded + leftover": f["read"] == f["covered"] + f["excluded"] + f["leftover"],
            "leftover = not_requirement + requirement_by_rule + ambiguous":
                f["leftover"] == f["not_requirement"] + f["requirement_by_rule"] + f["ambiguous"],
            "every region has one final outcome": sum(len(v) for v in by_outcome.values()) == f["read"],
        }

        flag_records = {fl["flag_id"]: self._flag_record(fl, region_records, groups, opts) for fl in flags}

        self.run.update(finished_at=_now(), duration_ms=int((time.perf_counter() - self._t0) * 1000), status="complete")
        return {
            "schema_version": SCHEMA_VERSION,
            "catalog_version": CATALOG_VERSION,
            "run": self.run,
            "stages": self.stages,
            "funnel": funnel,
            "outcomes": {k: {"label": CATALOG["outcomes"][k]["label"], "count": len(v), "region_ids": v}
                         for k, v in by_outcome.items()},
            "invariants": invariants,
            "llm": {"calls": len(self.llm_calls), "input_tokens": llm_stats.get("llm_input_tokens", 0),
                    "output_tokens": llm_stats.get("llm_output_tokens", 0), "cost_usd": llm_stats.get("cost_usd"),
                    "classify_error": llm_stats.get("llm_error"),
                    "transcription_error": llm_stats.get("transcription_error")},
            "rows": [{"char_no": r.char_no, "sheet": r.sheet, "requirement": r.requirement, "char_type": r.char_type,
                      "places": r.places, "results_count": r.results_count,
                      "bbox": r.bbox.to_dict() if r.bbox else None, "bbox_source": r.bbox_source,
                      "checked": r in checked_rows, "covered_region_ids": list(r.located_regions)} for r in rows],
            "groups": self.groups,
            "places_checks": self.places_checks,
            "regions": region_records,
            "llm_calls": self.llm_calls,
            "flags": flag_records,
        }

    def _flag_record(self, fl: dict, region_records: list[dict], groups: dict, opts) -> dict:
        regs = [rr for rr in region_records if rr["region_id"] in fl["region_ids"]]
        first = regs[0] if regs else None
        steps: list[dict] = []

        def add(kind, title, detail, data=None):
            steps.append({"n": len(steps) + 1, "kind": kind, "title": title, "detail": detail, "data": data or {}})

        n = len(regs)
        add("measured", "Read the text",
            f"Read \"{fl['drawing_text']}\" on sheet {fl['sheet']}, zone {fl['zone']}"
            + (f", from {n} text pieces." if n > 1 else "."),
            {"regions": [{"region_id": rr["region_id"], "text": rr["text"], "ocr_conf": rr["ocr_conf"],
                          "origin": rr["origin"], "source_label": CATALOG["sources"][rr["origin"]]["label"]}
                         for rr in regs]})

        if fl["class"] == "places":
            row_cn = fl["char_no"]
            add("measured", "Matched to a BoC row?", f"Yes. Row CN {row_cn} covers this text.",
                {"covered_by": first["coverage"]["covered_by"] if first else [], "candidates":
                 first["coverage"]["candidates"] if first else []})
            add("rule", "Excluded?", "Not checked: text a row covers is never excluded.", {"applied": None})
            chk = next((p for p in (first or {}).get("places") or [] if p.get("flag_id") == fl["flag_id"]), None)
            add("rule", "P1: " + CATALOG["rules"]["P1"]["label"], fl["reason"], {"rule_id": "P1", "check": chk})
            gid = None
        else:
            cands = [c for rr in regs for c in rr["coverage"]["candidates"]]
            near = first["coverage"]["nearest_row"] if first else None
            if cands:
                best = max(cands, key=lambda c: c["text_similarity"])
                detail = (f"No. {len({c['char_no'] for c in cands})} row(s) sit here but none matched: closest is "
                          f"CN {best['char_no']} ({best['requirement']}), {best['distance_mm']} mm away, text "
                          f"{best['text_similarity']:.0f}% similar (needs 60%), values "
                          f"{'agree' if best['numbers_compatible'] else 'differ'}.")
            elif near:
                detail = (f"No. No row's location overlaps it. Nearest row is CN {near['char_no']} "
                          f"({near['requirement']}), {near['distance_mm']} mm away.")
            else:
                detail = "No. No BoC row is located on this sheet."
            add("measured", "Matched to a BoC row?", detail, {"candidates": cands, "nearest_row": near})
            enabled = [CATALOG["exclusions"][k]["label"] for k in opts.exclusions if k in CATALOG["exclusions"]]
            add("rule", "Excluded?", "No exclusion applies. Checked: " + (", ".join(enabled) or "none enabled") + ".",
                {"enabled": list(opts.exclusions), "extra_regex": opts.extra_regex, "applied": None})
            gid = first["group_id"] if first else None
            g = groups.get(gid)
            if g:
                fired = next((c for c in g["checks"] if c["status"] == "fired"), None)
                ev = f" Matched: \"{fired['evidence']}\"." if fired and fired.get("evidence") else ""
                add("rule", f"{g['rule_id']}: {g['rule_label']}", g["reason"] + ev,
                    {"rule_id": g["rule_id"], "outcome": g["outcome"], "group_id": gid, "group_text": g["text"],
                     "checks": g["checks"]})

        model = self.model.get(gid) if gid else None
        if model is None:
            add("ai", "AI check not needed", "A rule decided this, so the AI was not asked.", {"status": "not_needed"})
        elif model["status"] == "verdict":
            v = model["verdict"]
            add("ai", "AI check: it is a requirement" if v["is_requirement"] else "AI check: not a requirement",
                f"{model['provider']} {model['model']} answered with {v['certainty']} certainty: \"{v['reason']}\" "
                f"(read as \"{v['transcription']}\").", model)
        else:
            err = model.get("error")
            label = CATALOG["llm_errors"].get(err or "", {}).get("label", err)
            add("ai", "AI check did not answer",
                f"The AI check did not run or did not answer ({label}), so it was scored as unconfirmed.", model)

        tr = self._transcribed.get(id(fl))
        if tr is not None:
            add("ai", "AI transcription (display only)",
                f"Shown as \"{fl.get('transcription') or fl['drawing_text']}\". This never changes the decision.",
                {"call_id": tr[0], "crop_index": tr[1], "transcription": fl.get("transcription")})

        if fl["class"] == "places":
            chk = next((p for p in self.places_checks if p.get("flag_id") == fl["flag_id"]), {})
            sc = dict(chk.get("score") or {})
        else:
            sc = dict(self.scores.get(gid) or {})
        # the band is recomputed from the product with the same thresholds the flag used
        sc["band"] = band_for(sc["product"])
        add("score", f"Score {sc['product']:.2f}: {CATALOG['bands'][sc['band']]['label']} confidence",
            f"{sc['base']} (rule) × {sc['locate_factor']} (location) × {sc['llm_factor']} (AI) = {sc['product']:.3f}. "
            f"Flags need 0.5; High is 0.8+, Medium 0.6+.", sc)
        return {"flag_id": fl["flag_id"], "class": fl["class"], "sheet": fl["sheet"], "region_ids": fl["region_ids"],
                "group_id": gid, "confidence": fl["confidence"], "score": fl["score"], "steps": steps}

    def partial(self, error: dict) -> dict:
        """What is known when a run fails: run-level data and the stages up to the failure."""
        self.run.update(finished_at=_now(), duration_ms=int((time.perf_counter() - self._t0) * 1000), status="error",
                        error=error)
        return {"schema_version": SCHEMA_VERSION, "catalog_version": CATALOG_VERSION, "run": self.run,
                "stages": self.stages, "funnel": [], "outcomes": {}, "invariants": {}, "llm": {}, "rows": [],
                "groups": self.groups, "places_checks": self.places_checks, "regions": [],
                "llm_calls": self.llm_calls, "flags": {}}


def summary(trace: dict) -> dict:
    """The /trace response: everything except the per-region records and the verbatim prompts."""
    calls = [{k: c.get(k) for k in ("call_id", "job", "provider", "model", "n_crops", "input_tokens", "output_tokens",
                                    "latency_ms", "cost_usd", "error", "stop_reason")}
             | {"url": f"/runs/{trace['run']['run_id']}/trace/llm/{c['call_id']}"} for c in trace.get("llm_calls", [])]
    flags = {k: {kk: v[kk] for kk in ("flag_id", "class", "sheet", "region_ids", "group_id", "confidence", "score")}
             for k, v in trace.get("flags", {}).items()}
    return {k: trace.get(k) for k in ("schema_version", "catalog_version", "run", "stages", "funnel", "invariants", "llm")} | {
        "outcomes": {k: {"label": v["label"], "count": v["count"]} for k, v in trace.get("outcomes", {}).items()},
        "llm_calls": calls, "flags": flags, "groups_count": len(trace.get("groups", [])),
        "regions_count": len(trace.get("regions", [])),
    }
