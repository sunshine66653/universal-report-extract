"""
Configurable chunker
─────────────────────────────────────────────────────────────────
Strategies (profile.chunking.strategy):
    cn_slice  use "# --- PDF 物理切片：第 X - Y 页 ---" markers + Markdown headings
    heading   Markdown headings only (English reports without slice markers)
    fixed     plain character windows (fallback when no headings exist)
    auto      detect: slice markers present -> cn_slice, otherwise heading

Chunk structure (kept compatible with the original engine):
    { chunk_id, page_start, page_end, section_path: [..], text }

NOTE: the slice marker text is a Chinese wire format written by engine.mineru —
the regexes below must keep matching it. Do not translate the patterns.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


def _split_into_chunks(
    md_text: str,
    max_chars: int,
    slice_pat: Optional[re.Pattern],
    heading_levels: int,
) -> List[Dict[str, Any]]:
    text = md_text or ""
    lines = text.splitlines()
    if heading_levels and heading_levels >= 1:
        heading_pat = re.compile(r"^(#{1," + str(heading_levels) + r"})\s+(.*)$")
    else:
        heading_pat = re.compile(r"(?!x)x")  # never matches (fixed strategy)

    chunks: List[Dict[str, Any]] = []
    chunk_id = 1
    cur_lines: List[str] = []
    cur_section_path: List[str] = []
    section_stack: List[str] = []
    cur_page_start: Optional[int] = None
    cur_page_end: Optional[int] = None

    def flush():
        nonlocal chunk_id, cur_lines
        body = "\n".join(x for x in cur_lines if x.strip()).strip()
        if body:
            chunks.append({
                "chunk_id": f"chunk_{chunk_id:04d}",
                "page_start": cur_page_start,
                "page_end": cur_page_end,
                "section_path": cur_section_path[:] if cur_section_path else [],
                "text": body,
            })
            chunk_id += 1
        cur_lines = []

    for line in lines:
        raw = line.rstrip("\n")
        stripped = raw.strip()

        # 1) physical-slice page marker
        if slice_pat is not None:
            m_slice = slice_pat.match(stripped)
            if m_slice:
                if cur_lines:
                    flush()
                cur_page_start = int(m_slice.group(1))
                cur_page_end = int(m_slice.group(2))
                continue

        # 2) Markdown heading -> update section path
        m_h = heading_pat.match(stripped)
        if m_h:
            level = len(m_h.group(1))
            title = m_h.group(2).strip()
            if cur_lines:
                flush()
            while len(section_stack) >= level:
                section_stack.pop()
            section_stack.append(title)
            cur_section_path = section_stack[:]
            cur_lines.append(raw)
            continue

        if not stripped:
            continue

        # 3) character window
        projected = "\n".join(cur_lines + [raw])
        if len(projected) > max_chars and cur_lines:
            flush()
            cur_section_path = section_stack[:]

        cur_lines.append(raw)

    flush()
    return chunks


# Universal slice-marker fallback: matches "# --- ... 第 X - Y 页 ... ---" and
# "# --- ... Pages X - Y ... ---" — same markers written by the mineru merger.
_UNIVERSAL_SLICE = re.compile(
    r"^\s*#\s*---.*?(?:第|Pages?)\s*(\d+)\s*-\s*(\d+)\s*(?:页)?.*?---\s*$",
    re.M | re.I,
)


def build_chunks(md_text: str, chunking_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    strategy = chunking_cfg.get("strategy", "auto")
    max_chars = int(chunking_cfg.get("max_chars", 4500))
    heading_levels = int(chunking_cfg.get("heading_levels", 6))
    marker_re = chunking_cfg.get("slice_marker_regex")
    cfg_pat = re.compile(marker_re, re.M) if marker_re else None

    # slice marker: prefer the profile-configured regex, with the universal
    # fallback always active too
    def _make_slice_pat():
        pats = [p for p in (cfg_pat, _UNIVERSAL_SLICE) if p]

        class _Multi:
            def match(self, s):
                for p in pats:
                    m = p.match(s)
                    if m:
                        return m
                return None

            def search(self, s):
                for p in pats:
                    m = p.search(s)
                    if m:
                        return m
                return None
        return _Multi()

    slice_pat = _make_slice_pat()

    if strategy == "auto":
        has_marker = bool(slice_pat.search(md_text or ""))
        strategy = "cn_slice" if has_marker else "heading"

    if strategy == "fixed":
        # plain windows: no heading parsing, but page markers still recognized
        return _split_into_chunks(md_text, max_chars, slice_pat, 0)

    # cn_slice / heading: both recognize page markers (so markers don't pollute
    # section_path); the only difference is whether headings are parsed
    levels = 0 if strategy == "fixed" else heading_levels
    return _split_into_chunks(md_text, max_chars, slice_pat, levels)


def format_chunk_with_breadcrumb(c: Dict[str, Any]) -> str:
    """Context block with the full heading breadcrumb (lets the model tell
    consolidated vs parent-company statement levels apart)."""
    section_path = c.get("section_path", []) or []
    breadcrumb_lines = []
    for depth, title in enumerate(section_path, start=1):
        h = "#" * min(depth, 6)
        breadcrumb_lines.append(f"{h} {title}")
    breadcrumb = "\n".join(breadcrumb_lines)
    meta = (f"[{c.get('chunk_id')}|pages:{c.get('page_start')}-{c.get('page_end')}"
            f"|section:{' > '.join(section_path)}]")
    body = c.get("text", "")
    if breadcrumb:
        return f"{meta}\n{breadcrumb}\n---\n{body}"
    return f"{meta}\n{body}"
