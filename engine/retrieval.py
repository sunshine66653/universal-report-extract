"""
Configurable scoring retrieval
─────────────────────────────────────────────────────────────────
score_chunk scores by rule aliases / section_hint / company_hints hits.
Weights come from profile.retrieval.weights so different businesses can tune.
Matching is normalized (whitespace stripped, full-width -> half-width,
case-folded) and works for both Chinese and English text.
"""
from __future__ import annotations

import unicodedata
from typing import Any, Dict, List

from engine.chunking import format_chunk_with_breadcrumb  # re-export for callers


def normalize_for_match(s: str, remove_all_spaces: bool = True) -> str:
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    s = s.lower()
    if remove_all_spaces:
        s = "".join(s.split())
    return s


def _split_or_group(token: str) -> List[str]:
    token = str(token).strip()
    if "/" not in token:
        return [token] if token else []
    parts = [x.strip() for x in token.split("/") if x.strip()]
    return parts if parts else ([token] if token else [])


def score_chunk(
    rule: Dict[str, Any],
    chunk: Dict[str, Any],
    weights: Dict[str, float],
    table_signals: List[str],
) -> float:
    text = chunk.get("text", "")
    section = " > ".join(chunk.get("section_path", []))
    text_n = normalize_for_match(text, remove_all_spaces=True)
    section_n = normalize_for_match(section, remove_all_spaces=True)

    aliases = rule.get("aliases", []) or []
    company_hints = rule.get("company_hints", []) or []
    section_hint = rule.get("section_hint", []) or []

    w_alias = weights.get("alias", 4.0)
    w_section_base = weights.get("section_base", 3.0)
    w_text_base = weights.get("text_base", 2.0)
    w_step = weights.get("step", 1.0)
    w_company = weights.get("company", 10.0)
    w_table = weights.get("table", 1.0)

    score = 0.0

    # 1) aliases: OR inside each token group
    for a in aliases:
        group = _split_or_group(a)
        if group and any(
            normalize_for_match(g, True) in text_n for g in group
        ):
            score += w_alias

    # 2) company_hints
    for e in company_hints:
        e_n = normalize_for_match(e, True)
        if not e_n:
            continue
        if e_n in text_n:
            score += w_company
        if e_n in section_n:
            score += w_company

    # 3) section_hint: later entries weigh more
    for idx, s in enumerate(section_hint):
        group = _split_or_group(s)
        if not group:
            continue
        text_w = w_text_base + idx * w_step
        section_w = w_section_base + idx * w_step
        if any(normalize_for_match(g, True) in text_n for g in group):
            score += text_w
        if any(normalize_for_match(g, True) in section_n for g in group):
            score += section_w

    # 4) table-structure signal
    if sum(1 for kw in table_signals if kw in text) >= 2:
        score += w_table

    return score


def retrieve_chunks(
    rule: Dict[str, Any],
    chunks: List[Dict[str, Any]],
    weights: Dict[str, float],
    table_signals: List[str],
    topk: int = 12,
    allow_parent_note: bool = False,
) -> List[Dict[str, Any]]:
    scored = []
    for chunk in chunks:
        is_parent = bool(chunk.get("is_parent_note_block", False))
        if allow_parent_note:
            if not is_parent:
                continue
        else:
            if is_parent:
                continue
        s = score_chunk(rule, chunk, weights, table_signals)
        if s > 0:
            scored.append((s, chunk))
    scored.sort(key=lambda x: (-x[0], x[1].get("chunk_id", "")))
    return [x[1] for x in scored[:topk]]
