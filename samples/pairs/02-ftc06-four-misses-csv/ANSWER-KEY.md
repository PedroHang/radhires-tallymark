# Answer key: FTC-06, CSV with coordinates and 4 planted misses

> **Synthetic BoC built for testing from a public NIST test drawing; misses were planted on purpose. It is not a real customer BoC.**

| | |
| --- | --- |
| Drawing | `drawing.pdf`: NIST FTC-06 (PMI Fully-Toleranced Test Case 6), 3 sheets, from the public NIST MBE PMI test set (`nist_ftc_06_asme1_rd.pdf`) |
| BoC | `boc.csv`: CSV with coordinates, 45 rows, the bbox columns give each row's box on its sheet (page fractions) |
| The complete BoC | 48 rows, transcribed by hand from the rendered sheets |
| Planted | 4 |

## How to test

1. Open Tallymark ([live](https://radhires-tallymark.vercel.app/) or `http://localhost:8031`).
2. Drop `drawing.pdf` on the drawing box and `boc.csv` on the BoC box.
3. Leave **Use the AI** off (the answer below is the rules-only check), then click **Run check**.
4. Compare the flag list on the right with the table below.

## Expected flags: 4

| Flag | Type | Sheet | Zone | Requirement (as in the complete BoC) | Text the app read on the drawing | Why it is flagged |
| --- | --- | --- | --- | --- | --- | --- |
| U-1 | U (unaccounted) | 1 | C7 | ⌓ .05 A B C APPLIES TO ALL UNTOLERANCED SURFACES (NOTE 4) | `.05 A B C APPLIESTOALL` | general-note profile frame: row 1 of the complete BoC was deleted |
| P-1 | P (places short) | 1 | A4 | 4X Ø.281 ±.008 | `4X 0.281 ±.008` | places count: row CN 19 says Places 1, the callout says 4X (complete BoC: 4) |
| U-2 | U (unaccounted) | 2 | B3 | 12.00 ±.01 | `12.00 ±.01` | toleranced dimension: row 33 of the complete BoC was deleted |
| U-3 | U (unaccounted) | 3 | D3 | ⌖ .025 D C K | `Φ .025 D C K` | position frame: row 38 of the complete BoC was deleted |

The BoC was renumbered after the deletions (an extractor numbers what it found), so the char numbers do not give the deleted rows away.

## What the app actually did (verified 2026-10-19)

- Rules only, model check off: **4 flags**; planted items matched exactly: **yes**.
- Rows read from the BoC: 45. Rows placed on the drawing: 45 of 45.
- Run time: 0.5 s on a laptop CPU. The text read (OCR) of this drawing ships with the app in `samples/cache/`, so no sheet is OCR'd again; on the live site add a few seconds for a cold start. A drawing with no cached read takes about 6 to 9 s per sheet for OCR.
- The same check on the complete BoC (before the misses were planted): 0 flags.
- Notices on the results screen: none.

## Good to know

- With **Use the AI** on (and a key), the model also judges ambiguous leftovers, so results can differ slightly from this key.
- Only the misses listed above were planted. Every other row matches the drawing.
- This is the same pair the app loads with **Use sample drawing**.
- Source of the drawing: NIST MBE PMI validation and conformance testing test cases (public). The original file is `samples/nist_ftc_06_asme1_rd.pdf` in this repo.
