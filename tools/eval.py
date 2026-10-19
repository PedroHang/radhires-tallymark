"""Evaluation harness. All numbers it prints are measured, on seeded data only.

Two tiers, reported separately because they measure different things:

Tier A, hand-verified ground truth (FTC-06). Rows were transcribed from the rendered sheets
and include requirements OCR never reads, so OCR misses count against recall here.
FTC-06 is also the demo drawing, which the pipeline was tuned on, so Tier A is not held out.

Tier B, reader-proposed ground truth (the other 10 NIST drawings, never used for tuning).
Every region group the deterministic rules call a requirement becomes a row (text lightly
reformatted so it is not byte-identical to OCR). This isolates the flagging logic (coverage
matching, grouping, exclusions, places) on unseen layouts. It cannot measure OCR recall,
because a requirement OCR never reads is not in this ground truth.

Seeded variants per drawing: drop rows (any sheet), drop a row on sheet 2+ when there is one,
reduce places on an nX row, jitter every bbox by up to 3% (noise, not an error), and a strip_bbox
copy of each variant for the no-location path.

Usage: python -m tools.eval [--variants 2] [--seeds-per-variant 4]
       python -m tools.eval --llm [--investigator] [--provider anthropic] [--model ...]

--llm turns the model check on (key from the environment: ANTHROPIC_API_KEY, OPENAI_API_KEY or
GEMINI_API_KEY), --investigator adds the bounded tool loop for ambiguous items. Those runs write
eval/results_llm.json and eval/REPORT_llm.md (the rules-only report is left alone) and report
model calls, tokens and cost per drawing.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import random
import re
import time
from pathlib import Path

from app import store
from app.pipeline.classify import classify_text, places_in, tidy_ocr_text
from app.pipeline.exclusions import DEFAULT_EXCLUSIONS, apply_exclusions, pick_template
from app.pipeline.geometry import Box, union_box
from app.pipeline.regions import Region, group_line_fragments, open_drawing
from app.pipeline.run import RunOptions, _cache_dir, execute, new_run_doc
from tools.make_samples import COLUMNS

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
OUT = ROOT / "eval"
FTC06 = SAMPLES / "nist_ftc_06_asme1_rd.pdf"


MODEL: dict = {"use_llm": False}  # set by main() from --llm / --investigator / --provider / --model
USAGE: dict[str, dict] = {}  # per drawing: model calls, tokens, cost over every eval run of it


def _note_usage(pdf: Path, doc: dict) -> None:
    st = doc.get("stats") or {}
    u = USAGE.setdefault(pdf.name, {"runs": 0, "llm_calls": 0, "input_tokens": 0, "output_tokens": 0,
                                    "cost_usd": 0.0, "errors": [], "investigated": 0, "linked_by_ai": 0})
    u["runs"] += 1
    u["llm_calls"] += st.get("llm_calls", 0)
    u["input_tokens"] += st.get("llm_input_tokens", 0)
    u["output_tokens"] += st.get("llm_output_tokens", 0)
    u["cost_usd"] = None if (u["cost_usd"] is None or st.get("cost_usd") is None) else u["cost_usd"] + st["cost_usd"]
    u["investigated"] += (st.get("investigator") or {}).get("investigated", 0)
    u["linked_by_ai"] += st.get("linked_by_ai", 0)
    for e in (st.get("llm_error"), st.get("transcription_error")):
        if e and e not in u["errors"]:
            u["errors"].append(e)


def _run(pdf: Path, boc_csv: str, boc_name: str, model_check: bool = True) -> tuple[dict, float]:
    d = pdf.read_bytes()
    b = boc_csv.encode("utf-8")
    opts = RunOptions(**MODEL) if model_check else RunOptions(use_llm=False)
    run_id = f"eval_{abs(hash((pdf.name, boc_name, len(b)))) % 10**10}"
    doc = new_run_doc(run_id, "eval", {"filename": pdf.name, "sha256": store.sha256(d)},
                      {"filename": boc_name, "sha256": store.sha256(b)}, opts)
    store.put_run(doc)
    t0 = time.perf_counter()
    execute(run_id, d, pdf.name, b, boc_name, opts)
    elapsed = time.perf_counter() - t0
    out = store.get_run(run_id)
    store.delete_run(run_id)
    if opts.use_llm:
        _note_usage(pdf, out)
    return out, elapsed


def _csv(rows: list[dict], columns: list[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def _cold_ocr_seconds(pdf: Path) -> tuple[float | None, int]:
    """Warm the region cache (timing the cold read once) and return seconds per sheet."""
    data = pdf.read_bytes()
    cache = _cache_dir(store.sha256(data))
    meta = cache / "timing.json"
    doc = open_drawing(data, pdf.name)
    if not (cache / f"sheet_{doc.page_count}_regions.json").exists():
        t0 = time.perf_counter()
        _run(pdf, "char_no,sheet,requirement\n1,1,X\n", "warm.csv", model_check=False)
        meta.write_text(json.dumps({"seconds": time.perf_counter() - t0, "sheets": doc.page_count}), encoding="utf-8")
    if meta.exists():
        m = json.loads(meta.read_text(encoding="utf-8"))
        return m["seconds"] / m["sheets"], m["sheets"]
    return None, doc.page_count


def reader_proposed_rows(pdf: Path) -> list[dict]:
    data = pdf.read_bytes()
    cache = _cache_dir(store.sha256(data))
    doc = open_drawing(data, pdf.name)
    page = doc[0].rect
    aspect = page.height / page.width
    template = pick_template(doc[0].get_text("text"))
    rows = []
    for n in range(1, doc.page_count + 1):
        regions = [Region.from_dict(r) for r in json.loads((cache / f"sheet_{n}_regions.json").read_text(encoding="utf-8"))]
        apply_exclusions(regions, template, DEFAULT_EXCLUSIONS)
        for group in group_line_fragments([r for r in regions if not r.excluded_by], aspect):
            text = " ".join(g.text for g in group if g.text)
            v = classify_text(text, max(g.fcf_cells for g in group))
            if v.outcome != "requirement":
                continue
            b = union_box([g.bbox for g in group])
            req = re.sub(r"(^|\s)0\.(?=\d)", r"\1Ø.", tidy_ocr_text(text))  # extractor-style formatting
            rows.append({"sheet": n, "char_type": v.kind, "requirement": req, "places": places_in(text) or 1,
                         "bbox_x": round(b.x, 4), "bbox_y": round(b.y, 4), "bbox_w": round(b.w, 4), "bbox_h": round(b.h, 4)})
    for i, r in enumerate(rows, start=1):
        r["char_no"] = str(i)
    return rows


def hand_rows() -> list[dict]:
    with (SAMPLES / "ftc06_boc_groundtruth.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["sheet"] = int(r["sheet"])
        r["places"] = int(r["places"])
        for k in ("bbox_x", "bbox_y", "bbox_w", "bbox_h"):
            r[k] = float(r[k])
    return rows


def make_variant(rows: list[dict], rng: random.Random, k: int) -> tuple[list[dict], list[dict]]:
    out = [dict(r) for r in rows]
    seeded = []
    # the places check reads the multiplier on the requirement's own callout line, so only rows
    # whose own text carries "nX" / "n PLCS" are seeded (a frame under a "2X" line is a known limit)
    multi = [r for r in out if r["places"] > 1 and places_in(r["requirement"])]
    later = [r for r in out if r["sheet"] > 1]
    ops = []
    if multi:
        ops.append("reduce_places")
    if later:
        ops.append("drop_later_sheet_row")
    ops += ["drop_row"] * max(0, k - len(ops))
    for op in ops[:k]:
        pool = {"reduce_places": multi, "drop_later_sheet_row": later, "drop_row": out}[op]
        pool = [r for r in pool if r in out and not any(s["row"] is r for s in seeded)]
        if not pool:
            continue
        r = rng.choice(pool)
        box = {"x": r["bbox_x"], "y": r["bbox_y"], "w": r["bbox_w"], "h": r["bbox_h"]}
        if op == "reduce_places":
            seeded.append({"op": op, "row": r, "sheet": r["sheet"], "bbox": box, "expect": "places"})
            r["places"] = 1
        else:
            seeded.append({"op": op, "row": r, "sheet": r["sheet"], "bbox": box, "expect": "unaccounted"})
    for s in seeded:
        if s["op"] != "reduce_places":
            out.remove(s["row"])
    for r in out:  # jitter_bbox: up to 3% position noise, not an error
        r["bbox_x"] = round(r["bbox_x"] + rng.uniform(-0.03, 0.03) * r["bbox_w"], 4)
        r["bbox_y"] = round(r["bbox_y"] + rng.uniform(-0.03, 0.03) * r["bbox_h"], 4)
    for i, r in enumerate(out, start=1):
        r["char_no"] = str(i)
    return out, [{k: v for k, v in s.items() if k != "row"} | {"requirement": s["row"]["requirement"]} for s in seeded]


def _hit(seed: dict, flags: list[dict]) -> dict | None:
    b = Box.from_dict(seed["bbox"])
    cx, cy = b.center
    for f in flags:
        fb = Box.from_dict(f["bbox"]).pad(0.01)
        if f["sheet"] == seed["sheet"] and f["class"] == seed["expect"] and fb.contains((cx, cy)):
            return f
    return None


def score(flags: list[dict], seeded: list[dict]) -> dict:
    matched = set()
    hits = 0
    for s in seeded:
        f = _hit(s, [x for x in flags if x["flag_id"] not in matched])
        if f:
            hits += 1
            matched.add(f["flag_id"])
    return {"seeded": len(seeded), "hits": hits, "flags": len(flags), "true_flags": len(matched)}


def evaluate(pdf: Path, rows: list[dict], variants: int, k: int, rng_seed: int) -> dict:
    no_bbox_cols = [c for c in COLUMNS if not c.startswith("bbox")]
    clean, _ = _run(pdf, _csv(rows, COLUMNS), "clean.csv")
    res = {"drawing": pdf.name, "rows": len(rows), "sheets": clean["drawing"]["sheets"],
           "clean_false_flags": len(clean["flags"]),
           "clean_false_flags_rules_only": sum(1 for f in clean["flags"] if f["rule_ids"] != ["R8"]),
           "clean_false_flag_texts": [f"{f['rule_ids'][0]}: {f['drawing_text']}" for f in clean["flags"]],
           "variants": []}
    for v in range(variants):
        rng = random.Random(rng_seed * 100 + v)
        var_rows, seeded = make_variant(rows, rng, k)
        doc, secs = _run(pdf, _csv(var_rows, COLUMNS), f"variant{v}.csv")
        nob, _ = _run(pdf, _csv(var_rows, no_bbox_cols), f"variant{v}_nobbox.csv")
        res["variants"].append({
            "seeded": [{k2: s[k2] for k2 in ("op", "sheet", "requirement", "expect")} for s in seeded],
            "with_bbox": score(doc["flags"], seeded), "no_bbox": score(nob["flags"], seeded),
            "rows": len(var_rows), "warm_seconds": round(secs, 2),
        })
    return res


def aggregate(results: list[dict]) -> dict:
    def tot(key, field):
        return sum(v[key][field] for r in results for v in r["variants"])

    seeded = tot("with_bbox", "seeded")
    agg = {
        "drawings": len(results),
        "sheets": sum(r["sheets"] for r in results),
        "seeded_items": seeded,
        "recall": tot("with_bbox", "hits") / seeded if seeded else None,
        "precision": tot("with_bbox", "true_flags") / tot("with_bbox", "flags") if tot("with_bbox", "flags") else None,
        "false_flags_per_clean_sheet": sum(r["clean_false_flags"] for r in results) / max(1, sum(r["sheets"] for r in results)),
        "false_flags_per_clean_sheet_rules_only": sum(r["clean_false_flags_rules_only"] for r in results) / max(1, sum(r["sheets"] for r in results)),
        "review_load": tot("with_bbox", "flags") / max(1, sum(v["rows"] for r in results for v in r["variants"])),
        "recall_no_bbox": tot("no_bbox", "hits") / seeded if seeded else None,
        "precision_no_bbox": tot("no_bbox", "true_flags") / tot("no_bbox", "flags") if tot("no_bbox", "flags") else None,
    }
    by_op: dict[str, list[int]] = {}
    for r in results:
        for v in r["variants"]:
            for s in v["seeded"]:
                by_op.setdefault(s["op"], [0, 0])[1] += 1
    # per-op recall needs per-seed hits; recompute cheaply from stored flags is not kept, so report counts only
    agg["seeded_by_op"] = {k: v[1] for k, v in by_op.items()}
    return agg


def fmt(x, pct=True):
    if x is None:
        return "n/a"
    return f"{x * 100:.0f}%" if pct else f"{x:.2f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", type=int, default=2)
    ap.add_argument("--seeds-per-variant", type=int, default=4)
    ap.add_argument("--llm", action="store_true", help="model check on (key from the environment)")
    ap.add_argument("--investigator", action="store_true", help="with --llm: bounded tool loop for ambiguous items")
    ap.add_argument("--provider", default=None)
    ap.add_argument("--model", default=None)
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    suffix = ""
    if a.llm:
        from app.pipeline.llm import DEFAULT_MODEL, DEFAULT_PROVIDER, PROVIDERS, resolve_credential

        provider = a.provider or DEFAULT_PROVIDER
        model = a.model or (DEFAULT_MODEL if provider == DEFAULT_PROVIDER else PROVIDERS[provider]["models"][0])
        if not resolve_credential(provider, None)[0]:
            raise SystemExit(f"--llm needs a key for {provider} in the environment ({', '.join(PROVIDERS[provider]['env'])}).")
        MODEL.update(use_llm=True, provider=provider, model=model, investigator=bool(a.investigator))
        suffix = "_llm"
        print(f"model check on: {provider} {model}, investigator {'on' if a.investigator else 'off'}", flush=True)

    drawings = [FTC06] + sorted((SAMPLES / "eval").glob("nist_*.pdf"))
    timing = {}
    for pdf in drawings:
        print(f"reading {pdf.name} ...", flush=True)
        timing[pdf.name] = _cold_ocr_seconds(pdf)

    tier_a = [evaluate(FTC06, hand_rows(), a.variants, a.seeds_per_variant, 6)]
    tier_b = []
    for i, pdf in enumerate(drawings[1:], start=1):
        rows = reader_proposed_rows(pdf)
        if len(rows) < a.seeds_per_variant + 2:
            print(f"skip {pdf.name}: only {len(rows)} reader-proposed rows")
            continue
        print(f"evaluating {pdf.name} ({len(rows)} rows)", flush=True)
        tier_b.append(evaluate(pdf, rows, a.variants, a.seeds_per_variant, i))

    report = {"tier_a_hand_verified": {"results": tier_a, "summary": aggregate(tier_a)},
              "tier_b_reader_proposed": {"results": tier_b, "summary": aggregate(tier_b)},
              "cold_read_seconds_per_sheet": {k: (round(v[0], 1) if v[0] else None) for k, v in timing.items()},
              "settings": {"variants": a.variants, "seeds_per_variant": a.seeds_per_variant,
                           "model_check": "off" if not a.llm else f"{MODEL['provider']} {MODEL['model']}",
                           "investigator": bool(a.llm and a.investigator)},
              "model_usage_per_drawing": USAGE}
    (OUT / f"results{suffix}.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = ["# Tallymark: evaluation report", "",
             "Measured by `python -m tools.eval` on public NIST MBE PMI drawings with seeded changes. "
             + ("Model check off (rules only), so these numbers do not depend on an API key." if not a.llm else
                f"Model check ON ({MODEL['provider']} {MODEL['model']}), investigator "
                f"{'on' if a.investigator else 'off'}. Model calls, tokens and cost are in the last section."), "",
             "| Metric | Target | Tier A: FTC-06, hand-verified GT | Tier B: unseen NIST drawings, reader-proposed GT |",
             "| --- | --- | --- | --- |"]
    sa, sb = report["tier_a_hand_verified"]["summary"], report["tier_b_reader_proposed"]["summary"]
    rows_def = [
        ("Seeded recall", ">= 90%", "recall", True),
        ("Flag precision", ">= 70%", "precision", True),
        ("False flags per clean sheet (all)", "<= 2", "false_flags_per_clean_sheet", False),
        ("False flags per clean sheet (excluding R8 items the model check would judge)", "(not set)", "false_flags_per_clean_sheet_rules_only", False),
        ("Review load (flags / BoC rows)", "<= 10%", "review_load", True),
        ("Recall without bbox", ">= 75%", "recall_no_bbox", True),
        ("Precision without bbox", "(not set)", "precision_no_bbox", True),
    ]
    for label, target, key, pct in rows_def:
        lines.append(f"| {label} | {target} | {fmt(sa[key], pct)} | {fmt(sb[key], pct)} |")
    skipped = [p.name for p in drawings[1:] if p.name not in {r["drawing"] for r in tier_b}]
    lines += ["", f"Tier B skipped (fewer than {a.seeds_per_variant + 2} reader-proposed rows): {', '.join(skipped) or 'none'}."]
    lines += ["", f"Tier A: {sa['drawings']} drawing, {sa['sheets']} sheets, {sa['seeded_items']} seeded items. "
              f"Tier B: {sb['drawings']} drawings, {sb['sheets']} sheets, {sb['seeded_items']} seeded items.", "",
              "## What each tier can and cannot tell you", "",
              "- **Tier A** uses rows hand-transcribed from the rendered FTC-06 sheets, including one flatness callout "
              "OCR never reads. It is the demo drawing and the pipeline was tuned on it, so it is not held out.",
              "- **Tier B** builds ground truth from the reader's own requirement regions on drawings that were never "
              "used for tuning. It tests coverage matching, grouping, exclusions and the places check on unseen "
              "layouts. It cannot measure OCR recall: a requirement OCR never reads is missing from this ground truth too.",
              "- Neither tier says anything about real customer drawings or about any real extractor's miss rate.", "",
              "## Per drawing", "", "| Drawing | Tier | Sheets | Rows | Clean false flags | Recall (bbox) | Recall (no bbox) | Flags on variants |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for tier, results in (("A", tier_a), ("B", tier_b)):
        for r in results:
            s = r["variants"]
            hits = sum(v["with_bbox"]["hits"] for v in s)
            nh = sum(v["no_bbox"]["hits"] for v in s)
            seeded = sum(v["with_bbox"]["seeded"] for v in s)
            flags = sum(v["with_bbox"]["flags"] for v in s)
            lines.append(f"| {r['drawing']} | {tier} | {r['sheets']} | {r['rows']} | {r['clean_false_flags']} | "
                         f"{hits}/{seeded} | {nh}/{seeded} | {flags} |")
    lines += ["", "## What the false flags on clean BoCs are", "",
              "R8 = a bare number with no tolerance or frame. With a key, the model decides these; in this run "
              "(model check off) they are flagged Low for review.", ""]
    for r in tier_a + tier_b:
        if r["clean_false_flag_texts"]:
            lines.append(f"- {r['drawing']}: " + "; ".join(f"`{t}`" for t in r["clean_false_flag_texts"]))
    lines += ["", "## Cold read time (OCR + frame detection, CPU, no cache)", "",
              "| Drawing | Seconds per sheet |", "| --- | --- |"]
    for k, v in report["cold_read_seconds_per_sheet"].items():
        lines.append(f"| {k} | {v if v is not None else 'cached before timing'} |")
    if a.llm:
        lines += ["", "## Model usage per drawing (all eval runs of it: clean + variants, with and without bbox)", "",
                  "| Drawing | Runs | Model calls | Input tokens | Output tokens | Cost (USD) | Investigated items | Linked by AI | Errors |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for name, u in USAGE.items():
            cost = f"{u['cost_usd']:.4f}" if u["cost_usd"] is not None else "not priced"
            lines.append(f"| {name} | {u['runs']} | {u['llm_calls']} | {u['input_tokens']} | {u['output_tokens']} | "
                         f"{cost} | {u['investigated']} | {u['linked_by_ai']} | {', '.join(u['errors']) or '-'} |")
    else:
        lines += ["", "Cost per sheet with the model check on is not measured here (no API key in the eval run). "
                  "The run stats record real token usage and cost whenever a key is set."]
    (OUT / f"REPORT{suffix}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:14]))


if __name__ == "__main__":
    main()
