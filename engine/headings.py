"""
Heading-level post-processing
─────────────────────────────────────────────────────────────────
Markdown heading quality differs by source:
  - local Docling (A-share): PDF2MD already injects #/##/### per Chinese rules
  - MinerU: almost every heading comes back as level-1 # with no hierarchy

This module re-levels heading lines by language (zh / en) so downstream
chunking yields a meaningful section_path. The Chinese rules mirror the
original A-share logic; the English rules target HK annual reports.

Entry point: relevel_markdown(md_text, language) -> processed md_text

NOTE: the Chinese regex patterns below match real Chinese heading numbering
(第X节 / 一、 / （一） / 1.1 ...). They are functional — do not translate.
"""
from __future__ import annotations

import re
from typing import List

# ── Chinese (A-share) heading patterns ─────────────────────────────────
_CN_L1 = [
    re.compile(r"^第[一二三四五六七八九十百]+节"),         # 第X节 (Section X)
    re.compile(r"^[一二三四五六七八九十百]+[、\s]"),        # 七、... (numbered L1)
]
_CN_L2 = re.compile(r"^[（(][一二三四五六七八九十]+[）)]")   # （一）
_CN_L3 = re.compile(r"^\d+[\s、\.]")                          # 4、 / 4.
_CN_L4 = [
    re.compile(r"^\d+\.\d+"),                                 # 1.1
    re.compile(r"^[（(]\d+[）)]"),                            # (1)
]

# ── English (H-share) heading patterns ─────────────────────────────────
# Level-1: standard annual-report sections (case-insensitive,
# "(Continued)" suffixes tolerated)
_EN_L1_SECTIONS = [
    r"important notice", r"contents?", r"chairman'?s statement",
    r"company information", r"financial highlights?", r"five[- ]year (?:financial )?summary",
    r"management discussion and analysis", r"business review",
    r"directors'? report", r"report of the directors?",
    r"report of the (?:board of )?supervisors?",
    r"corporate governance(?: report)?",
    r"environmental,? social and governance(?: report)?", r"esg report",
    r"directors,? supervisors and senior management",
    r"profiles? of directors", r"changes in share capital",
    r"substantial shareholders?", r"independent auditor'?s report",
    r"report on the audit",
    r"consolidated statement of profit or loss",
    r"consolidated income statement",
    r"consolidated statement of comprehensive income",
    r"consolidated statement of financial position",
    r"consolidated balance sheet",
    r"consolidated statement of changes in equity",
    r"consolidated statement of cash flows?",
    r"notes? to the (?:consolidated )?financial statements",
    r"definitions?", r"glossary",
]
_EN_L1_RE = re.compile(
    r"^\s*(?:section\s+[ivxlcdm]+\s*[:.\-]?\s*)?(?:" +
    "|".join(_EN_L1_SECTIONS) + r")\b", re.IGNORECASE,
)
# "SECTION I" / "PART 3" style
_EN_L1_PART = re.compile(r"^(?:section|part|chapter)\s+(?:[ivxlcdm]+|\d+)\b", re.IGNORECASE)

# Level-2: single-level numbering "1.", "1 ", "I.", "A."
_EN_L2 = re.compile(r"^(?:\d{1,2}|[IVXivx]{1,4}|[A-H])[\.\)]\s+\S")
# Level-3: second-level numbering "1.1", "(a)", "(1)", "(i)"
_EN_L3 = [
    re.compile(r"^\d{1,2}\.\d{1,2}(?!\d)"),
    re.compile(r"^[（(][a-z0-9]{1,3}[）)]\s*\S", re.IGNORECASE),
]
# Level-4: third-level numbering "1.1.1", "Note 5", "(i)(a)"
_EN_L4 = [
    re.compile(r"^\d{1,2}\.\d{1,2}\.\d{1,2}"),
    re.compile(r"^note\s+\d+", re.IGNORECASE),
]


def _strip_heading(line: str):
    """Returns (is_heading, plain text without #/bold, original line)."""
    m = re.match(r"^(#{1,6})\s*(.*)$", line)
    if not m:
        return False, "", line
    text = m.group(2).strip()
    # strip markdown bold
    text = re.sub(r"^\*\*(.*)\*\*$", r"\1", text).strip()
    return True, text, line


def _cn_level(text: str) -> int:
    for pat in _CN_L1:
        if pat.match(text):
            return 1
    if _CN_L2.match(text):
        return 2
    for pat in _CN_L4:
        if pat.match(text):
            return 4
    if _CN_L3.match(text):
        return 3
    return 0


def _en_level(text: str) -> int:
    if _EN_L1_RE.match(text) or _EN_L1_PART.match(text):
        return 1
    for pat in _EN_L4:
        if pat.match(text):
            return 4
    for pat in _EN_L3:
        if pat.match(text):
            return 3
    if _EN_L2.match(text):
        return 2
    return 0


def relevel_markdown(md_text: str, language: str = "zh") -> str:
    """
    Re-level all heading lines by language.
    - headings matching a numbering rule -> that rule's level (# .. ####)
    - headings matching nothing -> demoted to ## (stay headings, but never
      compete with level-1 sections)
    - physical slice markers "# --- ... ---" pass through untouched
    """
    out: List[str] = []
    slice_marker = re.compile(r"^\s*#\s*---.*---\s*$")

    for line in md_text.splitlines():
        if slice_marker.match(line):
            out.append(line)
            continue
        is_h, text, raw = _strip_heading(line)
        if not is_h or not text:
            out.append(raw)
            continue

        lvl = _en_level(text) if language == "en" else _cn_level(text)
        if lvl == 0:
            # heading without a recognizable number: keep as ## so it does not
            # pollute level-1 sections
            lvl = 2
        prefix = "#" * lvl
        out.append(f"{prefix} {text}")

    return "\n".join(out)
