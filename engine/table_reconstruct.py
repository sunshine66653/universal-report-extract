"""
Header-anchored table reconstruction for digital-born PDFs
─────────────────────────────────────────────────────────────────
Digital PDFs carry every glyph's exact (x, y). Image-based table models
(Docling TableFormer, PaddleOCR SLANeXt) throw that away and *guess* the
grid from a rendered picture — so they merge close columns, jumble wrapped
multi-line cells, and (for image OCR) misread long numbers.

This reconstructs a table deterministically from coordinates:
  1. the table's HEADER ROW is auto-detected (top physical line of the
     region); its label groups define the columns (NOT whitespace guessing,
     so "Offshore Renminbi" stays one cell);
  2. body words are assigned to a column by x, grouped into physical lines
     by y;
  3. wrapped lines merge into one logical row (a line whose anchor column is
     empty is a continuation of the row above).

General, template-free, PyMuPDF-only, no model/GPU. When the header is
ambiguous (multi-line header, no detectable columns) it returns None so the
caller can fall back to its existing table output — never a regression.
"""
from __future__ import annotations

import bisect
import re
from typing import List, Optional, Sequence, Tuple

_NUM_RE = re.compile(r"\(?\d[\d,]*\.\d{2}\)?|\(?\d[\d,]{3,}\)?")
# looser: any-precision decimal (e.g. an average price 126.097245) or a 4+
# digit integer. Used only to decide table-band membership, so trailing
# subtotal/average rows are kept in the table while digit-free prose
# (footnotes, disclaimers) stays out.
_BAND_NUM = re.compile(r"\(?\d[\d,]*\.\d+\)?|\(?\d[\d,]{3,}\)?")

# a PyMuPDF "word" tuple: (x0, y0, x1, y1, text, block, line, word_no)
Word = Tuple[float, float, float, float, str, int, int, int]


def _cluster_lines(words: Sequence[Word], y_tol: float) -> List[List[Word]]:
    """Group words into physical lines by vertical proximity."""
    lines: List[List[Word]] = []
    cur: List[Word] = []
    cy: Optional[float] = None
    for w in sorted(words, key=lambda w: (round(w[1], 1), w[0])):
        if cy is None or abs(w[1] - cy) <= y_tol:
            cur.append(w)
            cy = w[1] if cy is None else cy
        else:
            lines.append(cur)
            cur, cy = [w], w[1]
    if cur:
        lines.append(cur)
    return lines


def _median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _looks_like_header(line: Sequence[Word]) -> bool:
    """A header row has multiple multi-character labels. Reject prose lines
    and garbled/watermark lines that shatter into single characters."""
    toks = [w[4] for w in line if w[4].strip()]
    if len(toks) < 2:
        return False
    singles = sum(1 for t in toks if len(t) == 1)
    if singles > 0.4 * len(toks):          # mostly 1-char fragments -> garbled
        return False
    return len(_header_groups(line)) >= 2


def _header_groups(header: Sequence[Word]) -> List[Tuple[float, float, str]]:
    """Cluster header-row words into column labels by x-gap. A gap larger
    than the (adaptive) column-gap threshold starts a new column; smaller
    gaps are spaces inside one multi-word label."""
    hw = sorted(header, key=lambda w: w[0])
    if not hw:
        return []
    heights = [w[3] - w[1] for w in hw if w[3] > w[1]]
    h = _median(heights) or 7.0
    # intra-label spaces are well under one line-height; columns are wider
    gap_min = max(6.0, 0.9 * h)
    groups: List[List[Word]] = [[hw[0]]]
    for w in hw[1:]:
        if w[0] - groups[-1][-1][2] > gap_min:
            groups.append([w])
        else:
            groups[-1].append(w)
    return [(g[0][0], g[-1][2], " ".join(x[4] for x in g).strip()) for g in groups]


def _columns(header: Sequence[Word], body: Sequence[Word]
             ) -> Optional[Tuple[List[str], List[float]]]:
    """Return (labels, boundaries). boundaries has len(labels)-1 x-cuts.
    Adds an unlabeled leading column when body text consistently sits left
    of the first header label (the common blank 'item' column)."""
    groups = _header_groups(header)
    if len(groups) < 2:
        return None
    labels = [g[2] for g in groups]
    edges = [(g[0], g[1]) for g in groups]   # (x0, x1) per label

    # unlabeled leading column: body words clearly left of the first label,
    # present on a meaningful share of rows -> prepend an empty-label column
    first_x0 = edges[0][0]
    left = [w for w in body if w[2] < first_x0 - 4]
    if left:
        body_lines = len({round(w[1]) for w in body}) or 1
        left_lines = len({round(w[1]) for w in left})
        if left_lines >= max(2, 0.3 * body_lines):
            lead_x1 = max(w[2] for w in left)
            labels = [""] + labels
            edges = [(min(w[0] for w in left), lead_x1)] + edges

    boundaries = [(edges[i][1] + edges[i + 1][0]) / 2.0
                  for i in range(len(edges) - 1)]
    return labels, boundaries


def _evaluate_header(lines: List[List[Word]], i: int
                     ) -> Optional[Tuple[int, int, Tuple[List[str], List[float]]]]:
    """Score candidate header at line i by how strongly the lines below it
    form an aligned table: (max rows with a number in one column, rows with
    >=2 filled cells). Returns (numeric_score, multicell, columns) or None.
    This lets reconstruct pick the REAL table header over a page banner
    (which has few/zero consistently-numeric rows beneath it)."""
    header = lines[i]
    body = [w for ln in lines[i + 1:] for w in ln]
    cols = _columns(header, body)
    if cols is None:
        return None
    labels, boundaries = cols
    ncol = len(labels)

    def col_of(x: float) -> int:
        return bisect.bisect_right(boundaries, x)

    numeric = [0] * ncol
    multicell = 0
    for ln in lines[i + 1:]:
        cells: List[List[str]] = [[] for _ in range(ncol)]
        for w in ln:
            cells[col_of(w[0])].append(w[4])
        texts = [" ".join(c).strip() for c in cells]
        if sum(1 for t in texts if t) >= 2:
            multicell += 1
        for ci, t in enumerate(texts):
            if t and _NUM_RE.search(t):
                numeric[ci] += 1
    return (max(numeric) if numeric else 0, multicell, cols)


def _locate_table(lines: List[List[Word]]) -> Optional[Tuple[int, int]]:
    """Anchor the table on its value column: financial tables have a column
    of numbers aligned at a consistent x. Find that x (the densest cluster of
    numeric-token left edges), take the rows carrying a number there as the
    data band, and the nearest header-like line just above as the header.
    Returns (header_index, body_end_exclusive) or None. Robust against page
    banners — they have no numbers at the value-column x."""
    from collections import Counter
    xs: List[float] = []
    for ln in lines:
        for w in ln:
            if _NUM_RE.fullmatch(w[4]):
                xs.append(round(w[0] / 4) * 4)
    if not xs:
        return None
    modal_x, cnt = Counter(xs).most_common(1)[0]
    if cnt < 3:
        return None
    data_idx = [j for j, ln in enumerate(lines)
                if any(_NUM_RE.fullmatch(w[4]) and abs(w[0] - modal_x) <= 6
                       for w in ln)]
    if len(data_idx) < 3:
        return None
    first, last = data_idx[0], data_idx[-1]
    # extend past the last money line through trailing subtotal/total rows:
    # they carry a non-strict number (e.g. a multi-decimal average price or a
    # short total count) so the strict money pattern stops short. Tolerate a
    # small gap; stop at digit-free prose (footnotes/disclaimers).
    k, blanks = last + 1, 0
    while k < len(lines):
        if any(_BAND_NUM.fullmatch(w[4]) for w in lines[k]):
            last, blanks = k, 0
        else:
            blanks += 1
            if blanks > 2:
                break
        k += 1
    h = next((k for k in range(first - 1, max(-1, first - 6), -1)
              if _looks_like_header(lines[k])), None)
    if h is None:
        h = first - 1
    if h < 0:
        return None
    return h, last + 1


def _build_table(lines: List[List[Word]], h: int, body_end: int,
                 labels: List[str], assign, ncol: int, anchor_col: int = 0):
    """Build one table from header line h over body lines [h+1, body_end),
    using a pluggable `assign(line) -> [cell text per column]` (header-gap or
    data-driven). Folds a multi-line sub-header into the labels, merges wrapped
    rows, gates on a numeric column. Returns (labels, rows, (y0, y1)) or None."""
    labels = list(labels)
    body_lines = lines[h + 1:body_end]
    start = 0
    while start < len(body_lines):
        text = assign(body_lines[start])
        if not any(text):
            start += 1
            continue
        if text[anchor_col] or any(ch.isdigit() for c in text for ch in c):
            break
        labels = [(labels[i] + " " + text[i]).strip() for i in range(ncol)]
        start += 1
    body_lines = body_lines[start:]

    assigned = [assign(ln) for ln in body_lines]
    # A physical line is a wrapped CONTINUATION of the row above when it
    # carries no number AND either (a) its anchor column is empty — pure
    # spilled-over text like "TRANSFER VS EQUITY ACCOUNT"; or (b) its anchor
    # text is a label fragment that does NOT begin a new item (no "1、" /
    # "(一)" / "一、" marker) — the tail of a wrapped row label like
    # "其他权益工具持有 / 者投入资本". A line that carries a number, or whose
    # label starts with an item marker, is always its own row (so a totals
    # row "HKD Total: 1,234.00" and a real "1、所有者投入资本" row are kept).
    rows: List[List[str]] = []
    cur: Optional[List[str]] = None
    for text in assigned:
        if not any(text):
            continue
        has_num = any(_NUM_RE.search(c) for c in text if c)
        a = text[anchor_col]
        label_wrap = bool(a) and not _ITEM_MARKER.match(a)
        cur_has_num = cur is not None and any(_NUM_RE.search(c) for c in cur if c)
        # continuation when: (no number AND col0 empty/non-marker label) — a
        # plain wrapped label/description; OR a non-marker label tail that
        # carries the row's DATA while the row above is a still-numberless
        # label head ("1、提取盈余公积" + "（附注七、41） 1,133,677,909.26 …").
        is_cont = cur is not None and (
            (not has_num and (not a or label_wrap))
            or (label_wrap and not cur_has_num))
        if is_cont:
            for i in range(ncol):
                if text[i]:
                    cur[i] = (cur[i] + " " + text[i]).strip()
        else:
            if cur:
                rows.append(cur)
            cur = list(text)
    if cur:
        rows.append(cur)
    if not rows:
        return None
    need = max(3, int(0.3 * len(rows)))
    numeric_per_col = [sum(1 for r in rows if _NUM_RE.search(r[c]))
                       for c in range(ncol)]
    if max(numeric_per_col, default=0) < need:
        return None
    y0 = min((w[1] for w in lines[h]), default=0.0)
    used = [w for ln in body_lines for w in ln]
    y1 = max((w[3] for w in used), default=y0)
    return labels, rows, (y0, y1)


def _is_num(tok: str) -> bool:
    return bool(_BAND_NUM.fullmatch(tok)) or bool(_NUM_RE.fullmatch(tok))


_DASH_RE = re.compile(r"[-—－–]+")

# a row-label that STARTS a new item: Chinese ordinals (一、 / （一） / 第N),
# arabic item numbers (1、 1. (1)), or any leading digit/letter+number. A line
# whose leading label does NOT match this — and carries no number — is treated
# as the wrapped tail of the previous row's label rather than a new row.
_ITEM_MARKER = re.compile(
    r"^\s*(?:[（(]?[一二三四五六七八九十百零]+[）)、.]"
    r"|第[一二三四五六七八九十\d]"
    r"|\d+\s*[、.)）]"
    r"|[（(]\d+[）)])")


def _is_cell_value(tok: str) -> bool:
    """A right-aligned cell marker: a number OR a dash placeholder. Dashes
    ("-" / "—") stand in for an empty numeric cell and are right-aligned at
    the column position, so counting them lets a mostly-empty column (e.g. a
    perpetual-bond column that is almost all dashes) still form a right-edge
    cluster instead of being merged into a neighbour."""
    return _is_num(tok) or bool(_DASH_RE.fullmatch(tok))


# a note reference / small count in its own right-aligned column: "44",
# "(1)", or the "主号(子号)" form "57(1)" / "58(1)". Stays distinct from a
# 4-digit year, comma'd date parts like "07,", and CJK note labels like
# "（附注七、41）" (those keep with the item text).
_NOTE_RE = re.compile(r"\(?\d{1,3}\)?(?:[（(]\d{1,3}[）)])?")


def _is_colmark(tok: str) -> bool:
    """Cell markers used for DATA-DRIVEN column detection: numbers, dashes, and
    short integers (note refs / counts). Broader than _is_cell_value so a note
    column ("附注 44 ...") forms its own right-edge cluster instead of being
    swallowed by the item-label column."""
    return _is_cell_value(tok) or bool(_NOTE_RE.fullmatch(tok))


def _cluster_1d(vals: List[float], tol: float) -> List[List[float]]:
    vals = sorted(vals)
    cl = [[vals[0]]]
    for v in vals[1:]:
        if v - cl[-1][-1] <= tol:
            cl[-1].append(v)
        else:
            cl.append([v])
    return cl


def _spec_header(lines, h, body_end):
    """Header-gap column spec: columns from the header label x-positions,
    assignment by each token's LEFT edge. Returns (labels, assign, ncol)."""
    header = lines[h]
    body_words = [w for ln in lines[h + 1:body_end] for w in ln]
    cols = _columns(header, body_words)
    if cols is None:
        return None
    labels, boundaries = cols
    ncol = len(labels)

    def assign(ln):
        cells = [[] for _ in range(ncol)]
        for w in sorted(ln, key=lambda w: w[0]):
            cells[bisect.bisect_right(boundaries, w[0])].append(w[4])
        return [" ".join(c).strip() for c in cells]

    return labels, assign, ncol


def horizontal_lines(page) -> List[Tuple[float, float, float]]:
    """Thin horizontal strokes/rects on the page as (y, x0, x1). These mark
    multi-level header group spans (e.g. a rule under "合并" / "归属于母公司
    股东权益" telling which columns the group label covers)."""
    out = []
    try:
        for d in page.get_drawings():
            for it in d.get("items", []):
                if it[0] == "l":
                    p0, p1 = it[1], it[2]
                    if abs(p0.y - p1.y) < 1 and abs(p1.x - p0.x) > 4:
                        out.append((p0.y, min(p0.x, p1.x), max(p0.x, p1.x)))
                elif it[0] == "re":
                    r = it[1]
                    if r.height < 3 and r.width > 4:
                        out.append((r.y0, r.x0, r.x1))
    except Exception:
        pass
    return out


def _spec_data_led(lines, h, body_end, hlines=None):
    """Data-driven column spec for dense numeric tables: columns from the
    RIGHT edges of numeric cells across the data rows (financial numbers are
    right-aligned, so each column's right edge is a tight x-cluster). Numeric
    tokens are assigned by right edge — robust to tight, overlapping columns
    the header-gap method merges. Multi-row headers are assembled per column.
    Returns (labels, assign, ncol) or None when it does not apply."""
    body = lines[h + 1:body_end]
    rights, heights = [], []
    for ln in body:
        for w in ln:
            heights.append(w[3] - w[1])
            if _is_colmark(w[4]):
                rights.append(w[2])
    if sum(1 for ln in body for w in ln if _is_num(w[4])) < 6:
        return None
    hh = _median(heights) or 7.0
    tol = max(6.0, 1.2 * hh)
    nrows = max(1, len(body))
    R = sorted(_median(c) for c in _cluster_1d(rights, tol)
               if len(c) >= max(2, 0.12 * nrows))
    # data-driven only for genuinely DENSE numeric tables (>=3 value columns);
    # a 1-amount ledger that merely has a year in its date column makes a
    # spurious 2nd "column" — those go to the header-gap fallback, which
    # handles text-heavy tables correctly.
    if len(R) < 3:
        return None
    rb = [(R[i] + R[i + 1]) / 2 for i in range(len(R) - 1)]
    ncol = 1 + len(R)
    lead_max = R[0] - 2.0 * hh

    def _col_of(w):
        if _is_colmark(w[4]):
            return 1 + bisect.bisect(rb, w[2])        # number/dash/note by right edge
        if w[2] <= lead_max:
            return 0                                   # leading label column
        return 1 + bisect.bisect(rb, (w[0] + w[2]) / 2)

    def assign(ln):
        cells = [[] for _ in range(ncol)]
        for w in sorted(ln, key=lambda w: w[0]):
            cells[_col_of(w)].append(w[4])
        return [" ".join(c).strip() for c in cells]

    # observed x-range/centre per column (from the data), for header mapping
    lo = [1e9] * ncol
    hi = [-1e9] * ncol
    for ln in body:
        for w in ln:
            c = _col_of(w)
            lo[c] = min(lo[c], w[0])
            hi[c] = max(hi[c], w[2])
    centres = [(lo[c] + hi[c]) / 2 if hi[c] > 0 else (R[c - 1] if c else 0)
               for c in range(ncol)]

    def nearest(cx):
        return min(range(ncol), key=lambda i: abs(cx - centres[i]))

    # header region: the non-numeric lines just above the band, up to a big
    # vertical gap (which separates the column headers from the page title)
    band_top = body[0][0][1] if body else lines[h][0][1]
    hdr_idx, prev_y = [], band_top
    for k in range(h, -1, -1):
        if any(_is_num(w[4]) for w in lines[k]):
            break
        y = lines[k][0][1]
        if prev_y - y > 3.0 * hh:           # gap to the title block -> stop
            break
        hdr_idx.append(k)
        prev_y = y
    hdr_idx.reverse()

    hlines = hlines or []

    def cols_for(w):
        # a GROUP label: a horizontal rule just under the word spanning >=2
        # columns -> the label covers every column the rule spans (e.g. "合并"
        # over all columns, "归属于母公司股东权益" over a subset)
        wb, cx = w[3], (w[0] + w[2]) / 2
        best = None
        for (ly, lx0, lx1) in hlines:
            if 0 <= ly - wb <= 7 and lx0 - 2 <= cx <= lx1 + 2:
                spanned = [c for c in range(ncol) if lx0 - 3 <= centres[c] <= lx1 + 3]
                if len(spanned) >= 2 and (best is None or len(spanned) < len(best)):
                    best = spanned                       # tightest spanning rule
        return best if best else [nearest(cx)]

    labels = [[] for _ in range(ncol)]
    for k in hdr_idx:
        for w in sorted(lines[k], key=lambda w: w[0]):
            for c in cols_for(w):
                if w[4] not in labels[c]:                # don't repeat a group word
                    labels[c].append(w[4])
    labels = [" ".join(c).strip() for c in labels]
    return labels, assign, ncol


def _column_spec(lines, h, body_end, hlines=None):
    """Pick a column spec: data-driven first (best on dense numeric tables),
    header-gap as fallback (best on text-heavy / single-value tables)."""
    return (_spec_data_led(lines, h, body_end, hlines)
            or _spec_header(lines, h, body_end))


def reconstruct(page, clip=None, y_tol: float = 3.0, anchor_col: int = 0,
                with_span: bool = False):
    """Reconstruct a table from `page` (a fitz.Page), optionally limited to
    `clip` (a fitz.Rect table region). Returns (labels, rows) or None when no
    reliable single header row / columns can be found."""
    words = list(page.get_text("words", clip=clip)) if clip is not None \
        else list(page.get_text("words"))
    if clip is not None:
        # clip returns words merely *intersecting* the rect; keep only those
        # whose vertical centre is inside, so a partly-overlapping line just
        # above/below the table region does not leak in
        words = [w for w in words if clip.y0 <= (w[1] + w[3]) / 2 <= clip.y1]
    if len(words) < 4:
        return None
    lines = _cluster_lines(words, y_tol)
    if len(lines) < 2:
        return None

    hlines = horizontal_lines(page)
    # locate the table band: first anchor on the value column (skips page
    # banners), else fall back to scoring header candidates by aligned rows
    loc = _locate_table(lines)
    h = body_end = None
    if loc is not None and _column_spec(lines, *loc, hlines) is not None:
        h, body_end = loc
    if h is None:
        best = None  # (numeric, multicell, h)
        for i, ln in enumerate(lines[:-1]):
            if not _looks_like_header(ln):
                continue
            ev = _evaluate_header(lines, i)
            if ev is None:
                continue
            numeric, multicell, _ = ev
            if best is None or (numeric, multicell) > best[:2]:
                best = (numeric, multicell, i)
        if best is None or best[0] + best[1] == 0:
            return None
        h, body_end = best[2], len(lines)

    spec = _column_spec(lines, h, body_end, hlines)
    if spec is None:
        return None
    built = _build_table(lines, h, body_end, *spec, anchor_col=anchor_col)
    if built is None:
        return None
    labels, rows, span = built
    return (labels, rows, span) if with_span else (labels, rows)


def _data_bands(lines: List[List[Word]], min_numeric: int = 3,
                gap: int = 2) -> List[Tuple[int, int]]:
    """Segment the page into table bands: maximal runs of data lines, each
    prefixed by the nearest header-like line above. A line counts as a band
    MEMBER if it carries a number OR a dash placeholder, so an all-dash data
    row (common in equity-change statements) keeps the band together instead
    of orphaning the rows around it; the band must still contain enough real
    NUMERIC lines to qualify. Lets a page carry several stacked tables."""
    numeric = [any(_BAND_NUM.fullmatch(w[4]) for w in ln) for ln in lines]
    member = [any(_is_cell_value(w[4]) for w in ln) for ln in lines]
    n = len(lines)
    bands: List[Tuple[int, int]] = []
    i = 0
    prev_end = 0
    while i < n:
        if not member[i]:
            i += 1
            continue
        last, blanks, k = i, 0, i
        while k < n:
            if member[k]:
                last, blanks = k, 0
            else:
                blanks += 1
                if blanks > gap:
                    break
            k += 1
        if sum(1 for x in range(i, last + 1) if numeric[x]) >= min_numeric:
            lo = max(prev_end, i - 5)
            h = next((m for m in range(i - 1, lo - 1, -1)
                      if _looks_like_header(lines[m])), None)
            if h is None:
                h = i - 1 if i - 1 >= prev_end else i
            bands.append((max(h, 0), last + 1))
            prev_end = last + 1
        i = last + 1
    return bands


def reconstruct_all(page, clip=None, y_tol: float = 3.0, anchor_col: int = 0
                    ) -> List[Tuple[List[str], List[List[str]], Tuple[float, float]]]:
    """Reconstruct EVERY data table on a page/region (not just the dominant
    one). Returns a list of (labels, rows, (y0, y1)) sorted top-to-bottom."""
    words = list(page.get_text("words", clip=clip)) if clip is not None \
        else list(page.get_text("words"))
    if clip is not None:
        words = [w for w in words if clip.y0 <= (w[1] + w[3]) / 2 <= clip.y1]
    if len(words) < 4:
        return []
    lines = _cluster_lines(words, y_tol)
    hlines = horizontal_lines(page)
    out = []
    for h, body_end in _data_bands(lines):
        spec = _column_spec(lines, h, body_end, hlines)
        if spec is None:
            continue
        built = _build_table(lines, h, body_end, *spec, anchor_col=anchor_col)
        if built is not None:
            out.append(built)
    return sorted(out, key=lambda t: t[2][0])


def to_markdown(labels: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    head = "| " + " | ".join(labels) + " |"
    sep = "| " + " | ".join("---" for _ in labels) + " |"
    body = ["| " + " | ".join(c.replace("|", "\\|") for c in r) + " |"
            for r in rows]
    return "\n".join([head, sep, *body])


def region_markdown(page, clip=None, min_rows: int = 2) -> Optional[str]:
    """Convenience: reconstruct and render to markdown, or None on failure /
    too-few-rows (so the caller falls back to its existing table output)."""
    res = reconstruct(page, clip=clip)
    if res is None:
        return None
    labels, rows = res
    if len(rows) < min_rows:
        return None
    return to_markdown(labels, rows)
