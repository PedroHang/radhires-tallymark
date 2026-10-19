# Answer key: CTC-04, Form 3 BoC (no coordinates) with 3 planted misses

> **Synthetic BoC built for testing from a public NIST test drawing; misses were planted on purpose. It is not a real customer BoC.**

| | |
| --- | --- |
| Drawing | `drawing.pdf`: NIST CTC-04 (PMI Complex Test Case 4), 1 sheet, from the public NIST MBE PMI test set (`nist_ctc_04_asme1_rd.pdf`) |
| BoC | `boc.xlsx`: AS9102 Form 3 layout (.xlsx), 11 rows, no coordinates: each row's Reference Location gives the sheet and zone, and Tallymark places the row by matching its text |
| The complete BoC | 13 rows, transcribed by hand from the rendered sheets |
| Planted | 3 |

## How to test

1. Open Tallymark ([live](https://radhires-tallymark.vercel.app/) or `http://localhost:8031`).
2. Drop `drawing.pdf` on the drawing box and `boc.xlsx` on the BoC box.
3. Leave **Use the AI** off (the answer below is the rules-only check), then click **Run check**.
4. Compare the flag list on the right with the table below.

## Expected flags: 3

| Flag | Type | Sheet | Zone | Requirement (as in the complete BoC) | Text the app read on the drawing | Why it is flagged |
| --- | --- | --- | --- | --- | --- | --- |
| P-1 | P (places short) | 1 | C2 | 4X M12 x 1.75 - 6H ↧ 25 ±0.25 | `4X M12 x 1.75 - 6H v 25 ±0.25` | places count: row CN 6 says Places 1, the callout says 4X (complete BoC: 4) |
| U-1 | U (unaccounted) | 1 | C1 | 30X Ø14 ±0.1 | `-30X 014 ±0.1` | toleranced dimension: row 10 of the complete BoC was deleted |
| U-2 | U (unaccounted) | 1 | B7 | ⌓ 0.5 A B C | `0.5 A B C` | profile frame: row 12 of the complete BoC was deleted |

The BoC was renumbered after the deletions (an extractor numbers what it found), so the char numbers do not give the deleted rows away.

## What the app actually did (verified 2026-10-19)

- Rules only, model check off: **3 flags**; planted items matched exactly: **yes**.
- Rows read from the BoC: 11. Rows placed on the drawing: 11 of 11.
- Run time: 0.3 s on a laptop CPU. The text read (OCR) of this drawing ships with the app in `samples/cache/`, so no sheet is OCR'd again; on the live site add a few seconds for a cold start. A drawing with no cached read takes about 6 to 9 s per sheet for OCR.
- The same check on the complete BoC (before the misses were planted): 0 flags.
- Notices on the results screen: 
  - "This BoC has no location columns. Rows were located by matching text, so a few matches may be off."
  - "The model check did not run (it is turned off). 1 ambiguous leftover is flagged with low confidence for you to review."
  (That is the app's standard wording when the model check is off. In this run the ambiguous items scored under the 0.5 flag threshold, so none of them became a flag.)

## Good to know

- With **Use the AI** on (and a key), the model also judges ambiguous leftovers, so results can differ slightly from this key.
- Only the misses listed above were planted. Every other row matches the drawing.
- Source of the drawing: NIST MBE PMI validation and conformance testing test cases (public). The original file is `samples/eval/nist_ctc_04_asme1_rd.pdf` in this repo.
