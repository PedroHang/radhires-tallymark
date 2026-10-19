# Answer key: FTC-06, complete Form 3 BoC (nothing missing)

> **Synthetic BoC built for testing from a public NIST test drawing; misses were planted on purpose. It is not a real customer BoC.**

| | |
| --- | --- |
| Drawing | `drawing.pdf`: NIST FTC-06 (PMI Fully-Toleranced Test Case 6), 3 sheets, from the public NIST MBE PMI test set (`nist_ftc_06_asme1_rd.pdf`) |
| BoC | `boc.xlsx`: AS9102 Form 3 layout (.xlsx), 48 rows, no coordinates: each row's Reference Location gives the sheet and zone, and Tallymark places the row by matching its text |
| The complete BoC | 48 rows, transcribed by hand from the rendered sheets |
| Planted | nothing (this BoC is complete) |

## How to test

1. Open Tallymark ([live](https://radhires-tallymark.vercel.app/) or `http://localhost:8031`).
2. Drop `drawing.pdf` on the drawing box and `boc.xlsx` on the BoC box.
3. Leave **Use the AI** off (the answer below is the rules-only check), then click **Run check**.
4. Compare the flag list on the right with the table below.

## Expected flags: none

Every requirement on the drawing has a row, so the results screen should say nothing is missing and show **0 flags**. Covered items are greyed out on the drawing.

## What the app actually did (verified 2026-10-19)

- Rules only, model check off: **0 flags**; planted items matched exactly: **yes**.
- Rows read from the BoC: 48. Rows placed on the drawing: 44 of 48.
- Run time: 0.7 s on a laptop CPU. The text read (OCR) of this drawing ships with the app in `samples/cache/`, so no sheet is OCR'd again; on the live site add a few seconds for a cold start. A drawing with no cached read takes about 6 to 9 s per sheet for OCR.
- Notices on the results screen: 
  - "4 rows could not be placed on the drawing (CN 7, 22, 24, 34). Anything they cover may show as unaccounted."
  - "This BoC has no location columns. Rows were located by matching text, so a few matches may be off."
  - "The model check did not run (it is turned off). 2 ambiguous leftovers are flagged with low confidence for you to review."
  (That is the app's standard wording when the model check is off. In this run the ambiguous items scored under the 0.5 flag threshold, so none of them became a flag.)

## Good to know

- With **Use the AI** on (and a key), the model also judges ambiguous leftovers, so results can differ slightly from this key.
- Only the misses listed above were planted. Every other row matches the drawing.
- Source of the drawing: NIST MBE PMI validation and conformance testing test cases (public). The original file is `samples/nist_ftc_06_asme1_rd.pdf` in this repo.
