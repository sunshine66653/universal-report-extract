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
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from engine.chunking import build_chunks
from engine.retrieval import retrieve_chunks
from engine.prompts import build_extract_prompt
from engine.llm import extract_with_prompt


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


# ── single-rule extraction ────────────────────────────────────────────
def extract_one_rule(
    profile,
    rule: Dict[str, Any],
    chunks: List[Dict[str, Any]],
    api_key: str,
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
) -> pd.DataFrame:
    if rules is None:
        rules = profile.load_rules()
    log(f"Rules: {len(rules)}")

    chunks = build_chunks(md_text, profile.chunking)
    log(f"Chunks: {len(chunks)} (strategy={profile.chunking.get('strategy')})")

    results: List[Dict[str, Any]] = []
    total = len(rules)
    done = 0

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(extract_one_rule, profile, rule, chunks, api_key): rule
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


def save_results(df: pd.DataFrame, xlsx_path: str, json_path: str = None):
    from pathlib import Path
    Path(xlsx_path).parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(xlsx_path, index=False)
    if json_path:
        df.to_json(json_path, orient="records", force_ascii=False, indent=2)
