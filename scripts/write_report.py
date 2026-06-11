"""
Feature 3 — Report mimicry (builds on Features 1 + 2)
─────────────────────────────────────────────────────────────────
Given a SAMPLE report (the style/metric template) and one or more research
MATERIALS that need OCR, this script:

  1. OCRs every material that has no recognized MD yet (feature 1 path)
  2. Parses the sample into an outline (sections + body text)
  3. Asks the LLM to infer ONE shared metric-rule set from the sample,
     with locator fields aligned to the materials' actual languages/terms
     (so a Chinese sample works against English materials and vice versa)
  4. Extracts those metrics from every material (feature 2 path, using the
     inferred rules directly — no profile needs to be saved)
  5. Writes a Word report that mimics the sample's structure and tone,
     filled with the extracted values (multi-material -> comparative
     narrative), plus a metrics Excel for review

Outputs (to --out, default ./report_out):
  report_<timestamp>.docx        the mimicked report
  report_<timestamp>_metrics.xlsx extracted values (wide sheet when multiple
                                  materials, plus per-material detail sheets)
  inferred_rules.json            the rule set the LLM derived (for audit/reuse)

Usage:
  python scripts/write_report.py --sample sample.docx material1.pdf material2.pdf \
      --api-key sk-... --mineru-token ...
  python scripts/write_report.py --sample sample.md ./materials_folder \
      --api-key sk-... --save-profile my_rules
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path

# Windows GBK console compatibility: switch stdout/stderr to UTF-8
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

_SCRIPT_DIR = Path(__file__).resolve().parent
_SKILL_ROOT = _SCRIPT_DIR.parent
for p in (str(_SKILL_ROOT), str(_SCRIPT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from ocr import (  # noqa: E402
    scan_folder, ensure_pdf, ensure_md, make_ocr_profile, SUPPORTED_EXTS,
)
from extract_metrics import build_comparison_table, _safe_sheet  # noqa: E402


def _collect_materials(inputs: list[str], recursive: bool) -> list[Path]:
    files: list[Path] = []
    for raw in inputs:
        p = Path(raw)
        if not p.exists():
            print(f"Error: material not found: {p}")
            sys.exit(1)
        if p.is_dir():
            files.extend(scan_folder(p, recursive=recursive))
        elif p.suffix.lower() in SUPPORTED_EXTS or \
                p.suffix.lower() in (".md", ".markdown"):
            files.append(p)
        else:
            print(f"Error: unsupported material type: {p}")
            sys.exit(1)
    # dedupe, keep order
    seen, out = set(), []
    for f in files:
        key = str(f.resolve()).lower()
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=("Report mimicry: learn which metrics a sample report "
                     "needs, extract them from your research materials "
                     "(OCR runs automatically when needed), then write a "
                     "report in the sample's style."))
    ap.add_argument("materials", nargs="+",
                    help="one or more research files (PDF/image/recognized MD) "
                         "or folders")
    ap.add_argument("--sample", required=True,
                    help="sample report (.docx / .pdf / .md) — defines the "
                         "metric requirements and the writing style")
    ap.add_argument("--api-key", default=os.getenv("LLM_API_KEY", ""),
                    help="LLM API key (required: inference, extraction, writing)")
    ap.add_argument("--engine", default="mineru", choices=["mineru", "docling"],
                    help="OCR engine for materials/PDF samples without an MD")
    ap.add_argument("--mineru-token", default=os.getenv("MINERU_TOKEN", ""),
                    help="MinerU token (needed only if OCR has to run)")
    ap.add_argument("--base-profile", default="cn_securities",
                    help="profile supplying engine config (chunking/LLM/base_url) "
                         "— its RULES are NOT used; inferred rules replace them")
    ap.add_argument("--text-model", default="",
                    help="override the text model (default from base profile)")
    ap.add_argument("--save-profile", default="",
                    help="optionally persist the inferred rules as a new "
                         "profile under profiles/<name>/ for reuse")
    ap.add_argument("--title", default="", help="report title (docx heading)")
    ap.add_argument("--out", default="report_out", help="output directory")
    ap.add_argument("--no-recursive", action="store_true",
                    help="folders: do not descend into subfolders")
    ap.add_argument("--workers", type=int, default=5, help="extraction concurrency")
    args = ap.parse_args(argv)

    if not args.api_key:
        print("Error: this feature is LLM-driven end to end — provide "
              "--api-key or env LLM_API_KEY")
        sys.exit(1)

    from engine.profile import load_profile, list_profiles
    if args.base_profile not in list_profiles():
        print(f"Error: unknown base profile '{args.base_profile}'. "
              f"Available: {', '.join(list_profiles())}")
        sys.exit(1)
    base = load_profile(args.base_profile)
    if args.text_model:
        base.llm["text_model"] = args.text_model
    text_model = base.llm.get("text_model", "qwen-plus")
    base_url = base.llm.get("base_url")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    materials = _collect_materials(args.materials, recursive=not args.no_recursive)
    if not materials:
        print("Error: no usable materials found")
        sys.exit(1)
    print(f"[report] {len(materials)} material(s); sample: {args.sample}")

    ocr_profile = make_ocr_profile(
        language=base.language, engine=args.engine,
        mineru_token=args.mineru_token,
    )

    # ── 1) make sure every material has a recognized MD ─────────────────
    entity_mds: dict[str, Path] = {}     # label -> md path
    for m in materials:
        if m.suffix.lower() in (".md", ".markdown"):
            entity_mds[m.stem] = m
            continue
        pdf = ensure_pdf(m)
        md = ensure_md(pdf, ocr_profile, api_key=args.api_key)
        entity_mds[m.stem] = md
    print(f"[report] recognized MDs ready for {len(entity_mds)} material(s)")

    # ── 2) parse the sample ──────────────────────────────────────────────
    from engine.report_writer import (
        parse_sample, infer_rules_from_sample, save_as_profile,
        write_report_docx, build_metric_records_multi, metrics_records_from_df,
    )
    outline, sample_text = parse_sample(
        args.sample, profile=ocr_profile, api_key=args.api_key,
        convert_engine=args.engine,
    )
    print(f"[report] sample outline: {len(outline)} section(s)")

    # ── 3) infer one shared rule set, aligned to material terminology ───
    mats_for_infer = [
        (label, p.read_text(encoding="utf-8", errors="ignore"))
        for label, p in entity_mds.items()
    ]
    rules, material_lang = infer_rules_from_sample(
        sample_text, api_key=args.api_key, base_url=base_url,
        text_model=text_model, materials=mats_for_infer,
    )
    if not rules:
        print("Error: no rules inferred from the sample")
        sys.exit(2)
    rules_json = out_dir / "inferred_rules.json"
    rules_json.write_text(json.dumps(rules, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    print(f"[report] inferred rules saved for audit: {rules_json}")

    if args.save_profile:
        saved = save_as_profile(
            rules, args.save_profile, base_profile=args.base_profile,
            language=material_lang or base.language, overwrite=False,
        )
        print(f"[report] rules persisted as profile: {saved}")

    # ── 4) extract the inferred metrics from every material ─────────────
    from engine.extract import extract_all
    entity_results: dict = {}
    for i, (label, md_path) in enumerate(entity_mds.items(), 1):
        print(f"\n[report] extracting ({i}/{len(entity_mds)}): {label}")
        md_text = Path(md_path).read_text(encoding="utf-8", errors="ignore")
        df = extract_all(base, md_text, args.api_key,
                         max_workers=args.workers, rules=rules)
        entity_results[label] = df

    # metrics Excel
    import pandas as pd
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    metrics_xlsx = out_dir / f"report_{ts}_metrics.xlsx"
    if len(entity_results) == 1:
        next(iter(entity_results.values())).to_excel(metrics_xlsx, index=False)
    else:
        comp = build_comparison_table(entity_results)
        used: set = set()
        with pd.ExcelWriter(metrics_xlsx) as xw:
            comp.to_excel(xw, index=False,
                          sheet_name=_safe_sheet("Comparison", used))
            for label, df in entity_results.items():
                df.to_excel(xw, index=False,
                            sheet_name=_safe_sheet(label, used))
    print(f"[report] metrics Excel: {metrics_xlsx}")

    # ── 5) write the mimicked report ─────────────────────────────────────
    rule_aliases = {r["id"]: r.get("aliases", []) for r in rules}
    if len(entity_results) == 1:
        records = metrics_records_from_df(next(iter(entity_results.values())))
        for rec in records:
            rec["aliases"] = rule_aliases.get(rec.get("id"), [])
    else:
        records = build_metric_records_multi(entity_results, rule_aliases)

    docx_path = out_dir / f"report_{ts}.docx"
    write_report_docx(
        outline, records,
        out_path=str(docx_path),
        api_key=args.api_key, base_url=base_url, text_model=text_model,
        title=args.title or f"Report ({ts})",
    )

    print("\n========== Report-writing summary ==========")
    print(f"  sample sections : {len(outline)}")
    print(f"  inferred rules  : {len(rules)}  -> {rules_json.name}")
    print(f"  materials       : {len(entity_results)}")
    print(f"  ✅ report  : {docx_path}")
    print(f"  ✅ metrics : {metrics_xlsx}")
    return str(docx_path)


if __name__ == "__main__":
    main()
