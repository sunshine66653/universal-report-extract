"""
Excel rule sheet -> JSON converter
─────────────────────────────────────────────────────────────────
Maintainers manage metric rules in Excel; this module converts them to the
engine's internal JSON at runtime.

Supported columns (headers are case-insensitive; missing columns default):
    id              metric id (required, unique)
    name            metric name (required)
    source          definition / where to take the number from (fed to the LLM)
    enabled         TRUE/FALSE/1/0/是/否 (default TRUE)
    section_hint    section locator keywords, comma/semicolon/newline separated;
                    "/" inside one token means OR
    aliases         alias terms, same separators; "/" inside one token means OR
    extraction_mode direct / calc (default direct)
    calc            calculation expression or JSON (when extraction_mode=calc)
    aggregation     aggregation method (optional)
    allow_parent_note  whether parent-company note sections may match
                       (TRUE/FALSE, default FALSE)
    skip_reason     remarks / skip reason (optional)

Multi-sheet: every sheet is one "rule group"; the sheet name becomes `group`.

NOTE: _COLUMN_ALIASES contains Chinese header names on purpose — real rule
sheets (e.g. profiles/cn_securities) use Chinese column headers. Keep them.
"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional

import openpyxl


# ── header normalization map (Chinese + English headers both accepted) ──
_COLUMN_ALIASES = {
    "id": "id",
    "编号": "id",
    "name": "name",
    "名称": "name",
    "指标名称": "name",
    "name_in_material": "name_in_material",
    "素材名称": "name_in_material",
    "素材中的叫法": "name_in_material",
    "source": "source",
    "来源": "source",
    "来源说明": "source",
    "口径": "source",
    "enabled": "enabled",
    "启用": "enabled",
    "是否启用": "enabled",
    "section_hint": "section_hint",
    "章节线索": "section_hint",
    "定位线索": "section_hint",
    "aliases": "aliases",
    "别名": "aliases",
    "extraction_mode": "extraction_mode",
    "抽取模式": "extraction_mode",
    "calc": "calc",
    "计算": "calc",
    "aggregation": "aggregation",
    "聚合": "aggregation",
    "allow_parent_note": "allow_parent_note",
    "母公司附注": "allow_parent_note",
    "skip_reason": "skip_reason",
    "备注": "skip_reason",
    "跳过原因": "skip_reason",
}

_LIST_FIELDS = {"section_hint", "aliases"}
_BOOL_FIELDS = {"enabled", "allow_parent_note"}


def _norm_header(h: Any) -> Optional[str]:
    if h is None:
        return None
    key = str(h).strip().lower()
    return _COLUMN_ALIASES.get(key, _COLUMN_ALIASES.get(str(h).strip(), None))


def _parse_bool(v: Any, default: bool = True) -> bool:
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    # accepts Chinese 是 / check marks as truthy (real sheets use them)
    return s in ("true", "1", "yes", "y", "是", "√", "✓", "t")


def _parse_list(v: Any) -> List[str]:
    if v is None or v == "":
        return []
    s = str(v)
    # commas / semicolons / full-width commas / newlines all act as separators;
    # "/" is preserved (in-token OR)
    for sep in ["\n", "；", ";", "，"]:
        s = s.replace(sep, ",")
    return [x.strip() for x in s.split(",") if x.strip()]


def excel_to_rules(xlsx_path: str | Path) -> List[Dict[str, Any]]:
    """Read the Excel rule sheet, return a list of rules."""
    xlsx_path = Path(xlsx_path)
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    rules: List[Dict[str, Any]] = []

    for ws in wb.worksheets:
        if ws.max_row < 2:
            continue
        # header row
        header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
        col_map: Dict[int, str] = {}
        for ci, h in enumerate(header_row):
            nh = _norm_header(h)
            if nh:
                col_map[ci] = nh
        if "id" not in col_map.values() or "name" not in col_map.values():
            # not a rule sheet — skip
            continue

        for row in ws.iter_rows(min_row=2, values_only=True):
            rec: Dict[str, Any] = {}
            for ci, field in col_map.items():
                val = row[ci] if ci < len(row) else None
                if field in _LIST_FIELDS:
                    rec[field] = _parse_list(val)
                elif field in _BOOL_FIELDS:
                    default = True if field == "enabled" else False
                    rec[field] = _parse_bool(val, default)
                else:
                    rec[field] = (str(val).strip() if val is not None else None)
            if not rec.get("id") or not rec.get("name"):
                continue
            rec.setdefault("extraction_mode", "direct")
            rec.setdefault("enabled", True)
            rec["group"] = ws.title
            rules.append(rec)

    return rules


def _file_sig(path: Path) -> str:
    st = path.stat()
    raw = f"{path}|{st.st_mtime_ns}|{st.st_size}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def excel_to_rules_json(
    xlsx_path: str | Path,
    json_path: Optional[str | Path] = None,
    force: bool = False,
) -> Path:
    """
    Convert and cache as JSON. If the JSON exists and the Excel is unchanged,
    reuse it (idempotent). Returns the JSON path.
    """
    xlsx_path = Path(xlsx_path)
    if json_path is None:
        json_path = xlsx_path.with_suffix(".json")
    json_path = Path(json_path)

    sig = _file_sig(xlsx_path)
    if json_path.exists() and not force:
        try:
            cached = json.loads(json_path.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("_sig") == sig:
                return json_path
        except Exception:
            pass

    rules = excel_to_rules(xlsx_path)
    payload = {
        "_sig": sig,
        "_source": xlsx_path.name,
        "count": len(rules),
        "rules": rules,
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return json_path


def load_rules(
    rules_path: str | Path, force_convert: bool = False,
) -> List[Dict[str, Any]]:
    """
    Unified entry: a .xlsx path is converted to JSON first; a .json path is
    read directly. Returns enabled rules only.
    """
    rules_path = Path(rules_path)
    if rules_path.suffix.lower() in (".xlsx", ".xlsm"):
        json_path = excel_to_rules_json(rules_path, force=force_convert)
        data = json.loads(json_path.read_text(encoding="utf-8"))
        rules = data.get("rules", data) if isinstance(data, dict) else data
    else:
        data = json.loads(rules_path.read_text(encoding="utf-8"))
        rules = data.get("rules", data) if isinstance(data, dict) else data

    return [r for r in rules if r.get("enabled", True)]
