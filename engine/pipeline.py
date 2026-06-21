"""
Top-level pipeline orchestration
─────────────────────────────────────────────────────────────────
run_profile(profile_name, company, year, report_type, ...) one-shot:
    download -> pdf_to_md -> extract -> save

Stages can also run individually. Log/progress/abort callbacks thread through.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from engine.profile import load_profile, Profile
from engine.download import download as do_download
from engine.convert import pdf_to_md as do_convert, RECOGNIZED_MD_SUFFIX
from engine.extract import extract_all, save_results

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class PipelineResult:
    profile: str
    company: str
    pdf_path: Optional[str] = None
    md_path: Optional[str] = None
    xlsx_path: Optional[str] = None
    json_path: Optional[str] = None
    rows: int = 0
    error: str = ""
    logs: List[str] = field(default_factory=list)


def run_profile(
    profile_name: str,
    company: str,
    year: str,
    report_type: str,
    api_key: str,
    *,
    stages: List[str] = None,
    pdf_path: Optional[str] = None,
    md_path: Optional[str] = None,
    max_workers: int = 5,
    out_root: Optional[str] = None,
    convert_engine: Optional[str] = None,
    text_model: Optional[str] = None,
    vl_model: Optional[str] = None,
    mineru_token: Optional[str] = None,
    log: Callable[[str], None] = print,
    progress: Callable[[float], None] = lambda p: None,
    should_stop: Callable[[], bool] = lambda: False,
) -> PipelineResult:
    stages = stages or ["download", "pdf_to_md", "extract"]
    profile = load_profile(profile_name)
    # runtime overrides take precedence over profile.json
    if text_model:
        profile.llm["text_model"] = text_model
    if vl_model:
        profile.llm["vl_model"] = vl_model
    if convert_engine:
        profile.config.setdefault("convert", {})["engine"] = convert_engine
    if mineru_token:
        profile.config.setdefault("convert", {})["mineru_token"] = mineru_token
    res = PipelineResult(profile=profile_name, company=company)

    out_root = Path(out_root) if out_root else (ROOT / "outputs" / profile_name)
    reports_dir = out_root / "reports"
    extracted_dir = out_root / "extracted"

    def _log(m):
        res.logs.append(m)
        log(m)

    try:
        # ── 1. download ───────────────────────────────────────────
        if "download" in stages:
            _log(f"[download] {company} {year} {report_type}")
            pdf_path = do_download(
                company, year, report_type, profile,
                out_dir=reports_dir, log=_log,
            ) or pdf_path
            res.pdf_path = pdf_path
            if should_stop():
                return res

        # ── 2. pdf_to_md ──────────────────────────────────────────
        if "pdf_to_md" in stages:
            if not pdf_path:
                raise FileNotFoundError("No PDF path — cannot run PDF->MD")
            _log(f"[pdf_to_md] {pdf_path}")
            md_path = do_convert(
                pdf_path, profile, api_key=api_key, engine=convert_engine,
                log=_log,
                progress=lambda p: progress(0.1 + p * 0.4),
                should_stop=should_stop,
            )
            res.md_path = md_path
            if should_stop():
                return res

        # ── 3. extract ────────────────────────────────────────────
        if "extract" in stages:
            if not md_path:
                # try to infer from the pdf using the recognition-output suffix
                if pdf_path:
                    cand = (Path(pdf_path).parent /
                            f"{Path(pdf_path).stem}{RECOGNIZED_MD_SUFFIX}.md")
                    if cand.exists():
                        md_path = str(cand)
            if not md_path or not Path(md_path).exists():
                raise FileNotFoundError(f"No MD file — cannot extract: {md_path}")

            _log(f"[extract] {md_path}")
            md_text = Path(md_path).read_text(encoding="utf-8")
            df = extract_all(
                profile, md_text, api_key,
                max_workers=max_workers, log=_log,
                progress=lambda p: progress(0.5 + p * 0.5),
                should_stop=should_stop,
            )
            safe_company = company.replace("/", "_")
            xlsx = extracted_dir / f"{safe_company}_{year}_{report_type}.xlsx"
            jsonp = extracted_dir / f"{safe_company}_{year}_{report_type}.json"
            save_results(df, str(xlsx), str(jsonp))
            res.xlsx_path = str(xlsx)
            res.json_path = str(jsonp)
            res.rows = len(df)
            _log(f"✅ Extraction done: {len(df)} metrics -> {xlsx.name}")

    except Exception as e:
        import traceback
        res.error = str(e)
        _log(f"❌ Failed: {e}")
        _log(traceback.format_exc())

    return res
