"""Stage 5: classify leftover text (regions no BoC row covers, not excluded).

Deterministic rules decide everything they can. Only genuinely ambiguous
leftovers (a bare number with no tolerance and no frame) go to the vision model.
Rule IDs follow BRD section 7.3.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .boc import normalize_text

# OCR often reads the diameter sign as 0, 8, Q or Φ, and ± as +- or a replacement char.
TOL = r"(±|\+/-|\+-|�|\+\s*\.?\d*\.\d+\s*/\s*-)"
R1 = re.compile(r"\d*\.?\d+\s*" + TOL + r"\s*\d*\.?\d")  # inch (.500 ±.008) and metric (86 ±0.15)
R2 = re.compile(r"\d*\.\d+\s*[-–]\s*\d*\.\d+\b")  # 1.248-1.252 limit pair on one line
R3 = re.compile(r"(^|[\s(])(\d{1,3}\s*X\s*)?(Ø|⌀|Φ|DIA\b|S?R\s*\.?\d|CR\s*\.?\d|SØ|M\d+(\s*X\s*[\d.]+)?\b)", re.I)
# Feature control frame text: a tolerance value followed by 1-3 datum letters,
# optionally with separators and material modifiers ('.025|D|C|J', '0.015CAB', '.05ABC').
FCF_TEXT = re.compile(r"\.?\d*\.\d+(?!\s*(MAX|MIN|TYP)\b)\s*[MⓂ@Ⓛ]?\s*[|/ ]?\s*[A-HJ-NP-Z](\s*[|/ ]?\s*[A-HJ-NP-Z]){0,2}\s*$")
R5_NOTE = re.compile(r"^\s*\d{1,2}\s*[.)．]\s*[A-Z]", re.I)
R5_WORDS = re.compile(r"\b(BREAK|FINISH|PER|MAX|MIN|TYP|APPLIES|CHAMFER|DEBURR|TORQUE|HARDNESS)\b", re.I)
R6_SPEC = re.compile(r"\b(AMS|ASTM|MIL-[A-Z]+|SAE|ISO)[- ]?\S+", re.I)
R7_FINISH = re.compile(r"\b(\d{2,3})\s*(µ|MICRO|RA\b|MICROINCH)", re.I)
R8_BARE = re.compile(r"^\s*\.?\d*\.?\d+\s*$")
LETTERS = re.compile(r"[A-Za-z]")


@dataclass
class Verdict:
    outcome: str  # "requirement" | "ambiguous" | "not_requirement"
    rule_ids: list[str]
    base: float  # 0.9 High, 0.6 Medium
    kind: str  # dimension | gdt | note | spec | surface_finish | other
    title: str
    reason: str


def classify_text(text: str, fcf_cells: int = 0) -> Verdict:
    t = text.strip()
    up = t.upper()
    compact = normalize_text(t)
    letters = len(LETTERS.findall(t))
    digits = sum(ch.isdigit() for ch in t)

    if fcf_cells >= 3 and (not t or FCF_TEXT.search(up) or digits):
        return Verdict("requirement", ["R4"], 0.9, "gdt", "Feature control frame, no characteristic",
                       "A feature control frame (tolerance box with datum references) that no row covers.")
    if R1.search(up):
        kind = "dimension"
        title = "Toleranced dimension, no row"
        if re.match(r"^\s*\d{1,3}\s*X", up):
            title = "Toleranced dimension (repeated feature), no row"
        return Verdict("requirement", ["R1"], 0.9, kind, title,
                       "A number with a tolerance on the drawing that no row covers.")
    if FCF_TEXT.search(up) and digits:
        return Verdict("requirement", ["R4"], 0.9, "gdt", "Feature control frame, no characteristic",
                       "Reads as a geometric tolerance with datum references, and no row covers it.")
    if R2.search(up):
        return Verdict("requirement", ["R2"], 0.9, "dimension", "Limit dimension, no row",
                       "A limit dimension (upper and lower value) that no row covers.")
    if R6_SPEC.search(up):
        return Verdict("requirement", ["R6"], 0.9, "spec", "Specification reference, no row",
                       "A material or process specification is called out and no row covers it.")
    if R5_NOTE.search(t) and (digits > 2 or R5_WORDS.search(up)):
        return Verdict("requirement", ["R5"], 0.9, "note", "Numbered note, no characteristic",
                       "A numbered note that imposes a requirement, and no row covers it.")
    if R3.search(up) and digits:
        return Verdict("requirement", ["R3"], 0.9, "dimension", "Size callout, no row",
                       "A diameter, radius or thread callout that no row covers.")
    if R7_FINISH.search(up):
        return Verdict("requirement", ["R7"], 0.6, "surface_finish", "Surface finish, no row",
                       "Looks like a surface finish value, and no row covers it.")
    if len(compact) < 3:
        return Verdict("not_requirement", ["R11"], 0.0, "other", "Short label", "Under 3 characters (datum or axis label).")
    if R8_BARE.match(t):
        return Verdict("ambiguous", ["R8"], 0.6, "dimension", "Number with no row",
                       "A number with no tolerance and no frame. It may be a requirement or just a label.")
    if letters >= max(4, digits * 2) or re.search(r"[A-Z]{8,}", up):  # prose, standards references
        return Verdict("not_requirement", ["R10"], 0.0, "other", "Label or prose",
                       "Mostly words with no tolerance or specification pattern.")
    if digits:
        return Verdict("ambiguous", ["R8"], 0.6, "other", "Text with numbers, no row",
                       "Contains numbers but matches no requirement pattern.")
    return Verdict("not_requirement", ["R10"], 0.0, "other", "Label", "No numbers and no requirement pattern.")


PLACES = re.compile(r"(?<![0-9.])(\d{1,3})\s*(X(?![A-Z]{2})|PLCS\b|PLACES\b)", re.I)


def places_in(text: str) -> int | None:
    """'4X Ø.281 ±.008' -> 4, '6 PLCS' -> 6. 'TYP' alone returns None (customer-dependent)."""
    m = PLACES.search(text.upper())
    if not m:
        return None
    n = int(m.group(1))
    return n if n >= 2 else None


def tidy_ocr_text(text: str) -> str:
    """Light clean-up of OCR text for display only (never used for decisions):
    frame separators become spaces and trailing datum letters are spaced out."""
    t = re.sub(r"\s*[|/]\s*", " ", text).strip()
    t = re.sub(r"(\.\d+)\s*([A-HJ-NP-Z]{1,3})(?=\s|$)", lambda m: m.group(1) + " " + " ".join(m.group(2)), t)
    return re.sub(r"\s+", " ", t)
