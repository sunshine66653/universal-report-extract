"""
MinerU cloud API adapter (PDF -> Markdown)
─────────────────────────────────────────────────────────────────
Capabilities:
  1. Large PDFs are auto-split into batches of <=200 pages (MinerU per-task limit)
  2. Upload flow: request presigned URLs -> PUT upload -> poll -> download result zip
  3. Parse each batch result (full.md + images/) and merge the per-batch markdown
     using physical-slice markers
  4. HTML <table> blocks are preserved (incl. rowspan/colspan — easier for LLMs
     to read complex tables than pipe syntax)
  5. Unrecognized ![](images/xxx) figures in the md are described by a VL model
     and replaced in place
  6. Heading re-leveling and chunking happen upstream (engine.convert / chunking)

Official v4 API (https://mineru.net/apiManage/docs):
  POST /api/v4/file-urls/batch         request upload URLs (returns batch_id + file_urls)
  PUT  <presigned_url>                 upload PDF bytes
  GET  /api/v4/extract-results/batch/{batch_id}   poll for full_zip_url

NOTE: The physical-slice marker "# --- PDF 物理切片：第 X - Y 页 ---" is a wire
format shared by the chunker, the comparison-report generator, and profile
regex configs. It is intentionally Chinese — do NOT translate it.
"""
from __future__ import annotations

import io
import os
import re
import time
import zipfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import requests

MINERU_BASE = os.getenv("MINERU_BASE", "https://mineru.net")
PAGE_LIMIT = 200


# ════════════════════════════════════════════════════════════════════════
#  PDF batching
# ════════════════════════════════════════════════════════════════════════
def split_pdf(pdf_path: str, out_dir: Path, page_limit: int = PAGE_LIMIT
              ) -> List[Tuple[Path, int, int]]:
    """Split into <=page_limit-page sub-PDFs. Returns [(sub_pdf, start_1based, end_1based)].
    Even when total pages <= page_limit, the file is copied into out_dir under an
    ASCII name part_*.pdf — non-ASCII source filenames can make MinerU's
    file-urls/batch request fail."""
    import shutil
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(pdf_path)
    total = len(reader.pages)
    out_dir.mkdir(parents=True, exist_ok=True)
    parts: List[Tuple[Path, int, int]] = []

    if total <= page_limit:
        part_path = out_dir / f"part_1_{total}.pdf"
        shutil.copyfile(pdf_path, part_path)
        return [(part_path, 1, total)]

    for start in range(0, total, page_limit):
        end = min(start + page_limit, total)
        writer = PdfWriter()
        for i in range(start, end):
            writer.add_page(reader.pages[i])
        part_path = out_dir / f"part_{start+1}_{end}.pdf"
        with open(part_path, "wb") as f:
            writer.write(f)
        parts.append((part_path, start + 1, end))
    return parts


# ════════════════════════════════════════════════════════════════════════
#  MinerU API client
# ════════════════════════════════════════════════════════════════════════
class MinerUClient:
    def __init__(self, token: str, language: str = "en",
                 model_version: str = "vlm",
                 log: Callable[[str], None] = print):
        if not token:
            raise ValueError("Missing MinerU API token")
        self.token = token
        self.language = language
        self.model_version = model_version
        self.log = log
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }

    # ── request upload URLs ───────────────────────────────────────
    def request_upload(self, filenames: List[str]) -> Tuple[str, List[str]]:
        url = f"{MINERU_BASE}/api/v4/file-urls/batch"
        # MinerU language codes: English "en" / Chinese "ch"
        lang = "en" if self.language == "en" else "ch"
        data = {
            "enable_formula": True,
            "enable_table": True,
            "language": lang,
            "model_version": self.model_version,
            "files": [{"name": n, "is_ocr": True, "data_id": Path(n).stem}
                      for n in filenames],
        }
        r = requests.post(url, headers=self.headers, json=data, timeout=60)
        r.raise_for_status()
        res = r.json()
        if res.get("code") not in (0, 200):
            raise RuntimeError(f"MinerU upload request failed: {res.get('msg')} | {res}")
        d = res["data"]
        batch_id = d["batch_id"]
        file_urls = d.get("file_urls") or d.get("fileUrls") or []
        if not file_urls:
            raise RuntimeError(f"MinerU returned no upload URLs: {res}")
        return batch_id, file_urls

    # ── upload file (PUT, no auth header) ─────────────────────────
    def upload_file(self, presigned_url: str, file_path: Path):
        with open(file_path, "rb") as f:
            # Presigned direct upload — must NOT carry the Authorization header
            resp = requests.put(presigned_url, data=f, timeout=600)
        if resp.status_code not in (200, 201, 204):
            raise RuntimeError(f"Upload failed {file_path.name}: "
                               f"{resp.status_code} {resp.text[:200]}")

    # ── poll for results ──────────────────────────────────────────
    def poll_results(self, batch_id: str, n_files: int,
                     timeout: int = 1800, interval: int = 8,
                     should_stop: Callable[[], bool] = lambda: False
                     ) -> List[Dict]:
        url = f"{MINERU_BASE}/api/v4/extract-results/batch/{batch_id}"
        start = time.time()
        while True:
            if should_stop():
                raise RuntimeError("Aborted by user")
            if time.time() - start > timeout:
                raise TimeoutError(f"MinerU parsing timed out ({timeout}s)")
            r = requests.get(url, headers=self.headers, timeout=60)
            r.raise_for_status()
            res = r.json()
            data = res.get("data", {}) or {}
            results = (data.get("extract_result") or data.get("extractResult")
                       or data.get("results") or [])
            done, failed, running = [], [], 0
            for item in results:
                state = (item.get("state") or item.get("status") or "").lower()
                if state in ("done", "success", "finished"):
                    done.append(item)
                elif state in ("failed", "error"):
                    failed.append(item)
                else:
                    running += 1
            self.log(f"  MinerU progress: done {len(done)}/{n_files}"
                     f", running {running}, failed {len(failed)}")
            if failed:
                msgs = [it.get('err_msg') or it.get('msg') or it for it in failed]
                raise RuntimeError(f"MinerU task(s) failed: {msgs}")
            if len(done) >= n_files and running == 0:
                return done
            time.sleep(interval)

    # ── download and extract result zip ───────────────────────────
    def fetch_zip(self, zip_url: str, extract_to: Path) -> Path:
        extract_to.mkdir(parents=True, exist_ok=True)
        r = requests.get(zip_url, timeout=600)
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            zf.extractall(extract_to)
        return extract_to


# ════════════════════════════════════════════════════════════════════════
#  Figure recognition via VL model (replaces ![](images/xxx) placeholders)
# ════════════════════════════════════════════════════════════════════════
_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


def _analyze_image(img_path: Path, api_key: str, base_url: str,
                   vl_model: str, prompt: str, log: Callable[[str], None],
                   context_text: str = "") -> str:
    import base64
    try:
        from PIL import Image
        buf = io.BytesIO()
        im = Image.open(img_path)
        if im.mode in ("RGBA", "P"):
            im = im.convert("RGB")
        im.save(buf, format="JPEG", quality=92)
        b64 = base64.b64encode(buf.getvalue()).decode()
    except Exception:
        b64 = base64.b64encode(img_path.read_bytes()).decode()

    # Append surrounding document text so the model has chart context.
    # The header line is Chinese on purpose — it belongs to the tested prompt.
    dynamic_prompt = prompt
    if context_text:
        dynamic_prompt += f"\n\n【❗极其重要：图表上下文补充信息】\n{context_text}"

    payload = {
        "model": vl_model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": dynamic_prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }],
        "max_tokens": 2000,
    }
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {api_key}"}
    r = requests.post(base_url.rstrip("/") + "/chat/completions",
                      headers=headers, json=payload, timeout=200)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"].strip()
    content = re.sub(r"^```(?:markdown|md)?\s*", "", content, flags=re.I)
    content = re.sub(r"\s*```$", "", content).strip()
    return content


# ── Chart-analysis prompt for the VL model ──────────────────────────────
# Intentionally written in Chinese: it is tuned for Chinese financial-report
# charts and battle-tested against digit-confusion issues (6 vs 8). Do NOT
# translate — the prompt language is part of its tested behavior.
_EMBEDDED_CHART_PROMPT = """
请作为专业的金融图表分析师，提取这张图片中的核心数据。请务必遵循以下极其严格的提取规则：

1. **识别与过滤**：如果图片是普通照片、无数据价值的装饰插图，直接且仅输出：2. **图例与坐标系映射（极其重要，必须遵守）**：
   - **强制图例映射**：识别图表中的图例（Legend），必须准确捕捉图例的文本（如"营业收入"、"同比增速"）。在提取的数据表中，**必须强制使用识别到的图例真实文本作为列名或类别名**。
   - **严禁使用方位/视觉词汇**：绝对不允许在表格中使用"上部数据"、"中部数据"、"左侧"、"深色柱子"、"浅色折线"等描述物理空间或颜色的词汇作为数据类别名称。必须将其还原为图例表示的真实业务指标名称！
   - **单位提取**：仔细留意坐标轴顶部或图例旁的单位标识（如"亿元"、"万吨"、"%"），并将其合并到对应的表头名称中，例如写成"净利润(亿元)"。
   - **⚠️ 极易混淆数字校验（白色/浅色字体）**：图表中的白色或浅色小字体极易产生视觉发光效应，导致数字「6」和「8」互相混淆！请务必仔细辨认，并强制结合**上下文业务逻辑**（如：总计等于分项之和）或**前后年份趋势连贯性**进行数学逻辑上的二次交叉验证，坚决杜绝 6 和 8 看错的情况！

3. **数据提取规范**：
   - 对于柱状图/折线图：通常将横坐标（X轴）的类别（如年份/月份）作为表格的第一列，各个图例对应的指标作为后续的列名。交叉提取对应数值。若无数据标签，请根据Y轴刻度精确估算。
   - 对于饼图：提取每个扇区的标签名称及对应百分比/数值，形成如 `类别 | 占比(%)` 的两列表格。
   - 对于图片表格：忠实还原原始表头和所有单元格数据，处理好合并单元格的逻辑。
   - ⚠️ 【极度重要】：无论数字有多长（哪怕达到十亿、百亿级别且带有两位小数），【绝对禁止】使用科学计数法（例如 1.2e9）或任何形式的省略！你必须一字不差地输出图片中的每一位数字。如果图片中是 1234567890.12，你必须完整抄写 1234567890.12

4. **强制输出格式**：
   - 只输出 Markdown 格式的表格（如果是包含多个子图的复杂图片，可分别输出多个表格，以空行分隔）。
   - 绝对不要输出任何前言、后语、分析过程或解释性文字。
"""


def get_local_chart_prompt() -> str:
    """Return the chart-analysis prompt for the VL model.
    (The standalone skill always uses the embedded copy; the original project
    optionally read it from a legacy module.)"""
    return _EMBEDDED_CHART_PROMPT


def find_image_refs(md_text: str) -> List[str]:
    """Return all image reference paths in the md (deduped, order preserved)."""
    seen, out = set(), []
    for m in _IMG_RE.finditer(md_text):
        rel = m.group(1).strip()
        if rel not in seen:
            seen.add(rel)
            out.append(rel)
    return out


_IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}


def collect_image_roots(md_path: Path,
                        explicit_root: Optional[Path] = None) -> List[Path]:
    """
    Collect every directory that may hold referenced images — handles the
    "large PDF split into batches -> multiple result_*/images dirs" case.
    Priority: explicit root > sibling *_mineru work dirs (incl. all result_*
    subdirs) > the md's own directory.

    NOTE: "_提取结果" below is the fixed Chinese filename suffix the engine
    appends to recognition output ("<stem>_提取结果.md"). It is a wire format —
    do not translate.
    """
    roots: List[Path] = []

    def _add(p: Optional[Path]):
        if p and p.exists() and p not in roots:
            roots.append(p)

    _add(explicit_root)
    md_dir = md_path.parent
    # every sibling *_mineru work dir
    for wd in sorted(md_dir.glob("*_mineru")):
        _add(wd)
    # work dir named after the md stem (minus the output suffix)
    stem = md_path.stem.replace("_提取结果", "")
    _add(md_dir / f"{stem}_mineru")
    _add(md_dir)
    return roots


def build_image_index(roots: List[Path],
                      log: Callable[[str], None] = lambda *_: None
                      ) -> Dict[str, Path]:
    """
    Build a {filename: path} index across all roots in one pass (avoids
    per-image rglob). Spans multiple result_*/images dirs; on duplicate names
    the first hit wins (content-hash names rarely collide).
    """
    index: Dict[str, Path] = {}
    for root in roots:
        if not root or not root.exists():
            continue
        for p in root.rglob("*"):
            if p.is_file() and p.suffix.lower() in _IMG_EXTS:
                index.setdefault(p.name, p)
    if index:
        log(f"  Image index: {len(index)} files across {len(roots)} search roots")
    return index


def count_physical_images(roots: List[Path]) -> int:
    """Count distinct image files under the search roots."""
    return len(build_image_index(roots))


def _resolve_image(rel: str, index: Dict[str, Path],
                   search_roots: List[Path]) -> Optional[Path]:
    """Resolve by filename via the index first; fall back to relative paths."""
    name = Path(rel).name
    if name in index:
        return index[name]
    for root in search_roots:
        if not root:
            continue
        cand = root / rel
        if cand.exists():
            return cand
        cand = root / "images" / name
        if cand.exists():
            return cand
    return None


def archive_images_in_text(
    md_text: str,
    search_roots: List[Path],
    api_key: str,
    base_url: str,
    vl_model: str,
    *,
    context_chars: int = 600,
    max_workers: int = 4,
    log: Callable[[str], None] = print,
    progress: Callable[[float], None] = lambda p: None,
    should_stop: Callable[[], bool] = lambda: False,
) -> Tuple[str, int, int]:
    """
    Replace ![](images/xxx) placeholders in the md with VL recognition output.

    Each image lands in one of 4 buckets; failures never overwrite content:
      ok      data table recognized      -> placeholder replaced with the table
      empty   model judged it decorative -> placeholder removed
      failed  call error (403/timeout)   -> ORIGINAL ![](images/..) link kept
      missing image file not found       -> original link kept

    Returns (new_md, ok_count, total, stats) where stats counts each bucket.
    Requires api_key — caller must guard.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not api_key:
        raise ValueError("Figure recognition requires an LLM API key")

    refs = find_image_refs(md_text)
    total = len(refs)
    if total == 0:
        return md_text, 0, 0, {"ok": 0, "empty": 0, "failed": 0, "missing": 0}

    prompt = get_local_chart_prompt()
    # one-pass cross-directory image index (handles multi-batch result dirs)
    index = build_image_index(search_roots, log=log)
    log(f"  {total} image refs in MD — starting VL recognition...")

    # each ref -> (status, text)
    results: Dict[str, Tuple[str, str]] = {}
    _err_samples: List[str] = []

    def _work(rel: str) -> Tuple[str, str, str]:
        if should_stop():
            return rel, "failed", ""
        img_path = _resolve_image(rel, index, search_roots)
        if img_path is None:
            log(f"  ⚠ image file not found, keeping link: {rel}")
            return rel, "missing", ""
        idx = md_text.find(rel)
        ctx = ""
        if idx > 0:
            ctx = md_text[max(0, idx - context_chars):idx]
            ctx = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", ctx).strip()[-context_chars:]
        try:
            text = _analyze_image(img_path, api_key, base_url, vl_model,
                                  prompt, log, context_text=ctx)
        except Exception as e:
            if len(_err_samples) < 3:
                _err_samples.append(str(e))
            log(f"  ⚠ recognition failed, keeping link: {Path(rel).name}: {e}")
            return rel, "failed", ""
        if text.strip():
            return rel, "ok", text
        return rel, "empty", ""

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_work, rel): rel for rel in refs}
        done = 0
        for fut in as_completed(futs):
            rel, status, text = fut.result()
            results[rel] = (status, text)
            done += 1
            progress(done / total)
            if done % 5 == 0 or done == total:
                log(f"  figure recognition {done}/{total}")

    stats = {"ok": 0, "empty": 0, "failed": 0, "missing": 0}
    for status, _ in results.values():
        stats[status] = stats.get(status, 0) + 1

    def _repl(m: re.Match) -> str:
        rel = m.group(1).strip()
        status, txt = results.get(rel, ("failed", ""))
        if status == "ok":
            return f"\n\n{txt}\n\n"
        if status == "empty":
            return ""                 # confirmed decorative: drop placeholder
        return m.group(0)             # failed / missing: keep original link
    new_md = _IMG_RE.sub(_repl, md_text)

    if stats["failed"] or stats["missing"]:
        log(f"  ⚠ kept original links for {stats['failed']} failed and "
            f"{stats['missing']} missing images (nothing overwritten)")
    if stats["failed"] and _err_samples:
        hint = _err_samples[0]
        if "403" in hint or "Forbidden" in hint:
            log("  💡 Hint: 403 usually means this API key has no access to the "
                f"vision model (current: {vl_model}). Enable it or switch model/key.")
    return new_md, stats["ok"], total, stats


def archive_images_file(
    md_path: str,
    api_key: str,
    base_url: str,
    vl_model: str,
    *,
    search_root: Optional[str] = None,
    language: str = "en",
    log: Callable[[str], None] = print,
    progress: Callable[[float], None] = lambda p: None,
    should_stop: Callable[[], bool] = lambda: False,
) -> Tuple[int, int, Dict[str, int]]:
    """
    Run figure recognition over an on-disk MD, writing back in place.
    Returns (ok_count, total, stats).
    Safety: only write back when at least one image was recognized (ok) or
    confirmed decorative (empty); if everything failed/missing the file is
    left untouched so a retry stays possible.
    """
    md_path = Path(md_path)
    md_text = md_path.read_text(encoding="utf-8")
    roots = collect_image_roots(
        md_path, Path(search_root) if search_root else None,
    )
    new_md, ok, total, stats = archive_images_in_text(
        md_text, roots, api_key, base_url, vl_model,
        log=log, progress=progress, should_stop=should_stop,
    )
    changed = stats.get("ok", 0) + stats.get("empty", 0)
    if total > 0 and changed > 0:
        md_path.write_text(new_md, encoding="utf-8")
        log(f"  MD updated: {stats['ok']} figures -> tables, "
            f"{stats['empty']} decorative removed; "
            f"{stats['failed'] + stats['missing']} links kept")
    elif total > 0:
        log("  No figure recognized this run — original MD untouched "
            "(all image links kept, retry later)")
    return ok, total, stats


# ════════════════════════════════════════════════════════════════════════
#  Top level: PDF -> MD (MinerU)
# ════════════════════════════════════════════════════════════════════════
def _find_full_md(result_dir: Path) -> Optional[Path]:
    cands = list(result_dir.rglob("full.md"))
    if cands:
        return cands[0]
    cands = list(result_dir.rglob("*.md"))
    return cands[0] if cands else None


def mineru_work_dir(pdf_path: str) -> Path:
    """MinerU work dir (per-batch results + images) for downstream figure work."""
    pdf_path = Path(pdf_path)
    return pdf_path.parent / f"{pdf_path.stem}_mineru"


def mineru_pdf_to_md(
    pdf_path: str,
    token: str,
    *,
    language: str = "en",
    model_version: str = "vlm",
    work_dir: Optional[str] = None,
    log: Callable[[str], None] = print,
    progress: Callable[[float], None] = lambda p: None,
    should_stop: Callable[[], bool] = lambda: False,
) -> Tuple[str, Path]:
    """
    Batch upload -> parse -> download -> merge. Returns (merged md text, work_dir).
    This step does NOT do figure recognition — it yields a complete md that may
    still contain ![](images/..) links. Figure recognition runs as a separate
    follow-up step (archive_images_*) and needs an LLM API key.
    HTML <table> blocks are kept verbatim; heading re-leveling happens upstream.
    """
    pdf_path = Path(pdf_path)
    work_dir = Path(work_dir) if work_dir else mineru_work_dir(pdf_path)
    work_dir.mkdir(parents=True, exist_ok=True)
    client = MinerUClient(token, language=language,
                          model_version=model_version, log=log)

    # 1) split into batches
    parts = split_pdf(str(pdf_path), work_dir / "parts")
    log(f"PDF split into {len(parts)} batch(es) (<= {PAGE_LIMIT} pages each)")
    progress(0.05)

    merged_sections: List[Tuple[int, int, str]] = []

    # 2) process batch by batch (smoother progress than bulk upload)
    for idx, (part_path, p_start, p_end) in enumerate(parts, 1):
        if should_stop():
            raise RuntimeError("Aborted by user")
        log(f"[batch {idx}/{len(parts)}] pages {p_start}-{p_end}: requesting upload")
        batch_id, urls = client.request_upload([part_path.name])
        client.upload_file(urls[0], part_path)
        log(f"[batch {idx}] uploaded, batch_id={batch_id}, waiting for parse")
        done = client.poll_results(batch_id, 1, should_stop=should_stop)
        zip_url = (done[0].get("full_zip_url") or done[0].get("fullZipUrl")
                   or done[0].get("zip_url"))
        if not zip_url:
            raise RuntimeError(f"[batch {idx}] result missing zip URL: {done[0]}")
        res_dir = work_dir / f"result_{p_start}_{p_end}"
        client.fetch_zip(zip_url, res_dir)
        md_file = _find_full_md(res_dir)
        if not md_file:
            raise RuntimeError(f"[batch {idx}] no full.md in result: {res_dir}")
        md = md_file.read_text(encoding="utf-8")
        merged_sections.append((p_start, p_end, md))
        progress(0.05 + 0.9 * idx / len(parts))

    # 3) merge using the physical-slice wire-format marker (Chinese — keep as is;
    #    the chunker and the comparison report both match this exact pattern)
    out_parts: List[str] = []
    for p_start, p_end, md in merged_sections:
        out_parts.append(f"\n\n# --- PDF 物理切片：第 {p_start} - {p_end} 页 ---\n\n")
        out_parts.append(md)
    progress(1.0)
    return "".join(out_parts), work_dir
