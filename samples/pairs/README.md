# Sample pairs to test Tallymark

Five ready-made drawing + Bill of Characteristics (BoC) pairs, each with a short answer key.
The drawings are real, public NIST MBE PMI test drawings. **The BoCs are synthetic**: built for
testing by transcribing each drawing's requirements by hand, then deleting rows or lowering a
places count on purpose. None of them is a real customer BoC.

**How to use them**

1. Download a pair (or [all five as one zip](https://github.com/PedroHang/radhires-tallymark/raw/main/samples/pairs/tallymark-sample-pairs.zip), 0.5 MB).
2. In Tallymark ([live](https://radhires-tallymark.vercel.app/) or local), drop the drawing and the BoC, leave **Use the AI** off, click **Run check**.
3. Compare the flags with the pair's `ANSWER-KEY.md` (flag id, sheet, zone, and why).

| # | Drawing | BoC format | What was planted | Expected flags | Files |
| --- | --- | --- | --- | --- | --- |
| 01 | NIST FTC-06, 3 sheets | AS9102 Form 3 `.xlsx`, 48 rows, no coordinates | Nothing: the BoC is complete | **0** ("nothing missing") | [drawing](https://github.com/PedroHang/radhires-tallymark/raw/main/samples/pairs/01-ftc06-clean-form3/drawing.pdf) · [BoC](https://github.com/PedroHang/radhires-tallymark/raw/main/samples/pairs/01-ftc06-clean-form3/boc.xlsx) · [answer key](01-ftc06-clean-form3/ANSWER-KEY.md) |
| 02 | NIST FTC-06, 3 sheets | CSV with coordinates, 45 rows | 3 rows deleted (sheets 1, 2, 3), one 4X row set to 1 place | **4**: U-1, P-1, U-2, U-3 | [drawing](https://github.com/PedroHang/radhires-tallymark/raw/main/samples/pairs/02-ftc06-four-misses-csv/drawing.pdf) · [BoC](https://github.com/PedroHang/radhires-tallymark/raw/main/samples/pairs/02-ftc06-four-misses-csv/boc.csv) · [answer key](02-ftc06-four-misses-csv/ANSWER-KEY.md) |
| 03 | NIST CTC-04, 1 sheet | AS9102 Form 3 `.xlsx`, 11 rows, no coordinates | A profile frame and a 30X hole row deleted; 4X M12 thread set to 1 place | **3**: P-1, U-1, U-2 | [drawing](https://github.com/PedroHang/radhires-tallymark/raw/main/samples/pairs/03-ctc04-form3-misses/drawing.pdf) · [BoC](https://github.com/PedroHang/radhires-tallymark/raw/main/samples/pairs/03-ctc04-form3-misses/boc.xlsx) · [answer key](03-ctc04-form3-misses/ANSWER-KEY.md) |
| 04 | NIST CTC-03, 1 sheet | CSV with coordinates, 19 rows | A profile frame and a diameter row deleted; 4X .82 set to 1 place | **3**: U-1, U-2, P-1 | [drawing](https://github.com/PedroHang/radhires-tallymark/raw/main/samples/pairs/04-ctc03-csv-misses/drawing.pdf) · [BoC](https://github.com/PedroHang/radhires-tallymark/raw/main/samples/pairs/04-ctc03-csv-misses/boc.csv) · [answer key](04-ctc03-csv-misses/ANSWER-KEY.md) |
| 05 | NIST FTC-11, 2 sheets | CSV with coordinates, 8 rows | A diameter deleted on sheet 1; a profile frame deleted and 2X R0.1 set to 1 place on sheet 2 | **3**: U-1 (sheet 1), U-2 and P-1 (sheet 2) | [drawing](https://github.com/PedroHang/radhires-tallymark/raw/main/samples/pairs/05-ftc11-multisheet/drawing.pdf) · [BoC](https://github.com/PedroHang/radhires-tallymark/raw/main/samples/pairs/05-ftc11-multisheet/boc.csv) · [answer key](05-ftc11-multisheet/ANSWER-KEY.md) |

<kbd>U</kbd> = a requirement on the drawing with no BoC row. <kbd>P</kbd> = an "NX" callout whose row lists fewer places.

Every expected result above was produced by running the real pipeline, rules only (no model, no
key), and every complete BoC (before the misses were planted) gives 0 flags. Pair 02 is the same
pair the app loads with **Use sample drawing**.

Rebuild and re-verify all five with `TALLYMARK_DATA_DIR=<temp dir> python -m tools.make_pairs`.
It writes the files and answer keys and fails if any pair stops matching its planted items.
