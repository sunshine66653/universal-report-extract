"""
PDF -> Markdown
─────────────────────────────────────────────────────────────────
Two engines, selected via profile.convert.engine or a runtime argument:
  docling  local (reuses legacy PDF2MD Docling+LLM; best Chinese heading
           quality — requires the optional heavy Docling install)
  mineru   cloud API (engine/mineru: batch upload -> parse -> merge)

Idempotent: if {stem}_extracted.md already exists and is non-empty it is reused.

NOTE: the recognition-output suffix is defined once as
engine.mineru.RECOGNIZED_MD_SUFFIX ("_extracted") and imported here so it
stays consistent across modules (filename discovery depends on it).
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

from engine.mineru import RECOGNIZED_MD_SUFFIX  # noqa: E402  (single source of truth)


def _expected_md(pdf: Path) -> Path:
    return pdf.parent / f"{pdf.stem}{RECOGNIZED_MD_SUFFIX}.md"


# ════════════════════════════════════════════════════════════════════════
#  Landscape-table pre-pass (ported from the legacy PDF2MD pipeline)
# ════════════════════════════════════════════════════════════════════════
# OSD availability is probed once per process; None = unknown yet
_OSD_AVAILABLE: Optional[bool] = None


def _osd_orientation(page, log) -> Optional[tuple[int, float]]:
    """Optional visual second-verification: render the page (as it currently
    displays, i.e. honoring its /Rotate) and ask Tesseract OSD which way the
    text actually faces. Returns (rotate_degrees, confidence) where
    rotate_degrees ∈ {0,90,180,270} is the extra clockwise rotation needed to
    make the text upright, or None if OSD is unavailable / failed (the caller
    then falls back to the cheap dir heuristic — no regression). Never raises.
    """
    global _OSD_AVAILABLE
    if _OSD_AVAILABLE is False:
        return None
    try:
        import io
        import re as _re
        import fitz
        import pytesseract
        from PIL import Image

        pix = page.get_pixmap(matrix=fitz.Matrix(150 / 72, 150 / 72))
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        osd = pytesseract.image_to_osd(img)
        _OSD_AVAILABLE = True
        rot = _re.search(r"Rotate:\s*(\d+)", osd)
        conf = _re.search(r"Orientation confidence:\s*([\d.]+)", osd)
        if not rot:
            return None
        return int(rot.group(1)) % 360, float(conf.group(1)) if conf else 0.0
    except Exception as e:
        if _OSD_AVAILABLE is None:        # first failure: explain once
            _OSD_AVAILABLE = False
            log(f"  OSD unavailable ({type(e).__name__}) — install pytesseract "
                f"+ the Tesseract binary to enable; using the dir heuristic")
        return None


def auto_detect_and_rotate_text(pdf_path: Path,
                                log: Callable[[str], None] = print,
                                min_vertical_ratio: float = 0.85,
                                min_vertical_chars: int = 20,
                                use_osd: bool = False,
                                osd_upright_conf: float = 0.5,
                                osd_rotate_conf: float = 1.0,
                                ) -> tuple[Path, bool]:
    """Scan each page's text-direction vectors; pages dominated by vertical
    text (rotated landscape tables) get their /Rotate flag corrected so the
    OCR engine sees upright tables. The header side (left/right) decides
    clockwise vs counter-clockwise.
    Writes <stem>_rotated_ready.pdf next to the source when anything rotated.
    Returns (path_to_parse, is_temp). Needs a text layer: pure scans carry
    no direction info and pass through unchanged.

    Two guards keep readable pages from being wrongly flipped:

    1) Mixed-orientation guard — a page is only acted on when vertical text is
       the OVERWHELMING majority (vertical / (vertical + horizontal) >=
       min_vertical_ratio). Real rotated landscape pages have almost no
       horizontal text (only footers/page numbers stay upright, ~1%), whereas
       a readable page that merely contains stacked labels — org charts,
       tables with vertical column headers — keeps a large horizontal share.

    2) Existing-/Rotate guard — the raw text-direction vector ignores the
       page's /Rotate flag, so a landscape table already stored as a portrait
       page + /Rotate (displaying upright and readable) still reports vertical
       text. We therefore compute the ABSOLUTE /Rotate that makes the raw text
       upright (raw bottom-up -> 90, raw top-down -> 270) and SKIP the page
       when it already carries that rotation, instead of blindly adding
       another 90°/270° (which double-rotated such pages into garbage)."""
    import fitz

    doc = fitz.open(str(pdf_path))
    rotated = 0
    try:
        for page in doc:
            horiz = up = down = 0
            for block in page.get_text("dict").get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    d = line.get("dir", (1.0, 0.0))
                    n = sum(len(sp.get("text", ""))
                            for sp in line.get("spans", []))
                    if abs(d[1]) > 0.5:
                        if d[1] < 0:
                            up += n      # bottom-up text (header on the left)
                        else:
                            down += n    # top-down text (header on the right)
                    else:
                        horiz += n
            vert = up + down
            total = vert + horiz
            ratio = vert / total if total else 0.0
            if vert >= min_vertical_chars and ratio >= min_vertical_ratio:
                pno = page.number + 1
                # optional visual second-verification — biased toward NOT
                # rotating (over-rotation is the failure mode being guarded)
                if use_osd:
                    osd = _osd_orientation(page, log)
                    if osd is not None:
                        rot, conf = osd
                        if rot == 0 and conf >= osd_upright_conf:
                            log(f"  page {pno}: vertical dir {ratio:.0%} but "
                                f"OSD says upright (conf {conf:.1f}) -> kept")
                            continue
                        if rot in (90, 180, 270) and conf >= osd_rotate_conf:
                            tgt = (page.rotation + rot) % 360
                            if tgt == page.rotation:
                                continue
                            log(f"  page {pno}: OSD rotate {rot}° (conf "
                                f"{conf:.1f}) -> /Rotate {tgt}°")
                            page.set_rotation(tgt)
                            rotated += 1
                            continue
                        log(f"  page {pno}: OSD low confidence "
                            f"({conf:.1f}) -> using dir heuristic")
                # cheap dir heuristic: absolute /Rotate that makes raw text
                # upright; skip if the page already carries it (readable)
                target = 90 if up >= down else 270
                if page.rotation == target:
                    log(f"  page {pno}: vertical text {ratio:.0%} but already "
                        f"/Rotate {target}° (readable) -> kept")
                    continue
                log(f"  page {pno}: vertical text {ratio:.0%}, "
                    f"/Rotate {page.rotation}° -> {target}°")
                page.set_rotation(target)
                rotated += 1
            elif vert >= min_vertical_chars and ratio >= 0.5:
                # mixed page: more vertical than horizontal but plenty of
                # readable horizontal text (e.g. org chart, vertical headers)
                # — left upright on purpose
                log(f"  page {page.number + 1}: mixed orientation "
                    f"(vertical {ratio:.0%}), kept upright")
        if not rotated:
            return pdf_path, False
        tmp = pdf_path.parent / f"{pdf_path.stem}_rotated_ready.pdf"
        doc.save(str(tmp))
        log(f"Orientation pre-pass: {rotated} landscape page(s) rotated "
            f"-> {tmp.name}")
        return tmp, True
    finally:
        doc.close()


def _maybe_rotate(pdf_path: Path, profile, log) -> tuple[Path, bool]:
    """Apply the pre-pass when the profile wants it (zh + convert.rotate_detect,
    on by default)."""
    conv = profile.config.get("convert", {})
    do_rotate = profile.language == "zh" and conv.get("rotate_detect", True)
    if not do_rotate:
        log(f"language={profile.language}, rotate_detect off — "
            f"skipping orientation detection")
        return pdf_path, False
    log("Detecting text orientation (landscape-table pre-pass)...")
    return auto_detect_and_rotate_text(
        pdf_path, log=log,
        min_vertical_ratio=float(conv.get("rotate_min_vertical_ratio", 0.85)),
        use_osd=bool(conv.get("rotate_osd", False)),
    )


# ════════════════════════════════════════════════════════════════════════
#  Fast (coordinate-only, digital-born PDFs — no ML, no GPU)
# ════════════════════════════════════════════════════════════════════════
def _page_to_md(page) -> tuple[str, bool]:
    """One page -> markdown using the text layer: prose blocks in reading
    order with EVERY table on the page rebuilt from coordinates in place
    (multiple stacked tables supported). Returns (markdown, n_tables>0)."""
    from engine.table_reconstruct import reconstruct_all, to_markdown

    tables = reconstruct_all(page)
    if not tables:
        return page.get_text("text").strip(), False
    spans = [span for _, _, span in tables]
    items = [(y0, to_markdown(labels, rows)) for labels, rows, (y0, y1) in tables]
    for blk in page.get_text("dict").get("blocks", []):
        if blk.get("type") != 0:
            continue
        bx = blk["bbox"]
        cy = (bx[1] + bx[3]) / 2
        if any(y0 <= cy <= y1 for y0, y1 in spans):
            continue                       # inside a table band -> replaced
        txt = " ".join(s.get("text", "") for ln in blk.get("lines", [])
                       for s in ln.get("spans", [])).strip()
        if txt:
            items.append((bx[1], txt))
    items.sort(key=lambda t: t[0])
    return "\n\n".join(t[1] for t in items), True


def _convert_fast(pdf_path: Path, profile, log, progress, should_stop) -> str:
    """Digital-born PDF -> MD with coordinate-rebuilt tables, no ML/GPU.
    ~milliseconds/page. Scanned pages (no text layer) yield little here —
    use mineru/docling for those. Chart/figure recognition is not done."""
    import fitz

    expected_md = _expected_md(pdf_path)
    processed, is_temp = _maybe_rotate(pdf_path, profile, log)
    doc = fitz.open(str(processed))
    npages = doc.page_count
    parts, ntab = [], 0
    try:
        for i in range(npages):
            if should_stop():
                return ""
            md, had = _page_to_md(doc[i])
            ntab += int(had)
            parts.append(f"\n\n# --- PDF 物理切片：第 {i + 1} - {i + 1} 页 ---"
                         f"\n\n{md}\n")
            progress(0.1 + 0.85 * (i + 1) / max(npages, 1))
    finally:
        doc.close()
    expected_md.write_text("\n".join(parts), encoding="utf-8")
    if is_temp:
        try:
            Path(processed).unlink()
        except Exception:
            pass
    log(f"Fast engine: {npages} pages, {ntab} with a reconstructed table")
    progress(0.97)
    return str(expected_md)


# ════════════════════════════════════════════════════════════════════════
#  Docling (local, optional)
# ════════════════════════════════════════════════════════════════════════
def _convert_docling(pdf_path: Path, profile, log, progress, should_stop) -> str:
    try:
        from PDF2MD import parse_large_pdf_safely
    except ImportError as e:
        raise ImportError(
            "the local Docling engine needs the optional 'docling' package "
            "(pip install docling). Original error: " + str(e)) from e

    expected_md = _expected_md(pdf_path)
    # table-rebuild switch: bridge the profile flag to the legacy parser's
    # env toggle. True = header-anchored coordinate rebuild (default);
    # False = raw/original TableFormer output, untouched by the fast work.
    rebuild = profile.config.get("convert", {}).get("docling_table_rebuild", True)
    os.environ["HEADER_ANCHORED_TABLES"] = "1" if rebuild else "0"
    log("Docling tables: " + ("header-anchored coordinate rebuild"
                              if rebuild else "raw TableFormer (rebuild off)"))
    # rotation runs here (the skill's robust pre-pass); the legacy parser does
    # NOT rotate again, so there is no double-rotation
    processed, is_temp = _maybe_rotate(pdf_path, profile, log)
    processed_path = str(processed)
    progress(0.15)
    if should_stop():
        return ""

    log("Starting Docling + LLM parsing (this can take a while)...")
    # write straight to the expected name (named after the ORIGINAL pdf, even
    # when a rotated temp was parsed) — no fragile post-hoc rename
    parse_large_pdf_safely(processed_path, output_md_path=str(expected_md))
    progress(0.90)

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

    # 0) landscape-table pre-pass: fix rotated pages BEFORE the cloud parse
    parse_pdf, rot_temp = _maybe_rotate(pdf_path, profile, log)
    progress(0.08)
    if should_stop():
        return ""

    # 1) cloud parse -> complete md, may still contain ![](images/..)
    md, work_dir = mineru_pdf_to_md(
        str(parse_pdf), token,
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
    if rot_temp:
        try:
            parse_pdf.unlink()
        except Exception:
            pass
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
    elif engine == "fast":
        out = _convert_fast(original_pdf, profile, log, progress, should_stop)
    else:
        out = _convert_docling(original_pdf, profile, log, progress, should_stop)

    if not out or not Path(out).exists():
        raise FileNotFoundError(f"Parsing finished but output not found: {expected_md}")
    progress(1.0)
    log(f"✅ Markdown generated: {Path(out).name}")
    return out
