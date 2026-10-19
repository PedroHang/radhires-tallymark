"""Run a check from the command line and print the flags.

Usage: python -m tools.check <drawing.pdf> <boc.csv> [--no-llm] [--model claude-sonnet-5]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import uuid
from pathlib import Path

from app import store
from app.pipeline.run import RunOptions, execute, new_run_doc


def run_check(drawing: Path, boc: Path, use_llm: bool, model: str | None = None, exclusions=None) -> dict:
    d, b = drawing.read_bytes(), boc.read_bytes()
    opts = RunOptions(use_llm=use_llm, simulated="simulated" in boc.name)
    if model:
        opts.model = model
    if exclusions is not None:
        opts.exclusions = exclusions
    run_id = f"cli_{uuid.uuid4().hex[:8]}"
    doc = new_run_doc(run_id, dt.datetime.now(dt.timezone.utc).isoformat(),
                      {"filename": drawing.name, "sha256": store.sha256(d)},
                      {"filename": boc.name, "sha256": store.sha256(b)}, opts)
    store.put_run(doc)
    execute(run_id, d, drawing.name, b, boc.name, opts)
    return store.get_run(run_id)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("drawing")
    ap.add_argument("boc")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--model")
    a = ap.parse_args()
    doc = run_check(Path(a.drawing), Path(a.boc), not a.no_llm, a.model)
    if doc["status"] != "complete":
        print(json.dumps(doc["error"], indent=2))
        return
    print(json.dumps(doc["stats"], indent=2))
    for s in doc["stages"]:
        print(f"  {s['name']:28} {s['count']}  {s['ms']}ms")
    for f in doc["flags"]:
        print(f"{f['flag_id']:4} s{f['sheet']} {f['zone']:3} {f['confidence']:6} {f['rule_ids']} {f['title']} | {f['drawing_text']!r} | {f['reason']}")
    print("warnings:", doc["warnings"])


if __name__ == "__main__":
    main()
