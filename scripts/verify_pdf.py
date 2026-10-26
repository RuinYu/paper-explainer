#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""paper-explainer | 成品 PDF 自检

用法:
    python verify_pdf.py <book.pdf> [--render 1,4,7] [--out <图片目录>] [--scale 1.6]

检查项:
    1. 页数、每页字符数、每页首行（快速发现空白页、串页、图表丢失）
    2. 中文嵌入是否正常（CJK 字符占比过低 = 字体未嵌入或被替换成方框）
    3. 残留的 @@CH1@@ 标记（说明剥离失败，会污染复制出来的文本）
    4. 目录页码与实际章起始页是否一致（扫描"目录"页的数字 + 定位标题页）
    5. 可选：渲染指定页为 PNG，供肉眼核对版式

退出码: 0 全部通过 / 1 存在告警
"""

import argparse
import re
import sys
import unicodedata
from pathlib import Path

CJK = re.compile(r"[\u4e00-\u9fff]")
# 康熙部首 / CJK 部首补充：字体回退到 Chrome 内置 CJK 字体时会出现，
# 表现为"用"被写成 U+2F65、"目"被写成 U+2F6C，复制出来是错字。
KANGXI = re.compile(r"[\u2e80-\u2eff\u2f00-\u2fdf]")
MARK_RE = re.compile(r"@@[A-Za-z0-9_\-]+@@")
# 目录行：标题 + 页码（flex 布局下标签与页码间可能只有 1 个空格）
TOC_LINE = re.compile(r"^(.*?)[\s.·…]{1,}(\d{1,3})\s*$")
# 章节标题行
HEAD_RE = re.compile(r"^(第\s*[0-9一二三四五六七八九十]+\s*(章|部分)|附录\s*[A-Z])")
# 目录标签的前缀（"第一部分" / "第三章" / "附录 A"），去掉后剩下纯标题用于定位正文页
PREFIX_RE = re.compile(r"^(第[0-9一二三四五六七八九十]+(?:部分|章)|附录[A-Z])")
# 图注行："图 4-1　……"，用于识别含图页（--render key 的关键页之一）
FIG_CAP = re.compile(r"^图\s*\d+\s*[-–—]\s*\d+")


def core_label(label: str) -> str:
    """把目录标签剥成纯标题，便于在正文页顶部做包含匹配。

    正文页顶部往往是"01"这类编号行，标题在下一行，所以不能用整行相等来比。
    """
    return PREFIX_RE.sub("", re.sub(r"\s+", "", label))


def norm(text: str) -> str:
    """NFKC 归一化：把康熙部首还原成统一汉字，便于做文本判断。"""
    return unicodedata.normalize("NFKC", text)


def page_texts(pdf_path: Path):
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    return [(p.extract_text() or "") for p in reader.pages]


def first_line(text: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s:
            return s[:60]
    return "(空)"


def flat(s: str) -> str:
    """去掉所有空白，便于匹配被字距拉开的标题（"目　录" -> "目录"）。"""
    return re.sub(r"\s+", "", s)


def main():
    ap = argparse.ArgumentParser(description="成品 PDF 自检")
    ap.add_argument("pdf")
    ap.add_argument("--render", default="",
                    help="要渲染成图的页码：逗号分隔如 1,4,7；key=关键页（封面/目录/各章首页/含图页）；all=全部")
    ap.add_argument("--out", default="", help="渲染图片输出目录，默认 PDF 同级 _preview")
    ap.add_argument("--scale", type=float, default=1.6, help="渲染缩放，默认 1.6")
    ap.add_argument("--check-bg", action="store_true",
                    help="检查页面四角是否为满版底色（防白边/漏底）")
    ap.add_argument("--bg-color", default="#f5f4ed", help="期望的页面底色，默认羊皮纸")
    args = ap.parse_args()

    pdf_path = Path(args.pdf).expanduser().resolve()
    if not pdf_path.exists():
        raise SystemExit(f"文件不存在: {pdf_path}")

    raw_texts = page_texts(pdf_path)
    texts = [norm(t) for t in raw_texts]
    total = len(texts)
    warnings = []
    chapter_pages = set()

    print(f"文件: {pdf_path}")
    print(f"页数: {total}\n")

    print("页码 | 字符数 | 首行")
    print("-" * 72)
    for i, t in enumerate(texts, 1):
        chars = len(t.replace("\n", "").replace(" ", ""))
        print(f"{i:>4} | {chars:>6} | {first_line(t)}")
        if chars == 0:
            warnings.append(f"第 {i} 页无任何文本（可能是纯图页或渲染丢失）")

    # 中文嵌入
    joined = "".join(texts)
    cjk = len(CJK.findall(joined))
    ratio = cjk / max(len(joined), 1)
    print(f"\n中文字符占比: {ratio:.1%}（{cjk} 个）")
    if ratio < 0.10:
        warnings.append("中文字符占比过低，字体可能未正确嵌入（检查 CSS font-family 是否可用）")

    # 康熙部首：字体回退导致的文本层错字，复制出来会变形
    radicals = sum(len(KANGXI.findall(t)) for t in raw_texts)
    if radicals:
        warnings.append(
            f"文本层出现 {radicals} 个康熙部首码位（用→⽤、目→⽬）："
            "font-family 首选字体未安装，Chrome 回退到内置 CJK 字体。"
            "把已安装的系统字体（Microsoft YaHei / PingFang SC）放到首位后重新渲染"
        )

    # 满版底色：Chrome 不会把根元素背景铺到页边距，必须靠 build_pdf.py 垫底
    if args.check_bg:
        try:
            import pypdfium2 as pdfium
        except ImportError:
            warnings.append("未安装 pypdfium2，跳过底色检查（pip install pypdfium2）")
        else:
            hexs = args.bg_color.lstrip("#")
            exp = tuple(int(hexs[i:i + 2], 16) for i in (0, 2, 4))
            doc = pdfium.PdfDocument(str(pdf_path))
            bad = []
            for p in sorted({1, min(2, total), total}):
                img = doc[p - 1].render(scale=0.5).to_pil().convert("RGB")
                w, h = img.size
                for xy in [(2, 2), (w - 3, 2), (2, h - 3), (w - 3, h - 3)]:
                    px = img.getpixel(xy)
                    if max(abs(px[i] - exp[i]) for i in range(3)) > 6:
                        bad.append(f"第 {p} 页 {xy} 取到 {px}")
                        break
            if bad:
                warnings.append(
                    f"页面底色不是满版 {args.bg_color}（出现白边或漏底）："
                    + "；".join(bad)
                    + "。检查 build_pdf.py 是否传了 --page-bg"
                )
            else:
                print(f"页面底色: {args.bg_color} 满版 ✓")

    # 标记残留
    leftover = [i for i, t in enumerate(texts, 1) if MARK_RE.search(t)]
    if leftover:
        warnings.append(f"第 {leftover} 页残留 @@CHx@@ 标记，文本层会被污染（加 --keep-markers 关闭剥离时属预期）")
    print(f"标记残留: {'无' if not leftover else leftover}")

    # 目录页码一致性
    # 目录可能与前言同页，首行不是"目录"，所以不能只认首行：
    # 页内出现两条以上"第X部分/附录X + 页码"的行，就按目录页处理。
    def is_toc_page(text: str) -> bool:
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if lines and flat(lines[0]).startswith(("目录", "目次", "目錄")):
            return True
        hits = 0
        for s in lines:
            m = TOC_LINE.match(s)
            if m and HEAD_RE.match(flat(m.group(1).strip())):
                hits += 1
        return hits >= 2

    toc_pages = [i for i, t in enumerate(texts, 1) if is_toc_page(t)]
    if toc_pages:
        entries = {}
        for i in toc_pages:
            for line in texts[i - 1].splitlines():
                s = line.strip()
                m = TOC_LINE.match(s)
                if not m:
                    continue
                label = m.group(1).strip()
                if not HEAD_RE.match(flat(label)):
                    continue
                entries[label] = int(m.group(2))
        print(f"\n目录页码核对（目录在第 {toc_pages} 页）:")
        bad = 0
        for label, pg in entries.items():
            core = core_label(label)
            cand = []
            if core:
                for i, t in enumerate(texts, 1):
                    if i <= max(toc_pages):
                        continue
                    head = [flat(l) for l in t.splitlines() if l.strip()][:3]
                    if any(core in h for h in head):
                        cand.append(i)
            if not cand and core:
                # 章标题不一定在页首：附录可以接着上一节排在同一页，这时标题在页中。
                # 退一步按“整行等于标题”找，避免把正文里顺带提到的词当成标题。
                for i, t in enumerate(texts, 1):
                    if i <= max(toc_pages):
                        continue
                    if any(flat(l) == core for l in t.splitlines() if l.strip()):
                        cand.append(i)
            if not cand:
                print(f"  ?  {label}: 目录标 {pg}，未在正文中定位到标题页")
                continue
            real = min(cand)
            chapter_pages.add(real)
            ok = "✓" if real == pg else "✗"
            if real != pg:
                bad += 1
            print(f"  {ok}  {label}: 目录 {pg} / 实际 {real}")
        if bad:
            warnings.append(f"{bad} 条目录页码与实际不一致")
    else:
        print("\n未识别到目录页，跳过页码核对")

    # 关键页集合：封面 + 目录 + 各章首页 + 含图页。
    # 四类全部取自上面已有的扫描结果，不额外遍历，覆盖的正是全部版式风险点。
    fig_pages = {
        i for i, t in enumerate(texts, 1)
        if any(FIG_CAP.match(l.strip()) for l in t.splitlines() if l.strip())
    }
    key_pages = {1} | set(toc_pages) | chapter_pages | fig_pages

    # 渲染预览
    if args.render:
        try:
            import pypdfium2 as pdfium
        except ImportError:
            warnings.append("未安装 pypdfium2，跳过渲染（pip install pypdfium2）")
        else:
            if args.render == "all":
                pages = list(range(1, total + 1))
            elif args.render == "key":
                pages = sorted(p for p in key_pages if 1 <= p <= total)
                print(f"\n关键页（封面/目录/章首页/含图页）: {pages}")
            else:
                pages = [int(x) for x in args.render.split(",") if x.strip().isdigit()]
            out_dir = Path(args.out).expanduser() if args.out else pdf_path.parent / "_preview"
            out_dir.mkdir(parents=True, exist_ok=True)
            doc = pdfium.PdfDocument(str(pdf_path))
            for p in pages:
                if 1 <= p <= total:
                    img = doc[p - 1].render(scale=args.scale).to_pil()
                    f = out_dir / f"p{p:03d}.png"
                    img.save(f)
                    print(f"渲染: {f}  {img.size[0]}x{img.size[1]}")

    print("\n" + "=" * 72)
    if warnings:
        print(f"发现 {len(warnings)} 项告警:")
        for w in warnings:
            print("  ⚠ " + w)
        return 1
    print("✓ 全部检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
