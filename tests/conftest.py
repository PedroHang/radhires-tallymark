"""Every test run uses a throwaway data directory, never the app's real data/ store.

The OCR cache for the bundled sample is seeded from the repo's precomputed copy (or, when
present, from a local data/cache) so the suite does not re-run OCR on every session.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app import store

ROOT = Path(__file__).resolve().parent.parent
SEED_DIRS = [ROOT / "samples" / "cache", ROOT / "data" / "cache"]


@pytest.fixture(scope="session", autouse=True)
def isolated_data_dir(tmp_path_factory):
    base = tmp_path_factory.mktemp("tallymark-data")
    previous = store.DATA
    store.configure(base)
    from app.pipeline.run import _cache_dir

    sample_key = _cache_dir(store.sha256((ROOT / "samples" / "nist_ftc_06_asme1_rd.pdf").read_bytes())).name
    (store.CACHE / sample_key).rmdir()  # _cache_dir creates it empty; the seed copy goes in its place
    for seed in SEED_DIRS:
        if seed.is_dir():
            d = seed / sample_key
            if d.is_dir() and not (store.CACHE / sample_key).exists():
                shutil.copytree(d, store.CACHE / sample_key)
    yield base
    store.configure(previous)
