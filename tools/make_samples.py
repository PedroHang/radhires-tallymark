"""Build the FTC-06 sample BoCs.

1. Ground truth: one row per inspectable characteristic, requirement text hand-transcribed
   from the rendered NIST FTC-06 sheets (reviewed visually). Locations come from the
   region reader's boxes for that text, or a hand-measured box where OCR missed it.
2. Simulated export: the ground truth with 4 seeded changes (the demo storyboard),
   plus manifest.json describing each seeded item and its true location.

Usage: python -m tools.make_samples
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from app import store
from app.pipeline.geometry import Box, union_box
from app.pipeline.run import _cache_dir

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
PDF = SAMPLES / "nist_ftc_06_asme1_rd.pdf"

# (sheet, char_type, requirement, places, anchors or a manual box)
# An anchor is the region text exactly as the reader returns it, optionally with a side suffix
# ("#L" / "#R") when the same text appears twice on a sheet. Anchoring on text, not region ids,
# keeps the ground truth stable when OCR ordering changes.
GT = [
    # sheet 1, view A
    (1, "gdt", "⌓ .05 A B C APPLIES TO ALL UNTOLERANCED SURFACES (NOTE 4)", 1,
     [".05ABC", "C#L", "APPLIESTOALL", "UNTOLERANCEDSURFACES."]),
    (1, "dimension", "2X CR.50 ±.02", 2, ["2X CR.50 ±.02"]),
    (1, "dimension", "24X R.125 ±.020", 24, ["24X R.125 ±.020"]),
    (1, "dimension", "2X SØ1.250 ±.008", 2, ["2X S01.250 ±.008"]),
    (1, "gdt", "⌖ SØ.025 D B C", 2, ["中S0.025DB|C"]),
    (1, "gdt", "⌓ .02 A B C", 1, [".02|A/B/C"]),
    (1, "gdt", "▱ .01", 1, ["01"]),
    (1, "dimension", "2X Ø.562 ±.008", 2, ["2X 0.562 ±.008"]),
    (1, "gdt", "⌖ Ø.015 C A B", 2, ["|Q.015|C|A|B"]),
    (1, "dimension", "2X ⌴ Ø.812 ±.020", 2, ["2X L1 0.812 ±.020 V.56 ±.02"]),
    (1, "dimension", "↧ .56 ±.02", 2, ["2X L1 0.812 ±.020 V.56 ±.02"]),
    (1, "gdt", "⌖ Ø.025 Ⓜ C A B", 2, ["0.025MC|A|B"]),
    (1, "dimension", "4X Ø.415 ±.008", 4, ["4X8.415 ±.008"]),
    (1, "dimension", "⌴ Ø.625 ±.020", 4, ["O.625±.020V.50±.02"]),
    (1, "dimension", "↧ .50 ±.02", 4, ["O.625±.020V.50±.02"]),
    (1, "gdt", "⌖ Ø.025 Ⓜ A B C", 4, ["025@ABC"]),
    (1, "dimension", "⌴ Ø.40 ±.01", 4, ["10.40 ±.01 V.31 ±.02"]),
    (1, "dimension", "↧ .31 ±.02", 4, ["10.40 ±.01 V.31 ±.02"]),
    (1, "gdt", "⌖ Ø.02 Ⓜ F 4X INDIVIDUALLY", 4, ["|Q.02@|F", "4X INDIVIDUALLY"]),
    (1, "dimension", "4X Ø.281 ±.008", 4, ["4X 0.281 ±.008"]),
    (1, "gdt", "⌖ Ø.015 E A B", 4, ["0.015|E|AB"]),
    (1, "gdt", "▱ .01", 1, Box(0.389, 0.770, 0.054, 0.044)),  # flatness on datum A, OCR misses it
    # sheet 2, view B
    (2, "gdt", "⌓ .02 A B C", 1, [".02ABC"]),
    (2, "gdt", "▱ .01", 1, ["1.011"]),
    (2, "gdt", "⌓ .04 A B C", 1, [".04ABC"]),
    (2, "other", "1.00 : 2.00", 1, ["1.00:2.00"]),
    (2, "gdt", "⌓ .05 D B C", 1, [".05/D/B/C"]),
    (2, "gdt", "⌓ .01 D", 1, ["01D"]),
    (2, "other", "2X 1.00 : 3.00", 2, ["2X1.00:3.00"]),
    (2, "gdt", "⊥ .012 A B", 1, [".012AB"]),
    (2, "gdt", "⌓ .05 D B C", 1, [".05/D|BC"]),
    (2, "gdt", "⌓ .01 D", 1, ["01/D"]),
    (2, "dimension", "12.00 ±.01", 1, ["12.00 ±.01"]),
    (2, "gdt", "⊥ .012 A", 1, ["1.012"]),
    # sheet 3, view C
    (3, "dimension", ".500 ±.008", 1, [".500±.008#L"]),
    (3, "gdt", "⌖ .025 D C J", 1, ["Φ#L", ".025|D|C|J"]),
    (3, "dimension", ".500 ±.008", 1, [".500±.008#R"]),
    (3, "gdt", "⌖ .025 D C K", 1, ["Φ#R", ".025DCK"]),
    (3, "gdt", "⌓ .05 D B C", 2, ["05D|B"]),
    (3, "gdt", "⌓ .01 D 2 SURFACES", 2, [".01D#L", "2SURFACES"]),
    (3, "gdt", "⌓ .05 D B C", 2, [".05", "DBC"]),
    (3, "gdt", "⌓ .01 D 2 SURFACES", 2, [".01D#R", "2 SURFACES"]),
    (3, "dimension", "Ø.250 ±.008", 1, ["0.250 ±.008#L"]),
    (3, "dimension", "↧ .50 ±.05", 1, ["V.50 ±.05"]),
    (3, "gdt", "⌖ Ø.015 D G", 1, ["ΦQ.015|D|G"]),
    (3, "dimension", "Ø.250 ±.008", 1, ["0.250 ±.008#R"]),
    (3, "dimension", "↧ .50 ±.05", 1, ["T.50 ±.05"]),
    (3, "gdt", "⌖ Ø.015 D H", 1, ["ΦQ.015|DH"]),
]

# The 4 seeded items used in the demo (the demo storyboard).
SEEDS = [
    {"op": "drop_notes", "requirement": "⌓ .05 A B C APPLIES TO ALL UNTOLERANCED SURFACES (NOTE 4)", "sheet": 1,
     "expect": "unaccounted"},
    {"op": "reduce_places", "requirement": "4X Ø.281 ±.008", "sheet": 1, "places": 1, "expect": "places"},
    {"op": "drop_later_sheet_row", "requirement": "12.00 ±.01", "sheet": 2, "expect": "unaccounted"},
    {"op": "drop_row", "requirement": "⌖ .025 D C K", "sheet": 3, "expect": "unaccounted"},
]

COLUMNS = ["char_no", "sheet", "zone", "char_type", "requirement", "places", "bbox_x", "bbox_y", "bbox_w", "bbox_h"]


def _regions(sheet: int) -> dict[str, dict]:
    cache = _cache_dir(store.sha256(PDF.read_bytes()))
    data = json.loads((cache / f"sheet_{sheet}_regions.json").read_text(encoding="utf-8"))
    return {r["region_id"].split("-", 1)[1]: r for r in data}


def _resolve(sheet: int, ref, regions: dict[str, dict]) -> Box:
    if isinstance(ref, Box):
        return ref
    boxes = []
    for anchor in ref:
        text, _, side = anchor.partition("#")
        found = [r for r in regions.values() if r["text"] == text]
        if side:
            found = [r for r in found if (r["bbox"]["x"] < 0.5) == (side == "L")]
        if len(found) != 1:
            raise SystemExit(f"sheet {sheet}: anchor {anchor!r} matched {len(found)} regions; update GT")
        boxes.append(Box.from_dict(found[0]["bbox"]))
    return union_box(boxes)


def build_rows() -> list[dict]:
    from app.pipeline.geometry import zone_label

    rows = []
    cache: dict[int, dict] = {}
    for i, (sheet, ctype, req, places, ref) in enumerate(GT, start=1):
        regions = cache.setdefault(sheet, _regions(sheet))
        b = _resolve(sheet, ref, regions)
        rows.append({
            "char_no": str(i), "sheet": sheet, "zone": zone_label(b.center), "char_type": ctype,
            "requirement": req, "places": places,
            "bbox_x": round(b.x, 4), "bbox_y": round(b.y, 4), "bbox_w": round(b.w, 4), "bbox_h": round(b.h, 4),
        })
    return rows


def write_csv(path: Path, rows: list[dict], columns: list[str] = COLUMNS) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def seed(rows: list[dict], seeds: list[dict]) -> tuple[list[dict], list[dict]]:
    out = [dict(r) for r in rows]
    manifest = []
    for s in seeds:
        match = next(r for r in out if r["requirement"] == s["requirement"] and r["sheet"] == s["sheet"])
        box = {"x": match["bbox_x"], "y": match["bbox_y"], "w": match["bbox_w"], "h": match["bbox_h"]}
        if s["op"] == "reduce_places":
            manifest.append({**s, "char_no": match["char_no"], "true_places": match["places"], "bbox": box})
            match["places"] = s["places"]
        else:
            manifest.append({**s, "char_no": match["char_no"], "bbox": box})
            out.remove(match)
    # an extractor numbers what it found, so renumber: gaps would give the seeded rows away
    for i, r in enumerate(out, start=1):
        r["char_no"] = str(i)
    for m in manifest:
        m["gt_char_no"] = m.pop("char_no")
        if m["op"] == "reduce_places":
            m["char_no"] = next(r["char_no"] for r in out if r["requirement"] == m["requirement"] and r["sheet"] == m["sheet"])
    return out, manifest


def main() -> None:
    rows = build_rows()
    write_csv(SAMPLES / "ftc06_boc_groundtruth.csv", rows)
    sim, manifest = seed(rows, SEEDS)
    write_csv(SAMPLES / "ftc06_boc_simulated.csv", sim)
    nobbox = [{k: v for k, v in r.items() if not k.startswith("bbox")} for r in sim]
    write_csv(SAMPLES / "ftc06_boc_simulated_nobbox.csv", nobbox, [c for c in COLUMNS if not c.startswith("bbox")])
    (SAMPLES / "ftc06_boc_simulated.manifest.json").write_text(
        json.dumps({"drawing": PDF.name, "ground_truth_rows": len(rows), "simulated_rows": len(sim),
                    "seeded": manifest}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"ground truth {len(rows)} rows, simulated {len(sim)} rows, {len(manifest)} seeded")


if __name__ == "__main__":
    main()
