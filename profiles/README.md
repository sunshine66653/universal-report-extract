# profiles/ — Business Profiles (Maintainers Only Edit Here)

Each profile represents a specific business scenario (e.g., A-share Chinese Annual Reports, HKEX English Reports). Once the core engine (engine/) reads a profile, it executes the entire workflow automatically: Download ➔ PDF-to-Markdown ➔ Metric Extraction.

To extend the system to a new business scenario, simply add a new profile directory without modifying any underlying code.

```
profiles/<profile_name>/
├── profile.json        ← Chunking params / Model / Language / Retrieval weights / Download configs
├── prompts/
│   ├── global.txt      ← Global system prompt (shared system instructions for every extraction)
│   └── chunk.txt       ← Chunk-level prompt template (contains {placeholders})
└── rules/
    └── rules.xlsx      ← Metric rules (maintained via Excel, auto-compiled to rules.json at runtime)
```

## Daily Maintenance Guide

Maintainers typically only need to modify three major parts:

| Goal / Requirement | Target File to Edit |
|---|---|
| Add/remove metrics, adjust financial formulas, modify chapter positioning clues | `rules/rules.xlsx` |
| Adjust the global extraction tone / common constraints | `prompts/global.txt` |
| Modify the single-metric prompt structure / output format | `prompts/chunk.txt` |
| Adjust chunking granularity, retrieval Top-K, model selection, or language | `profile.json` |

> 💡 **Tip:** After updating `rules.xlsx`, you can run the engine directly. The engine automatically detects Excel changes and re-generates `rules.json` (idempotent caching; reuses the existing file if no changes are detected).

### The Excel is the ONLY hand-maintained surface

`rules.xlsx` is the single source of truth; `rules.json` is its compiled
cache (`_sig` = Excel signature) — never edit the JSON by hand.

Distribution without Excel works automatically: if the skill is uploaded
with only `rules.json` (some upload channels strip `.xlsx`), the first run
of feature 2 (`extract_metrics.py`) — or `python -m engine.cli rules
<profile>` — detects the missing Excel and **regenerates a hand-editable
`rules.xlsx` from the JSON** at the profile's `rules_file` path (the
conversion round-trips losslessly). From then on the normal loop applies:
edit the Excel, run the engine, the JSON resyncs on its own.

After editing, you can lint the rules:

```bash
python -m engine.cli check cn_securities        # or a direct path:
python -m engine.cli check path/to/rules.xlsx
```

It flags duplicate/missing ids, missing names, list/boolean fields of the
wrong type, and `calc` mode without a formula.

---

## rules.xlsx Column Specifications

| Column | Required | Description |
|---|---|---|
| `id` | ✅ | Unique metric ID (e.g., `R01`, `Z19`). |
| `name` | ✅ | Metric name. |
| `source` | | Data extraction logic/definition, fed to the LLM for precise location context. |
| `enabled` | | `TRUE`/`FALSE`. Disabling skips extraction (Defaults to `TRUE`). |
| `section_hint` | | Chapter positioning clues, comma-separated. Use `/` within a single cell for logical `OR`. |
| `aliases` | | Metric aliases, comma-separated. Use `/` within a single cell for logical `OR`. |
| `extraction_mode` | | `direct` (direct retrieval) / `calc` (requires calculation/formula). |
| `calc` | | Calculation expression / JSON string (Required when `extraction_mode=calc`). |
| `allow_parent_note` | | Set to `TRUE` to allow retrieval within the Parent Company Notes section (Defaults to `FALSE`). |
| `skip_reason` | | General remarks / Developer notes. |

* **Multi-sheet Support:** Multiple sheets are supported within the Excel file. Each sheet is treated as a separate rule group, and the sheet name will be populated under the `group` column in the final extraction results.

---

## prompts Template Placeholders

* Available in `global.txt`: `{report_period_hint}`, `{language}`
* Available in `chunk.txt`: `{global_prompt}`, `{rule_id}`, `{rule_name}`, `{rule_source}`, `{extraction_mode}`, `{company_instruction}`, `{calc_instruction}`, `{extra_instruction}`, `{report_period_hint}`, `{context}`
* Available in `whole.txt` (optional, used only by `--mode whole`): `{global_prompt}`, `{rules_block}` (the full metric list), `{report_period_hint}`, `{context}` (the document window). If absent, a built-in Chinese template is used.

> ⚠️ **Note:** Missing placeholders will be retained as raw text without raising errors. If you need literal curly braces `{}` to appear in your output (e.g., in a JSON format example), they must be escaped as `{{` and `}}`.

---

## profile.json Key Fields

```jsonc
{
  "language": "en",                 // "zh" / "en": Affects PDF-to-MD rules, default prompts, and heading re-hierarchization.
  "report_period_hint": "FY2024",   // Description of the "latest reporting period" in prompts.
  "chunking": {
    "strategy": "heading",          // Options: "auto" / "cn_slice" / "heading" / "fixed"
    "max_chars": 5000,              // Maximum characters per text chunk.
    "promote_headings": true,       // Promote plain-text numbered titles (一、/（一）/1.1) to headings before chunking (default true).
    "max_heading_chars": 0          // Promotion length guard (0 -> 40 zh / 90 en) to skip long sentences.
  },
  "retrieval": { "topk": 14 },      // Number of context chunks fed to the LLM per metric.
  "convert": {
    "engine": "mineru",             // "docling" (Local) / "mineru" (Cloud API)
    "rotate_detect": false,         // Turn rotated landscape pages upright before OCR (zh only).
    "rotate_min_vertical_ratio": 0.85, // Rotate a page only when vertical text is >= this share — guards readable pages (org charts, vertical column headers) from being wrongly flipped.
    "rotate_osd": false,            // Optional Tesseract OSD visual second-check on rotation candidates (needs pytesseract + tesseract binary; falls back to the heuristic if unavailable).
    "mineru_token": "",             // MinerU API Token (Can also be set via MINERU_TOKEN env var or UI).
    "mineru_model_version": "vlm",
    "recognize_images": true        // Uses Vision-Language (VL) models to recognize images in-place for MinerU results.
  },
  "download": {
    "adapter": "cninfo",            // "cninfo" (Cninfo Data) / "manual" (Manually place PDFs in directory)
    "report_language": "en"         // "zh" = Chinese / "en" = English (Useful for downloading English annual reports of A+H dual-listed firms).
  }
}
```

### PDF Parsing Engines: Local Docling vs Cloud MinerU API

| Feature / Metric | Local Docling | Cloud MinerU API |
| :--- | :--- | :--- |
| **Trigger Condition** | `convert.engine = "docling"` | `convert.engine = "mineru"` with a valid token configured. |
| **Best For** | A-share Chinese annual reports (excellent native heading hierarchy). | HKEX English reports / General use cases (Fast execution, no local GPU/compute required). |
| **Large PDFs** | Chunks files into 100-page intervals sequentially. | **Automatically splits into <= 200-page batches, uploads, and merges them backend.** |
| **Image Handling** | Docling inline Markdown representation. | OCR text extracted via VL Models and replaced in-place. |
| **Table Formatting** | Pipe-separated Markdown (`\|`). | Retains **HTML `<table>` formatting** (supports `rowspan`/`colspan`, optimal for complex financial tables). |
| **Heading Hierarchy** | Hierarchies injected natively during PDF2MD. | Re-structures poor native hierarchies **automatically based on language rules** (`zh` for mainland rules / `en` for HKEX rules). |

> 💡 **Runtime Flexibility:** You can dynamically switch engines or fill in your MinerU Token via the UI sidebar to override `profile.json` temporarily for the current session. The chunking infrastructure adapts seamlessly when switching languages.

---

### Create a New Profile in 3 Steps

1. **Duplicate** an existing profile directory (e.g., copy `hk_securities_en/` and rename it to `your_profile/`).
2. **Edit** `profile.json` to configure the target language, chunking strategies, and download methods.
3. **Populate** your metrics inside `rules/rules.xlsx` and adjust the two prompt templates in `prompts/` as needed.

Run：

```python
from engine import run_profile

# Run the complete pipeline
res = run_profile(
    "your_profile", 
    company="CITIC Securities", 
    year="2024",
    report_type="Annual Report", 
    api_key="sk-..."  # Pass your LLM API Key here securely
)

print(f"Extraction completed! Results saved to: {res.xlsx_path}")
```
