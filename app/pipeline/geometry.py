"""Small geometry helpers. All coordinates are normalized 0-1 page units unless noted."""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class Box:
    x: float
    y: float
    w: float
    h: float

    @property
    def x2(self) -> float:
        return self.x + self.w

    @property
    def y2(self) -> float:
        return self.y + self.h

    @property
    def area(self) -> float:
        return max(self.w, 0.0) * max(self.h, 0.0)

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.w / 2, self.y + self.h / 2)

    def pad(self, px: float, py: float | None = None) -> "Box":
        py = px if py is None else py
        return Box(self.x - px, self.y - py, self.w + 2 * px, self.h + 2 * py)

    def contains(self, pt: tuple[float, float]) -> bool:
        return self.x <= pt[0] <= self.x2 and self.y <= pt[1] <= self.y2

    def to_dict(self) -> dict:
        return {"x": round(self.x, 5), "y": round(self.y, 5), "w": round(self.w, 5), "h": round(self.h, 5)}

    @staticmethod
    def from_dict(d: dict) -> "Box":
        return Box(float(d["x"]), float(d["y"]), float(d["w"]), float(d["h"]))

    @staticmethod
    def from_points(pts: list[tuple[float, float]]) -> "Box":
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return Box(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def intersection(a: Box, b: Box) -> float:
    ix = max(0.0, min(a.x2, b.x2) - max(a.x, b.x))
    iy = max(0.0, min(a.y2, b.y2) - max(a.y, b.y))
    return ix * iy


def iou(a: Box, b: Box) -> float:
    inter = intersection(a, b)
    union = a.area + b.area - inter
    return inter / union if union > 0 else 0.0


def overlap_ratio(region: Box, other: Box) -> float:
    """Share of `region` area that lies inside `other`."""
    return intersection(region, other) / region.area if region.area > 0 else 0.0


def union_box(boxes: list[Box]) -> Box:
    x = min(b.x for b in boxes)
    y = min(b.y for b in boxes)
    x2 = max(b.x2 for b in boxes)
    y2 = max(b.y2 for b in boxes)
    return Box(x, y, x2 - x, y2 - y)


def union_poly(polys: list[list[tuple[float, float]]], angle_deg: float) -> list[tuple[float, float]]:
    """Oriented bounding quad of several quads that share roughly the same text angle."""
    a = math.radians(angle_deg)
    ca, sa = math.cos(a), math.sin(a)
    pts = [p for poly in polys for p in poly]
    # rotate into text-aligned frame, take extents, rotate back
    us = [p[0] * ca + p[1] * sa for p in pts]
    vs = [-p[0] * sa + p[1] * ca for p in pts]
    u1, u2, v1, v2 = min(us), max(us), min(vs), max(vs)
    corners = [(u1, v1), (u2, v1), (u2, v2), (u1, v2)]
    return [(u * ca - v * sa, u * sa + v * ca) for u, v in corners]


def distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def zone_label(center: tuple[float, float], cols: int = 8, rows: int = 4) -> str:
    """Drafting-style zone: numbers run right-to-left (1 at the right edge),
    letters run bottom-to-top (A at the bottom). Computed from a uniform grid,
    because many sheets (including the NIST test drawings) print no zone marks."""
    cx, cy = center
    col = cols - min(cols - 1, max(0, int(cx * cols)))
    row_from_bottom = min(rows - 1, max(0, int((1 - cy) * rows)))
    return f"{chr(ord('A') + row_from_bottom)}{col}"


def _poly_area(poly) -> float:
    import cv2
    import numpy as np

    return float(abs(cv2.contourArea(np.array(poly, np.float32))))


def poly_intersection(a, b) -> float:
    """Intersection area of two convex quads (any rotation). Affine-invariant ratios, so
    normalized page coordinates are fine for IoU and overlap shares."""
    import cv2
    import numpy as np

    pa = np.array(a, np.float32).reshape(-1, 1, 2)
    pb = np.array(b, np.float32).reshape(-1, 1, 2)
    # intersectConvexConvex needs consistent orientation
    pa = cv2.convexHull(pa)
    pb = cv2.convexHull(pb)
    area, _ = cv2.intersectConvexConvex(pa, pb)
    return max(0.0, float(area))


def poly_iou(a, b) -> float:
    inter = poly_intersection(a, b)
    union = _poly_area(a) + _poly_area(b) - inter
    return inter / union if union > 0 else 0.0


def poly_overlap(a, b) -> float:
    """Share of quad `a` area inside quad `b`."""
    area = _poly_area(a)
    return poly_intersection(a, b) / area if area > 0 else 0.0


def box_poly(box: Box) -> list[tuple[float, float]]:
    return [(box.x, box.y), (box.x2, box.y), (box.x2, box.y2), (box.x, box.y2)]
