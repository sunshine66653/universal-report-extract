# Universal Report Extract

Three progressive features for report documents (PDFs / scanned images):

```
1. ocr.py              Pure OCR            file/folder -> MD + Word + comparison HTML + tables Excel
                                            (no metric rules — ever)
2. extract_metrics.py  Metric extraction   OCR (reused/auto) + explicit --profile -> metrics Excel
                                            (batch -> Comparison wide sheet + per-file detail)
3. write_report.py     Report mimicry      sample report + materials -> inferred rules
                                            -> extracted metrics -> styled Word report
```

## Quick start

```bash
pip install -r requirements.txt

set MINERU_TOKEN=eyJ...        # cloud parsing (mineru.net/apiManage)
set LLM_API_KEY=sk-...         # extraction / chart recognition / writing

# 1) recognize a whole folder (nested subfolders included)
python scripts/ocr.py ./reports --out ./ocr_out

# 2) extract metrics with an explicit rule profile
python scripts/extract_metrics.py ./reports --profile cn_securities --api-key %LLM_API_KEY%

# 3) write a report mimicking a sample
python scripts/write_report.py material1.pdf material2.pdf --sample sample.docx
```

## Feature 1 — Pure OCR outputs (per file)

| File | Content |
|---|---|
| `*_提取结果.md` | recognized Markdown, HTML tables preserved (fixed engine suffix) |
| `*_recognized.docx` | recognition as Word (headings + tables restored) |
| `*_compare.html` | original ↔ recognition side-by-side, **editable**: fix text per section, export the corrected MD (self-contained single file) |
| `*_tables.xlsx` | every recognized table, in document order, one sheet — each table titled by the section heading it belongs to |

Engines: `--engine mineru` (cloud, default; >200-page PDFs auto-split) or
`--engine docling` (local, optional heavy install). Images are wrapped into
single-page PDFs automatically.

## Feature 2 — Metric extraction

- `--profile` is **required** — extraction never runs silently with default
  rules. Built-in profiles: `cn_securities` (A-share Chinese annual reports),
  `hk_securities_en` (HK English reports). List: `python -m engine.cli list`.
- Rules live in `profiles/<name>/rules/rules.xlsx`; edit the Excel, no code.
- Batch over a folder produces one Excel: a `Comparison` sheet
  (metric rows × file columns) plus a detail sheet per file.

## Feature 3 — Report mimicry

1. The sample (.docx/.pdf/.md) is parsed into an outline (style template).
2. The LLM infers ONE shared metric-rule set from the sample, with
   `section_hint`/`aliases` merged across all materials' actual languages and
   terminology — a Chinese sample against English materials just works.
3. Metrics are extracted from every material (rules used in-memory;
   `--save-profile NAME` persists them for reuse, `inferred_rules.json` is
   always written for audit).
4. A Word report is written section by section in the sample's tone; with
   multiple materials the narrative is comparative (rankings, gaps).

## Proofreading loop

Comparison HTML -> fix recognition per section -> "Download proofread MD" ->
overwrite the original `*_提取结果.md` -> re-run feature 2/3 (they reuse it).

## Privacy

- PDF bytes go to MinerU cloud for parsing; chunks/chart images go to the
  configured LLM endpoint. Use `--engine docling` for local-only parsing.
- The comparison HTML is offline-capable; only one Google Font is referenced
  (system fallback applies).

## License

MIT
