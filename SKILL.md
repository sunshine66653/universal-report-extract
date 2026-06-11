---
name: universal-report-extract
version: 1.1.0
description: "Use this skill when the user asks to OCR/parse a report PDF or image (single file or whole folder), extract rule-driven metrics from reports, or write a new report mimicking a sample. Three progressive features: (1) pure OCR producing Markdown, Word, an editable original-vs-recognized comparison HTML, and a tables Excel (all recognized tables in document order); (2) explicit rule-profile metric extraction into one Excel, batch-capable; (3) report mimicry — infer the metric requirements of a sample report, extract them from research materials, and write a styled report. Cloud parsing via MinerU (requires MINERU_TOKEN, >200-page PDFs auto-split) or optional local Docling; LLM_API_KEY needed for chart recognition, metric extraction, and report writing."
license: MIT
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
| `<stem>_提取结果.md` | recognized Markdown (fixed engine suffix — downstream tools discover MDs by it) |
| `<stem>_recognized.docx` | recognition as Word |
| `<stem>_compare.html` | original vs recognition side-by-side; per-section Edit mode; exports corrected MD |
| `<stem>_tables.xlsx` | every recognized table, document order, one sheet, titled by owning section heading |

Flags: `--engine docling` (local), `--language zh|en`, `--api-key`
(enables VL chart->table recognition), `--no-docx/--no-html/--no-xlsx`,
`--no-recursive`, `--dpi`.

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
- Maintain rules in `profiles/<name>/rules/rules.xlsx` (columns: id, name,
  source, section_hint, aliases, extraction_mode, ...).

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
  corrected MD over the original `<stem>_提取结果.md`, then re-run feature 2/3 —
  they will reuse the corrected file.
- Large documents take time on MinerU (polling); prefer background jobs.
- Legacy one-shot script `scripts/run_extract.py` is kept for compatibility;
  prefer the three feature scripts above.
