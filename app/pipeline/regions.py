"""Stage 1-2: render sheets and read every text region on them.

Region sources (independent of whatever extractor produced the BoC):
  * OCR (RapidOCR: PaddleOCR detection + recognition models on ONNX). Returns
    oriented quads, so rotated isometric callouts are read too.
  * PDF text layer (vector PDFs only). Never the only source, because CAD PDFs
    often drop GD&T glyphs or draw callouts as vector strokes.
  * Feature control frame (FCF) grids found as rows of small rectangular cells
    with OpenCV. A frame becomes a region even if OCR read nothing inside it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np
import pymupdf

from .geometry import Box, iou, overlap_ratio, poly_iou, poly_overlap, union_box

WORK_DPI = 300
VIEW_DPI = 150

_ocr_engine = None


def _ocr():
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR

        _ocr_engine = RapidOCR()
    return _ocr_engine


@dataclass
class Region:
    region_id: str
    sheet: int
    text: str
    bbox: Box
    poly: list[tuple[float, float]]
    angle: float = 0.0
    conf: float = 1.0
    sources: list[str] = field(default_factory=list)
    fcf_cells: int = 0  # >0 when an FCF grid overlaps this region
    grid_polys: list = field(default_factory=list)  # outlines of FCF grids attached to this region
    # filled by later stages
    covered_by: list[str] = field(default_factory=list)
    excluded_by: str | None = None

    def to_dict(self) -> dict:
        return {
            "region_id": self.region_id,
            "sheet": self.sheet,
            "text": self.text,
            "bbox": self.bbox.to_dict(),
            "poly": [[round(p[0], 5), round(p[1], 5)] for p in self.poly],
            "angle": round(self.angle, 1),
            "conf": round(self.conf, 3),
            "sources": self.sources,
            "fcf_cells": self.fcf_cells,
            "grid_polys": [[[round(p[0], 5), round(p[1], 5)] for p in g] for g in self.grid_polys],
        }

    @staticmethod
    def from_dict(d: dict) -> "Region":
        return Region(
            region_id=d["region_id"],
            sheet=d["sheet"],
            text=d["text"],
            bbox=Box.from_dict(d["bbox"]),
            poly=[(p[0], p[1]) for p in d["poly"]],
            angle=d.get("angle", 0.0),
            conf=d.get("conf", 1.0),
            sources=list(d.get("sources", [])),
            fcf_cells=d.get("fcf_cells", 0),
            grid_polys=[[(p[0], p[1]) for p in g] for g in d.get("grid_polys", [])],
        )


def open_drawing(data: bytes, filename: str) -> pymupdf.Document:
    name = filename.lower()
    if name.endswith((".tif", ".tiff")):
        return pymupdf.open(stream=data, filetype="tiff")
    return pymupdf.open(stream=data, filetype="pdf")


def render_page(page: pymupdf.Page, dpi: int) -> np.ndarray:
    """Returns an RGB uint8 image."""
    pix = page.get_pixmap(dpi=dpi, alpha=False, colorspace=pymupdf.csRGB)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, 3).copy()


def _ocr_pass(img_rgb: np.ndarray, angle: float) -> list[tuple[list[tuple[float, float]], str, float, float]]:
    """One OCR pass on the sheet rotated by `angle` degrees. Quads are mapped back to sheet pixels."""
    h, w = img_rgb.shape[:2]
    bgr = img_rgb[:, :, ::-1]
    if angle == 0:
        src, inv = bgr, None
    else:
        m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
        cos, sin = abs(m[0, 0]), abs(m[0, 1])
        nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
        m[0, 2] += nw / 2 - w / 2
        m[1, 2] += nh / 2 - h / 2
        src = cv2.warpAffine(bgr, m, (nw, nh), borderValue=(255, 255, 255))
        inv = cv2.invertAffineTransform(m)
    result, _ = _ocr()(src)
    out = []
    for quad, text, conf in result or []:
        local_angle = math.degrees(math.atan2(quad[1][1] - quad[0][1], quad[1][0] - quad[0][0]))
        if angle != 0 and abs(local_angle) > 10:
            continue  # rotated passes only contribute text they straightened
        pts = np.array(quad, np.float32)
        if inv is not None:
            pts = (inv[:, :2] @ pts.T).T + inv[:, 2]
        top = pts[1] - pts[0]
        out.append(([(float(x), float(y)) for x, y in pts], str(text).strip(), float(conf),
                    math.degrees(math.atan2(top[1], top[0]))))
    return out


def ocr_regions(img_rgb: np.ndarray, sheet: int, extra_angles: tuple[float, ...] = (-30.0, 30.0)) -> list[Region]:
    """OCR at 0 degrees, plus straightened passes for isometric callouts when the sheet has rotated text."""
    h, w = img_rgb.shape[:2]
    regions: list[Region] = []

    def add(quad, text, conf, angle, tag):
        if not text:
            return
        poly = [(x / w, y / h) for x, y in quad]
        box = Box.from_points(poly)
        for r in regions:
            if iou(r.bbox, box) > 0.05 and (poly_iou(r.poly, poly) > 0.3 or poly_overlap(poly, r.poly) > 0.6
                                            or poly_overlap(r.poly, poly) > 0.6):
                # same text seen twice. A straightened pass often reads the whole line where the
                # 0-degree pass caught a fragment, so a longer read that contains the old one wins.
                if poly_overlap(r.poly, poly) > 0.6 and len(text) > len(r.text) + 2:
                    r.text, r.conf, r.poly, r.bbox, r.angle = text, conf, poly, box, angle
                elif conf > r.conf + 0.05 and len(text) >= len(r.text):
                    r.text, r.conf = text, conf
                return
        regions.append(Region(f"s{sheet}-{tag}{len(regions)}", sheet, text, box, poly, angle, conf, ["ocr"]))

    base = _ocr_pass(img_rgb, 0)
    for quad, text, conf, angle in base:
        add(quad, text, conf, angle, "o")
    if any(abs(a) > 10 and abs(abs(a) - 90) > 10 for _, _, _, a in base):
        for extra in extra_angles:
            for quad, text, conf, angle in _ocr_pass(img_rgb, extra):
                if conf >= 0.6:
                    add(quad, text, conf, angle, "r")
    return regions


def text_layer_regions(page: pymupdf.Page, sheet: int) -> list[Region]:
    # get_text reports unrotated coordinates; map them onto the displayed (rotated) page
    pw, ph = page.rect.width, page.rect.height
    rot = page.rotation_matrix
    regions: list[Region] = []
    data = page.get_text("dict")
    k = 0
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            text = "".join(s.get("text", "") for s in line.get("spans", [])).strip()
            if not text:
                continue
            r = pymupdf.Rect(line["bbox"]) * rot
            x0, y0, x1, y1 = r.x0, r.y0, r.x1, r.y1
            dx, dy = line.get("dir", (1.0, 0.0))
            v = pymupdf.Point(dx, dy) * pymupdf.Matrix(rot.a, rot.b, rot.c, rot.d, 0, 0)
            angle = math.degrees(math.atan2(v.y, v.x))
            box = Box(x0 / pw, y0 / ph, (x1 - x0) / pw, (y1 - y0) / ph)
            poly = [(box.x, box.y), (box.x2, box.y), (box.x2, box.y2), (box.x, box.y2)]
            regions.append(
                Region(f"s{sheet}-t{k}", sheet, text, box, poly, angle, 1.0, ["text_layer"])
            )
            k += 1
    return regions


def detect_fcf_grids(img_rgb: np.ndarray) -> list[dict]:
    """Rows of small rectangular cells (feature control frames, datum boxes).

    Works on dark or saturated ink so it handles both black and coloured CAD
    output. Rotated frames are found through minAreaRect on each cell contour.
    """
    h, w = img_rgb.shape[:2]
    ink = (img_rgb.min(axis=2) < 160).astype(np.uint8) * 255
    ink = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(ink, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    cells = []
    min_side, max_side = 0.008 * w, 0.06 * w
    for c in contours:
        peri = cv2.arcLength(c, True)
        if peri < 4 * min_side:
            continue
        approx = cv2.approxPolyDP(c, 0.04 * peri, True)
        if len(approx) != 4 or not cv2.isContourConvex(approx):
            continue
        (cx, cy), (rw, rh), ang = cv2.minAreaRect(approx)
        short, long_ = min(rw, rh), max(rw, rh)
        if short < min_side or long_ > max_side or short > 0.025 * w:
            continue
        area = cv2.contourArea(approx)
        if area < 0.85 * rw * rh:
            continue
        cells.append(((cx, cy), (rw, rh), ang, short))
    if not cells:
        return []

    # group cells that touch: draw slightly inflated rects and take components
    canvas = np.zeros((h, w), np.uint8)
    for (cx, cy), (rw, rh), ang, _ in cells:
        box = cv2.boxPoints(((cx, cy), (rw + 10, rh + 10), ang)).astype(np.int32)
        cv2.fillConvexPoly(canvas, box, 255)
    n, labels = cv2.connectedComponents(canvas)
    grids = []
    for label in range(1, n):
        members = [c for c in cells if labels[int(c[0][1]), int(c[0][0])] == label]
        # de-duplicate outer/inner contours of the same cell
        uniq = []
        for m in members:
            if all(math.hypot(m[0][0] - u[0][0], m[0][1] - u[0][1]) > 0.5 * m[3] for u in uniq):
                uniq.append(m)
        if len(uniq) == 1:
            # a single small box: a basic dimension or a datum feature label
            (cx, cy), (rw, rh), ang, short = uniq[0]
            if short <= 0.02 * w:
                quad = cv2.boxPoints(((cx, cy), (rw, rh), ang))
                poly = [(float(p[0]) / w, float(p[1]) / h) for p in quad]
                grids.append({"poly": poly, "bbox": Box.from_points(poly), "cells": 1})
            continue
        pts = np.concatenate([cv2.boxPoints((m[0], m[1], m[2])) for m in uniq]).astype(np.float32)
        (gx, gy), (gw, gh), gang = cv2.minAreaRect(pts)
        short, long_ = min(gw, gh), max(gw, gh)
        # frames are long and thin; big squares are part geometry
        if long_ < 1.8 * short or short > 0.05 * w:
            continue
        quad = cv2.boxPoints(((gx, gy), (gw, gh), gang))
        poly = [(float(p[0]) / w, float(p[1]) / h) for p in quad]
        grids.append({"poly": poly, "bbox": Box.from_points(poly), "cells": len(uniq)})
    return grids


def merge_regions(ocr: list[Region], layer: list[Region], grids: list[dict], sheet: int) -> list[Region]:
    merged = list(ocr)
    for t in layer:
        dup = next((r for r in merged if iou(r.bbox, t.bbox) > 0.3 or overlap_ratio(t.bbox, r.bbox) > 0.6), None)
        if dup is not None:
            if "text_layer" not in dup.sources:
                dup.sources.append("text_layer")
            continue
        merged.append(t)
    g_i = 0
    for g in sorted(grids, key=lambda x: -x["cells"]):  # frames first, then single boxes
        hits = [r for r in merged if overlap_ratio(r.bbox, g["bbox"].pad(0.004)) > 0.5]
        if g["cells"] == 1:
            # only a boxed region counts as boxed: its text must fill most of the single box
            hits = [r for r in hits if r.bbox.area > 0.25 * g["bbox"].area and r.fcf_cells == 0]
            for r in hits:
                r.fcf_cells = 1
            continue
        if hits:
            for r in hits:
                r.fcf_cells = max(r.fcf_cells, g["cells"])
                r.grid_polys.append(g["poly"])
                if "fcf_grid" not in r.sources:
                    r.sources.append("fcf_grid")
            continue
        if g["cells"] >= 3:
            merged.append(
                Region(f"s{sheet}-g{g_i}", sheet, "", g["bbox"], g["poly"], 0.0, 0.5, ["fcf_grid"], g["cells"])
            )
            g_i += 1
    return merged


def group_line_fragments(regions: list[Region], aspect: float = 612 / 792) -> list[list[Region]]:
    """aspect = page height / page width."""
    remaining = sorted(regions, key=lambda r: (r.bbox.y, r.bbox.x))
    groups: list[list[Region]] = []
    used: set[str] = set()
    for r in remaining:
        if r.region_id in used:
            continue
        group = [r]
        used.add(r.region_id)
        changed = True
        while changed:
            changed = False
            for o in remaining:
                if o.region_id in used:
                    continue
                if any(_same_line(g, o, aspect) for g in group):
                    group.append(o)
                    used.add(o.region_id)
                    changed = True
        groups.append(sorted(group, key=lambda g: g.bbox.x))
    return groups


def _same_line(a: Region, b: Region, aspect: float) -> bool:
    """Fragments OCR split along one text line (e.g. '.05ABC', 'C', 'APPLIES TO ALL')."""
    if abs(_norm_angle(a.angle) - _norm_angle(b.angle)) > 8:
        return False
    if abs(_norm_angle(a.angle)) > 8:
        # rotated fragments: only join when boxes nearly touch
        return iou(a.bbox.pad(0.004), b.bbox.pad(0.004)) > 0.05 and abs(a.bbox.h - b.bbox.h) < 0.5 * max(a.bbox.h, b.bbox.h)
    top, bot = max(a.bbox.y, b.bbox.y), min(a.bbox.y2, b.bbox.y2)
    v_overlap = (bot - top) / min(a.bbox.h, b.bbox.h) if min(a.bbox.h, b.bbox.h) > 0 else 0
    gap = max(a.bbox.x, b.bbox.x) - min(a.bbox.x2, b.bbox.x2)
    height = max(a.bbox.h, b.bbox.h)
    return v_overlap > 0.5 and gap < 1.5 * height * aspect


def _norm_angle(a: float) -> float:
    while a > 90:
        a -= 180
    while a < -90:
        a += 180
    return a


def group_box(group: list[Region]) -> Box:
    return union_box([g.bbox for g in group])
