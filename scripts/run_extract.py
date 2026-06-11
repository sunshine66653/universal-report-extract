"""
通用报表提取 — 一键四输出
─────────────────────────────────────────────────────────────────
输入一个 PDF（或已识别的 MD），产出：
  1. <stem>_提取结果.md          识别 Markdown（HTML 表格保留）
  2. <stem>_对照.html            原文 vs 识别结果左右对照（可在线校对、导出修改后 MD）
  3. <stem>_识别结果.docx        识别内容转 Word
  4. <stem>_指标.xlsx            按 profile 规则抽取的指标表（需 API Key）

用法：
  python scripts/run_extract.py 报告.pdf --profile cn_securities
  python scripts/run_extract.py 报告.pdf --no-extract          # 跳过指标抽取
  python scripts/run_extract.py 报告.pdf --md 已有.md          # 复用已识别 MD
环境变量：LLM_API_KEY（指标抽取/图片识别）、MINERU_TOKEN（云端解析）
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_SKILL_ROOT = _SCRIPT_DIR.parent
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="通用报表提取：PDF → MD + 对照HTML + Word + 指标Excel")
    ap.add_argument("pdf", help="原始 PDF 路径")
    ap.add_argument("--profile", default="cn_securities",
                    help="业务画像（默认 cn_securities；用 list 查看全部）")
    ap.add_argument("--md", default=None, help="已识别 MD 路径（提供则跳过解析）")
    ap.add_argument("--engine", default="mineru", choices=["mineru", "docling"],
                    help="PDF 解析引擎（默认云端 mineru）")
    ap.add_argument("--api-key", default=os.getenv("LLM_API_KEY", ""),
                    help="大模型 API Key（指标抽取与图片识别用）")
    ap.add_argument("--mineru-token", default=os.getenv("MINERU_TOKEN", ""),
                    help="MinerU API Token（engine=mineru 时必需）")
    ap.add_argument("--no-extract", action="store_true",
                    help="跳过指标抽取（不需要 API Key）")
    ap.add_argument("--no-docx", action="store_true", help="跳过 Word 输出")
    ap.add_argument("--no-compare", action="store_true", help="跳过对照 HTML")
    ap.add_argument("--out", default="", help="输出目录（默认 PDF 同级 / outputs）")
    ap.add_argument("--workers", type=int, default=5, help="抽取并发数")
    ap.add_argument("--dpi", type=int, default=96, help="对照页渲染 DPI")
    args = ap.parse_args(argv)

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"错误：PDF 不存在：{pdf_path}")
        sys.exit(1)

    from engine.profile import load_profile
    profile = load_profile(args.profile)
    profile.config.setdefault("convert", {})["engine"] = args.engine
    if args.mineru_token:
        profile.config["convert"]["mineru_token"] = args.mineru_token

    out_dir = Path(args.out) if args.out else pdf_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = pdf_path.stem

    results: dict[str, str] = {}

    # ── 1) PDF → MD ──────────────────────────────────────────────────
    if args.md:
        md_path = Path(args.md)
        if not md_path.exists():
            print(f"错误：MD 不存在：{md_path}")
            sys.exit(1)
        print(f"[1/4] 复用已识别 MD：{md_path.name}")
    else:
        if args.engine == "mineru" and not (
                args.mineru_token
                or profile.config["convert"].get("mineru_token")
                or os.getenv("MINERU_TOKEN")):
            print("错误：engine=mineru 需要 MINERU_TOKEN（--mineru-token 或环境变量）")
            sys.exit(1)
        print(f"[1/4] PDF → MD（引擎={args.engine}）...")
        from engine.convert import pdf_to_md
        md_path = Path(pdf_to_md(
            str(pdf_path), profile, api_key=args.api_key, engine=args.engine))
    results["md"] = str(md_path)

    # ── 2) 对照 HTML（核心交付）──────────────────────────────────────
    if not args.no_compare:
        print("[2/4] 生成对照 HTML（可在线校对）...")
        from compare_html import build_compare_html
        html_path = out_dir / f"{stem}_对照.html"
        build_compare_html(pdf_path, md_path, html_path, dpi=args.dpi)
        results["html"] = str(html_path)
    else:
        print("[2/4] 跳过对照 HTML")

    # ── 3) Word ──────────────────────────────────────────────────────
    if not args.no_docx:
        print("[3/4] MD → Word...")
        from md_to_docx import md_to_docx
        docx_path = out_dir / f"{stem}_识别结果.docx"
        md_to_docx(md_path, docx_path, title=stem)
        results["docx"] = str(docx_path)
    else:
        print("[3/4] 跳过 Word")

    # ── 4) 指标抽取 Excel ────────────────────────────────────────────
    if not args.no_extract:
        if not args.api_key:
            print("[4/4] ⚠ 未提供 API Key，跳过指标抽取（--api-key 或 LLM_API_KEY）")
        else:
            print(f"[4/4] 指标抽取（profile={args.profile}，"
                  f"{len(profile.load_rules())} 条规则）...")
            from engine.extract import extract_all, save_results
            md_text = md_path.read_text(encoding="utf-8", errors="ignore")
            df = extract_all(profile, md_text, args.api_key,
                             max_workers=args.workers)
            xlsx_path = out_dir / f"{stem}_指标.xlsx"
            save_results(df, str(xlsx_path),
                         str(out_dir / f"{stem}_指标.json"))
            results["xlsx"] = str(xlsx_path)
            print(f"[4/4] ✅ 指标 Excel：{xlsx_path}（{len(df)} 项）")
    else:
        print("[4/4] 跳过指标抽取")

    print("\n========== 输出 ==========")
    for k, label in (("md", "识别 MD"), ("html", "对照 HTML"),
                     ("docx", "Word"), ("xlsx", "指标 Excel")):
        if k in results:
            print(f"  {label:10s}: {results[k]}")
    return results


if __name__ == "__main__":
    main()
