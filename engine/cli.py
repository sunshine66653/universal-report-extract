"""
Command-line entry
─────────────────────────────────────────────────────────────────
Examples:

  # list all profiles
  python -m engine.cli list

  # convert a profile's Excel rules to JSON only (no extraction)
  python -m engine.cli rules hk_securities_en

  # full pipeline (download -> PDF->MD -> extract)
  python -m engine.cli run hk_securities_en --company 中信证券 --year 2024 \
      --report 年报 --api-key sk-xxx

  # MD already exists: extraction only
  python -m engine.cli run hk_securities_en --company 中信证券 --year 2024 \
      --report 年报 --md path/to/report.md --stages extract --api-key sk-xxx

  # PDF already exists: convert + extract
  python -m engine.cli run hk_securities_en --company 中信证券 --year 2024 \
      --report 年报 --pdf path/to/report.pdf --stages pdf_to_md,extract --api-key sk-xxx

NOTE: --report takes Chinese report-type values (年报 / 半年报 / 一季报 / 三季报)
because they map to cninfo's category ids — see engine.download.
"""
from __future__ import annotations

import argparse
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

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="engine.cli",
                                 description="Rule-driven report extraction engine")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list all profiles")

    p_rules = sub.add_parser(
        "rules",
        help="refresh a profile's rules.json from its Excel; if the profile "
             "has no Excel (json-only distribution), a hand-editable one is "
             "generated from the json first")
    p_rules.add_argument("profile")
    p_rules.add_argument("--force", action="store_true", help="force re-conversion")

    p_check = sub.add_parser(
        "check",
        help="validate rules (duplicate/missing ids, bad field types, ...) — "
             "run this after hand-editing a rules.json")
    p_check.add_argument("target",
                         help="profile name, or a path to a rules .json/.xlsx")

    p_run = sub.add_parser("run", help="run the pipeline")
    p_run.add_argument("profile")
    p_run.add_argument("--company", required=True)
    p_run.add_argument("--year", required=True)
    p_run.add_argument("--report", default="年报",
                       help="report type (年报/半年报/一季报/三季报)")
    p_run.add_argument("--api-key", default=os.getenv("LLM_API_KEY", ""))
    p_run.add_argument("--pdf", default=None)
    p_run.add_argument("--md", default=None)
    p_run.add_argument("--stages", default="download,pdf_to_md,extract")
    p_run.add_argument("--workers", type=int, default=5)

    args = ap.parse_args(argv)

    if args.cmd == "list":
        from engine import list_profiles, load_profile
        for name in list_profiles():
            p = load_profile(name)
            n = len(p.load_rules())
            print(f"{name:24s}  {p.display_name}  (lang={p.language}, rules={n})")
        return

    if args.cmd == "rules":
        from engine.profile import load_profile
        p = load_profile(args.profile)
        p.ensure_rules_excel()
        rules = p.load_rules(force_convert=args.force)
        print(f"{args.profile}: {len(rules)} enabled rules -> "
              f"{p.rules_path().with_suffix('.json')}")
        return

    if args.cmd == "check":
        from engine.rules_excel import load_rules_raw, validate_rules
        target = Path(args.target)
        if not target.exists():
            from engine.profile import load_profile
            p = load_profile(args.target)
            target = p.rules_path()
            if not target.exists():
                cands = (list((p.dir / "rules").glob("*.xlsx"))
                         + list((p.dir / "rules").glob("*.json")))
                if not cands:
                    print(f"Error: profile '{args.target}' has no rules file")
                    sys.exit(1)
                target = cands[0]
        rules = load_rules_raw(target)
        problems = validate_rules(rules)
        n_on = sum(1 for r in rules
                   if isinstance(r, dict) and r.get("enabled", True))
        print(f"{target}: {len(rules)} rules ({n_on} enabled)")
        if problems:
            for msg in problems:
                print(f"  ✗ {msg}")
            print(f"\n{len(problems)} problem(s) found.")
            sys.exit(1)
        print("  ✓ no problems found")
        return

    if args.cmd == "run":
        from engine.pipeline import run_profile
        stages = [s.strip() for s in args.stages.split(",") if s.strip()]
        res = run_profile(
            args.profile, company=args.company, year=args.year,
            report_type=args.report, api_key=args.api_key,
            stages=stages, pdf_path=args.pdf, md_path=args.md,
            max_workers=args.workers,
        )
        print("\n========== Result ==========")
        print(f"PDF : {res.pdf_path}")
        print(f"MD  : {res.md_path}")
        print(f"XLSX: {res.xlsx_path}  ({res.rows} metrics)")
        if res.error:
            print(f"ERROR: {res.error}")
            sys.exit(1)


if __name__ == "__main__":
    main()
