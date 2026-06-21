"""
PDF + recognized MD -> side-by-side comparison HTML (with in-browser proofreading)
─────────────────────────────────────────────────────────────────
Produces one self-contained HTML file:
  Left:  original PDF page images (rendered via PyMuPDF, embedded as base64)
  Right: recognized Markdown rendered to HTML (HTML <table> blocks preserved)
         Every section has an Edit mode — modify the raw MD text directly;
         the "Download proofread MD" button at the top merges all edits and
         exports the corrected Markdown.

Sections follow the engine's physical-slice markers:
  # --- PDF 物理切片：第 X - Y 页 ---
(the marker text is a Chinese wire format written by engine.mineru — the regex
here and the JS exporter must keep it verbatim). Without markers the whole
document becomes a single section.

Header brand lockup: SunOCR (inline SVG + web text; works offline — only the
"Sun" script glyph loads the Allura font from Google Fonts, with a system
cursive fallback).

Dependencies: markdown2, pymupdf
"""
from __future__ import annotations

import argparse
import base64
import html
import math
import re
import sys
from pathlib import Path

# Windows GBK console compatibility: switch stdout/stderr to UTF-8
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import markdown2

_SCRIPT_DIR = Path(__file__).resolve().parent
_SKILL_ROOT = _SCRIPT_DIR.parent
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))


# ==============================================================================
# Slice parsing
# ==============================================================================

_SLICE_RE = re.compile(
    r"^\s*#\s*---\s*PDF\s*物理切片\s*[:：]\s*第\s*(\d+)\s*-\s*(\d+)\s*页\s*---\s*$",
    re.MULTILINE,
)


def split_md_by_slices(md_text: str, total_pages: int) -> list[dict]:
    """
    Split the MD on physical-slice markers.
    Returns [{"start":1,"end":25,"md":...,"marker":True}, ...]
    No markers -> a single full-document section (marker=False).
    """
    matches = list(_SLICE_RE.finditer(md_text))
    if not matches:
        return [{"start": 1, "end": total_pages, "md": md_text.strip(),
                 "marker": False}]

    sections: list[dict] = []
    # content before the first marker (e.g. re-leveling artifacts) joins section 1
    preamble = md_text[: matches[0].start()].strip()
    for i, m in enumerate(matches):
        start_page, end_page = int(m.group(1)), int(m.group(2))
        seg_start = m.end()
        seg_end = matches[i + 1].start() if i + 1 < len(matches) else len(md_text)
        body = md_text[seg_start:seg_end].strip()
        if i == 0 and preamble:
            body = preamble + "\n\n" + body
        sections.append({"start": start_page, "end": end_page,
                         "md": body, "marker": True})
    return sections


# ==============================================================================
# Block-level anchoring  (MD block  ->  PDF page + vertical extent)
# ------------------------------------------------------------------------------
# Anchor source priority:
#   1. OCR engine coordinates — MinerU middle.json (auto-discovered next to the
#      MD, or passed via --anchors). Works on SCANNED PDFs, which is the real
#      use case; this is the only source that can be cell-accurate for tables.
#   2. PyMuPDF text layer (digital PDFs only).
#   3. Nothing found -> JS proportional fallback (and a loud build warning).
# Tables are paired to table anchors BY ORDER (hard match, no fuzzy) — the MD
# is the engine's own output so table order is identical. Text blocks are
# fuzzy-matched monotonically inside the segments between table checkpoints.
# ==============================================================================

import json
from difflib import SequenceMatcher

_DROP_RE = re.compile(r"[^\w\u4e00-\u9fff]", re.UNICODE)
_DATAURI_RE = re.compile(r"data:[^)\s]+")


def _norm_text(s: str) -> str:
    return _DROP_RE.sub("", (s or "")).lower()


def iter_md_blocks(md: str):
    """Yield top-level MD blocks (blank-line separated, fence/table aware)."""
    lines = (md or "").splitlines()
    block: list[str] = []
    i, n, in_fence = 0, len(lines), False
    while i < n:
        ln = lines[i]
        st = ln.strip().lower()
        if st.startswith("```") or st.startswith("~~~"):
            in_fence = not in_fence
            block.append(ln); i += 1; continue
        if in_fence:
            block.append(ln); i += 1; continue
        if st.startswith("<table"):
            if "".join(block).strip():
                yield "\n".join(block); block = []
            tbl = [ln]; i += 1
            while i < n and "</table>" not in lines[i].lower():
                tbl.append(lines[i]); i += 1
            if i < n:
                tbl.append(lines[i]); i += 1
            yield "\n".join(tbl); continue
        if st == "":
            if "".join(block).strip():
                yield "\n".join(block)
            block = []; i += 1; continue
        block.append(ln); i += 1
    if "".join(block).strip():
        yield "\n".join(block)


_PIPE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}", re.MULTILINE)


def _is_md_table(block: str) -> bool:
    s = block.lstrip().lower()
    if s.startswith("<table"):
        return True
    lines = block.splitlines()
    return (len(lines) >= 2 and "|" in lines[0]
            and bool(_PIPE_SEP_RE.match(lines[1])))


# ---- anchor source 1: MinerU middle.json ------------------------------------

def find_engine_json(md_path: Path, pdf_stem: str) -> Path | None:
    """Look for MinerU output near the MD: same dir, parent, and the typical
    <stem>/auto/ layout. middle.json preferred over content_list.json."""
    cands: list[Path] = []
    seen: set[Path] = set()
    for root in (md_path.parent, md_path.parent.parent):
        if not root or not root.exists():
            continue
        for pat in ("*middle.json", "*/*middle.json", "*/auto/*middle.json",
                    "*content_list.json", "*/*content_list.json",
                    "*/auto/*content_list.json"):
            for c in root.glob(pat):
                if c.is_file() and c not in seen:
                    seen.add(c); cands.append(c)
    if not cands:
        return None

    def score(p: Path):
        s = 10 if "middle" in p.name.lower() else 0
        if pdf_stem and pdf_stem.lower() in p.stem.lower():
            s += 5
        return (s, p.stat().st_mtime)

    return max(cands, key=score)


def _middle_leaf_text(blk: dict) -> str:
    parts: list[str] = []
    for line in blk.get("lines") or []:
        for sp in line.get("spans") or []:
            parts.append(sp.get("content") or sp.get("html") or "")
    return " ".join(parts)


def _middle_all_text(blk: dict) -> str:
    subs = blk.get("blocks")
    if subs:
        return " ".join(_middle_all_text(s) for s in subs)
    return _middle_leaf_text(blk)


def _middle_lines(blk: dict, ph: float) -> list[dict]:
    """Per-line anchors {norm,yf0,yf1} from a (possibly nested) block."""
    subs = blk.get("blocks")
    if subs:
        out: list[dict] = []
        for s in subs:
            out.extend(_middle_lines(s, ph))
        return out
    out = []
    for line in blk.get("lines") or []:
        bbox = line.get("bbox")
        if not bbox or len(bbox) < 4:
            continue
        nt = _norm_text(" ".join(sp.get("content") or ""
                                 for sp in line.get("spans") or []))
        if nt:
            out.append({"norm": nt, "yf0": bbox[1] / ph, "yf1": bbox[3] / ph})
    return out


def _anchors_from_middle(data: dict) -> list[dict]:
    out: list[dict] = []
    for page in data.get("pdf_info") or []:
        pidx = page.get("page_idx", 0)
        ps = page.get("page_size") or [0, 0]
        ph = float(ps[1] or 1.0)
        for blk in (page.get("para_blocks") or page.get("preproc_blocks") or []):
            bbox = blk.get("bbox")
            if not bbox or len(bbox) < 4:
                continue
            btype = (blk.get("type") or "").lower()
            is_table = "table" in btype
            nt = _norm_text(_middle_all_text(blk))
            if not nt and not is_table and "image" not in btype:
                continue
            a = {"page": pidx + 1, "table": is_table, "norm": nt,
                 "yf0": bbox[1] / ph, "yf1": bbox[3] / ph}
            if not is_table:
                lines = _middle_lines(blk, ph)
                if lines:
                    a["lines"] = lines
            out.append(a)
    return out


def _anchors_from_content_list(items: list) -> list[dict]:
    """content_list.json has no page_size; normalize bbox per page. If all
    coords look <=1000 assume 0–1000 normalized, else use the per-page max y."""
    per_page: dict[int, float] = {}
    rows = []
    for it in items or []:
        bbox, pidx = it.get("bbox"), it.get("page_idx")
        if not bbox or len(bbox) < 4 or pidx is None:
            continue
        rows.append((pidx, bbox, it))
        per_page[pidx] = max(per_page.get(pidx, 0.0), float(bbox[3]))
    if not rows:
        return []
    norm1000 = all(v <= 1000.5 for v in per_page.values())
    out: list[dict] = []
    for pidx, bbox, it in rows:
        ph = 1000.0 if norm1000 else (per_page[pidx] * 1.02 or 1.0)
        btype = (it.get("type") or "").lower()
        is_table = btype == "table"
        nt = _norm_text(it.get("text") or it.get("table_body") or "")
        if not nt and not is_table and btype != "image":
            continue
        out.append({"page": pidx + 1, "table": is_table, "norm": nt,
                    "yf0": bbox[1] / ph, "yf1": bbox[3] / ph})
    return out


def load_engine_anchors(md_path: Path, pdf_stem: str,
                        anchors_path=None) -> tuple[list[dict] | None, Path | None]:
    p = Path(anchors_path) if anchors_path else find_engine_json(md_path, pdf_stem)
    if not p or not p.exists():
        return None, None
    try:
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None, p
    if isinstance(data, dict) and "pdf_info" in data:
        return _anchors_from_middle(data), p
    if isinstance(data, list):
        return _anchors_from_content_list(data), p
    return None, p


# ---- anchor source 2: digital-PDF text layer --------------------------------

def pdf_block_anchors(doc, start: int, end: int) -> list[dict]:
    out: list[dict] = []
    for pno in range(start, end + 1):
        if pno < 1 or pno > doc.page_count:
            continue
        page = doc[pno - 1]
        ph = page.rect.height or 1.0
        for b in page.get_text("dict")["blocks"]:
            if b.get("type") != 0:          # 0 = text block
                continue
            lines = []
            for ln in b.get("lines") or []:
                lb = ln.get("bbox")
                nt = _norm_text("".join(sp.get("text", "")
                                        for sp in ln.get("spans") or []))
                if nt and lb:
                    lines.append({"norm": nt,
                                  "yf0": lb[1] / ph, "yf1": lb[3] / ph})
            if not lines:
                continue
            bb = b["bbox"]
            out.append({"page": pno, "table": False,
                        "norm": "".join(l["norm"] for l in lines),
                        "yf0": bb[1] / ph, "yf1": bb[3] / ph,
                        "lines": lines})
    return out


# ---- alignment ---------------------------------------------------------------

def _sim(a: str, b: str) -> float:
    a, b = a[:200], b[:200]
    sm = SequenceMatcher(None, a, b)
    if sm.real_quick_ratio() < 0.3:
        return 0.0
    return sm.ratio()


def _fuzzy_monotone(md_meta, res, m_lo, m_hi, anchors, a_lo, a_hi,
                    floor: float = 0.35):
    j = a_lo
    for i in range(m_lo, m_hi):
        b = md_meta[i]
        if b["table"] or not b["norm"]:
            continue
        best, bs, bj = None, 0.0, j
        for k in range(j, a_hi):
            a = anchors[k]
            if a["table"]:
                continue
            s = _sim(b["norm"], a["norm"])
            if s > bs:
                bs, best, bj = s, a, k
        if best and bs >= floor:
            res[i] = best
            j = bj + 1


def _table_from_text_anchors(b: dict, anchors: list[dict], j_start: int):
    """No table-typed anchors (plain text layer): locate the table by finding
    the run of consecutive same-page text anchors whose content is contained
    in the table's cell text, and take the union of their bboxes."""
    start = None
    for k in range(j_start, len(anchors)):
        key = anchors[k]["norm"][:60]
        if len(key) >= 4 and key in b["norm"]:
            start = k
            break
    if start is None:
        return None, j_start
    page, end = anchors[start]["page"], start
    k = start + 1
    while k < len(anchors) and anchors[k]["page"] == page:
        key = anchors[k]["norm"][:60]
        if len(key) >= 4 and key in b["norm"]:
            end = k; k += 1
        else:
            break
    return {"page": page, "table": True,
            "yf0": min(anchors[i]["yf0"] for i in range(start, end + 1)),
            "yf1": max(anchors[i]["yf1"] for i in range(start, end + 1))}, end + 1


def align_section_blocks(md_meta: list[dict],
                         anchors: list[dict]) -> list[dict | None]:
    """Tables paired strictly by ordinal when the anchor source types them
    (engine JSON); otherwise inferred from contained text-anchor runs. Text
    fuzzy-matched monotonically. Unmatched -> None (JS proportional)."""
    res: list[dict | None] = [None] * len(md_meta)
    if not anchors:
        return res
    anchors = sorted(anchors, key=lambda a: (a["page"], a["yf0"]))
    an_t = [i for i, a in enumerate(anchors) if a["table"]]

    if not an_t:
        # plain text-layer anchors: one monotone pass, tables by containment
        j = 0
        for i, b in enumerate(md_meta):
            if not b["norm"]:
                continue
            if b["table"]:
                m, j = _table_from_text_anchors(b, anchors, j)
                if m:
                    res[i] = m
                continue
            best, bs, bj = None, 0.0, j
            for k in range(j, len(anchors)):
                s = _sim(b["norm"], anchors[k]["norm"])
                if s > bs:
                    bs, best, bj = s, anchors[k], k
            if best and bs >= 0.35:
                res[i] = best
                j = bj + 1
        return res

    md_t = [i for i, b in enumerate(md_meta) if b["table"]]
    pairs = list(zip(md_t, an_t))          # order-preserving hard pairing
    for mi, ai in pairs:
        res[mi] = anchors[ai]
    checkpoints = [(-1, -1)] + pairs + [(len(md_meta), len(anchors))]
    for (m0, a0), (m1, a1) in zip(checkpoints, checkpoints[1:]):
        _fuzzy_monotone(md_meta, res, m0 + 1, m1, anchors, a0 + 1, a1)
    return res


# ==============================================================================
# PDF page rendering (PyMuPDF)
# ==============================================================================

def render_pdf_pages(pdf_path: Path, dpi: int = 96,
                     quality: int = 70) -> list[str]:
    """Render every page as a JPEG base64 data URL (index 0 = page 1)."""
    import fitz  # pymupdf

    doc = fitz.open(str(pdf_path))
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    urls: list[str] = []
    for page in doc:
        pix = page.get_pixmap(matrix=mat, alpha=False)
        jpg = pix.tobytes("jpeg", jpg_quality=quality)
        urls.append("data:image/jpeg;base64,"
                    + base64.b64encode(jpg).decode("ascii"))
    doc.close()
    return urls


# ==============================================================================
# Inline MD image refs as base64 (reuses the engine's multi-root index)
# ==============================================================================

_IMG_REF_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_IMG_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}


def inline_md_images(md_text: str, md_path: Path) -> str:
    """Resolve ![](images/..) refs to local files and inline as base64;
    unresolvable refs become a visible placeholder note."""
    try:
        from engine.mineru import collect_image_roots, build_image_index, _resolve_image
        roots = collect_image_roots(md_path, None)
        index = build_image_index(roots)
    except Exception:
        roots, index = [], {}

    def _repl(m: re.Match) -> str:
        alt, rel = m.group(1), m.group(2).strip()
        if rel.startswith("data:"):
            return m.group(0)
        p = None
        try:
            p = _resolve_image(rel, index, roots)
        except Exception:
            p = None
        if p is None:
            cand = md_path.parent / rel
            p = cand if cand.exists() else None
        if p is not None and p.exists():
            mime = _IMG_MIME.get(p.suffix.lower(), "image/png")
            b64 = base64.b64encode(p.read_bytes()).decode("ascii")
            return f"![{alt}](data:{mime};base64,{b64})"
        return (f'> **[Unrecognized figure]** `{rel}` '
                f'(source file not found — add content in Edit mode)')

    return _IMG_REF_RE.sub(_repl, md_text)


# ==============================================================================
# Markdown -> HTML
# ==============================================================================

def _ensure_blank_lines_around_tables(md: str) -> str:
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip().lower()
        if stripped.startswith("<table>") or stripped.startswith("<table "):
            if out and out[-1].strip() != "":
                out.append("")
            out.append(line)
            if "</table>" not in stripped:
                i += 1
                while i < len(lines):
                    out.append(lines[i])
                    if "</table>" in lines[i].strip().lower():
                        i += 1
                        break
                    i += 1
            else:
                i += 1
            if i < len(lines) and out and out[-1].strip() != "":
                out.append("")
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _enhance_tables(html_content: str) -> str:
    """Wrap tables in a scroll container; tag numeric-only cells with a
    `numeric` class (tabular-figures font)."""
    def _wrap(m: re.Match) -> str:
        return f'<div class="table-wrapper">{m.group(0)}</div>'

    html_content = re.sub(r"<table[^>]*>.*?</table>", _wrap, html_content,
                          flags=re.DOTALL | re.IGNORECASE)

    def _classify(m: re.Match) -> str:
        tag, attrs, content = m.group(1), m.group(2) or "", m.group(3)
        if re.match(r"^[\s\-+$€¥£]?\(?[\d,]+(?:\.\d+)?\)?%?$", content.strip()):
            if 'class="' in attrs:
                attrs = re.sub(r'class="([^"]*)"', r'class="\1 numeric"', attrs)
            else:
                attrs = f'{attrs} class="numeric"'
        return f"<{tag}{attrs}>{content}</{tag}>"

    return re.sub(r"<(td|th)([^>]*)>([^<]*)</\1>", _classify, html_content,
                  flags=re.IGNORECASE)


def md_to_html_fragment(md_text: str) -> str:
    md = _ensure_blank_lines_around_tables(md_text or "")
    processor = markdown2.Markdown(extras=[
        "fenced-code-blocks", "code-friendly", "cuddled-lists",
        "header-ids", "strike", "tables", "task_list",
    ])
    out = processor.convert(md)
    out = _enhance_tables(out)
    return out


# ==============================================================================
# SunOCR brand lockup (inline SVG + web text)
# ==============================================================================

_INK = "#0e1a26"
_BLUE = "#1e6ee8"
_TEAL = "#00c2b2"
_AMBER = "#f5a524"


def _arc_sun_mark_svg(size: float, id_suffix: str = "lk") -> str:
    rays = []
    for deg in (-60, -30, 0, 30, 60):
        a = math.radians(deg)
        x1 = 32 + math.sin(a) * 14
        y1 = 38 - math.cos(a) * 14
        x2 = 32 + math.sin(a) * 18
        y2 = 38 - math.cos(a) * 18
        rays.append(
            f'<line x1="{x1:.3f}" y1="{y1:.3f}" x2="{x2:.3f}" y2="{y2:.3f}" '
            f'stroke="{_AMBER}" stroke-width="2.2" stroke-linecap="round"/>'
        )
    brush_arc = ("M 10 40 A 22 22 0 0 1 54 40 A 3.6 3.6 0 0 0 54 32.8 "
                 "C 44 16, 20 13.5, 10 40 Z")
    gid = f"as-{id_suffix}-arc"
    return (
        f'<svg class="sunocr-mark" width="{size:.2f}" height="{size:.2f}" '
        f'viewBox="0 0 64 64" aria-hidden="true" focusable="false">'
        f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="1" y2="0">'
        f'<stop offset="0%" stop-color="{_BLUE}" stop-opacity="0.85"/>'
        f'<stop offset="100%" stop-color="{_TEAL}"/>'
        f'</linearGradient></defs>'
        f'<path d="{brush_arc}" fill="url(#{gid})"/>'
        f'<circle cx="32" cy="38" r="9" fill="{_AMBER}"/>'
        f'{"".join(rays)}'
        f'<line x1="8" y1="50" x2="56" y2="50" stroke="{_BLUE}" '
        f'stroke-width="1.8" stroke-linecap="round" opacity="0.7"/></svg>'
    )


def _shutter_svg(render_size: float) -> str:
    cx, cy, R, r, sw = 32, 32, 25, 8.5, 6.0
    hex_pts = []
    for i in range(6):
        a = math.radians(i * 60)
        hex_pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    hex_path = "M " + " L ".join(f"{x:.3f} {y:.3f}" for x, y in hex_pts) + " Z"
    lines = []
    for i in range(6):
        a_o = math.radians(i * 60 - 30)
        a_i = math.radians(i * 60)
        lines.append(
            f'<line x1="{cx + R * math.cos(a_o):.3f}" y1="{cy + R * math.sin(a_o):.3f}" '
            f'x2="{cx + r * math.cos(a_i):.3f}" y2="{cy + r * math.sin(a_i):.3f}" '
            f'stroke="{_INK}" stroke-width="{sw}" stroke-linecap="round"/>'
        )
    return (
        f'<svg class="sunocr-shutter" viewBox="0 0 64 64" aria-hidden="true" '
        f'focusable="false" shape-rendering="geometricPrecision" '
        f'style="overflow:visible;width:{render_size:.2f}px;height:{render_size:.2f}px;">'
        f'<circle cx="{cx}" cy="{cy}" r="{R}" fill="none" stroke="{_INK}" stroke-width="{sw}"/>'
        f'{"".join(lines)}'
        f'<path d="{hex_path}" fill="none" stroke="{_INK}" '
        f'stroke-width="{sw}" stroke-linejoin="round"/></svg>'
    )


def sunocr_lockup_html(h: float = 56.0, lockup_gap: float = 18.0) -> str:
    mark_size = h * 1.35
    wm_h = h * 0.92
    cr_size = wm_h * 0.78
    cap_h = cr_size * 0.72
    sun_size = wm_h * 1.04
    shutter_slot = cap_h * 1.10
    shutter_render = shutter_slot * 1.07
    gap_l = wm_h * 0.05
    gap_r = wm_h * 0.018
    sun_dy = wm_h * 0.045
    return (
        f'<span class="sunocr-lockup" style="gap:{lockup_gap:.2f}px;">'
        f'{_arc_sun_mark_svg(mark_size)}'
        f'<span class="sunocr-wordmark">'
        f'<span class="sunocr-sun" style="font-size:{sun_size:.2f}px;'
        f'margin-right:{gap_l:.2f}px;transform:translateY({sun_dy:.2f}px);">Sun</span>'
        f'<span class="sunocr-shutter-wrap" style="margin-right:{gap_r:.2f}px;'
        f'width:{shutter_slot:.2f}px;height:{shutter_slot:.2f}px;">'
        f'{_shutter_svg(shutter_render)}</span>'
        f'<span class="sunocr-cr" style="font-size:{cr_size:.2f}px;">CR</span>'
        f'</span></span>'
    )


# ==============================================================================
# CSS / JS
# ==============================================================================

_CSS = r''':root {
  --bg-primary:#eef3f7; --bg-secondary:#f7fbff;
  --surface:rgba(255,255,255,0.72);
  --text:#0e1a26; --text-secondary:#314457; --muted:#536171;
  --brand-primary:#1e6ee8; --brand-secondary:#00c2b2;
  --brand-ink:#0e1a26; --brand-amber:#f5a524;
  --border:rgba(14,26,38,0.12); --glass-stroke:rgba(255,255,255,0.42);
  --shadow:0 22px 48px rgba(8,28,61,0.14);
  --shadow-soft:0 10px 28px rgba(8,28,61,0.10);
  --radius-xl:22px; --radius-lg:18px; --radius-md:14px; --radius-sm:10px;
  --maxw:1680px; --header-maxw:1160px;
  --font-ui:"Avenir Next","Segoe UI Variable","IBM Plex Sans","PingFang SC","Microsoft YaHei","Helvetica Neue",Arial,sans-serif;
  --font-display:var(--font-ui);
  --font-code:"JetBrains Mono","SFMono-Regular",Menlo,Consolas,monospace;
  --font-script:"Allura","Apple Chancery","Brush Script MT","Lucida Handwriting",cursive;
  --font-cr:Georgia,"Times New Roman",serif;
}
*,*::before,*::after{box-sizing:border-box}
html,body{margin:0;padding:0;min-height:100%}
body{font-family:var(--font-ui);font-size:14px;line-height:1.6;color:var(--text);
  background:radial-gradient(900px 560px at 8% 12%,rgba(0,194,178,0.12),transparent 55%),
    radial-gradient(960px 620px at 88% -8%,rgba(30,110,232,0.18),transparent 52%),
    linear-gradient(180deg,var(--bg-secondary) 0%,var(--bg-primary) 100%);
  background-attachment:fixed;-webkit-font-smoothing:antialiased;overflow-x:hidden}
.wrap{position:relative;z-index:1;min-height:100vh;display:flex;flex-direction:column}
header{position:sticky;top:0;z-index:20;width:100%;padding:10px clamp(16px,3vw,30px);
  background:rgba(255,255,255,0.44);border-bottom:1px solid var(--glass-stroke);
  -webkit-backdrop-filter:blur(18px) saturate(180%);backdrop-filter:blur(18px) saturate(180%)}
.header-content{max-width:var(--header-maxw);margin:0 auto;display:flex;flex-direction:column;
  align-items:center;gap:10px}
.brand{display:inline-flex;align-items:center;gap:12px;color:inherit;min-width:0}
.logo{display:inline-flex;align-items:center;justify-content:center;height:76px;padding:0 22px;
  border-radius:18px;background:linear-gradient(180deg,rgba(255,255,255,0.97),rgba(244,249,255,0.92));
  border:1px solid rgba(255,255,255,0.72);
  box-shadow:0 14px 34px rgba(8,28,61,0.11),0 1px 0 rgba(255,255,255,0.82) inset;
  overflow:hidden;position:relative}
.sunocr-lockup{display:inline-flex;align-items:center;line-height:1;color:var(--brand-ink)}
.sunocr-mark{display:block;flex:0 0 auto}
.sunocr-wordmark{display:inline-flex;align-items:center;line-height:1}
.sunocr-sun{display:inline-block;font-family:var(--font-script);line-height:1;
  white-space:nowrap;color:var(--brand-ink)}
.sunocr-shutter-wrap{display:inline-flex;align-items:center;justify-content:center;flex:0 0 auto}
.sunocr-shutter{display:block}
.sunocr-cr{display:inline-block;font-family:var(--font-cr);font-weight:400;
  letter-spacing:-0.01em;line-height:1;color:var(--brand-ink)}
.subtitle-badge{display:inline-flex;align-items:center;justify-content:center;padding:8px 20px;
  border-radius:9999px;background:linear-gradient(135deg,rgba(255,255,255,0.94),rgba(236,246,255,0.92));
  box-shadow:0 10px 24px rgba(8,28,61,0.09);border:1px solid rgba(30,110,232,0.10);
  font-size:12px;letter-spacing:0.02em}
.subtitle-badge .text{font-weight:600;letter-spacing:0.045em;color:#29506f;
  background:linear-gradient(135deg,#2c5f88 0%,#157d91 100%);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.toolbar-global{display:flex;align-items:center;gap:10px;flex-wrap:wrap;justify-content:center}
.btn{display:inline-flex;align-items:center;gap:6px;padding:7px 16px;border-radius:10px;
  border:1px solid rgba(30,110,232,0.25);cursor:pointer;font-size:12.5px;font-weight:600;
  background:linear-gradient(135deg,rgba(255,255,255,0.96),rgba(240,248,255,0.92));
  color:#1e5cb8;transition:transform .15s ease,box-shadow .15s ease}
.btn:hover{transform:translateY(-1px);box-shadow:0 8px 18px rgba(8,28,61,0.12)}
.btn-primary{background:linear-gradient(135deg,var(--brand-primary),var(--brand-secondary));
  color:#fff;border-color:transparent}
.edit-hint{font-size:12px;color:var(--muted)}
main{flex:1;width:100%;max-width:var(--maxw);margin:0 auto;
  padding:30px clamp(16px,3vw,32px) 44px}
.page-section{position:relative;margin-bottom:24px;border-radius:var(--radius-xl);
  overflow:clip;  /* NOT overflow:hidden — that creates a scroll container and silently kills the sticky image column */
  background:var(--surface);border:1px solid var(--glass-stroke);box-shadow:var(--shadow);
  -webkit-backdrop-filter:blur(18px) saturate(160%);backdrop-filter:blur(18px) saturate(160%)}
.page-header{position:relative;z-index:1;display:flex;align-items:center;gap:12px;
  padding:14px 24px;background:linear-gradient(135deg,var(--brand-primary),var(--brand-secondary));
  color:#fff;font-size:13px;font-weight:700;letter-spacing:0.09em;text-transform:uppercase;
  border-radius:0 0 18px 18px;box-shadow:0 4px 14px rgba(8,28,61,0.11)}
.page-indicator{width:10px;height:10px;border-radius:50%;background:#fff;
  box-shadow:0 0 0 6px rgba(255,255,255,0.18);flex:0 0 auto}
.page-section.modified .page-indicator{background:var(--brand-amber);
  box-shadow:0 0 0 6px rgba(245,165,36,0.30)}
.modified-tag{display:none;margin-left:auto;font-size:11px;font-weight:700;
  background:rgba(255,255,255,0.22);padding:3px 10px;border-radius:999px;letter-spacing:0.05em}
.page-section.modified .modified-tag{display:inline-block}
.sec-toolbar{margin-left:auto;display:flex;gap:8px}
.page-section.modified .sec-toolbar{margin-left:12px}
.btn-sec{padding:4px 12px;border-radius:8px;border:1px solid rgba(255,255,255,0.5);
  cursor:pointer;font-size:11.5px;font-weight:700;background:rgba(255,255,255,0.18);
  color:#fff;letter-spacing:0.05em}
.btn-sec:hover{background:rgba(255,255,255,0.32)}
.page-content{position:relative;z-index:1;display:grid;
  grid-template-columns:repeat(2,minmax(0,1fr));gap:18px;padding:18px;align-items:start}
.image-col{position:sticky;top:calc(var(--hdr-h,170px) + 12px);
  height:calc(100vh - var(--hdr-h,170px) - 26px);min-height:420px;align-self:start}
.image-panel{position:absolute;inset:0;overflow-y:auto;overflow-x:hidden;
  overscroll-behavior:contain;scrollbar-width:thin;
  scrollbar-color:rgba(30,110,232,0.35) transparent;
  border-radius:var(--radius-lg);border:1px solid var(--border);
  background:linear-gradient(180deg,rgba(255,255,255,0.96),rgba(244,250,255,0.90));
  box-shadow:inset 0 1px 0 rgba(255,255,255,0.72),var(--shadow-soft)}
.image-panel::-webkit-scrollbar{width:10px}
.image-panel::-webkit-scrollbar-thumb{background:rgba(30,110,232,0.28);
  border-radius:999px;border:2px solid transparent;background-clip:content-box}
.image-panel::-webkit-scrollbar-thumb:hover{background:rgba(30,110,232,0.46);
  border:2px solid transparent;background-clip:content-box}
.image-track{position:relative;display:flex;flex-direction:column}
::highlight(sync-sentence){background:rgba(245,165,36,0.40);color:inherit}
.page-unit{margin:0;padding:0}
.sync-flash{position:absolute;left:0;right:0;height:44px;
  pointer-events:none;border-radius:6px;opacity:0;z-index:2;
  background:linear-gradient(90deg,rgba(245,165,36,0.32),rgba(245,165,36,0.10));
  border:1px solid rgba(245,165,36,0.60);
  box-shadow:0 0 0 1px rgba(245,165,36,0.18);
  transition:opacity .25s ease}
.sync-flash.show{opacity:1}
.md-block.md-table[data-page] td,.md-block.md-table[data-page] th{cursor:pointer}
.image-panel img{display:block;width:100%;height:auto;background:#fff;
  border-bottom:1px solid rgba(14,26,38,0.08)}
.page-no{padding:4px 12px;font-size:11px;color:var(--muted);text-align:right;
  background:rgba(238,243,247,0.8);border-bottom:1px solid rgba(14,26,38,0.05)}
.image-missing{padding:20px;color:var(--muted);font-size:13px}
.markdown-panel{min-height:0;padding:18px 20px;border-radius:var(--radius-lg);
  border:1px solid rgba(14,26,38,0.08);
  background:linear-gradient(180deg,rgba(255,255,255,0.94),rgba(248,251,255,0.92));
  box-shadow:inset 0 1px 0 rgba(255,255,255,0.84)}
textarea.md-source{display:none;width:100%;min-height:480px;resize:vertical;
  padding:14px 16px;border-radius:var(--radius-md);border:1px solid rgba(30,110,232,0.30);
  background:#fdfefe;font-family:var(--font-code);font-size:13px;line-height:1.6;
  color:var(--text);outline:none}
textarea.md-source:focus{border-color:var(--brand-primary);
  box-shadow:0 0 0 3px rgba(30,110,232,0.12)}
.page-section.editing textarea.md-source{display:block}
.page-section.editing .markdown-content{display:none}
.markdown-content{color:var(--text);font-size:14px;line-height:1.72;
  word-wrap:break-word;overflow-wrap:break-word}
.md-block{display:block;position:relative;border-radius:8px}
.md-block[data-page]{cursor:pointer}
.md-block[data-page]:hover{background:rgba(30,110,232,0.045);
  box-shadow:-10px 0 0 rgba(30,110,232,0.045),
    inset 3px 0 0 rgba(30,110,232,0.30)}
.markdown-content h1,.markdown-content h2,.markdown-content h3,
.markdown-content h4,.markdown-content h5,.markdown-content h6{
  margin:1.45em 0 0.6em;font-family:var(--font-display);line-height:1.25;color:var(--text)}
.markdown-content h1:first-child,.markdown-content h2:first-child,
.markdown-content h3:first-child{margin-top:0}
.markdown-content h1{font-size:1.7em;font-weight:800;letter-spacing:-0.02em;
  padding-bottom:0.28em;border-bottom:1px solid rgba(30,110,232,0.14)}
.markdown-content h2{font-size:1.4em;font-weight:700;padding-bottom:0.24em;
  border-bottom:1px solid rgba(14,26,38,0.08)}
.markdown-content h3{font-size:1.2em;font-weight:700}
.markdown-content h4{font-size:1.06em;font-weight:700}
.markdown-content p{margin:0 0 1em}
.markdown-content blockquote{margin:1.15em 0;padding:0.85em 1.1em;
  border-left:4px solid var(--brand-amber);border-radius:0 10px 10px 0;
  background:linear-gradient(135deg,rgba(245,165,36,0.10),rgba(245,165,36,0.04));
  color:var(--text-secondary)}
.markdown-content blockquote p{margin:0}
.markdown-content ul,.markdown-content ol{margin:0.8em 0;padding-left:1.75em}
.markdown-content li{margin:0.3em 0}
.markdown-content code{font-family:var(--font-code);font-size:0.9em;
  padding:0.18em 0.42em;color:#103760;background:rgba(30,110,232,0.07);
  border:1px solid rgba(30,110,232,0.10);border-radius:6px}
.markdown-content pre{margin:1.15em 0;padding:1.05em 1.25em;overflow-x:auto;
  border-radius:var(--radius-md);border:1px solid rgba(17,54,93,0.18);
  background:linear-gradient(180deg,#1d2d43 0%,#142133 100%);color:#dbe9f7;
  font-size:13px;line-height:1.55}
.markdown-content pre code{padding:0;border:none;background:transparent;color:inherit}
.markdown-content img{max-width:100%;height:auto;border-radius:var(--radius-sm)}
.table-wrapper{margin:1.15em 0;overflow-x:auto;border-radius:16px;
  border:1px solid rgba(30,110,232,0.12);background:rgba(255,255,255,0.92);
  box-shadow:inset 0 1px 0 rgba(255,255,255,0.80)}
.markdown-content table{width:100%;border-collapse:collapse;font-size:13px;line-height:1.5}
.markdown-content thead{background:linear-gradient(135deg,rgba(30,110,232,0.11),rgba(0,194,178,0.10))}
.markdown-content th,.markdown-content td{padding:9px 14px;text-align:left;
  word-wrap:break-word;overflow-wrap:break-word}
.markdown-content th{font-weight:700;border-bottom:1px solid rgba(30,110,232,0.14);
  border-right:1px solid rgba(14,26,38,0.08)}
.markdown-content td{border-bottom:1px solid rgba(14,26,38,0.07);
  border-right:1px solid rgba(14,26,38,0.05);vertical-align:top}
.markdown-content th:last-child,.markdown-content td:last-child{border-right:none}
.markdown-content tbody tr:last-child td{border-bottom:none}
.markdown-content tbody tr:nth-child(even){background:rgba(30,110,232,0.025)}
.markdown-content tbody tr:hover{background:rgba(0,194,178,0.06)}
.markdown-content .numeric{font-family:var(--font-code);font-variant-numeric:tabular-nums}
footer{margin-top:auto;padding:0 clamp(16px,3vw,32px) 34px;color:var(--muted)}
.foot{max-width:var(--maxw);margin:0 auto;padding-top:18px;text-align:center;
  font-size:13px;border-top:1px solid rgba(14,26,38,0.08)}
.foot strong{color:var(--text)}
@media(max-width:1024px){
  header{position:relative}
  .page-content{grid-template-columns:1fr}
  .image-col{position:relative;top:auto;height:520px;max-width:860px;margin:0 auto;width:100%}
}
@media print{
  body{background:#fff}
  header{position:static;background:#fff;border-bottom:1px solid #ddd}
  .page-section{box-shadow:none;border:1px solid #d8e1ea;background:#fff;page-break-inside:avoid}
  .page-content{grid-template-columns:1fr}
  .image-col{position:static;height:auto;min-height:0}
  .image-panel{position:static;overflow:visible}
  .btn,.btn-sec,.toolbar-global{display:none!important}
}'''


# NOTE: the slice-marker string inside gatherMd() is the Chinese wire format —
# the exported MD must round-trip through the engine's chunker. Keep verbatim.
_JS = r'''(function(){
  document.getElementById('year').textContent = new Date().getFullYear();

  // sticky-left sizing: expose the live header height to CSS so the
  // image column can fill exactly the viewport space below the header
  var _hdr = document.querySelector('header');
  function setHdrH(){
    document.documentElement.style.setProperty('--hdr-h',
      (_hdr ? _hdr.offsetHeight : 160) + 'px');
  }
  setHdrH();
  window.addEventListener('resize', setHdrH);

  // per-section Edit / Preview toggle
  document.querySelectorAll('.btn-sec[data-act="toggle"]').forEach(function(btn){
    btn.addEventListener('click', function(){
      var sec = btn.closest('.page-section');
      var editing = sec.classList.toggle('editing');
      btn.textContent = editing ? 'Preview' : 'Edit';
    });
  });

  // modified flag
  document.querySelectorAll('textarea.md-source').forEach(function(ta){
    ta.addEventListener('input', function(){
      var sec = ta.closest('.page-section');
      if (ta.value !== ta.defaultValue) sec.classList.add('modified');
      else sec.classList.remove('modified');
    });
  });

  // merge and export the proofread MD
  function gatherMd(){
    var parts = [];
    document.querySelectorAll('.page-section').forEach(function(sec){
      var ta = sec.querySelector('textarea.md-source');
      if (!ta) return;
      var txt = ta.value.replace(/\s+$/, '');
      if (sec.dataset.marker === '1') {
        parts.push('# --- PDF 物理切片：第 ' + sec.dataset.pstart +
                   ' - ' + sec.dataset.pend + ' 页 ---\n\n' + txt);
      } else {
        parts.push(txt);
      }
    });
    return parts.join('\n\n') + '\n';
  }

  var dl = document.getElementById('btn-download-md');
  if (dl) dl.addEventListener('click', function(){
    var blob = new Blob([gatherMd()], {type: 'text/markdown;charset=utf-8'});
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = dl.dataset.filename || 'proofread.md';
    document.body.appendChild(a);
    a.click();
    setTimeout(function(){ URL.revokeObjectURL(a.href); a.remove(); }, 800);
  });

  // Edit all / Preview all
  var allEdit = document.getElementById('btn-all-edit');
  if (allEdit) allEdit.addEventListener('click', function(){
    var secs = document.querySelectorAll('.page-section');
    var anyPreview = Array.prototype.some.call(secs, function(s){
      return !s.classList.contains('editing');
    });
    secs.forEach(function(s){
      s.classList.toggle('editing', anyPreview);
      var b = s.querySelector('.btn-sec[data-act="toggle"]');
      if (b) b.textContent = anyPreview ? 'Preview' : 'Edit';
    });
    allEdit.textContent = anyPreview ? 'Preview all' : 'Edit all';
  });

  // ── click-to-sync ─────────────────────────────────────────────
  // The left image column is sticky (fills the viewport below the
  // header) and is a NATIVE scroll container — wheel over it scrolls
  // only the original pages; the right side scrolls with the document.
  // Clicking a sentence in the rendered MD resolves its exact source
  // band live (engine line bboxes + on-the-fly text search) and
  // centers it in the left panel. Double-click the panel: back to top.
  document.querySelectorAll('.page-section').forEach(function(sec){
    var panel = sec.querySelector('.image-panel');
    if (!panel) return;
    panel.addEventListener('dblclick', function(){
      animateScrollTo(panel, 0);
    });
  });

  function flashAt(panel, band){
    var track = panel.querySelector('.image-track') || panel;
    var flash = track.querySelector('.sync-flash');
    if (!flash){
      flash = document.createElement('div');
      flash.className = 'sync-flash';
      track.appendChild(flash);
    }
    flash.style.top = (band.top|0) + 'px';
    flash.style.height = Math.max(12, band.height|0) + 'px';
    flash.style.left = (band.left|0) + 'px';
    flash.style.right = (band.right|0) + 'px';
    flash.style.marginTop = '0';
    void flash.offsetWidth;            // reflow so the transition replays
    flash.classList.add('show');
    clearTimeout(flash._t);
    flash._t = setTimeout(function(){ flash.classList.remove('show'); }, 1800);
  }

  // ── real-time sentence resolution ───────────────────────────
  // Normalization must mirror the Python side (keep letters/digits/_
  // and CJK, lowercase) so client offsets line up with engine norms.
  function normStr(s){
    try { return s.replace(/[^\p{L}\p{N}_]/gu, '').toLowerCase(); }
    catch(_){ return s.replace(/[^\w\u4e00-\u9fff]/g, '').toLowerCase(); }
  }
  function caretPoint(x, y){
    if (document.caretRangeFromPoint){
      var r = document.caretRangeFromPoint(x, y);
      if (r) return {node: r.startContainer, off: r.startOffset};
    } else if (document.caretPositionFromPoint){
      var p = document.caretPositionFromPoint(x, y);
      if (p) return {node: p.offsetNode, off: p.offset};
    }
    return null;
  }
  function isSentEnd(t, i){
    var c = t[i];
    if ('\u3002\uff0e\uff01\uff1f!?\uff1b;\n'.indexOf(c) >= 0) return true;
    // '.' ends a sentence only when not inside a number / abbreviation
    if (c === '.') return i + 1 >= t.length || /\s/.test(t[i + 1]);
    return false;
  }
  // sentence under the click, computed live: char span in the block's
  // DOM text + normalized offsets + the sentence's own normalized text
  function sentenceAt(blk, x, y){
    var pos = caretPoint(x, y);
    if (!pos || pos.node.nodeType !== 3 || !blk.contains(pos.node)) return null;
    var w = document.createTreeWalker(blk, NodeFilter.SHOW_TEXT);
    var nodes = [], full = '', caret = -1, n;
    while ((n = w.nextNode())){
      if (n === pos.node) caret = full.length + pos.off;
      nodes.push({node: n, start: full.length});
      full += n.textContent;
    }
    if (caret < 0) return null;
    caret = Math.min(caret, full.length);
    var s = caret, e = caret;
    while (s > 0 && !isSentEnd(full, s - 1)) s--;
    while (e < full.length && !isSentEnd(full, e)) e++;
    if (e < full.length) e++;                  // include the terminator
    while (s < e && /\s/.test(full[s])) s++;
    if (e <= s) return null;
    return {s: s, e: e, full: full, nodes: nodes,
            n0: normStr(full.slice(0, s)).length,
            n1: normStr(full.slice(0, e)).length,
            norm: normStr(full.slice(s, e))};
  }
  function sentenceRange(info){
    function loc(off){
      for (var i = info.nodes.length - 1; i >= 0; i--){
        if (info.nodes[i].start <= off){
          return {node: info.nodes[i].node,
                  off: Math.min(off - info.nodes[i].start,
                                info.nodes[i].node.textContent.length)};
        }
      }
      return null;
    }
    var a = loc(info.s), b = loc(info.e);
    if (!a || !b) return null;
    var r = document.createRange();
    r.setStart(a.node, a.off);
    r.setEnd(b.node, b.off);
    return r;
  }
  // flash the matched sentence on the MD side (CSS Custom Highlight API;
  // silently skipped on browsers without it)
  function highlightSentence(info){
    try {
      if (!(window.Highlight && CSS.highlights)) return;
      var r = sentenceRange(info);
      if (!r) return;
      CSS.highlights.set('sync-sentence', new Highlight(r));
      clearTimeout(highlightSentence._t);
      highlightSentence._t = setTimeout(function(){
        CSS.highlights.delete('sync-sentence');
      }, 1600);
    } catch(_){}
  }
  function blockLines(blk){
    if (blk._lines !== undefined) return blk._lines;
    var raw = blk.dataset.lines;
    if (!raw){ blk._lines = null; return null; }
    blk._lines = raw.split(';').map(function(s){
      var a = s.split(',');
      return [parseInt(a[0], 10), parseFloat(a[1]), parseFloat(a[2])];
    });
    return blk._lines;
  }
  function blockLineCat(blk){
    if (blk._lcat !== undefined) return blk._lcat;
    blk._lcat = blk.dataset.ltext ? blk.dataset.ltext.split(';').join('') : null;
    return blk._lcat;
  }
  // sentence -> union bbox of the engine lines it covers.
  // Primary: search the sentence's normalized text inside the
  // concatenated line norms (occurrence nearest the DOM offset wins) --
  // stays exact even when MD formatting shifts the offsets; fallback:
  // the raw cumulative offsets from the line table.
  function lineBandForRange(blk, info){
    var lines = blockLines(blk);
    if (!lines || !lines.length) return null;
    var n0 = info.n0, n1 = Math.max(info.n1, info.n0 + 1);
    var cat = blockLineCat(blk);
    if (cat && info.norm && info.norm.length >= 4){
      var idx = -1, from = 0, best = -1, bestD = Infinity;
      while ((idx = cat.indexOf(info.norm, from)) !== -1){
        var d = Math.abs(idx - info.n0);
        if (d < bestD){ bestD = d; best = idx; }
        from = idx + 1;
      }
      if (best >= 0){ n0 = best; n1 = best + info.norm.length; }
    }
    var li0 = 0, li1 = 0;
    for (var i = 0; i < lines.length; i++){
      if (lines[i][0] <= n0) li0 = i;
      if (lines[i][0] < n1) li1 = i;
    }
    if (li1 < li0) li1 = li0;
    var y0 = lines[li0][1], y1 = lines[li1][2];
    for (var k = li0; k <= li1; k++){
      y0 = Math.min(y0, lines[k][1]);
      y1 = Math.max(y1, lines[k][2]);
    }
    return [y0, y1];
  }

  // eased scroll via rAF — native behavior:'smooth' is unreliable
  // across embedders. If no frame arrives within 200ms (hidden /
  // occluded page: rAF is frozen there), jump straight to the target.
  function animateScrollTo(el, to){
    var from = el.scrollTop, t0 = null;
    var D = Math.min(420, 160 + Math.abs(to - from));  // ms
    if (el._anim) cancelAnimationFrame(el._anim);
    clearTimeout(el._animFb);
    function step(ts){
      if (t0 === null) t0 = ts;
      var p = Math.min(1, (ts - t0) / D);
      p = p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2;
      el.scrollTop = from + (to - from) * p;
      el._anim = p < 1 ? requestAnimationFrame(step) : null;
    }
    el._anim = requestAnimationFrame(step);
    el._animFb = setTimeout(function(){
      if (t0 === null){ cancelAnimationFrame(el._anim); el._anim = null; el.scrollTop = to; }
    }, 200);
  }
  function animateWindowBy(dy){
    var se = document.scrollingElement || document.documentElement;
    animateScrollTo(se, se.scrollTop + dy);
  }

  function syncImageToClick(sec, e){
    var panel = sec.querySelector('.image-panel');
    var md = sec.querySelector('.markdown-content');
    if (!panel || !md) return;
    if (!panel.querySelector('img')) return;            // nothing to locate to

    var blk = e.target.closest('.md-block');
    var cell = e.target.closest('td,th');
    var isTable = blk && blk.classList.contains('md-table');
    var targetY = null;
    var band = null;                       // {top,height,left,right} in track px

    if (blk && blk.dataset.page){
      var img = panel.querySelector('img[data-page="' + blk.dataset.page + '"]');
      if (img && img.offsetHeight > 0){
        if (isTable && cell && blk.dataset.yf0){
          // ── cell-precise: row band inside the table's bbox ──
          var table = cell.closest('table');
          var yf0 = parseFloat(blk.dataset.yf0), yf1 = parseFloat(blk.dataset.yf1);
          var nRows = (table && table.rows.length) || 1;
          var r = cell.parentNode.rowIndex || 0;
          var rowH = (yf1 - yf0) / nRows;
          var yfTop = yf0 + r * rowH;
          targetY = img.offsetTop + (yfTop + rowH / 2) * img.offsetHeight;
          band = {top: img.offsetTop + yfTop * img.offsetHeight,
                  height: rowH * img.offsetHeight, left: 0, right: 0};
          if (table && table.offsetWidth > 0){
            var lf = cell.offsetLeft / table.offsetWidth;
            var rf = (cell.offsetLeft + cell.offsetWidth) / table.offsetWidth;
            band.left = lf * img.offsetWidth;
            band.right = (1 - Math.min(1, rf)) * img.offsetWidth;
          }
        } else if (isTable && blk.dataset.yf0){
          // table block, clicked outside a cell -> whole-table band
          var a0 = parseFloat(blk.dataset.yf0), b0 = parseFloat(blk.dataset.yf1);
          targetY = img.offsetTop + ((a0 + b0) / 2) * img.offsetHeight;
          band = {top: img.offsetTop + a0 * img.offsetHeight,
                  height: (b0 - a0) * img.offsetHeight, left: 0, right: 0};
        } else if (blk.dataset.yf0){
          // ── text block: SENTENCE-precise, computed live at click ──
          // 1) sentence under the click -> exact engine line span,
          //    matched live against the embedded line texts
          var info = sentenceAt(blk, e.clientX, e.clientY);
          if (info){
            var seg = lineBandForRange(blk, info);
            if (seg){
              band = {top: img.offsetTop + seg[0] * img.offsetHeight,
                      height: Math.max(10, (seg[1] - seg[0]) * img.offsetHeight),
                      left: 0, right: 0};
              targetY = band.top + band.height / 2;
              highlightSentence(info);
            }
          }
          // 2) no line table: offset-proportional inside the block extent
          if (targetY === null && info){
            var t0 = parseFloat(blk.dataset.yf0), t1 = parseFloat(blk.dataset.yf1);
            var tot = normStr(info.full).length || 1;
            var f0 = Math.min(1, info.n0 / tot);
            var f1 = Math.min(1, Math.max(info.n1 / tot, f0 + 0.04));
            band = {top: img.offsetTop + (t0 + f0 * (t1 - t0)) * img.offsetHeight,
                    height: Math.max(12, (f1 - f0) * (t1 - t0) * img.offsetHeight),
                    left: 0, right: 0};
            targetY = band.top + band.height / 2;
            highlightSentence(info);
          }
          // 3) caret missed: geometric interpolation inside the block
          if (targetY === null){
            var bRect = blk.getBoundingClientRect();
            var g0 = parseFloat(blk.dataset.yf0), g1 = parseFloat(blk.dataset.yf1);
            var fr = bRect.height > 0
                   ? Math.max(0, Math.min(1, (e.clientY - bRect.top) / bRect.height))
                   : 0.5;
            var yfp = g0 + fr * (g1 - g0);
            targetY = img.offsetTop + yfp * img.offsetHeight;
            band = {top: targetY - 14, height: 28, left: 0, right: 0};
          }
        } else if (blk.dataset.yf){
          // legacy: block center only
          var yfc = parseFloat(blk.dataset.yf);
          targetY = img.offsetTop + yfc * img.offsetHeight;
          band = {top: targetY - 22, height: 44, left: 0, right: 0};
        }
      }
    }
    // fallback: proportional mapping through the page TRACK
    if (targetY === null){
      var mdRect = md.getBoundingClientRect();
      if (mdRect.height <= 0) return;
      var track0 = sec.querySelector('.image-track');
      if (!track0) return;
      var f = (e.clientY - mdRect.top) / mdRect.height;
      f = Math.max(0, Math.min(1, f));
      targetY = f * track0.offsetHeight;
      band = {top: targetY - 22, height: 44, left: 0, right: 0};
    }

    // keep the whole left viewer on screen: near the section's top or
    // bottom the sticky column gets pushed out by the section edge, so
    // a click at the bottom of the MD would locate into an off-screen
    // panel — scroll the window back to the pinned position first
    // (the clicked sentence stays visible: it lives in the section's
    // last/first panel-height of content)
    var col = sec.querySelector('.image-col');
    if (col && getComputedStyle(col).position === 'sticky'){
      var pin = parseFloat(getComputedStyle(col).top) || 0;
      var dy = col.getBoundingClientRect().top - pin;
      if (Math.abs(dy) > 1) animateWindowBy(dy);
    }
    // scroll the left panel so the target band is centered in view
    animateScrollTo(panel,
      Math.max(0, band.top + band.height / 2 - panel.clientHeight / 2));
    flashAt(panel, band);
  }

  document.querySelectorAll('.page-section').forEach(function(sec){
    var md = sec.querySelector('.markdown-content');
    if (!md) return;
    md.addEventListener('click', function(e){
      if (e.target.closest('a')) return;               // keep links clickable
      var selLen = window.getSelection ? String(window.getSelection()).length : 0;
      if (selLen > 0) return;                          // don't fire on text-select
      syncImageToClick(sec, e);
    });
  });
})();'''


# ==============================================================================
# Render-source resolution
# ------------------------------------------------------------------------------
# The pipeline may hand us MinerU's image-only PDF, but the ORIGINAL input PDF
# usually has a text layer — rendering that instead makes the left panel both
# sharper and self-anchoring (PyMuPDF text blocks work, no middle.json needed).
# Resolution order: explicit --source-pdf > given PDF if it has text > a
# discovered sibling original (text layer + same page count) > given PDF.
# ==============================================================================

def _open_pdf_meta(p: Path):
    """Return (page_count, has_text) or None if unopenable."""
    import fitz
    try:
        d = fitz.open(str(p))
    except Exception:
        return None
    try:
        n = d.page_count
        has_text = any(d[i].get_text().strip()
                       for i in range(min(3, n)))
        return n, has_text
    finally:
        d.close()


_VIS_PDF_RE = re.compile(r"(_layout|_span|_spans|_model)\.pdf$", re.IGNORECASE)


def find_original_pdf(given: Path, page_count: int) -> Path | None:
    """Search near the given PDF for the original (text-layer) document."""
    cands: list[Path] = []
    seen = {given.resolve()}
    for root in (given.parent, given.parent.parent):
        if not root or not root.exists():
            continue
        for pat in ("*.pdf", "*/*.pdf"):
            for c in root.glob(pat):
                r = c.resolve()
                if r in seen or _VIS_PDF_RE.search(c.name):
                    continue
                seen.add(r); cands.append(c)
    best, best_score = None, 0.0
    for c in cands[:40]:                       # bound the probing work
        meta = _open_pdf_meta(c)
        if not meta:
            continue
        n, has_text = meta
        if not has_text or n != page_count:    # must be同一文档且有文字层
            continue
        s = 10.0
        if "origin" in c.stem.lower():
            s += 3.0
        s += SequenceMatcher(None, c.stem.lower(), given.stem.lower()).ratio() * 5
        if s > best_score:
            best_score, best = s, c
    return best


def resolve_render_pdf(given: Path, source_pdf=None) -> Path:
    if source_pdf:
        sp = Path(source_pdf)
        if sp.exists():
            print(f"[compare] left panel source (explicit): {sp}")
            return sp
        print(f"[compare] ⚠️  --source-pdf not found: {sp} — using given PDF")
        return given
    meta = _open_pdf_meta(given)
    if meta is None:
        return given
    n, has_text = meta
    if has_text:
        return given
    alt = find_original_pdf(given, n)
    if alt:
        print(f"[compare] given PDF has no text layer (image-only); "
              f"switching left panel to original: {alt}")
        return alt
    print("[compare] given PDF has no text layer and no original found nearby "
          "— pass the original via --source-pdf for precise anchoring")
    return given


# ==============================================================================
# Document assembly
# ==============================================================================

def _esc(t: str) -> str:
    return html.escape(t, quote=False)


def _esc_attr(t: str) -> str:
    return html.escape(str(t), quote=True)


_TAG_RE = re.compile(r"<[^>]+>")


def build_block_panels(sections: list[dict], pdf_path: Path, md_path: Path,
                       anchors_path=None) -> None:
    """Render each section block-by-block, tagging blocks with PDF positions.
    Tables get data-yf0/data-yf1 (full vertical extent -> JS computes the
    clicked cell's row band inside it); text gets data-yf (block center)."""
    import fitz
    engine, src = load_engine_anchors(md_path, pdf_path.stem, anchors_path)
    if engine:
        print(f"[compare] anchors: engine coordinates <- {src}")
    else:
        print("[compare] anchors: no engine JSON found "
              f"({'unreadable: ' + str(src) if src else 'searched next to MD'})"
              " — falling back to PDF text layer")
    doc = fitz.open(str(pdf_path))
    try:
        n_blk = n_anc = n_tbl = n_tbl_anc = 0
        for sec in sections:
            blocks = list(iter_md_blocks(sec["md_inlined"]))
            md_meta = [{"norm": _norm_text(
                            _TAG_RE.sub(" ", _DATAURI_RE.sub("", b))),
                        "table": _is_md_table(b)} for b in blocks]
            lo = sec["start"] if sec["marker"] else 1
            hi = sec["end"] if sec["marker"] else doc.page_count
            if engine:
                anchors = [a for a in engine if lo <= a["page"] <= hi]
            else:
                anchors = pdf_block_anchors(doc, lo, hi)
            mapped = align_section_blocks(md_meta, anchors)

            parts: list[str] = []
            for b, meta, m in zip(blocks, md_meta, mapped):
                frag = md_to_html_fragment(b)
                if not frag.strip():
                    continue
                n_blk += 1
                if meta["table"]:
                    n_tbl += 1
                if m and meta["table"]:
                    n_anc += 1; n_tbl_anc += 1
                    parts.append(
                        f'<div class="md-block md-table" data-page="{m["page"]}" '
                        f'data-yf0="{m["yf0"]:.4f}" data-yf1="{m["yf1"]:.4f}">'
                        f'{frag}</div>')
                elif m:
                    n_anc += 1
                    yf = (m["yf0"] + m["yf1"]) / 2.0
                    attrs = (f' data-page="{m["page"]}" data-yf="{yf:.4f}"'
                             f' data-yf0="{m["yf0"]:.4f}"'
                             f' data-yf1="{m["yf1"]:.4f}"')
                    lines = m.get("lines")
                    if lines:
                        # data-lines: cumulative norm offset + line bbox;
                        # data-ltext: the norm text itself (word chars only,
                        # so ';' is a safe separator) -> lets the JS locate a
                        # clicked sentence by live text search, not offsets
                        cum, lparts, tparts = 0, [], []
                        for ln in lines:
                            lparts.append(
                                f"{cum},{ln['yf0']:.4f},{ln['yf1']:.4f}")
                            tparts.append(ln["norm"])
                            cum += len(ln["norm"])
                        attrs += f' data-lines="{";".join(lparts)}"'
                        attrs += f' data-ltext="{_esc_attr(";".join(tparts))}"'
                    parts.append(f'<div class="md-block"{attrs}>{frag}</div>')
                else:
                    parts.append(f'<div class="md-block">{frag}</div>')
            sec["panel_html"] = "\n".join(parts) or (
                '<div class="image-missing">No recognized content in this '
                'section</div>')
        print(f"[compare] anchored {n_anc}/{n_blk} blocks "
              f"(tables {n_tbl_anc}/{n_tbl})")
        if n_blk and n_anc == 0:
            print("[compare] ⚠️  NOTHING anchored — clicks will use the rough "
                  "proportional fallback. If the PDF is scanned, pass the "
                  "MinerU output explicitly:  --anchors path\\to\\*_middle.json")
    finally:
        doc.close()


def _section_html(idx: int, sec: dict, page_urls: list[str]) -> str:
    start, end = sec["start"], sec["end"]
    label = f"Pages {start} – {end}" if sec["marker"] else "Full document"

    imgs = []
    for pno in range(start, end + 1):
        if 1 <= pno <= len(page_urls):
            imgs.append(
                f'<figure class="page-unit"><div class="page-no">P{pno}</div>'
                f'<img src="{page_urls[pno - 1]}" alt="page {pno}" '
                f'data-page="{pno}" loading="lazy" decoding="async"/></figure>'
            )
    img_html = "".join(imgs) or '<div class="image-missing">No page images</div>'

    rendered = sec.get("panel_html") or md_to_html_fragment(sec["md_inlined"])
    if not rendered.strip():
        rendered = '<div class="image-missing">No recognized content in this section</div>'

    raw = sec["md"].replace("</textarea>", "&lt;/textarea&gt;")

    return f'''<section class="page-section" data-marker="{1 if sec["marker"] else 0}"
  data-pstart="{start}" data-pend="{end}">
  <div class="page-header">
    <span class="page-indicator" aria-hidden="true"></span>
    <span>{_esc(label)}</span>
    <span class="modified-tag">Modified</span>
    <span class="sec-toolbar">
      <button class="btn-sec" data-act="toggle" type="button">Edit</button>
    </span>
  </div>
  <div class="page-content">
    <div class="image-col">
      <div class="image-panel"><div class="image-track">{img_html}</div></div>
    </div>
    <div class="markdown-panel">
      <div class="markdown-content">{rendered}</div>
      <textarea class="md-source" spellcheck="false">{_esc(raw)}</textarea>
    </div>
  </div>
</section>'''


def build_compare_html(
    pdf_path: str | Path,
    md_path: str | Path,
    output_html: str | Path,
    *,
    dpi: int = 96,
    quality: int = 70,
    title: str = "",
    anchors: str | Path | None = None,
    source_pdf: str | Path | None = None,
) -> Path:
    """Generate the comparison HTML; returns the output path."""
    pdf_path = Path(pdf_path)
    md_path = Path(md_path)
    out_path = Path(output_html)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    render_pdf = resolve_render_pdf(pdf_path, source_pdf)

    md_text = md_path.read_text(encoding="utf-8-sig", errors="replace")

    print(f"[compare] rendering PDF pages (dpi={dpi}): {render_pdf.name}")
    page_urls = render_pdf_pages(render_pdf, dpi=dpi, quality=quality)
    total = len(page_urls)
    print(f"[compare] {total} pages")

    sections = split_md_by_slices(md_text, total)
    print(f"[compare] MD split into {len(sections)} section(s)")

    for sec in sections:
        sec["md_inlined"] = inline_md_images(sec["md"], md_path)

    print("[compare] building block-level anchors")
    build_block_panels(sections, render_pdf, md_path, anchors)

    sec_html = "\n".join(
        _section_html(i, s, page_urls) for i, s in enumerate(sections)
    )

    title = title or f"Recognition Comparison · {pdf_path.stem}"
    dl_name = f"{md_path.stem}_proofread.md"
    lockup = sunocr_lockup_html(h=56.0, lockup_gap=18.0)

    doc = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<meta name="description" content="OCR Extraction Comparison Report - SunOCR">
<meta name="color-scheme" content="light">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='0'%3E%3Cstop offset='0%25' stop-color='%231e6ee8' stop-opacity='0.85'/%3E%3Cstop offset='100%25' stop-color='%2300c2b2'/%3E%3C/linearGradient%3E%3C/defs%3E%3Cpath d='M 10 40 A 22 22 0 0 1 54 40 A 3.6 3.6 0 0 0 54 32.8 C 44 16, 20 13.5, 10 40 Z' fill='url(%23g)'/%3E%3Ccircle cx='32' cy='38' r='9' fill='%23f5a524'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Allura&display=swap" rel="stylesheet">
<style>
{_CSS}
</style>
</head>
<body>
<div class="wrap">
  <header aria-label="Report header">
    <div class="header-content">
      <span class="brand" aria-label="SunOCR">
        <span class="logo">{lockup}</span>
      </span>
      <div class="subtitle-badge"><span class="text">{_esc(title)}</span></div>
      <div class="toolbar-global">
        <button class="btn" id="btn-all-edit" type="button">Edit all</button>
        <button class="btn btn-primary" id="btn-download-md" type="button"
                data-filename="{_esc_attr(dl_name)}">Download proofread MD</button>
        <span class="edit-hint">Click "Edit" on any section to fix the recognized
          text, then export the merged result here · Click any sentence on the
          right to locate &amp; flash it on the original page; the left pane
          scrolls on its own — double-click it to jump back to top</span>
      </div>
    </div>
  </header>
  <main role="main">
{sec_html}
  </main>
  <footer>
    <div class="foot">
      &copy; <span id="year"></span> <strong>SunOCR</strong> · Universal Report
      Extract · Comparison &amp; Proofreading Report
    </div>
  </footer>
</div>
<script>
{_JS}
</script>
</body>
</html>'''

    out_path.write_text(doc, encoding="utf-8")
    size_mb = out_path.stat().st_size / 1048576
    print(f"[compare] ✅ report: {out_path} ({size_mb:.1f} MB, "
          f"{len(sections)} sections / {total} pages)")
    return out_path


# ==============================================================================
# CLI
# ==============================================================================

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=("Generate a side-by-side HTML report comparing the original "
                     "PDF with the recognized Markdown — proofread in the browser "
                     "and export the corrected MD."))
    ap.add_argument("pdf", help="original PDF path")
    ap.add_argument("md", help="recognized MD path (*_extracted.md)")
    ap.add_argument("output", help="output HTML path")
    ap.add_argument("--dpi", type=int, default=96, help="page render DPI (default 96)")
    ap.add_argument("--quality", type=int, default=70, help="JPEG quality (default 70)")
    ap.add_argument("--title", default="", help="page title")
    ap.add_argument("--anchors", default=None,
                    help="MinerU middle.json / content_list.json for precise "
                         "click positioning (auto-discovered next to the MD "
                         "if omitted)")
    ap.add_argument("--source-pdf", default=None,
                    help="original (text-layer) PDF to render on the left; "
                         "auto-discovered if the given PDF is image-only")
    args = ap.parse_args()

    build_compare_html(args.pdf, args.md, args.output,
                       dpi=args.dpi, quality=args.quality, title=args.title,
                       anchors=args.anchors, source_pdf=args.source_pdf)
