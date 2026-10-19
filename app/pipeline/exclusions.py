"""Stage 4: exclusions. Hide regions that are never inspectable requirements.

Zone templates mask fixed sheet furniture (title, border strip, revision tag).
Rule exclusions hide text by pattern. Exclusions never delete BoC rows and
never un-cover a covered region; they only stop leftovers from being flagged.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .geometry import Box
from .regions import Region

EXCLUSION_LABELS = {
    "title_block": "Title block",
    "border": "Border and zone labels",
    "revision_block": "Revision block",
    "view_labels": "View labels (SECTION A-A, DETAIL B)",
    "reference_dims": "Reference dimensions (in parentheses)",
    "scale_notes": "Scale notes",
    "basic_dims": "Basic dimensions (boxed numbers)",
}
DEFAULT_EXCLUSIONS = list(EXCLUSION_LABELS.keys())


@dataclass
class Template:
    name: str
    zones: dict[str, list[Box]]  # exclusion key -> zones where it applies


# NIST MBE PMI test drawings. All sheets share one frame layout (title band,
# caption, rotated file name, Rev tag), so fixed zones work across the set.
NIST_PMI = Template(
    name="nist_pmi",
    zones={
        "border": [
            Box(0.0, 0.0, 1.0, 0.072),    # green "NIST PMI Test Models" title band
            Box(0.952, 0.0, 0.048, 1.0),   # right strip with rotated file name
            Box(0.0, 0.0, 0.043, 1.0),     # left margin
            Box(0.0, 0.968, 1.0, 0.032),   # bottom margin
        ],
        "title_block": [
            Box(0.30, 0.895, 0.52, 0.075),  # test case caption, atomic test case list
            Box(0.755, 0.072, 0.20, 0.06),  # "Feature and Specification Index" box
        ],
        "revision_block": [Box(0.88, 0.93, 0.075, 0.04)],  # "Rev D"
    },
)

GENERIC = Template(
    name="generic",
    zones={
        "border": [Box(0, 0, 1, 0.03), Box(0, 0.97, 1, 0.03), Box(0, 0, 0.03, 1), Box(0.97, 0, 0.03, 1)],
        "title_block": [Box(0.62, 0.80, 0.35, 0.17)],
        "revision_block": [Box(0.62, 0.03, 0.35, 0.08)],
    },
)


def pick_template(first_page_text: str) -> Template:
    return NIST_PMI if "NIST PMI Test Models" in first_page_text else GENERIC


RE_VIEW_LABEL = re.compile(r"^\s*(SECTION|DETAIL|VIEW)\b", re.I)
RE_SCALE = re.compile(r"(\bSCALE\b|\bSHEET\s*\d+\s*OF\s*\d+|VIEW\s*ROTATED)", re.I)
RE_REFERENCE = re.compile(r"^\s*\(.*\)\s*[-.]?\s*$")
RE_BARE_NUMBER = re.compile(r"^\s*[\d.]+\s*$")


def apply_exclusions(
    regions: list[Region],
    template: Template,
    enabled: list[str],
    extra_regex: str | None = None,
) -> int:
    """Marks region.excluded_by in place. Returns how many were excluded."""
    extra = None
    if extra_regex:
        try:
            extra = re.compile(extra_regex, re.I)
        except re.error:
            extra = None
    n = 0
    for r in regions:
        if r.covered_by:
            continue
        reason = _exclusion_for(r, template, set(enabled), extra)
        if reason:
            r.excluded_by = reason
            n += 1
    return n


def _exclusion_for(r: Region, t: Template, enabled: set[str], extra: re.Pattern | None) -> str | None:
    cx, cy = r.bbox.center
    for key in ("border", "title_block", "revision_block"):
        if key in enabled and any(z.contains((cx, cy)) for z in t.zones.get(key, [])):
            return key
    text = r.text.strip()
    if "view_labels" in enabled and RE_VIEW_LABEL.search(text):
        return "view_labels"
    if "scale_notes" in enabled and RE_SCALE.search(text):
        return "scale_notes"
    if "reference_dims" in enabled and RE_REFERENCE.match(text):
        return "reference_dims"
    if "basic_dims" in enabled and r.fcf_cells == 1 and RE_BARE_NUMBER.match(text):
        return "basic_dims"
    if extra is not None and extra.search(text):
        return "custom_regex"
    return None
