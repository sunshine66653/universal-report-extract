"""
Profile loader
─────────────────────────────────────────────────────────────────
A profile = one business scenario, self-contained:

profiles/<name>/
├── profile.json          chunking params, models, language, retrieval weights,
│                         download config
├── prompts/
│   ├── global.txt        global prompt template (system-level instructions)
│   └── chunk.txt         per-metric extraction prompt template (placeholders)
└── rules/
    └── rules.xlsx        metric rules (maintained in Excel, converted to JSON
                          at runtime)

profile.json fields (all optional; defaults in DEFAULT_PROFILE):
{
  "display_name": "HK English Reports",
  "language": "en",                  # zh / en — affects PDF->MD and prompts
  "report_period_hint": "FY2024",    # "latest reporting period" wording for prompts
  "llm": { "text_model": "qwen-plus", "vl_model": "qwen-vl-max",
           "temperature": 0.0, "base_url": "...", "max_chars_context": 32000 },
  "chunking": { "strategy": "auto", "max_chars": 4500,
                "slice_marker_regex": "...", "heading_levels": 6,
                "promote_headings": true, "max_heading_chars": 0 },
  "retrieval": { "topk": 12,
                 "weights": {"alias":4.0,"section_base":3.0,"text_base":2.0,
                             "step":1.0,"company":10.0,"table":1.0} },
  "rules_file": "rules/rules.xlsx",
  "download": { "adapter": "cninfo", "report_language": "en",
                "company_codes": { "中信证券": ["600030","org_id"] } }
}

NOTE: several DEFAULT_PROFILE values below contain Chinese text on purpose —
the slice-marker regex matches the engine's Chinese wire-format marker, the
table_signals are Chinese financial-table keywords, and report_period_hint is
prompt text for Chinese-language profiles. Do not translate them.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
PROFILES_DIR = ROOT / "profiles"


# ── defaults (merged under profile.json) ──────────────────────────────
DEFAULT_PROFILE: Dict[str, Any] = {
    "display_name": "",
    "language": "zh",
    "report_period_hint": "最新一个完整报告期",
    "llm": {
        "text_model": "qwen-plus",
        "vl_model": "qwen-vl-max",
        "temperature": 0.0,
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "max_chars_context": 32000,
    },
    "chunking": {
        "strategy": "auto",            # auto / cn_slice / heading / fixed
        "max_chars": 4500,
        "slice_marker_regex":
            r"^\s*#\s*---\s*PDF\s*物理切片\s*[:：]\s*第\s*(\d+)\s*-\s*(\d+)\s*页\s*---\s*$",
        "heading_levels": 6,
        "promote_headings": True,      # promote plain numbered titles -> headings before chunking
        "max_heading_chars": 0,        # promotion length guard (0 -> 40 zh / 90 en)
    },
    "retrieval": {
        "topk": 12,
        "weights": {
            "alias": 4.0,
            "section_base": 3.0,
            "text_base": 2.0,
            "step": 1.0,
            "company": 10.0,
            "table": 1.0,
        },
        "table_signals": ["单位：", "项目", "本期", "上期", "同比", "%",
                          "Unit", "Item", "Current", "Prior", "YoY"],
    },
    "convert": {
        "engine": "docling",           # docling (local) / mineru (cloud API)
        "rotate_detect": True,         # zh only: turn rotated landscape pages upright before OCR
        "rotate_min_vertical_ratio": 0.85,  # only rotate when vertical text is this share of the page (guards readable pages with vertical labels)
        "rotate_osd": False,           # optional Tesseract OSD visual second-check (needs pytesseract+tesseract; falls back to heuristic if absent)
        "docling_table_rebuild": True, # docling: replace TableFormer tables with the header-anchored coordinate rebuild (False = raw/original TableFormer output)
        "mineru_token": "",            # MinerU API token (or env MINERU_TOKEN)
        "mineru_model_version": "vlm",
        "recognize_images": True,      # run VL figure recognition on MinerU output
    },
    "rules_file": "rules/rules.xlsx",
    "download": {
        "adapter": "cninfo",
        "report_language": "zh",       # zh / en (H-share English editions)
        "company_codes": {},
    },
}


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Profile:
    name: str
    dir: Path
    config: Dict[str, Any]
    global_prompt: str
    chunk_prompt: str
    whole_prompt: str = ""
    _rules_cache: Optional[List[Dict[str, Any]]] = field(default=None, repr=False)

    # ── convenience accessors ─────────────────────────────────────
    @property
    def display_name(self) -> str:
        return self.config.get("display_name") or self.name

    @property
    def language(self) -> str:
        return self.config.get("language", "zh")

    @property
    def llm(self) -> Dict[str, Any]:
        return self.config["llm"]

    @property
    def chunking(self) -> Dict[str, Any]:
        return self.config["chunking"]

    @property
    def retrieval(self) -> Dict[str, Any]:
        return self.config["retrieval"]

    @property
    def download(self) -> Dict[str, Any]:
        return self.config["download"]

    def rules_path(self) -> Path:
        return self.dir / self.config.get("rules_file", "rules/rules.xlsx")

    def ensure_rules_excel(self) -> Optional[Path]:
        """The Excel is the only hand-maintained rule surface. If the profile
        currently ships only a rules JSON (e.g. the skill was uploaded
        without .xlsx files), materialize a hand-editable Excel from it at
        the profile's rules_file path. From then on the normal flow applies:
        edit the Excel -> the JSON cache resyncs automatically on each run.
        Returns the xlsx path (existing or generated), or None if the
        profile has no rules at all."""
        rp = self.rules_path()
        if rp.suffix.lower() not in (".xlsx", ".xlsm"):
            return rp if rp.exists() else None   # profile opted into raw JSON
        if rp.exists():
            return rp
        jp = rp.with_suffix(".json")
        if not jp.exists():
            cands = list((self.dir / "rules").glob("*.json"))
            if not cands:
                return None
            jp = cands[0]
        from engine.rules_excel import load_rules_raw, rules_to_excel
        rules_to_excel(load_rules_raw(jp), rp)
        print(f"[rules] no hand-editable Excel found — generated {rp.name} "
              f"from {jp.name}; maintain rules there from now on (Excel "
              f"edits sync back to JSON automatically)")
        return rp

    def load_rules(self, force_convert: bool = False) -> List[Dict[str, Any]]:
        if self._rules_cache is not None and not force_convert:
            return self._rules_cache
        from engine.rules_excel import load_rules
        rp = self.rules_path()
        if not rp.exists():
            # fallback: any .xlsx / rules.json in the rules dir
            cands = list((self.dir / "rules").glob("*.xlsx")) + \
                    list((self.dir / "rules").glob("*.json"))
            if cands:
                rp = cands[0]
            else:
                raise FileNotFoundError(
                    f"profile '{self.name}' is missing its rules file: {rp}")
        self._rules_cache = load_rules(rp, force_convert=force_convert)
        return self._rules_cache


def load_profile(name: str) -> Profile:
    pdir = PROFILES_DIR / name
    if not pdir.is_dir():
        raise FileNotFoundError(f"Profile not found: {name} (expected at {pdir})")

    cfg_path = pdir / "profile.json"
    user_cfg = {}
    if cfg_path.exists():
        user_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    config = _deep_merge(DEFAULT_PROFILE, user_cfg)

    def _read(p: Path) -> str:
        return p.read_text(encoding="utf-8") if p.exists() else ""

    global_prompt = _read(pdir / "prompts" / "global.txt")
    chunk_prompt = _read(pdir / "prompts" / "chunk.txt")
    whole_prompt = _read(pdir / "prompts" / "whole.txt")

    return Profile(
        name=name, dir=pdir, config=config,
        global_prompt=global_prompt, chunk_prompt=chunk_prompt,
        whole_prompt=whole_prompt,
    )


def list_profiles() -> List[str]:
    if not PROFILES_DIR.is_dir():
        return []
    return sorted(
        p.name for p in PROFILES_DIR.iterdir()
        if p.is_dir() and (p / "profile.json").exists()
    )
