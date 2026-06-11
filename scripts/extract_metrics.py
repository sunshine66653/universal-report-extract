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
                     ocr_profile, max_workers: int = 5):
    """OCR if needed, then run rule extraction. Returns a DataFrame."""
    from engine.extract import extract_all

    pdf = ensure_pdf(src)
    md_path = ensure_md(pdf, ocr_profile, api_key=api_key)
    md_text = md_path.read_text(encoding="utf-8", errors="ignore")
    return extract_all(profile, md_text, api_key, max_workers=max_workers)


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
    ap.add_argument("--profile", required=True,
                    help=f"rule profile to apply (available: {', '.join(available) or 'none'})")
    ap.add_argument("--api-key", default=os.getenv("LLM_API_KEY", ""),
                    help="LLM API key (required — extraction is LLM-driven)")
    ap.add_argument("--engine", default="mineru", choices=["mineru", "docling"],
                    help="OCR engine when a file has no recognized MD yet")
    ap.add_argument("--mineru-token", default=os.getenv("MINERU_TOKEN", ""),
                    help="MinerU token (needed only if OCR has to run)")
    ap.add_argument("--out", default="",
                    help="output dir (default: next to the input)")
    ap.add_argument("--no-recursive", action="store_true",
                    help="batch mode: do not descend into subfolders")
    ap.add_argument("--workers", type=int, default=5, help="extraction concurrency")
    args = ap.parse_args(argv)

    if args.profile not in available:
        print(f"Error: unknown profile '{args.profile}'. "
              f"Available: {', '.join(available) or '(none)'}")
        sys.exit(1)
    if not args.api_key:
        print("Error: metric extraction needs an LLM API key "
              "(--api-key or env LLM_API_KEY)")
        sys.exit(1)

    src = Path(args.input)
    if not src.exists():
        print(f"Error: input not found: {src}")
        sys.exit(1)

    profile = load_profile(args.profile)
    n_rules = len(profile.load_rules())
    print(f"[extract] profile={args.profile} ({profile.display_name}, "
          f"{n_rules} rules)")

    # the OCR side uses the profile's language but its own engine choice;
    # rules never leak into the OCR step
    from ocr import make_ocr_profile
    ocr_profile = make_ocr_profile(
        language=profile.language, engine=args.engine,
        mineru_token=args.mineru_token,
    )

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
        print(f"\n[extract] ({i}/{len(files)}) {f.name}")
        try:
            df = extract_for_file(f, profile, args.api_key, ocr_profile,
                                  max_workers=args.workers)
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
