"""
Feature 2 — Metric extraction (builds on Feature 1: pure OCR)
─────────────────────────────────────────────────────────────────
Given a document (or folder, nested subfolders included) and an EXPLICIT
rule profile, extract the profile's metrics from each file's recognized MD
and write them into one Excel.

Progressive design:
  - If a file has no recognized MD yet, the OCR step (feature 1's engine
    path) runs first automatically. Existing MDs are reused (idempotent).
  - The rule profile must be named explicitly via --profile. There is NO
    default — pure OCR and rule-driven extraction stay separate concerns.

Outputs:
  single file -> <stem>_metrics.xlsx           (one sheet of metric rows)
  folder      -> <out>/metrics_batch.xlsx      sheet "Comparison": wide table
                                               (metric rows × file columns)
                                               + one detail sheet per file

Usage:
  python scripts/extract_metrics.py report.pdf --profile cn_securities \
      --api-key sk-... --mineru-token ...
  python scripts/extract_metrics.py ./folder --profile hk_securities_en \
      --api-key sk-...
"""
from __future__ import annotations

import argparse
import os
import re
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

from ocr import scan_folder, ensure_pdf, ensure_md, SUPPORTED_EXTS  # noqa: E402


# ==============================================================================
# Helpers
# ==============================================================================

def _safe_sheet(name: str, used: set) -> str:
    """Excel sheet names: strip : \\ / ? * [ ], cap at 31 chars, keep unique."""
    s = re.sub(r'[:\\/?*\[\]]', '_', str(name)).strip() or "file"
    s = s[:28]
    base, k = s, 1
    while s in used:
        s = f"{base[:25]}_{k}"
        k += 1
    used.add(s)
    return s


def _fmt_value(val, unit) -> str | None:
    if val in (None, "", "null"):
        return None
    unit = unit or ""
    return f"{val}{(' ' + str(unit)) if unit else ''}"


def build_comparison_table(entity_results: dict):
    """{label: DataFrame} -> wide DataFrame: rows=metrics, cols=[id, name,
    group, label1, label2, ...] with 'value unit' strings."""
    import pandas as pd

    merged = None
    for entity, df in entity_results.items():
        sub = df[["id", "name", "group"]].copy()
        sub[entity] = [
            _fmt_value(df.iloc[i].get("value"), df.iloc[i].get("unit"))
            for i in range(len(df))
        ]
        sub = sub[["id", "name", "group", entity]]
        if merged is None:
            merged = sub
        else:
            merged = merged.merge(sub[["id", entity]], on="id", how="outer")
    if merged is None:
        return pd.DataFrame(columns=["id", "name", "group"])
    return merged


def extract_for_file(src: Path, profile, api_key: str,
                     ocr_profile, max_workers: int = 5,
                     debug_dir: Path | None = None,
                     mode: str = "retrieval", pages: str = ""):
    """OCR if needed, then run rule extraction. Returns a DataFrame.
    mode='retrieval' = per-rule chunk retrieval (default); mode='whole' =
    feed the whole MD (or a --pages range) and extract all metrics at once.
    debug_dir: when set, the final LLM prompt(s) are dumped there
    (same wire format as the original project's _debug_prompts)."""
    from engine.extract import extract_all, extract_whole

    pdf = ensure_pdf(src)
    md_path = ensure_md(pdf, ocr_profile, api_key=api_key)
    md_text = md_path.read_text(encoding="utf-8", errors="ignore")
    if mode == "whole":
        return extract_whole(profile, md_text, api_key, pages=pages or None,
                             debug_dir=debug_dir)
    return extract_all(profile, md_text, api_key, max_workers=max_workers,
                       debug_dir=debug_dir)


# ── per-file rule routing (batch over mixed scenarios) ─────────────────
def load_routes(path: Path):
    """Parse a routing manifest:
        { "default": "cn_securities",
          "routes": [ {"match": "*港股*", "profile": "hk_securities_en"},
                      {"match": "subdir/**", "profile": "cn_securities"} ] }
    Returns (default_profile_or_None, [(glob, profile), ...])."""
    import json
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    default = data.get("default")
    routes = []
    for r in data.get("routes", []):
        g, p = r.get("match"), r.get("profile")
        if g and p:
            routes.append((str(g), str(p)))
    return default, routes


def resolve_profile_name(rel_path: str, routes, default: str) -> str:
    """First route whose glob matches the file's relative path (or its bare
    name) wins; otherwise the default. Matching is case-insensitive."""
    import fnmatch
    name = Path(rel_path).name
    low = rel_path.replace("\\", "/").lower()
    for glob, prof in routes:
        g = glob.replace("\\", "/").lower()
        if fnmatch.fnmatch(low, g) or fnmatch.fnmatch(name.lower(), g):
            return prof
    return default


# ==============================================================================
# CLI
# ==============================================================================

def main(argv=None):
    from engine.profile import list_profiles, load_profile

    available = list_profiles()
    ap = argparse.ArgumentParser(
        description=("Rule-driven metric extraction on top of OCR. The rule "
                     "profile is an explicit, required choice — run "
                     "scripts/ocr.py instead if you only want recognition."))
    ap.add_argument("input", help="a PDF/image file, or a folder to batch-process")
    ap.add_argument("--profile", default="",
                    help=f"rule profile to apply (available: {', '.join(available) or 'none'}). "
                         f"Required unless --route supplies a default")
    ap.add_argument("--route", default="",
                    help="batch mode: a routing manifest (JSON) mapping file "
                         "globs to profiles, so different files use different "
                         "rule sets. {\"default\": \"p\", \"routes\": "
                         "[{\"match\": \"*港股*\", \"profile\": \"hk_securities_en\"}]}")
    ap.add_argument("--mode", default="retrieval", choices=["retrieval", "whole"],
                    help="retrieval (default): per-rule chunk retrieval. "
                         "whole: feed the whole MD (or --pages range) and "
                         "extract all metrics at once, bypassing section_hint")
    ap.add_argument("--pages", default="",
                    help="whole mode only: restrict to a page range, e.g. 5-12 "
                         "(needs page markers in the MD; ignored otherwise)")
    ap.add_argument("--api-key", default=os.getenv("LLM_API_KEY", ""),
                    help="LLM API key (required — extraction is LLM-driven)")
    ap.add_argument("--engine", default="mineru",
                    choices=["mineru", "docling", "fast"],
                    help="OCR engine when a file has no recognized MD yet "
                         "(fast = local coordinate-only, digital-born PDFs)")
    ap.add_argument("--mineru-token", default=os.getenv("MINERU_TOKEN", ""),
                    help="MinerU token (needed only if OCR has to run)")
    ap.add_argument("--out", default="",
                    help="output dir (default: next to the input)")
    ap.add_argument("--no-recursive", action="store_true",
                    help="batch mode: do not descend into subfolders")
    ap.add_argument("--workers", type=int, default=5, help="extraction concurrency")
    ap.add_argument("--debug-prompts", action="store_true",
                    help="dump every rule's final LLM prompt to "
                         "<out>/<file-stem>/_debug_prompts/ for auditing "
                         "(off by default)")
    ap.add_argument("--no-rotate-detect", action="store_true",
                    help="when OCR has to run: skip the landscape-table "
                         "pre-pass (zh documents only, on by default)")
    ap.add_argument("--rotate-osd", action="store_true",
                    help="when OCR has to run: add a Tesseract OSD visual "
                         "second-check on rotation candidates (needs "
                         "pytesseract + tesseract; falls back if absent)")
    ap.add_argument("--no-table-rebuild", action="store_true",
                    help="docling engine: use raw/original TableFormer table "
                         "output instead of the header-anchored rebuild")
    args = ap.parse_args(argv)

    if not args.api_key:
        print("Error: metric extraction needs an LLM API key "
              "(--api-key or env LLM_API_KEY)")
        sys.exit(1)

    # resolve routing: --route manifest (per-file profiles) over --profile
    routes, route_default = [], ""
    if args.route:
        rpath = Path(args.route)
        if not rpath.exists():
            print(f"Error: route manifest not found: {rpath}")
            sys.exit(1)
        route_default, routes = load_routes(rpath)
    default_profile = args.profile or route_default
    if not default_profile:
        print("Error: no profile given. Pass --profile, or a --route manifest "
              "with a \"default\".")
        sys.exit(1)

    # every referenced profile must exist
    referenced = {default_profile} | {p for _, p in routes}
    unknown = [p for p in referenced if p not in available]
    if unknown:
        print(f"Error: unknown profile(s): {', '.join(unknown)}. "
              f"Available: {', '.join(available) or '(none)'}")
        sys.exit(1)

    src = Path(args.input)
    if not src.exists():
        print(f"Error: input not found: {src}")
        sys.exit(1)

    from ocr import make_ocr_profile
    _profile_cache: dict = {}
    _ocr_cache: dict = {}

    def get_profile(name: str):
        if name not in _profile_cache:
            p = load_profile(name)
            # the Excel is the hand-maintained rule surface — materialize one
            # from json if the skill was distributed without it
            p.ensure_rules_excel()
            _profile_cache[name] = p
        return _profile_cache[name]

    def get_ocr_profile(p):
        # OCR uses the rule profile's language but its own engine choice;
        # rules never leak into the OCR step
        if p.language not in _ocr_cache:
            _ocr_cache[p.language] = make_ocr_profile(
                language=p.language, engine=args.engine,
                mineru_token=args.mineru_token,
                rotate_detect=not args.no_rotate_detect,
                rotate_osd=args.rotate_osd,
                docling_table_rebuild=not args.no_table_rebuild,
            )
        return _ocr_cache[p.language]

    dft = get_profile(default_profile)
    print(f"[extract] default profile={default_profile} ({dft.display_name}, "
          f"{len(dft.load_rules())} rules), mode={args.mode}"
          + (f", {len(routes)} route(s)" if routes else ""))

    # work list
    if src.is_dir():
        files = scan_folder(src, recursive=not args.no_recursive)
        if not files:
            print(f"No supported files under {src}")
            sys.exit(1)
        print(f"[extract] batch: {len(files)} file(s)")
    else:
        if src.suffix.lower() not in SUPPORTED_EXTS:
            print(f"Error: unsupported file type: {src.suffix}")
            sys.exit(1)
        files = [src]

    out_dir = Path(args.out) if args.out else (src if src.is_dir() else src.parent)
    out_dir.mkdir(parents=True, exist_ok=True)

    results: dict = {}
    failed: list = []
    for i, f in enumerate(files, 1):
        rel = str(f.relative_to(src)) if src.is_dir() else f.name
        pname = resolve_profile_name(rel, routes, default_profile)
        profile = get_profile(pname)
        tag = f" [{pname}]" if (routes or pname != default_profile) else ""
        print(f"\n[extract] ({i}/{len(files)}) {f.name}{tag}")
        debug_dir = (out_dir / f.stem / "_debug_prompts"
                     if args.debug_prompts else None)
        try:
            df = extract_for_file(f, profile, args.api_key,
                                  get_ocr_profile(profile),
                                  max_workers=args.workers,
                                  debug_dir=debug_dir,
                                  mode=args.mode, pages=args.pages)
            results[f.stem] = df
        except Exception as e:
            failed.append((f, str(e)))
            print(f"  ❌ failed: {e}")

    if not results:
        print("No file extracted successfully.")
        sys.exit(2)

    # write Excel
    import pandas as pd
    if len(files) == 1 and len(results) == 1:
        stem = files[0].stem
        xlsx = out_dir / f"{stem}_metrics.xlsx"
        next(iter(results.values())).to_excel(xlsx, index=False)
    else:
        xlsx = out_dir / "metrics_batch.xlsx"
        comp = build_comparison_table(results)
        used: set = set()
        with pd.ExcelWriter(xlsx) as xw:
            comp.to_excel(xw, index=False,
                          sheet_name=_safe_sheet("Comparison", used))
            for label, df in results.items():
                df.to_excel(xw, index=False,
                            sheet_name=_safe_sheet(label, used))

    print("\n========== Extraction summary ==========")
    print(f"  files extracted: {len(results)}   failed: {len(failed)}")
    print(f"  ✅ Excel: {xlsx}")
    for f, err in failed:
        print(f"  ❌ {f.name}: {err}")
    if failed:
        sys.exit(2)
    return str(xlsx)


if __name__ == "__main__":
    main()
