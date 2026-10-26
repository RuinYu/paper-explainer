#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""paper-explainer | HTML 书稿 -> 成品 PDF（目录页码 / 页脚页码 / 书签 / 元数据）

用法:
    python build_pdf.py --html book.html --out book.pdf [--title "..."] [--subtitle "..."]
                        [--browser <msedge/chrome 路径>] [--footer-skip 1] [--keep-markers]

HTML 必须遵守两条约定（assets/template.html 已内置）:

  1) 每个章/部分起始处放一个不可见标记，用于定位其所在页码:
         <span class="pmark">@@CH1@@</span>
     CSS: .pmark{ font-size:0.5pt; color:#fff; }   —— 白字，肉眼不可见，文本层可提取

  2) 目录页码处留空占位，由本脚本回填:
         <span class="toc-pg" data-toc-key="CH1"
               data-toc-title="第一部分　这个方向在干什么"></span>

流程:
    学习轮（渲染含标记的源文件 -> 定位章节页码）
    -> 回填目录页码
    -> 产出轮（回填 + 剥离标记，合并为一次渲染）
    -> 叠加页脚页码 -> 加书签与元数据

为什么最少两次: 页码必须先知道章节落在哪页才能填，填完必须重新排版一次。
为什么不是三次: 回填与剥离标记可以合并进同一次渲染，产物与分开渲染完全一致。
"""

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

MARK_RE = re.compile(r"@@([A-Za-z0-9_\-]+)@@")
MARK_SPAN_RE = re.compile(r'<span class="pmark">@@[A-Za-z0-9_\-]+@@</span>')
TOC_RE = re.compile(
    r'(<span class="toc-pg"\s+data-toc-key="([^"]+)"(?:\s+data-toc-title="([^"]*)")?\s*>)'
    r"([^<]*)"
    r"(</span>)"
)
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)

# ── 目录高度估算（A4，模板页边距 20/22/22/22）──
# 目录项是 flex 单行、页码右对齐，页码从空变成 "12" 不会引起换行，
# 所以"项数 × 行高"就是目录高度的充分估计，可以用来判断要不要跨页。
TOC_AVAIL_MM = 255.0    # 297 - 上 20 - 下 22
TOC_HEAD_MM = 16.0      # 22pt 标题行 + 18pt 下间距
TOC_ITEM_MM = 9.7       # 6pt 上下内边距 + 11pt × 1.4 行高
TOC_SAFE_ITEMS = 20     # 容量约 24 项，留 4 项余量
FRONT_BLOCK_MM = 75.0   # 前言（导语 + 五部分表）与目录同页时占掉的高度


def toc_estimate(n_items: int, front_shared: bool = False):
    """目录项数 -> (估算高度 mm, 容量, 是否单页)。"""
    avail = TOC_AVAIL_MM - (FRONT_BLOCK_MM if front_shared else 0.0)
    capacity = int((avail - TOC_HEAD_MM) // TOC_ITEM_MM)
    return n_items * TOC_ITEM_MM, capacity, n_items <= min(TOC_SAFE_ITEMS, capacity)


def comment_spans(html: str):
    """HTML 注释区间。注释里的示例代码不能当成真的目录项处理。"""
    return [(m.start(), m.end()) for m in COMMENT_RE.finditer(html)]


def in_spans(pos: int, spans) -> bool:
    return any(a <= pos < b for a, b in spans)


def iter_toc(html: str):
    """遍历真正的目录占位（跳过注释内的示例）。"""
    spans = comment_spans(html)
    for m in TOC_RE.finditer(html):
        if not in_spans(m.start(), spans):
            yield m

BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/microsoft-edge",
]


def find_browser(explicit=None):
    if explicit:
        p = Path(explicit)
        if p.exists():
            return str(p)
        raise SystemExit(f"指定的浏览器不存在: {explicit}")
    for c in BROWSER_CANDIDATES:
        if Path(c).exists():
            return c
    raise SystemExit(
        "未找到 Edge/Chrome。请用 --browser 指定可执行文件路径。\n"
        "已尝试:\n  " + "\n  ".join(BROWSER_CANDIDATES)
    )


def render(browser, html_path: Path, pdf_path: Path):
    """用无头浏览器把 HTML 打成 PDF。"""
    if pdf_path.exists():
        pdf_path.unlink()
    base = [browser, "--headless=new", "--disable-gpu", "--no-sandbox"]
    url = html_path.resolve().as_uri()
    variants = [
        base + ["--no-pdf-header-footer", f"--print-to-pdf={pdf_path}", url],
        base + ["--print-to-pdf-no-header", f"--print-to-pdf={pdf_path}", url],
        base + [f"--print-to-pdf={pdf_path}", url],
    ]
    last = None
    for cmd in variants:
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=600)
        except subprocess.CalledProcessError as e:
            last = e
            continue
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            return
        last = RuntimeError("浏览器未产出 PDF")
    raise SystemExit(f"PDF 渲染失败: {last}")


def page_texts(pdf_path: Path):
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    return [(p.extract_text() or "") for p in reader.pages]


def locate(texts, keys=None):
    """标记 -> 页码（1 起）。同名标记只取首次出现。"""
    found = {}
    for idx, text in enumerate(texts, 1):
        for m in MARK_RE.finditer(text):
            k = m.group(1)
            if keys is None or k in keys:
                found.setdefault(k, idx)
    return found


def fill_toc(html: str, mapping: dict):
    """把页码写进目录占位；未定位到的条目清空，避免残留旧页码。注释内的示例跳过。"""
    spans = comment_spans(html)
    out, last = [], 0
    for m in TOC_RE.finditer(html):
        if in_spans(m.start(), spans):
            continue
        page = mapping.get(m.group(2))
        out.append(html[last:m.start()])
        out.append(m.group(1) + (str(page) if page else "") + m.group(5))
        last = m.end()
    out.append(html[last:])
    return "".join(out)


def _register_cjk_font():
    """注册一个可渲染中文的 TTF 给页脚用。返回字体名或 None。

    只挑 reportlab 能直接吃的单体 TTF（.ttc 集合需 subfontIndex，易出问题）。
    顺序按"文本层干净"排：STSong / SimSun 系的 ToUnicode 映射正常，
    Noto CJK / Source Han 在部分机器上会把常用字映射成康熙部首。
    """
    from pathlib import Path

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = [
        (r"C:\Windows\Fonts\STSONG.TTF", "FooterCJK"),
        (r"C:\Windows\Fonts\simfang.ttf", "FooterCJK"),
        (r"C:\Windows\Fonts\simkai.ttf", "FooterCJK"),
        ("/System/Library/Fonts/Supplemental/Songti.ttc", "FooterCJK"),
        ("/usr/share/fonts/truetype/arphic/uming.ttc", "FooterCJK"),
    ]
    for path, name in candidates:
        if not Path(path).exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont(name, path))
            return name
        except Exception:
            continue
    return None


def _bg_page(width, height, color_hex):
    """生成一整页纯色底，用于垫在内容下方。

    Chrome 不会把 html/body 背景铺到 @page 页边距，只铺内容框，
    结果成品四周留白。这里用 pypdf 的 over=False 把底色垫到最底层，
    才能得到真正的满版底色。
    """
    import io

    from reportlab.lib.colors import HexColor
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))
    c.setFillColor(HexColor(color_hex))
    c.rect(0, 0, width, height, stroke=0, fill=1)
    c.showPage()
    c.save()
    buf.seek(0)
    from pypdf import PdfReader

    return PdfReader(buf).pages[0]


def produce(browser, html_path: Path, html: str, tmp_pdf: Path, keep_markers: bool):
    """产出轮：把「回填页码」与「剥离标记」合并成一次渲染。

    旧实现先渲染回填后的版本、再渲染剥离后的版本——两次排版内容只差
    @@KEY@@ 那几十个 0.5pt 字符，属于重复劳动。合并后少一次整本渲染，
    产物完全一致（最终仍是"已填页码 + 已剥标记"那一版）。

    返回 (页数, 每页文本)。
    """
    final_html = html_path.with_name(html_path.stem + ".final.html")
    body = html if keep_markers else MARK_SPAN_RE.sub("", html)
    final_html.write_text(body, encoding="utf-8")
    try:
        render(browser, final_html, tmp_pdf)
        texts = page_texts(tmp_pdf)
    finally:
        final_html.unlink(missing_ok=True)
    return len(texts), texts


def overlay(src_pdf: Path, dst_pdf: Path, footer_skip: int, bookmarks, meta,
            footer_title: str = "", footer_color: str = "#6b6a64",
            page_bg: str = ""):
    """叠加满版底色 + 页脚页码 + 书签 + 元数据。"""
    import io

    from pypdf import PdfReader, PdfWriter
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    reader = PdfReader(str(src_pdf))
    total = len(reader.pages)
    width, height = A4

    cjk_font = _register_cjk_font() if footer_title else None
    color = HexColor(footer_color)
    bg = _bg_page(width, height, page_bg) if page_bg else None

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for i in range(total):
        if i >= footer_skip:
            c.setFillColor(color)
            if footer_title and cjk_font:
                c.setFont(cjk_font, 9)
                c.drawCentredString(width / 2, 12 * 2.8346,
                                    f"{i + 1}  ·  {footer_title}")
            else:
                c.setFont("Helvetica", 8.5)
                c.drawCentredString(width / 2, 12 * 2.8346, str(i + 1))
        c.showPage()
    c.save()
    buf.seek(0)
    footer = PdfReader(buf)

    writer = PdfWriter()
    for i, page in enumerate(reader.pages):
        if bg is not None:
            page.merge_page(bg, over=False)   # 垫在最底层
        page.merge_page(footer.pages[i])      # 页脚盖在内容上
        writer.add_page(page)

    for title, page in sorted(bookmarks, key=lambda x: x[1]):
        if 1 <= page <= total:
            writer.add_outline_item(title, page - 1)

    if meta:
        writer.add_metadata(meta)

    with open(dst_pdf, "wb") as f:
        writer.write(f)
    return total


def main():
    ap = argparse.ArgumentParser(description="HTML 书稿 -> 成品 PDF")
    ap.add_argument("--html", required=True, help="源 HTML（会被回填目录页码）")
    ap.add_argument("--out", required=True, help="输出 PDF 路径")
    ap.add_argument("--title", default="", help="PDF 标题元数据")
    ap.add_argument("--subtitle", default="", help="PDF 主题元数据")
    ap.add_argument("--keywords", default="", help="PDF 关键词")
    ap.add_argument("--author", default="paper-explainer", help="PDF 作者")
    ap.add_argument("--browser", default=None, help="Edge/Chrome 可执行文件路径")
    ap.add_argument("--footer-skip", type=int, default=1, help="前 N 页不印页码（封面等）")
    ap.add_argument("--footer-title", default="", help="页脚书名，给定时印成「页码 · 书名」")
    ap.add_argument("--footer-color", default="#6b6a64", help="页脚颜色，默认 kami 暖灰")
    ap.add_argument("--page-bg", default="#f5f4ed",
                    help="满版页面底色，默认 kami 羊皮纸；传空字符串关闭")
    ap.add_argument("--no-page-bg", action="store_true", help="不垫满版底色")
    ap.add_argument("--retries", type=int, default=3, help="目录页码修正轮数上限（上限 3）")
    ap.add_argument("--no-toc-fastpath", action="store_true",
                    help="关闭目录单页快速路径（估算为单页时默认只跑 2 轮）")
    ap.add_argument("--keep-markers", action="store_true", help="保留不可见标记（默认最后一步剥离）")
    args = ap.parse_args()

    html_path = Path(args.html).resolve()
    out_pdf = Path(args.out).resolve()
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    if not html_path.exists():
        raise SystemExit(f"HTML 不存在: {html_path}")

    browser = find_browser(args.browser)
    tmp_dir = Path(tempfile.mkdtemp(prefix="pe_build_"))
    tmp_pdf = tmp_dir / "pass.pdf"

    current = html_path.read_text(encoding="utf-8")
    keys = set(m.group(2) for m in iter_toc(current))
    if not keys:
        print("⚠ 未找到任何 toc-pg 占位，目录页码将不会回填", file=sys.stderr)

    # ── 渲染流程：学习轮 → 回填 → 产出轮（回填与剥离合并）──
    # 最少 2 次渲染：页码必须先知道章节落在哪页才能填，填完必须重新排版一次。
    # 只有"剥离标记后总页数变化"这一种情况才需要第 3 次。
    # 前言与目录同页时，目录可用高度要扣掉前言那一块
    front_shared = bool(re.search(r'class="front"', current)) and bool(
        re.search(r"\.front\{[^}]*page-break-after:\s*avoid", current))
    est_mm, capacity, fits = toc_estimate(len(keys), front_shared)
    fast = fits and not args.no_toc_fastpath
    avail = TOC_AVAIL_MM - (FRONT_BLOCK_MM if front_shared else 0.0)
    print(f"目录预估：{len(keys)} 项 × {TOC_ITEM_MM} mm ≈ {est_mm:.0f} mm，"
          f"可用 {avail:.0f} mm（容量 {capacity} 项，{'与前言同页' if front_shared else '独占一页'}）"
          f" -> {'单页' if fits else '可能跨页'}，"
          f"{'快速路径，无需复核轮' if fast else '保留复核轮'}")

    rounds = 2 if fast else max(2, args.retries)
    mapping = {}
    texts = []
    converged = False
    for attempt in range(1, rounds + 1):
        # 学习轮：渲染源文件（含标记），定位每个章节的页码
        render(browser, html_path, tmp_pdf)
        texts = page_texts(tmp_pdf)
        mapping = locate(texts, keys or None) or mapping
        updated = fill_toc(current, mapping)
        if updated != current:
            current = updated
            html_path.write_text(current, encoding="utf-8")

        # 产出轮：回填页码 + 剥离标记，一次渲染完成
        n2, t2 = produce(browser, html_path, current, tmp_pdf, args.keep_markers)
        if n2 == len(texts):
            converged = True
            texts = t2
            print(f"第 {attempt} 轮：{n2} 页，已定位 {len(mapping)}/{len(keys)} 个章节，目录页码稳定")
            break

        print(f"⚠ 第 {attempt} 轮：剥离标记后页数由 {len(texts)} 变为 {n2}，"
              "目录页码整体偏移，需重算", file=sys.stderr)
        if not args.keep_markers:
            # 标记确实影响分页：退回保留标记（与旧行为一致，文本层会残留 @@KEY@@）
            n3, t3 = produce(browser, html_path, current, tmp_pdf, True)
            if n3 == len(texts):
                converged = True
                texts = t3
                print(f"⚠ 已回退为保留标记渲染（{n3} 页），文本层会残留 @@KEY@@", file=sys.stderr)
                break

    if not converged:
        print(f"⚠ 目录页码未收敛（已用满 {rounds} 轮），采用最后一轮结果", file=sys.stderr)

    missing = sorted(keys - set(mapping))
    if missing:
        print(f"⚠ 以下标记在 PDF 中未找到，对应目录页码为空: {', '.join(missing)}", file=sys.stderr)

    # 书签：按目录条目取标题与页码
    bookmarks = []
    for m in iter_toc(current):
        key, title = m.group(2), (m.group(3) or key)
        if key in mapping:
            bookmarks.append((title.strip(), mapping[key]))

    meta = {}
    if args.title:
        meta["/Title"] = args.title
    if args.subtitle:
        meta["/Subject"] = args.subtitle
    if args.keywords:
        meta["/Keywords"] = args.keywords
    if args.author:
        meta["/Author"] = args.author

    total = overlay(tmp_pdf, out_pdf, args.footer_skip, bookmarks, meta,
                    footer_title=args.footer_title, footer_color=args.footer_color,
                    page_bg="" if args.no_page_bg else args.page_bg)

    print(f"\n✓ 已生成: {out_pdf}")
    print(f"  总页数: {total}")
    print(f"  书签:   {len(bookmarks)} 条")
    print("  目录页码对照:")
    for m in iter_toc(current):
        key, title = m.group(2), (m.group(3) or key)
        pg = mapping.get(key)
        print(f"    {key:<10} {str(pg) if pg else '?':>4}  {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
