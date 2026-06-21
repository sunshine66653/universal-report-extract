"""
Extraction orchestration
─────────────────────────────────────────────────────────────────
Input: md text + profile + api_key
Flow: chunk -> per-rule retrieval of topk chunks -> render prompt ->
      LLM extraction -> normalize -> DataFrame
Supports concurrency (max_workers) and an abort callback (should_stop).
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from engine.chunking import build_chunks, format_chunk_with_breadcrumb
from engine.retrieval import retrieve_chunks
from engine.prompts import build_extract_prompt, build_whole_prompt
from engine.llm import extract_with_prompt, extract_array_with_prompt
from engine.headings import relevel_markdown


# ── shared MD preprocessing ───────────────────────────────────────────
def prepare_md_for_chunking(md_text: str, profile,
                            log: Callable[[str], None] = print) -> str:
    """Promote plain-text numbered section titles into Markdown headings so
    chunking yields a usable section_path (MinerU often emits titles as plain
    paragraphs). Idempotent on already-heading'd MD. Gated by
    chunking.promote_headings (default on)."""
    if not profile.chunking.get("promote_headings", True):
        return md_text
    before = len(re.findall(r"(?m)^#{1,6}\s", md_text))
    md2 = relevel_markdown(
        md_text, profile.language, promote_plain=True,
        max_heading_chars=int(profile.chunking.get("max_heading_chars", 0)),
    )
    after = len(re.findall(r"(?m)^#{1,6}\s", md2))
    if after > before:
        log(f"Heading promotion: {before} -> {after} headings "
            f"(+{after - before} plain titles recovered)")
    return md2


# ── numeric normalization ─────────────────────────────────────────────
def parse_number_like(s: Any) -> Optional[float]:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    txt = str(s).strip()
    if txt.lower() in ("null", "none", "", "n/a", "na", "—", "-"):
        return None
    neg = False
    if txt.startswith("(") and txt.endswith(")"):
        neg = True
        txt = txt[1:-1]
    # strip both ASCII and full-width thousands separators
    txt = txt.replace(",", "").replace("，", "")
    m = re.search(r"-?\d+(?:\.\d+)?", txt)
    if not m:
        return None
    val = float(m.group(0))
    return -val if (neg and val > 0) else val


def normalize_result_record(
    rule: Dict[str, Any], result: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "id": rule.get("id"),
        "name": rule.get("name"),
        "group": rule.get("group", ""),
        "value": result.get("value"),
        "value_num": parse_number_like(result.get("value")),
        "unit": result.get("unit"),
        "source_text": result.get("source_text"),
        "error": result.get("_error", ""),
    }


# ── debug prompt dump ─────────────────────────────────────────────────
_SAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\s]+')


def save_prompt_debug(prompt: str, debug_dir: Path | str,
                      rule_id: str, scope: str = "main") -> Optional[str]:
    """Dump the final LLM prompt of one rule for auditing. File layout is
    the original project's wire format: .../_debug_prompts/
    <YYYYmmdd_HHMMSS_us>_<scope>_<rule_id>.txt  — keep it byte-compatible
    so existing tooling/habits keep working. Never raises."""
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe_rule = _SAFE_NAME_RE.sub("_", str(rule_id or "unknown"))
        safe_scope = _SAFE_NAME_RE.sub("_", str(scope or "main"))
        d = Path(debug_dir)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{ts}_{safe_scope}_{safe_rule}.txt"
        p.write_text(prompt or "", encoding="utf-8")
        return str(p)
    except Exception:
        return None


# ── single-rule extraction ────────────────────────────────────────────
def extract_one_rule(
    profile,
    rule: Dict[str, Any],
    chunks: List[Dict[str, Any]],
    api_key: str,
    debug_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    rtv = profile.retrieval
    weights = rtv.get("weights", {})
    table_signals = rtv.get("table_signals", [])
    topk = int(rtv.get("topk", 12))
    allow_parent = bool(rule.get("allow_parent_note", False))

    selected = retrieve_chunks(
        rule, chunks, weights, table_signals,
        topk=topk, allow_parent_note=allow_parent,
    )
    if not selected:
        return normalize_result_record(
            rule, {"value": None, "unit": None, "source_text": None,
                   "_error": "no relevant context retrieved"},
        )

    prompt = build_extract_prompt(profile, rule, selected)
    if debug_dir:
        save_prompt_debug(prompt, debug_dir, rule.get("id"))
    llm = profile.llm
    result = extract_with_prompt(
        prompt,
        model=llm.get("text_model", "qwen-plus"),
        api_key=api_key,
        base_url=llm.get("base_url"),
        temperature=float(llm.get("temperature", 0.0)),
    )
    return normalize_result_record(rule, result)


# ── concurrent batch extraction ───────────────────────────────────────
def extract_all(
    profile,
    md_text: str,
    api_key: str,
    max_workers: int = 5,
    log: Callable[[str], None] = print,
    progress: Callable[[float], None] = lambda p: None,
    should_stop: Callable[[], bool] = lambda: False,
    rules: Optional[List[Dict[str, Any]]] = None,
    debug_dir: Optional[Path] = None,
) -> pd.DataFrame:
    if rules is None:
        rules = profile.load_rules()
    log(f"Rules: {len(rules)}")
    if debug_dir:
        log(f"Debug prompts -> {debug_dir}")

    md_text = prepare_md_for_chunking(md_text, profile, log)
    chunks = build_chunks(md_text, profile.chunking)
    log(f"Chunks: {len(chunks)} (strategy={profile.chunking.get('strategy')})")

    results: List[Dict[str, Any]] = []
    total = len(rules)
    done = 0

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(extract_one_rule, profile, rule, chunks, api_key,
                      debug_dir): rule
            for rule in rules
        }
        for fut in as_completed(futures):
            if should_stop():
                log("Abort signal received, stopping extraction")
                break
            rule = futures[fut]
            try:
                rec = fut.result()
            except Exception as e:
                rec = normalize_result_record(
                    rule, {"value": None, "_error": str(e)},
                )
            results.append(rec)
            done += 1
            progress(done / total if total else 1.0)
            flag = "✓" if rec.get("value") not in (None, "null", "") else "·"
            log(f"  [{done}/{total}] {flag} {rec['id']} {rec['name']} = {rec.get('value')}")

    # restore original rule order
    order = {r.get("id"): i for i, r in enumerate(rules)}
    results.sort(key=lambda x: order.get(x["id"], 9999))
    return pd.DataFrame(results)


# ── whole-document extraction (no per-rule retrieval) ─────────────────
def _parse_page_range(pages: Optional[str]):
    if not pages:
        return None
    m = re.match(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*$", str(pages))
    if not m:
        raise ValueError(f"bad --pages value {pages!r} (use e.g. 5-12 or 7)")
    lo = int(m.group(1))
    hi = int(m.group(2)) if m.group(2) else lo
    return (min(lo, hi), max(lo, hi))


def _filter_chunks_by_pages(chunks, page_range, log):
    """Keep chunks overlapping [lo, hi]. Chunks with no page info are dropped
    only when page filtering is active AND some chunk does carry page info."""
    lo, hi = page_range
    has_pages = any(c.get("page_start") is not None for c in chunks)
    if not has_pages:
        log(f"⚠ --pages {lo}-{hi} requested but the MD has no page markers — "
            f"using the whole document")
        return chunks
    kept = []
    for c in chunks:
        ps, pe = c.get("page_start"), c.get("page_end")
        if ps is None:
            continue
        if pe is None:
            pe = ps
        if pe >= lo and ps <= hi:
            kept.append(c)
    return kept


def _pack_windows(chunks, budget: int):
    """Pack consecutive chunk texts (with breadcrumbs) into windows no larger
    than `budget` chars. A single oversized chunk becomes its own window."""
    windows, cur, cur_len = [], [], 0
    for c in chunks:
        block = format_chunk_with_breadcrumb(c)
        blen = len(block) + 2
        if cur and cur_len + blen > budget:
            windows.append("\n\n".join(cur))
            cur, cur_len = [], 0
        cur.append(block)
        cur_len += blen
    if cur:
        windows.append("\n\n".join(cur))
    return windows


def extract_whole(
    profile,
    md_text: str,
    api_key: str,
    pages: Optional[str] = None,
    log: Callable[[str], None] = print,
    progress: Callable[[float], None] = lambda p: None,
    should_stop: Callable[[], bool] = lambda: False,
    rules: Optional[List[Dict[str, Any]]] = None,
    debug_dir: Optional[Path] = None,
) -> pd.DataFrame:
    """Whole-document extraction: feed the full text (optionally a page range)
    plus the complete metric list to the model in one shot, no per-rule
    retrieval or section_hint. Oversized documents are auto-split into windows
    (max_chars_context) and merged by id (first non-null value wins)."""
    if rules is None:
        rules = profile.load_rules()
    log(f"Rules: {len(rules)} (mode=whole)")

    md_text = prepare_md_for_chunking(md_text, profile, log)
    chunks = build_chunks(md_text, profile.chunking)
    page_range = _parse_page_range(pages)
    if page_range:
        chunks = _filter_chunks_by_pages(chunks, page_range, log)
        log(f"Page filter {page_range[0]}-{page_range[1]}: {len(chunks)} chunks kept")

    budget = int(profile.llm.get("max_chars_context", 32000))
    windows = _pack_windows(chunks, budget)
    total = len(windows)
    log(f"Whole-doc windows: {total} (budget={budget} chars/window)")
    if total > 12 and not page_range:
        log(f"⚠ {total} windows means ~{total} LLM calls — whole mode suits a "
            f"focused MD or a --pages range; for a full report prefer the "
            f"default retrieval mode")
    if not windows:
        windows = [""]
        total = 1

    llm = profile.llm
    all_ids = [str(r.get("id")) for r in rules]
    # merged[id] = best record so far; first non-null value wins
    merged: Dict[str, Dict[str, Any]] = {}
    filled: set = set()
    for wi, win in enumerate(windows, 1):
        if should_stop():
            log("Abort signal received, stopping whole-doc extraction")
            break
        prompt = build_whole_prompt(profile, rules, win)
        if debug_dir:
            save_prompt_debug(prompt, debug_dir, f"window{wi:02d}", scope="whole")
        try:
            items = extract_array_with_prompt(
                prompt,
                model=llm.get("text_model", "qwen-plus"),
                api_key=api_key, base_url=llm.get("base_url"),
                temperature=float(llm.get("temperature", 0.0)),
            )
        except Exception as e:
            log(f"  window {wi}/{total} failed: {e}")
            items = []
        by_id = {str(it.get("id")): it for it in items if it.get("id") is not None}
        new_here = 0
        for rid in all_ids:
            it = by_id.get(rid)
            if not it or it.get("value") in (None, "", "null"):
                continue
            if rid not in filled:                 # first non-null wins
                merged[rid] = it
                filled.add(rid)
                new_here += 1
        if new_here:
            log(f"  window {wi}/{total}: +{new_here} value(s) "
                f"({len(filled)}/{len(all_ids)} filled)")
        progress(len(filled) / len(all_ids) if all_ids else 1.0)
        if len(filled) == len(all_ids):           # everything found — stop early
            log(f"  all {len(all_ids)} metrics found by window {wi}, stopping")
            break

    results = [normalize_result_record(r, merged.get(str(r.get("id")), {}))
               for r in rules]
    return pd.DataFrame(results)


def save_results(df: pd.DataFrame, xlsx_path: str, json_path: str = None):
    from pathlib import Path
    Path(xlsx_path).parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(xlsx_path, index=False)
    if json_path:
        df.to_json(json_path, orient="records", force_ascii=False, indent=2)
