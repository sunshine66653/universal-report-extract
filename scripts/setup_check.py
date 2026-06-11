"""
环境自检：依赖 + 凭证 + profile 完整性
用法：python scripts/setup_check.py
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

REQUIRED = ["requests", "pandas", "openpyxl", "pypdf", "fitz",
            "markdown2", "docx", "PIL"]
OPTIONAL = {"docling": "本地 Docling 解析（不装则只能用云端 MinerU）"}


def main() -> int:
    ok = True
    print("── 依赖检查 ──")
    for mod in REQUIRED:
        try:
            importlib.import_module(mod)
            print(f"  ✅ {mod}")
        except ImportError:
            print(f"  ❌ {mod}（pip install -r requirements.txt）")
            ok = False
    for mod, note in OPTIONAL.items():
        try:
            importlib.import_module(mod)
            print(f"  ✅ {mod}（可选）")
        except ImportError:
            print(f"  ⚪ {mod} 未安装 — {note}")

    print("── 凭证检查 ──")
    if os.getenv("MINERU_TOKEN"):
        print("  ✅ MINERU_TOKEN 已设置（云端解析可用）")
    else:
        print("  ⚪ MINERU_TOKEN 未设置 — engine=mineru 时必需")
    if os.getenv("LLM_API_KEY"):
        print("  ✅ LLM_API_KEY 已设置（指标抽取/图片识别可用）")
    else:
        print("  ⚪ LLM_API_KEY 未设置 — 指标抽取与图片识别将跳过")

    print("── Profile 检查 ──")
    try:
        from engine.profile import list_profiles, load_profile
        for name in list_profiles():
            p = load_profile(name)
            try:
                n = len(p.load_rules())
                print(f"  ✅ {name}（{p.display_name}，{n} 条规则）")
            except Exception as e:
                print(f"  ⚠ {name}：规则加载失败 — {e}")
    except Exception as e:
        print(f"  ❌ 引擎加载失败：{e}")
        ok = False

    print("\n" + ("✅ 自检通过" if ok else "❌ 存在缺失，请按提示修复"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
