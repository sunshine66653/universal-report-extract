---
name: universal-report-extract
version: 1.1.0
description: "Use this skill when the user asks to OCR/parse a report PDF or image (single file or whole folder), extract rule-driven metrics from reports, or write a new report mimicking a sample. Three progressive features: (1) pure OCR producing Markdown, Word, an editable original-vs-recognized comparison HTML, and a tables Excel (all recognized tables in document order); (2) explicit rule-profile metric extraction into one Excel, batch-capable; (3) report mimicry — infer the metric requirements of a sample report, extract them from research materials, and write a styled report. Cloud parsing via MinerU (requires MINERU_TOKEN, >200-page PDFs auto-split) or optional local Docling; LLM_API_KEY needed for chart recognition, metric extraction, and report writing."
license: MIT-0
metadata:
  openclaw:
    requires:
      env:
        - MINERU_TOKEN
      anyBins:
        - python3
        - python
    primaryEnv: MINERU_TOKEN
---

# Universal Report Extract

Run this skill only after the user expresses OCR / metric-extraction /
report-writing intent. Pick the matching feature — they are progressive and
separate; never run metric rules when the user only asked for recognition.

| Feature | Script | What it does | Needs |
|---|---|---|---|
| 1. Pure OCR | `scripts/ocr.py` | file or folder -> MD + Word + comparison HTML + tables Excel. **No metric rules involved.** | `MINERU_TOKEN` (cloud) or local Docling; `LLM_API_KEY` optional (chart recognition) |
| 2. Metric extraction | `scripts/extract_metrics.py` | OCR (reused/auto) + **explicit `--profile`** -> one metrics Excel; batch -> comparison wide sheet | `LLM_API_KEY`; profile choice is required, never defaulted |
| 3. Report mimicry | `scripts/write_report.py` | sample report + research materials -> inferred rules -> extracted metrics -> styled Word report | `LLM_API_KEY` end-to-end |

## Security And Privacy

- `MINERU_TOKEN` (sensitive) — PDF bytes are uploaded to the MinerU cloud API
  (`https://mineru.net`) for layout recognition. For confidential documents use
  `--engine docling` (local, optional heavy install) or get user consent.
- `LLM_API_KEY` (sensitive, optional for feature 1) — chart images and text
  chunks go to an OpenAI-compatible endpoint (default DashScope; configurable
  per profile via `llm.base_url`).
- The comparison HTML is self-contained (page images embedded). It loads one
  Google Font for the brand wordmark with a system fallback — no other
  external requests.
- Never commit populated `.env` files or tokens.

## Pre-Run Notice

Tell the user briefly before running: which feature, which engine
(cloud MinerU vs local Docling), where outputs land, and — for feature 2 —
which rule profile will be applied. Proceed unless they object.

## Setup

```bash
pip install -r requirements.txt
```

## Feature 1 — Pure OCR

```bash
# single file (PDF or image)
python scripts/ocr.py report.pdf --mineru-token $MINERU_TOKEN

# whole folder, nested subfolders included; outputs mirror the structure
python scripts/ocr.py ./reports_folder --out ./ocr_out --mineru-token $MINERU_TOKEN
```

Per-file outputs:

| File | Content |
|---|---|
| `<stem>_extracted.md` | recognized Markdown (fixed engine suffix — downstream tools discover MDs by it) |
| `<stem>_recognized.docx` | recognition as Word |
| `<stem>_compare.html` | original vs recognition side-by-side; per-section Edit mode; exports corrected MD |
| `<stem>_tables.xlsx` | every recognized table, document order, one sheet, titled by owning section heading |

Flags: `--engine mineru|docling|fast`, `--language zh|en`, `--api-key`
(enables VL chart->table recognition), `--no-docx/--no-html/--no-xlsx`,
`--no-recursive`, `--dpi`, `--no-rotate-detect`.

Engines:
- `mineru` (default) — cloud VLM parser, strong on tables; uploads bytes.
- `docling` — local ML parser (layout + TableFormer); accurate but slow on
  CPU. Tables get the header-anchored coordinate post-processor (below).
- `fast` — **local, coordinate-only, no ML/GPU, ~ms/page**. For digital-born
  PDFs: per page it emits the text layer with EVERY table rebuilt from glyph
  coordinates (`engine/table_reconstruct.py`, `reconstruct_all` finds multiple
  stacked tables per page) — columns preserved, wrapped cells merged, numbers
  exact. ~6 ms/page (a 129-page statement in ~0.6 s vs ~4 min for docling),
  ~99.9% money-number recall vs the text layer. Skips chart/figure
  recognition; scanned pages (no text layer) yield little — use mineru/docling
  for those. Output is the same `<stem>_extracted.md`, so the comparison HTML,
  tables Excel, Word, and feature-2 metric extraction all consume it
  unchanged.

Landscape-table pre-pass: before parsing, pages whose text layer runs
vertically (rotated landscape tables) are detected and turned upright
(`<stem>_rotated_ready.pdf`, cleaned up afterwards). zh documents only, on
by default; disable with `--no-rotate-detect` or `convert.rotate_detect`.
Pure scans have no text layer, so they pass through unchanged. A page is
only rotated when vertical text is the overwhelming majority
(`convert.rotate_min_vertical_ratio`, default 0.85) — so already-readable
pages that merely contain stacked labels (org charts, vertical column
headers) are left upright instead of being wrongly flipped. Pages already
stored as portrait + `/Rotate` (displaying upright) are recognized via the
absolute target rotation and skipped rather than double-rotated. For
residual ambiguity (e.g. scans, odd embeddings) `--rotate-osd` adds a
Tesseract OSD visual second-check on rotation candidates — opt-in, needs
`pytesseract` + the tesseract binary, and falls back to the heuristic if
they're absent (so it never breaks a run).

Coordinate table reconstruction (`engine/table_reconstruct.py`): digital-born
PDFs carry exact glyph coordinates, but image table models (TableFormer,
SLANeXt) guess the grid from a render and so merge close columns and jumble
wrapped multi-line cells. The `fast` engine rebuilds every table from
coordinates; the `docling` engine post-processes each table Docling detects
the same way (falling back to TableFormer when reconstruction can't apply, or
fully with `--no-table-rebuild` / env `HEADER_ANCHORED_TABLES=0`). Column
detection is layered, no template, with graceful fallback:

1. **Data-driven (primary)** — for dense numeric tables, columns come from the
   RIGHT edges of numeric cells across the data rows (financial numbers are
   right-aligned, so each column's right edge is a tight x-cluster). Numeric
   cells are assigned by right edge, which separates tight/overlapping columns
   that header-gap logic merges; multi-row stacked headers are assembled per
   column from the label lines that span the table. Kicks in when there are
   ≥2 numeric columns.
2. **Header-gap (fallback)** — columns from the header row's label x-positions,
   assignment by left edge. Best for text-heavy / single-value tables (e.g. a
   ledger with one amount column) where the data-driven test doesn't apply.
3. **TableFormer (docling only, last resort)** — when neither coordinate spec
   is reliable.

Trailing subtotal/total rows (a multi-decimal average or a short count, no
standard money token) are kept in the table; digit-free prose (footnotes,
disclaimers) is excluded. Limit: columns with no consistent gutter AND
overlapping right edges remain ambiguous — those need a VLM.

## Feature 2 — Metric extraction (explicit rules)

```bash
python scripts/extract_metrics.py report.pdf --profile cn_securities \
    --api-key $LLM_API_KEY --mineru-token $MINERU_TOKEN

# batch: one Excel, "Comparison" wide sheet (metric x file) + detail sheets
python scripts/extract_metrics.py ./folder --profile hk_securities_en \
    --api-key $LLM_API_KEY
```

- `--profile` is REQUIRED. List profiles with `python -m engine.cli list`.
- Existing recognized MDs are reused; OCR runs automatically otherwise.
- Maintain rules in `profiles/<name>/rules/rules.xlsx` ONLY (columns: id,
  name, source, section_hint, aliases, extraction_mode, ...). The engine
  compiles it to `rules.json` automatically on every run (signature-based —
  Excel edits are picked up with no manual sync step; never edit the JSON).
- If the skill was distributed without the Excel (json-only), feature 2
  regenerates a hand-editable `rules.xlsx` from `rules.json` on first run
  (lossless round-trip) and tells the user where it is. Lint rules with
  `python -m engine.cli check <profile>`. Details: `profiles/README.md`.
- `--debug-prompts` (off by default — ask the user, or enable when they want
  to audit/debug a wrong value): dumps every rule's final LLM prompt
  (rule + retrieved context) to `<out>/<file-stem>/_debug_prompts/` as
  `<timestamp>_main_<rule-id>.txt`, the same layout as the original project.
- `--mode whole`: skip per-rule retrieval — feed the whole MD (or a `--pages
  A-B` range) plus the full metric list to the model and get all values in
  one shot. Bypasses `section_hint`. Good for a focused MD or when chunk
  retrieval misses a section; for a full report keep the default `retrieval`
  mode (whole mode auto-windows oversized docs and warns). Page filtering
  needs page markers in the MD.
- `--route routes.json` (batch): different files use different profiles.
  Manifest: `{"default": "cn_securities", "routes": [{"match": "*港股*",
  "profile": "hk_securities_en"}]}` — first glob match (vs the file's
  relative path or name) wins, else `default`/`--profile`. Lets one batch
  span mixed scenarios into one comparison Excel.
- Heading promotion: before chunking, plain-text numbered section titles
  (`一、` / `（一）` / `1.1` / "Note 5") that MinerU emitted as paragraphs
  are promoted to Markdown headings so `section_hint` matching works. On by
  default; disable per profile with `chunking.promote_headings: false`. This
  is the usual fix when "a heading written in the rule wasn't sliced out".

## Feature 3 — Report mimicry

```bash
python scripts/write_report.py material1.pdf material2.pdf \
    --sample sample_report.docx --api-key $LLM_API_KEY \
    --mineru-token $MINERU_TOKEN --out report_out
```

- Sample formats: .docx / .pdf / .md. Materials: PDFs/images/recognized MDs
  or folders.
- The inferred rule set is shared across all materials, with locator terms
  merged across the materials' languages (Chinese sample + English materials
  works). It is saved to `inferred_rules.json` for audit; `--save-profile NAME`
  persists it as a reusable profile.
- Outputs: mimicked `report_*.docx` + `report_*_metrics.xlsx`.
- `--base-profile` only supplies engine config (chunking/LLM endpoint); its
  rules are NOT used.

## Notes

- Inputs: PDF or images (png/jpg/bmp/webp/tif/gif — wrapped into single-page
  PDFs automatically). PDFs over 200 pages are auto-split for MinerU.
- Proofreading loop: fix recognition in the comparison HTML, export the
  corrected MD over the original `<stem>_extracted.md`, then re-run feature 2/3 —
  they will reuse the corrected file.
- Large documents take time on MinerU (polling); prefer background jobs.
- Legacy one-shot script `scripts/run_extract.py` is kept for compatibility;
  prefer the three feature scripts above.
