#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
对比 pdfminer.six（若安装）与 PyMuPDF 的文本质量（面向学术 PDF 的“先验信息”评估）。

输出：
- JSON：包含各项指标与中间统计（便于后续批量汇总）
- Markdown：一张表 + 简短解释（便于直接阅读）
"""

from __future__ import annotations

import argparse
import json
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_RE_BLANKLINES = re.compile(r"\n\s*\n+", re.MULTILINE)
_RE_WS = re.compile(r"\s+")
_RE_ALPHA_WORD = re.compile(r"[A-Za-z]{2,}")


def _now_iso() -> str:
    # 不引入 datetime，减少依赖；ISO 仅用于展示
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _write_text(path: Path, s: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(s, encoding="utf-8")


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _norm(s: str) -> str:
    # 轻量归一：统一换行、压缩空白，方便做包含匹配
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return _RE_WS.sub(" ", s).strip()


def _split_paragraphs(text: str) -> list[str]:
    parts = [p.strip() for p in _RE_BLANKLINES.split(text.replace("\r\n", "\n").replace("\r", "\n"))]
    return [p for p in parts if p]


def _is_printable_char(ch: str) -> bool:
    if ch in ("\n", "\t", "\r"):
        return True
    cat = unicodedata.category(ch)
    # C*：控制/格式/私用/未分配
    if cat.startswith("C"):
        return False
    return True


def _char_stats(text: str) -> dict[str, float]:
    total = len(text)
    if total == 0:
        return {
            "chars_total": 0,
            "printable_ratio": 0.0,
            "ascii_printable_ratio": 0.0,
            "whitespace_ratio": 0.0,
            "alpha_ratio": 0.0,
        }
    printable = 0
    ascii_printable = 0
    whitespace = 0
    alpha = 0
    for ch in text:
        if _is_printable_char(ch):
            printable += 1
        o = ord(ch)
        if 32 <= o <= 126 or ch in ("\n", "\t", "\r"):
            ascii_printable += 1
        if ch.isspace():
            whitespace += 1
        if ch.isalpha():
            alpha += 1
    return {
        "chars_total": total,
        "printable_ratio": printable / total,
        "ascii_printable_ratio": ascii_printable / total,
        "whitespace_ratio": whitespace / total,
        "alpha_ratio": alpha / total,
    }


def _line_noise_stats(text: str, head_lines: int = 250) -> dict[str, float]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    stripped = [ln.strip() for ln in lines]
    non_empty = [ln for ln in stripped if ln]
    if not non_empty:
        return {
            "lines_total": float(len(lines)),
            "lines_non_empty": 0.0,
            "avg_line_len": 0.0,
            "p_le2": 0.0,
            "p_eq1": 0.0,
            "leading_short_run": 0.0,
        }

    lens = [len(ln) for ln in non_empty]
    le2 = sum(1 for n in lens if n <= 2)
    eq1 = sum(1 for n in lens if n == 1)

    # 头部“旋转/边栏噪声”常表现为：开头连续大量单字符或超短行
    head = [ln for ln in stripped[:head_lines] if ln]
    run = 0
    for ln in head:
        if len(ln) <= 2:
            run += 1
        else:
            break

    return {
        "lines_total": float(len(lines)),
        "lines_non_empty": float(len(non_empty)),
        "avg_line_len": float(sum(lens) / len(lens)),
        "p_le2": float(le2 / len(lens)),
        "p_eq1": float(eq1 / len(lens)),
        "leading_short_run": float(run),
    }


def _token_stats(text: str) -> dict[str, float]:
    # 轻量可读性信号：英文词（>=2字母）的密度
    norm = _norm(text)
    if not norm:
        return {"alpha_words": 0.0, "alpha_words_per_1k_chars": 0.0}
    words = _RE_ALPHA_WORD.findall(norm)
    return {
        "alpha_words": float(len(words)),
        "alpha_words_per_1k_chars": float(len(words) / max(1, len(norm)) * 1000.0),
    }


def _load_gathered_paragraphs(gathered_json: Path) -> list[str]:
    data = json.loads(_read_text(gathered_json))
    paras = data.get("paragraphs") or []
    out: list[str] = []
    for p in paras:
        if not isinstance(p, dict):
            continue
        t = str(p.get("text") or "").strip()
        if not t:
            continue
        out.append(t)
    return out


def _select_reference_paragraphs(paras: list[str]) -> list[str]:
    # 只选“像正文段落”的较长段，减少标题/编号/短噪声对评分的干扰
    ref: list[str] = []
    for t in paras:
        tn = _norm(t)
        if len(tn) < 120:
            continue
        if len(_RE_ALPHA_WORD.findall(tn)) < 8:
            continue
        ref.append(t)
    return ref


@dataclass
class ParagraphMatchStats:
    ref_count: int
    coverage_in_text_ratio: float
    kept_together_ratio: float
    ref_sampled: int


def _paragraph_match_stats(
    *,
    extracted_text: str,
    extracted_paragraphs: list[str],
    reference_paragraphs: list[str],
    sig_chars: int = 64,
) -> ParagraphMatchStats:
    if not reference_paragraphs:
        return ParagraphMatchStats(ref_count=0, coverage_in_text_ratio=0.0, kept_together_ratio=0.0, ref_sampled=0)

    text_n = _norm(extracted_text)
    paras_n = [_norm(p) for p in extracted_paragraphs]

    in_text = 0
    together = 0
    sampled = 0

    for ref in reference_paragraphs:
        r = _norm(ref)
        if len(r) < sig_chars * 2:
            continue
        sampled += 1
        head = r[:sig_chars]
        tail = r[-sig_chars:]

        if head in text_n:
            in_text += 1

        # “段落重建率”近似：同一 extracted paragraph 同时包含 head 与 tail
        found = False
        for p in paras_n:
            if head in p and tail in p:
                found = True
                break
        if found:
            together += 1

    denom = max(1, sampled)
    return ParagraphMatchStats(
        ref_count=len(reference_paragraphs),
        coverage_in_text_ratio=in_text / denom,
        kept_together_ratio=together / denom,
        ref_sampled=sampled,
    )


def _extract_pymupdf_text(pdf: Path) -> tuple[str, dict[str, Any]]:
    import fitz

    t0 = time.time()
    doc = fitz.open(str(pdf))
    try:
        pages = []
        for pno in range(len(doc)):
            pages.append(doc[pno].get_text("text"))
        text = "\n\n".join(pages)
    finally:
        doc.close()
    dt = time.time() - t0
    meta = {"engine": "pymupdf", "pages": len(pages), "seconds": dt}
    return text, meta


def _extract_pdfminer_text(pdf: Path) -> tuple[str, dict[str, Any]]:
    from pdfminer.high_level import extract_text

    t0 = time.time()
    text = extract_text(str(pdf))
    dt = time.time() - t0
    meta = {"engine": "pdfminer.six", "seconds": dt}
    return text, meta


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _fmt_num(x: float) -> str:
    if abs(x - int(x)) < 1e-9:
        return str(int(x))
    return f"{x:.2f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True, help="PDF 路径")
    ap.add_argument("--gathered-json", default=None, help="gathered_text.json 路径（默认: <pdf_dir>/text/gathered_text.json）")
    ap.add_argument("--out-json", default=None, help="输出 JSON（默认: <pdf_dir>/text/text_quality_report.json）")
    ap.add_argument("--out-md", default=None, help="输出 Markdown（默认: <pdf_dir>/text/text_quality_report.md）")
    args = ap.parse_args()

    pdf = Path(args.pdf)
    if not pdf.exists():
        raise SystemExit(f"PDF 不存在：{pdf}")

    pdf_dir = pdf.parent
    gathered_json = Path(args.gathered_json) if args.gathered_json else (pdf_dir / "text" / "gathered_text.json")

    out_json = Path(args.out_json) if args.out_json else (pdf_dir / "text" / "text_quality_report.json")
    out_md = Path(args.out_md) if args.out_md else (pdf_dir / "text" / "text_quality_report.md")

    gathered_paras: list[str] = []
    ref_paras: list[str] = []
    gathered_ok = False
    gathered_error = None
    if gathered_json.exists():
        try:
            gathered_paras = _load_gathered_paragraphs(gathered_json)
            ref_paras = _select_reference_paragraphs(gathered_paras)
            gathered_ok = True
        except Exception as e:
            gathered_error = str(e)

    engines: list[dict[str, Any]] = []

    engine_fns: list[tuple[str, Any]] = [("pymupdf", _extract_pymupdf_text)]
    pdfminer_available = True
    try:
        # 仅用于探测是否已安装；真正调用在 _extract_pdfminer_text
        import pdfminer  # type: ignore
    except Exception:
        pdfminer_available = False
    if pdfminer_available:
        engine_fns.append(("pdfminer", _extract_pdfminer_text))

    for name, fn in engine_fns:
        text, meta = fn(pdf)
        paras = _split_paragraphs(text)
        cs = _char_stats(text)
        ls = _line_noise_stats(text)
        ts = _token_stats(text)
        pm = _paragraph_match_stats(
            extracted_text=text,
            extracted_paragraphs=paras,
            reference_paragraphs=ref_paras,
        )

        engines.append(
            {
                "name": name,
                "meta": meta,
                "char_stats": cs,
                "line_noise": ls,
                "token_stats": ts,
                "paragraphs": {
                    "paragraph_count": len(paras),
                    "avg_paragraph_len": (sum(len(_norm(p)) for p in paras) / max(1, len(paras))),
                },
                "paragraph_match_vs_gathered": {
                    "ref_total": pm.ref_count,
                    "ref_sampled": pm.ref_sampled,
                    "coverage_in_text_ratio": pm.coverage_in_text_ratio,
                    "kept_together_ratio": pm.kept_together_ratio,
                },
            }
        )

    report = {
        "generated_at": _now_iso(),
        "pdf": str(pdf).replace("\\", "/"),
        "gathered_json": str(gathered_json).replace("\\", "/"),
        "gathered_ok": gathered_ok,
        "gathered_error": gathered_error,
        "gathered_paragraphs": len(gathered_paras),
        "reference_paragraphs_selected": len(ref_paras),
        "engines": engines,
        "notes": {
            "rotation_noise_proxy": "p_eq1/p_le2/leading_short_run 用于近似衡量“旋转/边栏文本噪声”（单字符/超短行）。",
            "readable_proxy": "printable_ratio + alpha_words_per_1k_chars 作为可读性信号（非严格语义质量）。",
            "paragraph_reconstruction_proxy": "以 gathered_text.json 的长段落为参考：coverage_in_text=是否能在全文中找到；kept_together=是否能在同一段落里同时命中首尾片段。",
        },
    }

    _write_json(out_json, report)

    # Markdown summary
    rows = []
    for e in engines:
        cs = e["char_stats"]
        ls = e["line_noise"]
        ts = e["token_stats"]
        pm = e["paragraph_match_vs_gathered"]
        ps = e["paragraphs"]
        rows.append(
            {
                "引擎": e["meta"].get("engine") or e["name"],
                "耗时(s)": _fmt_num(float(e["meta"].get("seconds") or 0.0)),
                "总字符": str(int(cs["chars_total"])),
                "可打印字符": _fmt_pct(float(cs["printable_ratio"])),
                "ASCII占比": _fmt_pct(float(cs["ascii_printable_ratio"])),
                "空白占比": _fmt_pct(float(cs["whitespace_ratio"])),
                "平均行长": _fmt_num(float(ls["avg_line_len"])),
                "单字符行占比": _fmt_pct(float(ls["p_eq1"])),
                "≤2字符行占比": _fmt_pct(float(ls["p_le2"])),
                "开头短行run": _fmt_num(float(ls["leading_short_run"])),
                "英文词/1k字": _fmt_num(float(ts["alpha_words_per_1k_chars"])),
                "段落数": str(int(ps["paragraph_count"])),
                "段均长度": _fmt_num(float(ps["avg_paragraph_len"])),
                "参考段落(长)": f"{pm['ref_sampled']}/{pm['ref_total']}",
                "参考覆盖率": _fmt_pct(float(pm["coverage_in_text_ratio"])),
                "段落保持率": _fmt_pct(float(pm["kept_together_ratio"])),
            }
        )

    headers = list(rows[0].keys()) if rows else []
    md_lines = []
    md_lines.append(f"# 文本质量对比报告（PyMuPDF vs pdfminer.six（若安装））")
    md_lines.append("")
    md_lines.append(f"- PDF：`{str(pdf).replace('\\', '/')}`")
    md_lines.append(f"- 生成时间：`{report['generated_at']}`")
    if gathered_ok:
        md_lines.append(f"- 参考结构化文本：`{str(gathered_json).replace('\\', '/')}`（paragraphs={len(gathered_paras)}，选取长段落={len(ref_paras)}）")
    else:
        md_lines.append(f"- 参考结构化文本：`{str(gathered_json).replace('\\', '/')}`（不可用：{gathered_error or 'missing'}）")
    md_lines.append("")

    if headers:
        md_lines.append("| " + " | ".join(headers) + " |")
        md_lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
        for r in rows:
            md_lines.append("| " + " | ".join(str(r[h]) for h in headers) + " |")
        md_lines.append("")

    md_lines.append("## 指标说明（简版）")
    md_lines.append("- `单字符行占比/≤2字符行占比/开头短行run`：用来近似衡量“旋转/边栏文本噪声”（例如 arXiv 侧栏信息常会被抽成逐字符换行）。")
    md_lines.append("- `可打印字符/英文词密度`：粗略可读性信号；并不等价于语义质量。")
    md_lines.append("- `参考覆盖率/段落保持率`：以 `gathered_text.json` 的长段落为参考；覆盖率=能否在全文中找到；保持率=首尾片段能否落在同一段落中（近似段落重建质量）。")
    md_lines.append("")

    _write_text(out_md, "\n".join(md_lines))

    print(f"[OK] wrote: {out_json}")
    print(f"[OK] wrote: {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
