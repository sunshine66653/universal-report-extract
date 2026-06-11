"""
PDF -> Markdown
─────────────────────────────────────────────────────────────────
Two engines, selected via profile.convert.engine or a runtime argument:
  docling  local (reuses legacy PDF2MD Docling+LLM; best Chinese heading
           quality — requires the optional heavy Docling install)
  mineru   cloud API (engine/mineru: batch upload -> parse -> merge)

Idempotent: if {stem}_提取结果.md already exists and is non-empty it is reused.

NOTE: "_提取结果" ("extraction result") is the fixed Chinese filename suffix
for recognition output. It is a wire format shared with the image-root
discovery in engine.mineru and with the original project's UI — do NOT rename.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
LEGACY = ROOT / "legacy"
for p in (str(ROOT), str(LEGACY)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _expected_md(pdf: Path) -> Path:
    return pdf.parent / f"{pdf.stem}_提取结果.md"


# ════════════════════════════════════════════════════════════════════════
#  Docling (local, optional)
# ════════════════════════════════════════════════════════════════════════
def _convert_docling(pdf_path: Path, profile, log, progress, should_stop) -> str:
    from PDF2MD import auto_detect_and_rotate_text, parse_large_pdf_safely

    expected_md = _expected_md(pdf_path)
    do_rotate = profile.language == "zh" and \
        profile.config.get("convert", {}).get("rotate_detect", True)

    if do_rotate:
        log("Detecting text orientation...")
        processed_path, is_temp = auto_detect_and_rotate_text(str(pdf_path))
    else:
        log(f"language={profile.language}, skipping orientation detection")
        processed_path, is_temp = str(pdf_path), False
    progress(0.15)
    if should_stop():
        return ""

    log("Starting Docling + LLM parsing (this can take a while)...")
    parse_large_pdf_safely(processed_path)
    progress(0.90)

    actual_stem = Path(processed_path).stem
    actual_md = Path(processed_path).parent / f"{actual_stem}_提取结果.md"
    if actual_md.exists() and actual_md != expected_md:
        if expected_md.exists():
            expected_md.unlink()
        shutil.move(str(actual_md), str(expected_md))

    if is_temp and os.path.exists(processed_path):
        try:
            os.remove(processed_path)
        except Exception:
            pass
    return str(expected_md)


# ════════════════════════════════════════════════════════════════════════
#  MinerU (cloud)
# ════════════════════════════════════════════════════════════════════════
def _convert_mineru(pdf_path: Path, profile, api_key, log, progress, should_stop) -> str:
    from engine.mineru import (
        mineru_pdf_to_md, archive_images_in_text, find_image_refs,
        collect_image_roots,
    )
    from engine.headings import relevel_markdown

    conv = profile.config.get("convert", {})
    token = conv.get("mineru_token") or os.getenv("MINERU_TOKEN", "")
    if not token:
        raise ValueError("MinerU token not configured "
                         "(profile.convert.mineru_token or env MINERU_TOKEN)")

    # 1) cloud parse -> complete md, may still contain ![](images/..)
    md, work_dir = mineru_pdf_to_md(
        str(pdf_path), token,
        language=profile.language,
        model_version=conv.get("mineru_model_version", "vlm"),
        log=log, progress=lambda p: progress(0.1 + p * 0.65),
        should_stop=should_stop,
    )

    # 2) heading re-leveling (MinerU's raw heading levels are poor)
    log("Re-leveling headings...")
    md = relevel_markdown(md, profile.language)

    # 3) figure recognition (after the full md exists; needs LLM API key)
    n_imgs = len(find_image_refs(md))
    if n_imgs and conv.get("recognize_images", True):
        if api_key:
            log(f"Starting figure recognition ({n_imgs} images)...")
            roots = [work_dir, pdf_path.parent] + collect_image_roots(
                _expected_md(pdf_path), work_dir,
            )
            md, ok, total, stats = archive_images_in_text(
                md, roots, api_key,
                profile.llm.get("base_url"),
                profile.llm.get("vl_model", "qwen-vl-max"),
                log=log, progress=lambda p: progress(0.8 + p * 0.15),
                should_stop=should_stop,
            )
            # failed/missing figures keep their original links — nothing lost
            log(f"Figure recognition done: {ok} ok, {stats['empty']} decorative "
                f"removed, {stats['failed']} failed, {stats['missing']} missing "
                f"(failed/missing links kept)")
        else:
            log(f"⚠ MD contains {n_imgs} images but no LLM API key was given — "
                f"figure recognition skipped (links kept, can run later)")

    expected_md = _expected_md(pdf_path)
    expected_md.write_text(md, encoding="utf-8")
    progress(0.97)
    return str(expected_md)


# ════════════════════════════════════════════════════════════════════════
#  Unified entry point
# ════════════════════════════════════════════════════════════════════════
def pdf_to_md(
    pdf_path: str,
    profile,
    api_key: str = "",
    engine: Optional[str] = None,
    log: Callable[[str], None] = print,
    progress: Callable[[float], None] = lambda p: None,
    should_stop: Callable[[], bool] = lambda: False,
) -> str:
    original_pdf = Path(pdf_path)
    if not original_pdf.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    expected_md = _expected_md(original_pdf)
    if expected_md.exists() and expected_md.stat().st_size > 1024:
        log(f"✅ Recognition output already exists, reusing: {expected_md.name}")
        progress(1.0)
        return str(expected_md)

    # engine choice: runtime arg > profile.convert.engine > default docling
    engine = (engine or profile.config.get("convert", {}).get("engine")
              or "docling").lower()
    log(f"PDF->MD engine: {engine}")
    progress(0.05)
    if should_stop():
        return ""

    if engine == "mineru":
        out = _convert_mineru(original_pdf, profile, api_key, log, progress, should_stop)
    else:
        out = _convert_docling(original_pdf, profile, log, progress, should_stop)

    if not out or not Path(out).exists():
        raise FileNotFoundError(f"Parsing finished but output not found: {expected_md}")
    progress(1.0)
    log(f"✅ Markdown generated: {Path(out).name}")
    return out
