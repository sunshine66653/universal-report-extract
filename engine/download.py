"""
Download adapters
─────────────────────────────────────────────────────────────────
Unified interface: download(company, year, report_type, profile, out_dir) -> path | None

Built-in adapters:
    cninfo   cninfo.com.cn (China listed-company disclosure site). For A+H
             dual-listed companies, report_language selects the Chinese or
             English edition of the annual report.
    manual   no auto-download; the user drops the PDF into out_dir
             (fallback for HK-only listings / no source)

profile.download.adapter selects the adapter; company_codes can override the
code lookup.

NOTE: the Chinese strings below (report-type names like 年报, cninfo category
ids, title keywords, blacklists) are functional values used by the cninfo API
and title matching against real Chinese announcement titles — do not translate.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Optional

import requests

ROOT = Path(__file__).resolve().parent.parent
LEGACY = ROOT / "legacy"
for p in (str(ROOT), str(LEGACY)):
    if p not in sys.path:
        sys.path.insert(0, p)


_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}


# report type (annual/semi-annual/Q1/Q3) -> cninfo category id
_CNINFO_CATEGORY = {
    "年报": "category_ndbg_szsh",
    "半年报": "category_bndbg_szsh",
    "一季报": "category_yjdbg_szsh",
    "三季报": "category_sjdbg_szsh",
}
# official-title keywords per report type (match real announcement titles)
_OFFICIAL_KW = {
    "年报": ["年度报告"], "半年报": ["半年度报告"],
    "一季报": ["第一季度", "一季度"], "三季报": ["第三季度", "三季度"],
}
# markers commonly present in English-edition report titles
_EN_TITLE_KW = ["英文", "english", "annual report", "(h share)", "h股"]


def _cninfo_stock_info(company_name: str, profile=None):
    # profile-provided company_codes take precedence
    if profile is not None:
        codes = profile.download.get("company_codes", {})
        if company_name in codes and len(codes[company_name]) >= 2:
            return codes[company_name][0], codes[company_name][1]
    url = "http://www.cninfo.com.cn/new/information/topSearch/query"
    try:
        r = requests.post(url, headers=_HEADERS,
                          data={"keyWord": company_name, "maxNum": 10}, timeout=10)
        for item in r.json():
            if company_name in (item.get("zwjc") or ""):
                return item.get("code"), item.get("orgId")
    except Exception:
        pass
    return None, None


def _cninfo_search(stock_code, org_id, year, report_type, want_english: bool,
                   log: Callable[[str], None]):
    """Returns the announcement adjunctUrl. want_english=True prefers the
    English edition."""
    url = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
    payload = {
        "pageNum": 1, "pageSize": 30, "column": "szse", "tabName": "fulltext",
        "stock": f"{stock_code},{org_id}",
        "category": _CNINFO_CATEGORY.get(report_type, "category_ndbg_szsh"),
        "seDate": f"{year}-01-01~{int(year)+1}-06-30",
        "isHLG": "true",
    }
    try:
        r = requests.post(url, headers=_HEADERS, data=payload, timeout=15)
        anns = r.json().get("announcements", []) or []
    except Exception as e:
        log(f"⚠ cninfo search error: {e}")
        return None

    official = _OFFICIAL_KW.get(report_type, ["年度报告"])
    # blacklist (both languages): summaries, advisories, flash reports, ...
    common_black = ["摘要", "提示性", "披露", "预案", "业绩快报", "说明", "summary"]

    def _title(a): return a.get("announcementTitle", "") or ""

    if want_english:
        # English edition: title carries an English marker; the Chinese
        # "annual report" keyword is not required
        for a in anns:
            t = _title(a).lower()
            if str(year) in _title(a) and any(k in t for k in _EN_TITLE_KW) \
               and not any(b in _title(a).lower() for b in common_black):
                log(f"English edition matched: {_title(a)}")
                return a.get("adjunctUrl")
        log("⚠ English edition not found, falling back to Chinese")

    # Chinese edition (default): official keyword required; English/summary excluded
    cn_black = common_black + ["英文", "english"]
    for a in anns:
        title = _title(a)
        if str(year) in title and any(kw in title for kw in official) \
           and not any(b in title.lower() for b in [x.lower() for x in cn_black]):
            log(f"Chinese edition matched: {title}")
            return a.get("adjunctUrl")
    return None


def download_cninfo(
    company: str, year: str, report_type: str, profile, out_dir: Path,
    log: Callable[[str], None] = print,
) -> Optional[str]:
    """cninfo download; report_language=en prefers the English annual report
    (works for A+H dual-listed companies)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    want_en = profile.download.get("report_language", "zh") == "en"

    stock_code, org_id = _cninfo_stock_info(company, profile)
    if not stock_code:
        log(f"⚠ could not resolve a stock code for {company}")
        return None
    log(f"cninfo search: {company}({stock_code}) {year} {report_type} "
        f"language={'en' if want_en else 'zh'}")

    suffix = _cninfo_search(stock_code, org_id, year, report_type, want_en, log)
    if not suffix:
        log("⚠ cninfo: full report not found")
        return None

    lang_tag = "_EN" if want_en else ""
    target = out_dir / f"{company}{year}{report_type}{lang_tag}.pdf"
    if target.exists() and target.stat().st_size > 1024:
        log(f"✅ file already exists: {target.name}")
        return str(target)

    try:
        pdf_url = f"http://static.cninfo.com.cn/{suffix}"
        resp = requests.get(pdf_url, headers=_HEADERS, stream=True, timeout=60)
        with open(target, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        log(f"✅ downloaded -> {target}")
        return str(target)
    except Exception as e:
        log(f"❌ download failed: {e}")
        return None


def download_manual(
    company: str, year: str, report_type: str, profile, out_dir: Path,
    log: Callable[[str], None] = print,
) -> Optional[str]:
    """No auto-download: look for a user-provided PDF in out_dir by
    conventional names."""
    out_dir.mkdir(parents=True, exist_ok=True)
    patterns = [
        f"*{company}*{year}*.pdf",
        f"*{company}*.pdf",
        f"*{year}*{report_type}*.pdf",
    ]
    for pat in patterns:
        hits = list(out_dir.glob(pat))
        if hits:
            log(f"✅ found user-provided file: {hits[0].name}")
            return str(hits[0])
    log(f"⚠ manual adapter: place the PDF into {out_dir} "
        f"(filename should contain '{company}' and '{year}')")
    return None


_ADAPTERS = {
    "cninfo": download_cninfo,
    "manual": download_manual,
}


def download(
    company: str, year: str, report_type: str, profile,
    out_dir: str | Path = None,
    log: Callable[[str], None] = print,
) -> Optional[str]:
    adapter_name = profile.download.get("adapter", "cninfo")
    fn = _ADAPTERS.get(adapter_name)
    if fn is None:
        raise ValueError(f"Unknown download adapter: {adapter_name} "
                         f"(available: {list(_ADAPTERS)})")
    if out_dir is None:
        out_dir = ROOT / "outputs" / "reports" / profile.name
    return fn(company, year, report_type, profile, Path(out_dir), log)
