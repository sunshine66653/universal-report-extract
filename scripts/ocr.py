"""
Feature 1 — Pure OCR (no metric rules involved)
─────────────────────────────────────────────────────────────────
Recognize a document (or a whole folder, nested subfolders included) and
produce four outputs per file:

  1. <stem>_extracted.md    recognized Markdown (HTML tables preserved)
                            (engine.mineru.RECOGNIZED_MD_SUFFIX — downstream
                            tools discover MDs by it)
  2. <stem>_recognized.docx recognition converted to Word
  3. <stem>_compare.html    original vs recognition side-by-side proofreading
                            report (editable, exports corrected MD)
  4. <stem>_tables.xlsx     every recognized table, in document order, in one
                            sheet — each table titled by its section heading

This feature NEVER runs metric rules. Metric extraction is the separate,
explicit feature 2 (scripts/extract_metrics.py), and report writing is
feature 3 (scripts/write_report.py). They build on this one.

Engines:
  mineru   cloud API (default; requires MINERU_TOKEN; >200-page PDFs are
           auto-split into batches)
  docling  local parsing (requires the optional heavy Docling install)

Inputs: PDF or common image formats (images are wrapped into a single-page
PDF first so the whole pipeline stays uniform).

Usage:
  python scripts/ocr.py report.pdf --engine mineru --mineru-token ...
  python scripts/ocr.py ./folder --recursive --out ./ocr_out
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Callable, Optional

# Windows GBK console compatibility: switch stdout/stderr to UTF-8
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

_SCRIPT_DIR = Path(__file__).resolve().parent
_SKILL_ROOT = _SCRIPT_DIR.parent
for p in (str(_SKILL_ROOT), str(_SCRIPT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

PDF_EXTS = {".pdf"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff", ".gif"}
SUPPORTED_EXTS = PDF_EXTS | IMAGE_EXTS


# ==============================================================================
# Lightweight OCR profile (language + engine config only, NO rules)
# ==============================================================================

def make_ocr_profile(language: str = "zh",
                     engine: str = "mineru",
                     mineru_token: str = "",
                     vl_model: str = "",
                     recognize_images: bool = True,
                     rotate_detect: bool = True,
                     rotate_min_vertical_ratio: float = 0.85,
                     rotate_osd: bool = False,
                     docling_table_rebuild: bool = True):
    """
    Build an in-memory profile carrying only what PDF->MD needs (language,
    convert config, VL model for figure recognition). It is decoupled from
    the metric-rule profiles under profiles/ on purpose — pure OCR must not
    silently pick up anyone's extraction rules.
    """
    from engine.profile import Profile, DEFAULT_PROFILE, _deep_merge, PROFILES_DIR

    over = {
        "display_name": "(pure OCR)",
        "language": language,
        "convert": {
            "engine": engine,
            "mineru_token": mineru_token,
            "recognize_images": recognize_images,
            "rotate_detect": rotate_detect,
            "rotate_min_vertical_ratio": rotate_min_vertical_ratio,
            "rotate_osd": rotate_osd,
            "docling_table_rebuild": docling_table_rebuild,
        },
    }
    if vl_model:
        over["llm"] = {"vl_model": vl_model}
    config = _deep_merge(DEFAULT_PROFILE, over)
    return Profile(name="_ocr", dir=PROFILES_DIR, config=config,
                   global_prompt="", chunk_prompt="")


# ==============================================================================
# Input discovery / normalization
# ==============================================================================

def scan_folder(root: str | Path, recursive: bool = True) -> list[Path]:
    """List supported files under a folder (nested subfolders included by
    default), sorted by path. Engine intermediates are skipped: image-wrapper
    PDFs (*_asimg.pdf) and anything inside a *_mineru work dir (per-batch
    part_*.pdf copies, downloaded result images, ...)."""
    root = Path(root)
    if not root.is_dir():
        raise NotADirectoryError(f"Not a folder: {root}")
    it = root.rglob("*") if recursive else root.glob("*")
    files = []
    for p in it:
        if not (p.is_file() and p.suffix.lower() in SUPPORTED_EXTS):
            continue
        if p.name.endswith("_asimg.pdf"):
            continue
        if p.name.endswith("_rotated_ready.pdf"):   # orientation pre-pass temp
            continue
        if any(part.endswith("_mineru") for part in p.parent.parts):
            continue
        files.append(p)
    return sorted(files, key=lambda p: str(p).lower())


def ensure_pdf(path: Path) -> Path:
    """Images are wrapped into a single-page PDF (<stem>_asimg.pdf) so the
    rest of the pipeline only ever sees PDFs. PDFs pass through unchanged."""
    if path.suffix.lower() in PDF_EXTS:
        return path
    from PIL import Image
    pdf_path = path.parent / f"{path.stem}_asimg.pdf"
    if pdf_path.exists() and pdf_path.stat().st_size > 0:
        return pdf_path
    im = Image.open(path)
    if im.mode != "RGB":
        im = im.convert("RGB")
    im.save(str(pdf_path), "PDF", resolution=200.0)
    return pdf_path


def ensure_md(pdf_path: Path, profile, api_key: str = "",
              log: Callable[[str], None] = print) -> Path:
    """PDF -> recognized MD via the engine (idempotent: an existing non-empty
    <stem>_extracted.md is reused). Returns the MD path."""
    from engine.convert import pdf_to_md
    md = pdf_to_md(str(pdf_path), profile, api_key=api_key, log=log)
    return Path(md)


# ==============================================================================
# Per-file OCR pipeline
# ==============================================================================

def ocr_one(
    src: Path,
    profile,
    *,
    api_key: str = "",
    out_dir: Optional[Path] = None,
    make_docx: bool = True,
    make_html: bool = True,
    make_xlsx: bool = True,
    dpi: int = 96,
    log: Callable[[str], None] = print,
) -> dict:
    """
    Run the full pure-OCR pipeline for one file.
    Returns {"md": ..., "docx": ..., "html": ..., "xlsx": ...} (paths as str;
    only the outputs that were produced).
    The MD always lands next to the source (engine convention); the other
    outputs go to out_dir when given, else also next to the source.
    """
    results: dict = {}
    pdf = ensure_pdf(src)
    if pdf != src:
        log(f"  image wrapped as PDF: {pdf.name}")

    md_path = ensure_md(pdf, profile, api_key=api_key, log=log)
    results["md"] = str(md_path)

    dest = out_dir if out_dir else src.parent
    dest.mkdir(parents=True, exist_ok=True)
    stem = src.stem

    if make_docx:
        from md_to_docx import md_to_docx
        docx_path = dest / f"{stem}_recognized.docx"
        md_to_docx(md_path, docx_path, title=stem)
        results["docx"] = str(docx_path)

    if make_html:
        from compare_html import build_compare_html
        html_path = dest / f"{stem}_compare.html"
        build_compare_html(pdf, md_path, html_path, dpi=dpi)
        results["html"] = str(html_path)

    if make_xlsx:
        from md_to_excel import md_to_excel
        xlsx_path = dest / f"{stem}_tables.xlsx"
        md_to_excel(md_path, xlsx_path)
        results["xlsx"] = str(xlsx_path)

    return results


# ==============================================================================
# CLI (single file or batch folder)
# ==============================================================================

def main(argv=None):
    ap = argparse.ArgumentParser(
        description=("Pure OCR: recognize a document (or a folder of documents, "
                     "nested subfolders included) into MD + Word + comparison "
                     "HTML + tables Excel. No metric rules are involved — use "
                     "extract_metrics.py for that."))
    ap.add_argument("input", help="a PDF/image file, or a folder to batch-process")
    ap.add_argument("--engine", default="mineru",
                    choices=["mineru", "docling", "fast"],
                    help="parsing engine: mineru (cloud), docling (local ML), "
                         "or fast (local coordinate-only, digital-born PDFs; "
                         "no ML/GPU, ~ms/page)")
    ap.add_argument("--language", default="zh", choices=["zh", "en"],
                    help="document language (drives parsing + heading levels)")
    ap.add_argument("--mineru-token", default=os.getenv("MINERU_TOKEN", ""),
                    help="MinerU API token (required for --engine mineru)")
    ap.add_argument("--api-key", default=os.getenv("LLM_API_KEY", ""),
                    help="optional LLM API key — enables VL figure recognition "
                         "during parsing (charts -> data tables)")
    ap.add_argument("--vl-model", default="",
                    help="vision model for figure recognition (default from engine)")
    ap.add_argument("--no-rotate-detect", action="store_true",
                    help="skip the landscape-table pre-pass (rotated pages are "
                         "detected via the text layer and turned upright before "
                         "parsing; zh documents only, on by default)")
    ap.add_argument("--rotate-osd", action="store_true",
                    help="add a Tesseract OSD visual second-check on rotation "
                         "candidates (needs pytesseract + the tesseract binary; "
                         "falls back to the heuristic if unavailable)")
    ap.add_argument("--no-table-rebuild", action="store_true",
                    help="docling engine: use the RAW/original TableFormer "
                         "table output instead of the header-anchored "
                         "coordinate rebuild (rebuild is on by default)")
    ap.add_argument("--out", default="",
                    help="output dir for docx/html/xlsx (default: next to each "
                         "source file; batch mode mirrors the folder structure)")
    ap.add_argument("--no-recursive", action="store_true",
                    help="batch mode: do not descend into subfolders")
    ap.add_argument("--no-docx", action="store_true", help="skip the Word output")
    ap.add_argument("--no-html", action="store_true", help="skip the comparison HTML")
    ap.add_argument("--no-xlsx", action="store_true", help="skip the tables Excel")
    ap.add_argument("--dpi", type=int, default=96,
                    help="comparison-HTML page render DPI (default 96)")
    args = ap.parse_args(argv)

    src = Path(args.input)
    if not src.exists():
        print(f"Error: input not found: {src}")
        sys.exit(1)
    if args.engine == "mineru" and not args.mineru_token:
        print("Error: --engine mineru requires a MinerU token "
              "(--mineru-token or env MINERU_TOKEN)")
        sys.exit(1)

    profile = make_ocr_profile(
        language=args.language, engine=args.engine,
        mineru_token=args.mineru_token, vl_model=args.vl_model,
        rotate_detect=not args.no_rotate_detect,
        rotate_osd=args.rotate_osd,
        docling_table_rebuild=not args.no_table_rebuild,
    )
    out_root = Path(args.out) if args.out else None

    # collect work list
    if src.is_dir():
        files = scan_folder(src, recursive=not args.no_recursive)
        if not files:
            print(f"No supported files ({', '.join(sorted(SUPPORTED_EXTS))}) "
                  f"under {src}")
            sys.exit(1)
        print(f"[ocr] batch: {len(files)} file(s) under {src}")
    else:
        if src.suffix.lower() not in SUPPORTED_EXTS:
            print(f"Error: unsupported file type: {src.suffix}")
            sys.exit(1)
        files = [src]

    ok, failed = [], []
    for i, f in enumerate(files, 1):
        print(f"\n[ocr] ({i}/{len(files)}) {f}")
        # batch + --out: mirror the relative subfolder structure
        dest = None
        if out_root is not None:
            dest = out_root
            if src.is_dir():
                rel = f.parent.relative_to(src)
                dest = out_root / rel
        try:
            res = ocr_one(
                f, profile, api_key=args.api_key, out_dir=dest,
                make_docx=not args.no_docx, make_html=not args.no_html,
                make_xlsx=not args.no_xlsx, dpi=args.dpi,
            )
            ok.append((f, res))
        except Exception as e:
            failed.append((f, str(e)))
            print(f"  ❌ failed: {e}")

    print("\n========== OCR summary ==========")
    print(f"  succeeded: {len(ok)}   failed: {len(failed)}")
    for f, res in ok:
        print(f"  ✅ {f.name}")
        for k in ("md", "docx", "html", "xlsx"):
            if k in res:
                print(f"       {k:5s} {res[k]}")
    for f, err in failed:
        print(f"  ❌ {f.name}: {err}")
    if failed:
        sys.exit(2)
    return ok


if __name__ == "__main__":
    main()
