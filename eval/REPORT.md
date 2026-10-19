# Tallymark: evaluation report

Measured by `python -m tools.eval` on public NIST MBE PMI drawings with seeded changes. Model check off (rules only), so these numbers do not depend on an API key.

| Metric | Target | Tier A: FTC-06, hand-verified GT | Tier B: unseen NIST drawings, reader-proposed GT |
| --- | --- | --- | --- |
| Seeded recall | >= 90% | 100% | 98% |
| Flag precision | >= 70% | 100% | 34% |
| False flags per clean sheet (all) | <= 2 | 0.00 | 2.78 |
| False flags per clean sheet (excluding R8 items the model check would judge) | (not set) | 0.00 | 0.48 |
| Review load (flags / BoC rows) | <= 10% | 9% | 117% |
| Recall without bbox | >= 75% | 62% | 94% |
| Precision without bbox | (not set) | 45% | 50% |

Tier B skipped (fewer than 6 reader-proposed rows): nist_ctc_05_asme1_rd.pdf, nist_ftc_11_asme1_rb.pdf.

Tier A: 1 drawing, 3 sheets, 8 seeded items. Tier B: 8 drawings, 23 sheets, 64 seeded items.

## What each tier can and cannot tell you

- **Tier A** uses rows hand-transcribed from the rendered FTC-06 sheets, including one flatness callout OCR never reads. It is the demo drawing and the pipeline was tuned on it, so it is not held out.
- **Tier B** builds ground truth from the reader's own requirement regions on drawings that were never used for tuning. It tests coverage matching, grouping, exclusions and the places check on unseen layouts. It cannot measure OCR recall: a requirement OCR never reads is missing from this ground truth too.
- Neither tier says anything about real customer drawings or about any real extractor's miss rate.

## Per drawing

| Drawing | Tier | Sheets | Rows | Clean false flags | Recall (bbox) | Recall (no bbox) | Flags on variants |
| --- | --- | --- | --- | --- | --- | --- | --- |
| nist_ftc_06_asme1_rd.pdf | A | 3 | 48 | 0 | 8/8 | 5/8 | 8 |
| nist_ctc_01_asme1_rd.pdf | B | 1 | 6 | 8 | 8/8 | 8/8 | 24 |
| nist_ctc_02_asme1_rc.pdf | B | 3 | 13 | 3 | 8/8 | 8/8 | 12 |
| nist_ctc_03_asme1_rc.pdf | B | 1 | 9 | 0 | 8/8 | 7/8 | 8 |
| nist_ctc_04_asme1_rd.pdf | B | 1 | 6 | 1 | 8/8 | 8/8 | 8 |
| nist_ftc_07_asme1_rd.pdf | B | 4 | 9 | 16 | 7/8 | 7/8 | 40 |
| nist_ftc_08_asme1_rc.pdf | B | 4 | 10 | 18 | 8/8 | 7/8 | 43 |
| nist_ftc_09_asme1_rd.pdf | B | 4 | 22 | 9 | 8/8 | 7/8 | 26 |
| nist_ftc_10_asme1_rb.pdf | B | 5 | 30 | 9 | 8/8 | 8/8 | 26 |

## What the false flags on clean BoCs are

R8 = a bare number with no tolerance or frame. With a key, the model decides these; in this run (model check off) they are flagged Low for review.

- nist_ctc_01_asme1_rd.pdf: `R8: 35.2`; `R8: 10.75 A BIC`; `R8: 020 +0.05`; `R8: 十035° -0.2`; `R8: -0.10`; `R8: S0'0-`; `R8: 60°±0.5`; `R8: 035 +0.2`
- nist_ctc_02_asme1_rc.pdf: `R1: 040±0.15`; `R8: 10.5DE@X`; `R8: 0.05`
- nist_ctc_04_asme1_rd.pdf: `R4: 01.5 D E F`
- nist_ftc_07_asme1_rd.pdf: `R8: .003`; `R8: +.003`; `R8: -F1.2`; `R8: -.000`; `R8: .015`; `R8: F1.1`; `R8: .000`; `R8: .01 01.00 .06 A BC 03 A`; `R8: 2XQ.594+.012`; `R8: 中0.040@0.045 M A X ABC`; `R8: .002`; `R8: 4X 8.173+.005 ①.015 A .060 A B C -.001`; `R8: 0.040@0.045MAXABC`; `R8: SIM REQT .070 A B C 015 A`; `R8: 2X 8.594 +.012`; `R8: .002`
- nist_ftc_08_asme1_rc.pdf: `R8: -.001`; `R8: 03? 015`; `R8: 0.020 M A B .238+.005`; `R8: 001`; `R4: 10.015 M A`; `R8: -.001`; `R8: .05 A B C .03①A .015①A`; `R8: .03 A 06 ABC`; `R8: .015 L1<→L2`; `R8: .015DG@0`; `R8: -.001`; `R8: .015 DH@-J@O .015DHM-JM`; `R8: 0.020 M D B C 0.050 D B C SIMREQT1 0.228 t005`; `R8: .001`; `R8: -.001 005`; `R8: SIMREQT2 0.020MDBC 0.242_+005 Q.050MDBC DB`; `R8: .001`; `R8: 0.020`
- nist_ftc_09_asme1_rd.pdf: `P1: 4X.03±.01X.03±.01`; `R8: .000`; `R8: 2XR`; `R8: 4XR`; `R8: D.02AF@`; `R8: 中01AGH`; `R1: 140 ±.008 1.010 A G B`; `R8: 0.010`; `R8: [0.010`
- nist_ftc_10_asme1_rb.pdf: `R1: 86±0.15`; `R8: 1.75 A B C 0.2 15`; `R3: Φ00.8 D EM-FM-GM`; `R8: F65-`; `R1: 4X L010 ±0.25`; `R8: F81`; `R4: 00.12 03.5 G6 00.8 D B`; `R4: 00.8 D`; `R4: `

## Cold read time (OCR + frame detection, CPU, no cache)

| Drawing | Seconds per sheet |
| --- | --- |
| nist_ftc_06_asme1_rd.pdf | cached before timing |
| nist_ctc_01_asme1_rd.pdf | 8.7 |
| nist_ctc_02_asme1_rc.pdf | 8.8 |
| nist_ctc_03_asme1_rc.pdf | 8.2 |
| nist_ctc_04_asme1_rd.pdf | 7.6 |
| nist_ctc_05_asme1_rd.pdf | 6.1 |
| nist_ftc_07_asme1_rd.pdf | 7.6 |
| nist_ftc_08_asme1_rc.pdf | 7.4 |
| nist_ftc_09_asme1_rd.pdf | 7.3 |
| nist_ftc_10_asme1_rb.pdf | 5.9 |
| nist_ftc_11_asme1_rb.pdf | 5.8 |

Cost per sheet with the model check on is not measured here (no API key in the eval run). The run stats record real token usage and cost whenever a key is set.
