"""
Prompt rendering
─────────────────────────────────────────────────────────────────
Renders the final prompts from the profile's prompts/global.txt and
prompts/chunk.txt. Maintainers only edit those two txt files plus the Excel
rule sheet — no code changes needed.

Template placeholders ({name} style; unknown placeholders pass through):
global.txt may use:
    {report_period_hint}  {language}

chunk.txt may use:
    {global_prompt}       rendered global prompt
    {rule_id} {rule_name} {rule_source} {extraction_mode}
    {company_instruction} {calc_instruction} {extra_instruction}
    {report_period_hint}  {context}

If a profile ships no templates, the built-in Chinese defaults below are used
(identical to the original engine).

NOTE: the default templates are Chinese LLM prompts for Chinese-language
profiles — functional text, do not translate. English profiles override them
with their own prompts/ files (see profiles/hk_securities_en).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from engine.retrieval import format_chunk_with_breadcrumb


# ── built-in default templates (used when a profile ships no txt files) ──
_DEFAULT_GLOBAL = (
    "你是一个金融年报数据抽取助手。\n"
    "你只能根据提供的上下文抽取，不允许使用外部知识，不允许编造。"
)

_DEFAULT_CHUNK = """{global_prompt}
请基于以下规则和上下文，只抽取这一个指标。

规则：
- id: {rule_id}
- name: {rule_name}
- source: {rule_source}
- extraction_mode: {extraction_mode}{company_instruction}{calc_instruction}{extra_instruction}

抽取要求：
1. 严格按照 source 描述寻找。
2. 若出现多个年度，只提取{report_period_hint}对应数据。
3. 保持原单位，不要换算。如果货币不是人民币，unit请返回这种形式：如"港币亿元"、"美元万元"。
4. 除source明确要求不保留负号外，负数请保留负号（括号表示的负数请转换为带负号的数字）。
5. 若无法确认，请返回null，不要猜测数值。

输出必须是 JSON 对象，请返回：
{{
  "result": {{
    "id": "{rule_id}",
    "name": "{rule_name}",
    "value": "数值或null",
    "unit": "单位或null",
    "source_text": "原文片段或null"
  }}
}}

上下文：
{context}
"""

# whole-document mode: one prompt, all metrics at once, no per-rule retrieval
# (section_hint is intentionally NOT included — the model sees the full text)
_DEFAULT_WHOLE = """{global_prompt}
下面给你一份报告全文（或其中一段），以及一批需要抽取的指标清单。
请通读全文，为清单里的【每一个】指标抽取数值，一次性输出。

抽取要求：
1. 按每个指标的 name / source 描述在全文中定位。
2. 若出现多个年度，只提取{report_period_hint}对应数据。
3. 保持原单位，不要换算。如果货币不是人民币，unit请返回这种形式：如"港币亿元"、"美元万元"。
4. 除source明确要求不保留负号外，负数请保留负号（括号表示的负数请转换为带负号的数字）。
5. 本段未出现的指标，value 与 unit 返回 null，不要猜测，也不要遗漏该 id。

指标清单：
{rules_block}

输出必须是 JSON 对象，results 数组按清单顺序给出每个指标：
{{
  "results": [
    {{"id": "指标id", "value": "数值或null", "unit": "单位或null", "source_text": "原文片段或null"}}
  ]
}}

报告内容：
{context}
"""


class _SafeDict(dict):
    """For str.format_map: unknown keys stay as literal {key}."""
    def __missing__(self, key):
        return "{" + key + "}"


def _render(template: str, mapping: Dict[str, Any]) -> str:
    return template.format_map(_SafeDict(mapping))


def render_global_prompt(profile, extra: Dict[str, Any] = None) -> str:
    tpl = profile.global_prompt or _DEFAULT_GLOBAL
    mapping = {
        "report_period_hint": profile.config.get("report_period_hint", ""),
        "language": profile.language,
    }
    if extra:
        mapping.update(extra)
    return _render(tpl, mapping)


def build_extract_prompt(
    profile,
    rule: Dict[str, Any],
    selected_chunks: List[Dict[str, Any]],
    extra_instruction: str = "",
) -> str:
    max_ctx = int(profile.llm.get("max_chars_context", 32000))
    context = "\n\n".join(
        format_chunk_with_breadcrumb(c) for c in selected_chunks
    )[:max_ctx]

    company_hints = rule.get("company_hints", []) or []
    company_instruction = (
        f"\n- 指定公司实体: {', '.join(company_hints)}" if company_hints else ""
    )
    calc = rule.get("calc")
    calc_instruction = (
        f"\n- 计算规则: {json.dumps(calc, ensure_ascii=False)}" if calc else ""
    )
    extra_instr = f"\n- 额外要求: {extra_instruction}" if extra_instruction else ""

    tpl = profile.chunk_prompt or _DEFAULT_CHUNK
    mapping = {
        "global_prompt": render_global_prompt(profile),
        "rule_id": rule.get("id"),
        "rule_name": rule.get("name"),
        "rule_source": rule.get("source"),
        "extraction_mode": rule.get("extraction_mode", "direct"),
        "company_instruction": company_instruction,
        "calc_instruction": calc_instruction,
        "extra_instruction": extra_instr,
        "report_period_hint": profile.config.get("report_period_hint", ""),
        "context": context,
    }
    return _render(tpl, mapping)


def _rules_block(rules: List[Dict[str, Any]]) -> str:
    """Compact one-line-per-metric listing for the whole-document prompt."""
    lines = []
    for r in rules:
        src = (r.get("source") or "").replace("\n", " ").strip()
        line = f"- id: {r.get('id')} | name: {r.get('name')}"
        if src:
            line += f" | source: {src}"
        calc = r.get("calc")
        if calc:
            line += f" | calc: {json.dumps(calc, ensure_ascii=False)}"
        lines.append(line)
    return "\n".join(lines)


def build_whole_prompt(
    profile,
    rules: List[Dict[str, Any]],
    doc_text: str,
) -> str:
    """Whole-document prompt: the full text (one window of it) plus the
    complete metric list, asking for all values in a single array. Bypasses
    per-rule retrieval and section_hint entirely. The chunk.txt template is
    NOT used here; a profile may override via prompts/whole.txt."""
    max_ctx = int(profile.llm.get("max_chars_context", 32000))
    tpl = getattr(profile, "whole_prompt", "") or _DEFAULT_WHOLE
    mapping = {
        "global_prompt": render_global_prompt(profile),
        "report_period_hint": profile.config.get("report_period_hint", ""),
        "rules_block": _rules_block(rules),
        "context": (doc_text or "")[:max_ctx],
    }
    return _render(tpl, mapping)
