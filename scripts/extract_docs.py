#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""paper-explainer | 批量提取论文全文（PDF / DOCX -> TXT）

用法:
    python extract_docs.py <路径...> --out <输出目录> [--max-pages N]

<路径> 可以是目录（递归遍历）或单个文件，可一次给多个。
支持 .pdf / .docx / .txt / .md。旧版 .doc 需先另存为 .docx。

产出:
    <输出目录>/<文件名>.txt      每篇一份纯文本；PDF 带 "===== PAGE n =====" 分页标记
    <输出目录>/manifest.json     清单：来源、页数、字数、告警

设计意图:
    提取结果供后续"结构化提取 + 横向比较"使用，因此保留分页标记，
    便于回指原文页码（论文引用要求给出 Fig./Table/页码）。
"""

import argparse
import json
import logging
import sys
from pathlib import Path

logging.getLogger("pypdf").setLevel(logging.ERROR)

SUPPORTED = {".pdf", ".docx", ".txt", ".md"}
LEGACY = {".doc", ".ppt", ".pptx", ".caj"}
# 单页字符数低于此值视为"疑似扫描件/图片版"，需要 OCR 或改用中文译本
SCAN_THRESHOLD = 150


def extract_pdf(path: Path):
    """返回 [(页码, 文本), ...]"""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    out = []
    for i, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        out.append((i, text.strip()))
    return out


def extract_docx(path: Path):
    """按正文顺序提取段落与表格，返回 [(1, 文本)]（DOCX 无稳定分页）"""
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(str(path))
    blocks = []
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            text = Paragraph(child, doc).text.strip()
            if text:
                blocks.append(text)
        elif child.tag == qn("w:tbl"):
            for row in Table(child, doc).rows:
                cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                if any(cells):
                    blocks.append(" | ".join(cells))
            blocks.append("")
    return [(1, "\n".join(blocks))]


def _parse_one(task):
    """解析单个文件，返回 (路径, kind, pages, error)。

    必须是模块级函数——进程池要靠 pickle 传递它。
    注意：pypdf 的 extract_text 是纯 Python、CPU 密集，线程受 GIL 限制没有收益，
    所以并行只能用进程池；进程启动有成本，少量小文件时反而更慢，故默认不开。
    """
    path_str = task[0]
    p = Path(path_str)
    suffix = p.suffix.lower()
    try:
        if suffix == ".pdf":
            return path_str, "pdf", extract_pdf(p), None
        if suffix == ".docx":
            return path_str, "docx", extract_docx(p), None
        return path_str, "text", [(1, p.read_text(encoding="utf-8", errors="ignore"))], None
    except Exception as e:
        return path_str, suffix.lstrip("."), None, f"{type(e).__name__}: {e}"


def write_text(out_dir: Path, src: Path, pages, kind: str):
    body_parts = []
    for no, text in pages:
        if kind == "pdf":
            body_parts.append(f"===== PAGE {no} =====")
        body_parts.append(text)
        body_parts.append("")
    body = "\n".join(body_parts).strip()

    out_file = out_dir / (src.stem + ".txt")
    n = 1
    while out_file.exists():
        n += 1
        out_file = out_dir / f"{src.stem}_{n}.txt"
    out_file.write_text(body, encoding="utf-8")

    chars = len(body.replace(" ", "").replace("\n", ""))
    warnings = []
    if kind == "pdf":
        per_page = chars / max(len(pages), 1)
        if per_page < SCAN_THRESHOLD:
            warnings.append(
                f"疑似扫描件/图片版（平均每页 {per_page:.0f} 字符），文本可能不可用，"
                "需 OCR 或改用中文译本"
            )
    if chars < 200:
        warnings.append("提取内容过少，请人工确认该文件是否可读")

    return {
        "source": str(src),
        "text": str(out_file),
        "kind": kind,
        "pages": len(pages),
        "chars": chars,
        "warnings": warnings,
    }


def collect(paths):
    files = []
    for raw in paths:
        p = Path(raw).expanduser()
        if not p.exists():
            print(f"[跳过] 路径不存在: {p}", file=sys.stderr)
            continue
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and f.suffix.lower() in SUPPORTED:
                    files.append(f)
        elif p.suffix.lower() in SUPPORTED:
            files.append(p)
        elif p.suffix.lower() in LEGACY:
            print(f"[跳过] 旧版格式需先转换: {p}", file=sys.stderr)
        else:
            print(f"[跳过] 不支持的格式: {p}", file=sys.stderr)
    # 去重并保持顺序
    seen, uniq = set(), []
    for f in files:
        key = str(f.resolve())
        if key not in seen:
            seen.add(key)
            uniq.append(f)
    return uniq


def main():
    ap = argparse.ArgumentParser(description="批量提取论文全文（PDF / DOCX -> TXT）")
    ap.add_argument("paths", nargs="+", help="目录或文件路径，可多个")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--max-pages", type=int, default=0, help="单篇最多提取页数，0=不限")
    ap.add_argument("--workers", type=int, default=0,
                    help="并行解析进程数；0=自动（≥3 个文件时开，取 min(4, CPU 核数)），1=强制串行")
    args = ap.parse_args()

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    files = collect(args.paths)
    if not files:
        print("没有找到可提取的文件。", file=sys.stderr)
        return 1

    # 解析：多文件默认并行（pypdf 是 CPU 密集，进程池有实收益；线程受 GIL 限制无效）
    workers = args.workers
    if workers == 0:
        import os

        workers = 1 if len(files) < 3 else min(4, os.cpu_count() or 1)
    tasks = [(str(f),) for f in files]
    if workers > 1 and len(files) > 1:
        from concurrent.futures import ProcessPoolExecutor

        print(f"[并行] {workers} 个进程解析 {len(files)} 个文件")
        try:
            with ProcessPoolExecutor(max_workers=workers) as ex:
                parsed = list(ex.map(_parse_one, tasks))   # map 保序，与串行结果一致
        except Exception as e:
            print(f"[并行失败，退回串行] {type(e).__name__}: {e}", file=sys.stderr)
            parsed = [_parse_one(t) for t in tasks]
    else:
        parsed = [_parse_one(t) for t in tasks]

    manifest = []
    for f, (src, kind, pages, err) in zip(files, parsed):
        if err:
            print(f"[失败] {f.name}: {err}", file=sys.stderr)
            manifest.append({"source": str(f), "kind": kind, "error": err})
            continue

        if args.max_pages > 0:
            pages = pages[: args.max_pages]

        entry = write_text(out_dir, f, pages, kind)
        manifest.append(entry)
        flag = "  ⚠ " + "；".join(entry["warnings"]) if entry["warnings"] else ""
        print(f"[OK] {f.name}  ->  {Path(entry['text']).name}  "
              f"({entry['pages']} 页 / {entry['chars']} 字符){flag}")

    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    ok = sum(1 for m in manifest if "error" not in m)
    print(f"\n完成：{ok}/{len(files)} 篇，清单 -> {out_dir / 'manifest.json'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
