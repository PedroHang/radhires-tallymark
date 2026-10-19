"""Debug helper: read every text region on a drawing and write an overlay PNG with region ids.

Usage: python -m tools.dump_regions samples/nist_ftc_06_asme1_rd.pdf
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2

from app import store
from app.pipeline.regions import (
    VIEW_DPI, WORK_DPI, Region, detect_fcf_grids, merge_regions, ocr_regions, open_drawing, render_page,
    text_layer_regions,
)
from app.pipeline.run import _cache_dir


def main(path: str, out_dir: str = "scratch") -> None:
    data = Path(path).read_bytes()
    cache = _cache_dir(store.sha256(data))
    doc = open_drawing(data, path)
    out = Path(out_dir)
    out.mkdir(exist_ok=True)
    all_regions = []
    for n in range(1, doc.page_count + 1):
        wpath, vpath, rpath = cache / f"sheet_{n}_work.png", cache / f"sheet_{n}_view.png", cache / f"sheet_{n}_regions.json"
        if not wpath.exists():
            img = render_page(doc[n - 1], WORK_DPI)
            cv2.imwrite(str(wpath), img[:, :, ::-1])
            cv2.imwrite(str(vpath), render_page(doc[n - 1], VIEW_DPI)[:, :, ::-1])
        img = cv2.imread(str(wpath))[:, :, ::-1].copy()
        if rpath.exists():
            regs = [Region.from_dict(r) for r in json.loads(rpath.read_text(encoding="utf-8"))]
        else:
            regs = merge_regions(ocr_regions(img, n), text_layer_regions(doc[n - 1], n), detect_fcf_grids(img), n)
            rpath.write_text(json.dumps([r.to_dict() for r in regs]), encoding="utf-8")
        h, w = img.shape[:2]
        canvas = img[:, :, ::-1].copy()
        for r in regs:
            pts = [[int(p[0] * w), int(p[1] * h)] for p in r.poly]
            cv2.polylines(canvas, [__import__("numpy").array(pts)], True, (0, 160, 0), 3)
            cv2.putText(canvas, r.region_id.split("-")[1], (int(r.bbox.x * w), int(r.bbox.y * h) - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 200), 2)
        cv2.imwrite(str(out / f"regions_s{n}.png"), canvas)
        all_regions += regs
        for r in regs:
            b = r.bbox
            print(f"{r.region_id:8} {r.text!r:45} x={b.x:.4f} y={b.y:.4f} w={b.w:.4f} h={b.h:.4f} ang={r.angle:.0f} {'+'.join(r.sources)} cells={r.fcf_cells}")
    print(f"cache: {cache}")


if __name__ == "__main__":
    main(*sys.argv[1:])
