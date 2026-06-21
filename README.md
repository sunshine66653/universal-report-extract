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
| `*_extracted.md` | recognized Markdown, HTML tables preserved (fixed engine suffix) |
| `*_recognized.docx` | recognition as Word (headings + tables restored) |
| `*_compare.html` | original ↔ recognition side-by-side, **editable**: fix text per section, export the corrected MD (self-contained single file) |
| `*_tables.xlsx` | every recognized table, in document order, one sheet — each table titled by the section heading it belongs to |

Engines: `--engine mineru` (cloud, default; >200-page PDFs auto-split),
`--engine docling` (local, optional heavy install), or `--engine fast`
(local, coordinate-only, no ML/GPU — for digital-born PDFs: text layer +
tables rebuilt from glyph coordinates, ~3 ms/page, numbers exact; skips
charts and scanned pages). All three write the same `<stem>_extracted.md`,
so every downstream output (comparison HTML, tables Excel, Word, metric
extraction) works regardless of engine. Images are wrapped into single-page
PDFs automatically.

Coordinate table reconstruction (`fast` engine, and the `docling` engine's
table post-processor): digital PDFs carry exact glyph coordinates, so tables
are rebuilt from those instead of guessed from a render — no template.
Column detection is layered with graceful fallback:
1. **data-driven** (primary, dense numeric tables) — columns from the right
   edges of numeric cells (financial numbers are right-aligned → each column's
   right edge is a tight cluster); separates tight columns image models merge,
   and assembles multi-row stacked headers per column;
2. **header-gap** (fallback) — columns from the header label positions; best
   for text-heavy / single-amount tables;
3. **TableFormer** (docling, last resort) — when neither applies.
Trailing subtotal rows are kept; footnotes/prose are excluded. `docling` can
be forced to raw TableFormer with `--no-table-rebuild`. Remaining hard case:
columns with no gutter and overlapping right edges (needs a VLM).

Landscape-table pre-pass (zh, on by default): pages whose text layer runs
vertically — rotated landscape tables — are turned upright before parsing.
Only pages that are overwhelmingly vertical are rotated, so readable pages
with stacked labels (org charts, vertical column headers) are not wrongly
flipped (`convert.rotate_min_vertical_ratio`, default 0.85). Pages already
stored as portrait + `/Rotate` (displaying upright) are detected and left
as-is rather than double-rotated. `--rotate-osd` adds an opt-in Tesseract
OSD visual second-check (needs `pytesseract` + tesseract; falls back to the
heuristic if absent). Disable the whole pre-pass with `--no-rotate-detect`.

## Feature 2 — Metric extraction

- `--profile` is **required** — extraction never runs silently with default
  rules. Built-in profiles: `cn_securities` (A-share Chinese annual reports),
  `hk_securities_en` (HK English reports). List: `python -m engine.cli list`.
- Rules live in `profiles/<name>/rules/rules.xlsx`; edit the Excel, no code —
  the `rules.json` cache resyncs automatically on every run. Distributed
  without the Excel? Feature 2 regenerates a hand-editable one from the JSON
  on first run. Lint with `python -m engine.cli check <profile>`.
- Batch over a folder produces one Excel: a `Comparison` sheet
  (metric rows × file columns) plus a detail sheet per file.
- `--debug-prompts` (opt-in) dumps every rule's final LLM prompt to
  `<out>/<file-stem>/_debug_prompts/<timestamp>_main_<rule-id>.txt` —
  the exact rule + retrieved context sent to the model, for auditing
  wrong or missing values.

### Two extraction modes

- `--mode retrieval` (default): per-rule chunk retrieval guided by
  `section_hint` — efficient on big reports.
- `--mode whole`: skip retrieval, feed the whole MD (or `--pages A-B`) plus
  the full metric list to the model, all values in one shot. Use for a
  focused MD or when retrieval misses a section. Oversized docs are
  auto-split into windows and merged (first non-null wins; stops once every
  metric is found).

### Per-file rule routing (mixed batches)

`--route routes.json` lets one batch apply different profiles to different
files, e.g. A-share and H-share reports side by side into one comparison:

```json
{ "default": "cn_securities",
  "routes": [ { "match": "*港股*",   "profile": "hk_securities_en" },
              { "match": "hk/**",    "profile": "hk_securities_en" } ] }
```

First glob match (against the file's path relative to the batch root, or its
bare name) wins; otherwise `default` (or `--profile`).

### Chunking: heading promotion

MinerU often emits section titles (`一、`, `（一）`, `1.1`, `Note 5`) as plain
paragraphs, not `#` headings — so a `section_hint` referencing them never
matched. Before chunking, such numbered lines are now promoted to headings
(length-guarded to skip long sentences). This is the fix when "a heading is
in the rule but didn't get sliced out". Disable per profile with
`chunking.promote_headings: false`.

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
overwrite the original `*_extracted.md` -> re-run feature 2/3 (they reuse it).

## Privacy

- PDF bytes go to MinerU cloud for parsing; chunks/chart images go to the
  configured LLM endpoint. Use `--engine docling` for local-only parsing.
- The comparison HTML is offline-capable; only one Google Font is referenced
  (system fallback applies).

## License

MIT-0
