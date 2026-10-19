"""Stage 3: match BoC rows to drawing regions (coverage subtraction).

A region counts as covered by a row when it sits where the row says it is AND
its text is compatible with the row's requirement. The text gate matters:
callouts are dense, so a purely geometric rule would let a neighbouring row
"cover" the one requirement nobody extracted.

Rows with no bbox are re-located by fuzzy text match on the same sheet
(bbox_source = "text_match"), and flags near them lose one confidence level.
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz

from .boc import Characteristic, normalize_text
from .geometry import Box, overlap_ratio, union_box
from .regions import Region

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


def locate_rows_without_bbox(rows: list[Characteristic], regions: list[Region], placements: list | None = None) -> int:
    """Global greedy assignment: best (row, region) pairs first, each row placed once.
    A region hosts a second row only when it holds more text than that row (one callout
    line carrying both counterbore diameter and depth, for example).
    placements: when a list, each placement is appended as {char_no, region_id, score} (trace only)."""
    pairs = []
    for row in rows:
        if row.bbox is not None:
            continue
        for reg in regions:
            if reg.sheet == row.sheet:
                s = match_score(row.requirement, reg.text)
                if s >= MATCH_MIN:
                    pairs.append((s, row, reg))
    pairs.sort(key=lambda p: -p[0])
    placed: set[int] = set()
    hosted: set[str] = set()
    for s, row, reg in pairs:
        if id(row) in placed:
            continue
        if reg.region_id in hosted and len(normalize_text(reg.text)) <= len(normalize_text(row.requirement)) + 2:
            continue
        row.bbox = reg.bbox
        row.bbox_source = "text_match"
        if placements is not None:
            placements.append({"char_no": row.char_no, "region_id": reg.region_id, "score": round(s, 1)})
        placed.add(id(row))
        hosted.add(reg.region_id)
    return len(placed)


def subtract_coverage(rows: list[Characteristic], regions: list[Region], aspect: float = 612 / 792,
                      trace: dict | None = None) -> dict[str, int]:
    """Marks region.covered_by in place. Returns per-row covered region counts.
    trace: when a dict, every (row, region) pair that passed the location gate is recorded under
    trace[region_id] with the measured values and which test, if any, made it cover (trace only)."""
    counts: dict[str, int] = {}
    for row in rows:
        if row.bbox is None:
            continue
        zone = row.bbox.pad(PAD, PAD / aspect)
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
                rx, ry = row.bbox.center
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
        for reg in hits:
            if row.char_no not in reg.covered_by:
                reg.covered_by.append(row.char_no)
        row.located_regions = [h.region_id for h in hits]
        counts[row.char_no] = len(hits)
    return counts


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
