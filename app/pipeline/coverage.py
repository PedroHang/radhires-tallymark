"""Stage 3: match BoC rows to drawing regions (coverage subtraction).

A region counts as covered by a row when it sits where the row says it is AND
its text is compatible with the row's requirement. The text gate matters:
callouts are dense, so a purely geometric rule would let a neighbouring row
"cover" the one requirement nobody extracted.

Rows with no bbox are re-located by fuzzy text match on the same sheet
(bbox_source = "text_match"), and flags near them lose one confidence level.
Relocation scores whole callouts, not only single OCR regions: fragments OCR split along one
line are also tried together. A candidate must
carry one of the row's tolerance values when the row has any, so trailing note text such as
"2 SURFACES" can't take the row from its frame. A row with places = N may claim up to N
callouts (identical copies of one requirement on the drawing).
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz

from .boc import Characteristic, normalize_text
from .geometry import Box, overlap_ratio, union_box, zone_label
from .regions import Region, group_line_fragments

PAD = 0.006  # ~5 pt on a letter-size sheet
MATCH_MIN = 80  # score for text-only relocation (see match_score)


def text_similarity(requirement: str, region_text: str) -> float:
    a, b = normalize_text(requirement), normalize_text(region_text)
    if not a or not b:
        return 0.0
    if len(b) < len(a):
        return float(fuzz.partial_ratio(b, a))
    return float(fuzz.ratio(a, b))


_DECIMAL = re.compile(r"\d*\.\d+")


def decimals(text: str) -> set[str]:
    """Decimal values in normalized form ('Ø.250 ±.008' -> {'.250', '.008'})."""
    return set(_DECIMAL.findall(normalize_text(text).replace("+-", " ")))


def numbers_compatible(row_decimals: set[str], region_text: str) -> bool:
    """True when the region carries no decimal value, or shares at least one with the row."""
    reg = decimals(region_text)
    return not reg or not row_decimals or bool(reg & row_decimals)


def match_score(requirement: str, region_text: str) -> float:
    """How well a region's text matches a row's requirement, for rows with no location."""
    a, b = normalize_text(requirement), normalize_text(region_text)
    if len(b) < 3 or not a:
        return 0.0
    score = float(fuzz.ratio(a, b))
    if min(len(a), len(b)) >= 5:
        # one side contains the other: a split counterbore row inside a one-line OCR read,
        # or a short OCR fragment of a longer requirement
        score = max(score, 0.95 * float(fuzz.partial_ratio(a, b)))
    return score


_NUMBER = re.compile(r"\d*\.?\d+")
ZONE_BONUS = 3.0  # tie-break only: the candidate sits in the zone the row names (Form 3 "Reference Location")


def _values(text: str) -> set[str]:
    """The decimal values of a text as bare digit strings ('⌓ .05 D B C' -> {'05'}, '1.00 : 2.00' ->
    {'100', '200'}), because OCR often loses the decimal point of a frame value ('05D|B')."""
    return {d.replace(".", "") for d in decimals(text)}


def _carries_value(row_values: set[str], text: str) -> bool:
    runs = {m.replace(".", "") for m in _NUMBER.findall(normalize_text(text).replace("+-", " "))}
    for v in row_values:
        for r in runs:
            if r == v or (len(v) >= 3 and len(r) >= 3 and fuzz.ratio(v, r) >= 67):
                return True
    return False


def relocation_score(requirement: str, text: str) -> float:
    """match_score for a candidate callout, gated on values: when the row has tolerance values, the
    candidate must carry one of them."""
    a, b = normalize_text(requirement), normalize_text(text)
    if len(b) < 3 or not a:
        return 0.0
    vals = _values(requirement)
    if vals and not _carries_value(vals, text):
        return 0.0
    score = float(fuzz.ratio(a, b))
    # containment: an OCR fragment (4+ characters) of a longer requirement, or (as before) a requirement
    # of 5+ characters inside a longer OCR line. A short requirement is not matched inside a longer line
    # ('▱ .01' must not land on '12.00 ±.01').
    if (len(b) < len(a) and len(b) >= 4) or min(len(a), len(b)) >= 5:
        score = max(score, 0.95 * float(fuzz.partial_ratio(a, b)))
    return score


def relocation_units(regions: list[Region], aspect: float = 612 / 792) -> list[tuple[str, list[Region], str]]:
    """Candidate callouts per sheet: every region, and every run of fragments OCR split along one line
    (a frame read as '.05' + 'DBC'). (unit_id, regions, text).
    Regions sharing a detected frame grid are not merged: on the NIST set, frame grids attach to
    regions loosely and such groups spanned up to a third of the sheet."""
    units: list[tuple[str, list[Region], str]] = []
    seen: set[tuple] = set()

    def add(regs: list[Region]) -> None:
        regs = [r for r in regs if r.text]
        key = tuple(sorted(r.region_id for r in regs))
        if not regs or key in seen:
            return
        seen.add(key)
        text = " ".join(r.text for r in sorted(regs, key=lambda r: (round(r.bbox.y, 2), r.bbox.x)))
        units.append(("+".join(key), regs, text))

    for r in regions:
        add([r])
    for sheet in sorted({r.sheet for r in regions}):
        on_sheet = [r for r in regions if r.sheet == sheet]
        for group in group_line_fragments(on_sheet, aspect):
            if len(group) > 1:
                add(group)
    return units


def locate_rows_without_bbox(rows: list[Characteristic], regions: list[Region], placements: list | None = None,
                             aspect: float = 612 / 792) -> int:
    """Global greedy assignment, best (row, callout) pairs first. Round 1 places every row once;
    round 2 lets a row with places = N claim up to N - 1 more callouts that nobody else took.
    A region hosts a second row only when it holds more text than that row (one callout
    line carrying both counterbore diameter and depth, for example).
    placements: when a list, each placement is appended as {char_no, region_id, region_ids, score}
    (trace only). Returns how many rows were placed."""
    todo = [r for r in rows if r.bbox is None]
    if not todo:
        return 0
    units = relocation_units([r for r in regions if r.sheet in {row.sheet for row in todo}], aspect)
    pairs = []
    for row in todo:
        zone = (row.zone or "").strip().upper()
        for uid, regs, text in units:
            if regs[0].sheet != row.sheet:
                continue
            s = relocation_score(row.requirement, text)
            if s < MATCH_MIN:
                continue
            box = union_box([r.bbox for r in regs])
            bonus = ZONE_BONUS if zone and zone_label(box.center) == zone else 0.0
            pairs.append((s + bonus, s, len(regs), uid, row, regs, text, box))
    # best score first; on ties the smaller callout (fewer fragments) and a stable order
    pairs.sort(key=lambda p: (-p[0], p[2], p[3], p[4].char_no))
    claims: dict[int, int] = {}
    hosted: set[str] = set()

    def free(row, regs, text) -> bool:
        if any(r.region_id in hosted for r in regs):
            return len(normalize_text(text)) > len(normalize_text(row.requirement)) + 2
        return True

    def claim(row, regs, box, score) -> None:
        if row.bbox is None:
            row.bbox = box
            row.bbox_source = "text_match"
        else:
            row.extra_bboxes.append(box)
        claims[id(row)] = claims.get(id(row), 0) + 1
        hosted.update(r.region_id for r in regs)
        if placements is not None:
            placements.append({"char_no": row.char_no, "region_id": regs[0].region_id,
                               "region_ids": [r.region_id for r in regs], "score": round(score, 1),
                               "claim": claims[id(row)]})

    for _, s, _, _, row, regs, text, box in pairs:  # round 1: one callout per row
        if id(row) not in claims and free(row, regs, text):
            claim(row, regs, box, s)
    for _, s, _, _, row, regs, text, box in pairs:  # round 2: repeated callouts for places = N
        n = claims.get(id(row), 0)
        if 0 < n < max(1, row.places or 1) and not any(r.region_id in hosted for r in regs):
            if not any(overlap_ratio(box, b) > 0.3 for b in [row.bbox, *row.extra_bboxes]):
                claim(row, regs, box, s)
    return len(claims)


def subtract_coverage(rows: list[Characteristic], regions: list[Region], aspect: float = 612 / 792,
                      trace: dict | None = None) -> dict[str, int]:
    """Marks region.covered_by in place. Returns per-row covered region counts.
    trace: when a dict, every (row, region) pair that passed the location gate is recorded under
    trace[region_id] with the measured values and which test, if any, made it cover (trace only)."""
    counts: dict[str, int] = {}
    for row in rows:
        if row.bbox is None:
            continue
        all_hits: list[Region] = []
        for row_box in [row.bbox, *row.extra_bboxes]:
            for h in _cover_zone(row, row_box, regions, aspect, trace):
                if h not in all_hits:
                    all_hits.append(h)
        for reg in all_hits:
            if row.char_no not in reg.covered_by:
                reg.covered_by.append(row.char_no)
        row.located_regions = [h.region_id for h in all_hits]
        counts[row.char_no] = len(all_hits)
    return counts


def _cover_zone(row: Characteristic, row_box: Box, regions: list[Region], aspect: float,
                trace: dict | None) -> list[Region]:
    """The regions one placed box of a row covers."""
    zone = row_box.pad(PAD, PAD / aspect)
    candidates = [r for r in regions if r.sheet == row.sheet]
    best, best_key = None, (False, -1.0, -1.0)
    hits = []
    entries: dict[str, dict] = {}
    row_decimals = decimals(row.requirement)
    for reg in candidates:
        ov = overlap_ratio(reg.bbox, zone)
        center_in = zone.contains(reg.bbox.center)
        if ov < 0.5 and not center_in:
            continue
        sim = text_similarity(row.requirement, reg.text) if reg.text else 70.0
        compat = numbers_compatible(row_decimals, reg.text)
        key = (compat, sim, ov)
        if key > best_key:
            best, best_key = reg, key
        # fragment of this row's text (OCR split one callout into pieces). Values must agree:
        # fuzzy text alone lets '12.00 ±.01' cover a neighbouring '.012' frame.
        why = None
        if compat and (sim >= 60 or (ov >= 0.9 and sim >= 40)):
            hits.append(reg)
            why = ("text_match" if reg.text else "blank_in_zone") if sim >= 60 else "tight_overlap"
        elif not reg.text and reg.fcf_cells and row.char_type == "gdt":
            hits.append(reg)
            why = "blank_frame_gdt"
        if trace is not None:
            rx, ry = row_box.center
            cx, cy = reg.bbox.center
            entries[reg.region_id] = {
                "char_no": row.char_no, "requirement": row.requirement, "row_bbox_source": row.bbox_source,
                "overlap": round(ov, 3), "center_in_zone": center_in,
                "distance_pw": round(((cx - rx) ** 2 + ((cy - ry) * aspect) ** 2) ** 0.5, 5),
                "text_similarity": round(sim, 1), "text_blank": not reg.text,
                "numbers_compatible": compat, "passed": why is not None, "passed_by": why}
            trace.setdefault(reg.region_id, []).append(entries[reg.region_id])
    # the row's own location always covers its best region, even if OCR garbled the text
    if best is not None and best not in hits and overlap_ratio(best.bbox, zone) >= 0.5 and best_key[1] >= 30:
        hits.append(best)
        if trace is not None:
            entries[best.region_id].update(passed=True, passed_by="row_best_region")
    return hits


def nearest_row(rows: list[Characteristic], sheet: int, box: Box, aspect: float = 612 / 792) -> tuple[Characteristic | None, float]:
    """Nearest located row on the same sheet, distance in page-width units."""
    best, best_d = None, 9.0
    cx, cy = box.center
    for row in rows:
        if row.sheet != sheet or row.bbox is None:
            continue
        rx, ry = row.bbox.center
        d = ((cx - rx) ** 2 + ((cy - ry) * aspect) ** 2) ** 0.5
        if d < best_d:
            best, best_d = row, d
    return best, best_d


def covering_rows_box(regions: list[Region]) -> Box:
    return union_box([r.bbox for r in regions])
