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
.page-section{position:relative;margin-bottom:24px;overflow:hidden;border-radius:var(--radius-xl);
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
  grid-template-columns:minmax(320px,42%) minmax(0,1fr);gap:18px;padding:18px;align-items:start}
.image-panel{position:sticky;top:104px;align-self:start;overflow-y:auto;
  max-height:calc(100vh - 130px);border-radius:var(--radius-lg);
  border:1px solid var(--border);
  background:linear-gradient(180deg,rgba(255,255,255,0.96),rgba(244,250,255,0.90));
  box-shadow:inset 0 1px 0 rgba(255,255,255,0.72),var(--shadow-soft)}
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
  .image-panel{position:static;max-height:480px;max-width:860px;margin:0 auto}
}
@media print{
  body{background:#fff}
  header{position:static;background:#fff;border-bottom:1px solid #ddd}
  .page-section{box-shadow:none;border:1px solid #d8e1ea;background:#fff;page-break-inside:avoid}
  .page-content{grid-template-columns:1fr}
  .image-panel{position:static;max-height:400px}
  .btn,.btn-sec,.toolbar-global{display:none!important}
}'''


# NOTE: the slice-marker string inside gatherMd() is the Chinese wire format —
# the exported MD must round-trip through the engine's chunker. Keep verbatim.
_JS = r'''(function(){
  document.getElementById('year').textContent = new Date().getFullYear();

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
})();'''


# ==============================================================================
# Document assembly
# ==============================================================================

def _esc(t: str) -> str:
    return html.escape(t, quote=False)


def _esc_attr(t: str) -> str:
    return html.escape(str(t), quote=True)


def _section_html(idx: int, sec: dict, page_urls: list[str]) -> str:
    start, end = sec["start"], sec["end"]
    label = f"Pages {start} – {end}" if sec["marker"] else "Full document"

    imgs = []
    for pno in range(start, end + 1):
        if 1 <= pno <= len(page_urls):
            imgs.append(
                f'<div class="page-no">P{pno}</div>'
                f'<img src="{page_urls[pno - 1]}" alt="page {pno}" '
                f'loading="lazy" decoding="async"/>'
            )
    img_html = "".join(imgs) or '<div class="image-missing">No page images</div>'

    rendered = md_to_html_fragment(sec["md_inlined"])
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
    <div class="image-panel">{img_html}</div>
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
) -> Path:
    """Generate the comparison HTML; returns the output path."""
    pdf_path = Path(pdf_path)
    md_path = Path(md_path)
    out_path = Path(output_html)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    md_text = md_path.read_text(encoding="utf-8-sig", errors="replace")

    print(f"[compare] rendering PDF pages (dpi={dpi}): {pdf_path.name}")
    page_urls = render_pdf_pages(pdf_path, dpi=dpi, quality=quality)
    total = len(page_urls)
    print(f"[compare] {total} pages")

    sections = split_md_by_slices(md_text, total)
    print(f"[compare] MD split into {len(sections)} section(s)")

    for sec in sections:
        sec["md_inlined"] = inline_md_images(sec["md"], md_path)

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
          text, then export the merged result here</span>
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
    ap.add_argument("md", help="recognized MD path (*_提取结果.md)")
    ap.add_argument("output", help="output HTML path")
    ap.add_argument("--dpi", type=int, default=96, help="page render DPI (default 96)")
    ap.add_argument("--quality", type=int, default=70, help="JPEG quality (default 70)")
    ap.add_argument("--title", default="", help="page title")
    args = ap.parse_args()

    build_compare_html(args.pdf, args.md, args.output,
                       dpi=args.dpi, quality=args.quality, title=args.title)
