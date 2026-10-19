"""Rows without coordinates (Form 3 sheets, no-bbox exports) are placed by text.

The two FTC-06 cases that used to fail on sheet 3: '⌓ .01 D 2 SURFACES' landed on the '2SURFACES'
note text instead of its '.01D' frame, and '⌓ .05 D B C' found nothing because OCR splits that frame
into '.05' + 'DBC'. Rows with places = 2 also need to account for both copies of a callout.

Run: .venv/Scripts/python -m pytest -q tests/test_relocation.py
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from app.pipeline.boc import Characteristic
from app.pipeline.coverage import locate_rows_without_bbox, relocation_score, subtract_coverage
from app.pipeline.geometry import Box
from app.pipeline.regions import Region

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
PDF = SAMPLES / "nist_ftc_06_asme1_rd.pdf"


def _region(rid: str, text: str, x: float, y: float, w: float = 0.04, h: float = 0.015, sheet: int = 3) -> Region:
    b = Box(x, y, w, h)
    return Region(rid, sheet, text, b, [(b.x, b.y), (b.x2, b.y), (b.x2, b.y2), (b.x, b.y2)], sources=["ocr"])


def test_relocation_score_needs_the_rows_value():
    assert relocation_score("⌓ .01 D 2 SURFACES", "2SURFACES") == 0  # note text carries no .01
    assert relocation_score("⌓ .01 D 2 SURFACES", ".01D") >= 80  # the frame fragment of the row
    assert relocation_score("⌓ .05 D B C", "05D|B") >= 80  # OCR dropped the decimal point
    assert relocation_score("⌓ .05 D B C", ".025DCK") == 0  # a different frame value
    assert relocation_score("▱ .01", "12.00 ±.01") < 80  # a short row is not found inside a longer line
    assert relocation_score("1.00 : 2.00", "1.00:2.00") >= 80
    assert relocation_score("MATERIAL PER AMS-QQ-A-250/11", "MATERIALPERAMS-QQ-A-250/11") >= 80


def test_split_frame_and_two_copies_are_located():
    """Two copies of a two-line frame (left C7, right C2), the upper line split by OCR, a note under each."""
    regions = [
        _region("a1", ".05", 0.777, 0.343, 0.021), _region("a2", "DBC", 0.793, 0.342, 0.06),
        _region("a3", ".01D", 0.775, 0.359), _region("a4", "2 SURFACES", 0.738, 0.38, 0.093),
        _region("b1", "05D|B", 0.216, 0.352, 0.06), _region("b2", ".01D", 0.214, 0.368),
        _region("b3", "2SURFACES", 0.179, 0.392, 0.092),
    ]
    rows = [Characteristic("38", 3, "⌓ .05 D B C", zone="C2", char_type="gdt", places=2),
            Characteristic("39", 3, "⌓ .01 D 2 SURFACES", zone="C2", char_type="gdt", places=2)]
    placements: list = []
    assert locate_rows_without_bbox(rows, regions, placements) == 2
    subtract_coverage(rows, regions)
    covered = {r.region_id: r.covered_by for r in regions}
    # every frame fragment is covered, by the right row, on both copies (places = 2)
    assert covered["a1"] == ["38"] and covered["a2"] == ["38"] and covered["b1"] == ["38"]
    assert covered["a3"] == ["39"] and covered["b2"] == ["39"]
    # the note text is not where row 39 was placed
    assert not any(p["region_id"] in ("a4", "b3") for p in placements)
    assert {p["claim"] for p in placements} == {1, 2}


def test_one_row_per_copy_when_the_boc_lists_each_copy():
    """Form 3 style: a row per copy, each with its zone. Each copy gets its own row."""
    regions = [_region("a1", ".05", 0.777, 0.343, 0.021), _region("a2", "DBC", 0.793, 0.342, 0.06),
               _region("b1", "05D|B", 0.216, 0.352, 0.06)]
    rows = [Characteristic("39", 3, "⌓ .05 D B C", zone="C7", places=2),
            Characteristic("41", 3, "⌓ .05 D B C", zone="C2", places=2)]
    locate_rows_without_bbox(rows, regions)
    subtract_coverage(rows, regions)
    assert {r.region_id: r.covered_by for r in regions} == {"a1": ["41"], "a2": ["41"], "b1": ["39"]}


def _check(boc: Path) -> dict:
    from tools.check import run_check

    return run_check(PDF, boc, use_llm=False)


def test_simulated_export_without_bbox_flags_only_the_seeded_items():
    doc = _check(SAMPLES / "ftc06_boc_simulated_nobbox.csv")
    manifest = json.loads((SAMPLES / "ftc06_boc_simulated.manifest.json").read_text(encoding="utf-8"))
    assert doc["status"] == "complete" and len(doc["flags"]) == len(manifest["seeded"])
    for seed in manifest["seeded"]:
        b = seed["bbox"]
        cx, cy = b["x"] + b["w"] / 2, b["y"] + b["h"] / 2
        assert [f for f in doc["flags"] if f["sheet"] == seed["sheet"] and f["class"] == seed["expect"]
                and f["bbox"]["x"] - 0.01 <= cx <= f["bbox"]["x"] + f["bbox"]["w"] + 0.01
                and f["bbox"]["y"] - 0.01 <= cy <= f["bbox"]["y"] + f["bbox"]["h"] + 0.01], seed["requirement"]


def test_complete_form3_without_coordinates_has_no_flags(tmp_path):
    """The complete ground truth as an AS9102 Form 3 sheet: zones in Reference Location, no bbox."""
    from openpyxl import Workbook

    with (SAMPLES / "ftc06_boc_groundtruth.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    wb = Workbook()
    ws = wb.active
    ws.append(["FORM 3 - CHARACTERISTIC ACCOUNTABILITY"])
    ws.append(["1. Part Number:", "FTC-06"])
    ws.append([])
    ws.append(["5. Char. No.", "6. Reference Location", "7. Characteristic Designator", "8. Requirement",
               "Places (qty)", "9. Results"])
    for r in rows:
        ws.append([int(r["char_no"]), f"Sheet {r['sheet']}, zone {r['zone']}", "", r["requirement"], int(r["places"]), ""])
    p = tmp_path / "ftc06_form3.xlsx"
    buf = io.BytesIO()
    wb.save(buf)
    p.write_bytes(buf.getvalue())
    doc = _check(p)
    assert doc["status"] == "complete" and not doc["boc"]["has_bbox"]
    assert doc["flags"] == [], [(f["flag_id"], f["drawing_text"]) for f in doc["flags"]]
