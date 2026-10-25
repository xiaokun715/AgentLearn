#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Markdown → PDF（浏览器打印管线）。

和「用浏览器打开 md、Ctrl+P 存成 PDF」是同一条路子：先把 Markdown 渲染成
一份排版好的 HTML，再用无头浏览器打印成 PDF。区别只在于把手工操作固化成
一条命令，并且补上了打印时才需要的细节（分页、页边距、代码高亮、中文字体、
页码、表格不跨页断裂、PDF 书签大纲）。

导航方式：**只生成 PDF 书签/大纲**（阅读器左侧可折叠面板），
不往正文里插入文本目录页 —— 后者既占版面，又要和分页规则搏斗。

之所以不用 reportlab / fpdf 这类纯 Python 方案：它们要自己实现换行、表格、
代码高亮的排版，中文换行尤其麻烦。而浏览器本来就是干这个的 ——
用同一套 CSS 引擎，结果也更容易和「浏览器里看到的」保持一致。

用法
----
    python md2pdf.py 第十八章：风控 Agent 设计说明书.md
    python md2pdf.py a.md b.md c.md
    python md2pdf.py --all                 # 当前目录下所有 .md
    python md2pdf.py --all --dir 技术面试题目答题模板
    python md2pdf.py input.md -o out.pdf
    python md2pdf.py input.md --chapter-breaks   # 每个 H1 另起一页
    python md2pdf.py input.md --keep-html         # 保留中间 HTML，便于调样式

    # 题库类文档（标题块 + 红字答案块 + 居中页码）
    python md2pdf.py 交叉主管面.md --theme doc         --header "非技术面试准备材料" --subtitle "非技术面试准备材料 · 答题模板"

依赖
----
    pip install markdown pygments playwright
    python -m playwright install chromium      # 只需一次

    没有 Playwright 时会自动退回本机的 Chrome / Edge（`--headless --print-to-pdf`）。
    退回模式下不支持页码。
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

# ----------------------------------------------------------------------
# 依赖检查（给出可执行的修复建议，而不是一句 ImportError）
# ----------------------------------------------------------------------
try:
    import markdown
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖：pip install markdown pygments")

try:
    from pygments.formatters import HtmlFormatter
    _PYGMENTS = True
except ImportError:  # pragma: no cover
    _PYGMENTS = False


# ======================================================================
# 样式
# ======================================================================
BASE_CSS = r"""
:root {
  --fg: #1a1a1a;
  --fg-soft: #4a4a4a;
  --border: #d9dde3;
  --bg-soft: #f6f8fa;
  --accent: #1f6feb;
  --accent-soft: #eaf2ff;
  --code-bg: #f6f8fa;
}

@page {
  size: A4;
  margin: 18mm 16mm 20mm 16mm;
}

* { box-sizing: border-box; }

html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }

body {
  margin: 0;
  color: var(--fg);
  background: #fff;
  font-family: "Microsoft YaHei", "PingFang SC", "Hiragino Sans GB",
               "Noto Sans CJK SC", "Source Han Sans SC", -apple-system,
               "Segoe UI", Helvetica, Arial, sans-serif;
  font-size: 10.5pt;
  line-height: 1.78;
  word-wrap: break-word;
  overflow-wrap: break-word;
}

/* ---------------- 标题 ---------------- */
h1, h2, h3, h4, h5, h6 {
  font-weight: 700;
  line-height: 1.35;
  margin: 1.6em 0 0.6em;
  break-after: avoid;          /* 标题不要孤零零留在页底 */
  page-break-after: avoid;
}
h1 {
  font-size: 20pt;
  margin-top: 0;
  padding-bottom: 0.35em;
  border-bottom: 2px solid var(--border);
  color: #0d1b2a;
}
h2 {
  font-size: 15.5pt;
  padding-left: 0.55em;
  border-left: 4px solid var(--accent);
  color: #12304f;
}
/* 文档副标题（``# 标题`` 紧接 ``## —— 副标题``）。
   **判据是写法，不是位置。** 只用「第一个 H1 后面的那个 H2」来判定，
   会把 `# 标题` 后面真正的第一节目录也当成副标题 ——
   那些文档（例如 `## 一、项目深挖：…`）的第一个二级标题会变成灰色小字，
   而第二个恢复成正常样式，看起来就像「两个同级标题格式不一样」。
   所以由 Python 侧检查文本是否以破折号开头，命中才打上 .subtitle。 */
h2.subtitle {
  font-size: 12.5pt;
  font-weight: 500;
  color: var(--fg-soft);
  border-left: 0;
  padding: 0 0 0.55em;
  margin: 0.25em 0 1.4em;
  border-bottom: 1px solid var(--border);
}
h3 { font-size: 12.8pt; color: #1b3a5c; }
h4 { font-size: 11.4pt; color: #2a4a6b; }
h5, h6 { font-size: 10.8pt; color: var(--fg-soft); }

/* ---------------- 正文 ---------------- */
p { margin: 0.7em 0; text-align: justify; }

a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

strong { font-weight: 700; color: #0d1b2a; }
em { font-style: italic; }

hr {
  border: 0;
  border-top: 1px solid var(--border);
  margin: 1.8em 0;
}

/* ---------------- 列表 ---------------- */
ul, ol { margin: 0.6em 0; padding-left: 1.7em; }
li { margin: 0.28em 0; }
li > p { margin: 0.25em 0; }
li::marker { color: var(--fg-soft); }

/* ---------------- 引用 ---------------- */
blockquote {
  margin: 1em 0;
  padding: 0.7em 1em;
  border-left: 4px solid var(--accent);
  background: var(--accent-soft);
  color: var(--fg-soft);
  break-inside: avoid;
  page-break-inside: avoid;
}
blockquote p:first-child { margin-top: 0; }
blockquote p:last-child { margin-bottom: 0; }

/* ---------------- 代码 ---------------- */
code {
  font-family: "Cascadia Code", "JetBrains Mono", Consolas,
               "Source Code Pro", "Courier New", monospace;
  font-size: 0.9em;
  background: var(--code-bg);
  border: 1px solid #e6eaef;
  border-radius: 3px;
  padding: 0.1em 0.35em;
  color: #b3271e;
}
pre {
  margin: 0.9em 0;
  padding: 0.85em 1em;
  background: var(--code-bg);
  border: 1px solid #e6eaef;
  border-radius: 5px;
  font-size: 8.6pt;
  /* 注意：Pygments 会注入 `pre { line-height: 125% }` 且排在本文件之后，
     所以这里写的倍数在 codehilite 包裹的代码块上并不会生效 ——
     想控制行高要用 `.codehilite pre`（见 DOC_THEME_CSS）。 */
  line-height: 1.55;
  /* 代码块很长时必须允许跨页，否则会整块被推到下一页甚至溢出 */
  break-inside: auto;
  page-break-inside: auto;
  overflow: visible;
}
pre code {
  background: none;
  border: 0;
  padding: 0;
  color: var(--fg);
  font-size: inherit;
  /* 长行折行而不是溢出纸张 */
  white-space: pre-wrap;
  word-break: break-word;
}

/* ---------------- 表格 ---------------- */
table {
  width: 100%;
  border-collapse: collapse;
  margin: 1em 0;
  font-size: 9pt;
  /* 窄表尽量不跨页；长表由 thead 重复表头兜底 */
  break-inside: auto;
  page-break-inside: auto;
}
thead { display: table-header-group; }   /* 跨页时自动重复表头 */
tfoot { display: table-footer-group; }
tr { break-inside: avoid; page-break-inside: avoid; }
th, td {
  border: 1px solid var(--border);
  padding: 0.45em 0.6em;
  text-align: left;
  vertical-align: top;
}
th {
  background: #eef2f7;
  font-weight: 700;
  color: #12304f;
}
tbody tr:nth-child(even) { background: #fbfcfd; }

/* ---------------- 图片 ---------------- */
img {
  max-width: 100%;
  height: auto;
  display: block;
  margin: 1em auto;
  break-inside: avoid;
  page-break-inside: avoid;
}

/* ---------------- 目录 ---------------- */
/* 这套样式只在文档里**显式写了 [TOC]** 时生效 —— 默认不再生成文本目录，
   导航由 PDF 书签/大纲承担。允许跨页：长目录不可能塞进一页。 */
.toc-wrap {
  margin: 1.2em 0 0;
  padding: 1em 1.2em;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg-soft);
}
.toc { break-inside: auto; page-break-inside: auto; }
.toc ul { list-style: none; padding-left: 1.15em; margin: 0.06em 0; }
.toc > ul { padding-left: 0; }
.toc li { margin: 0.08em 0; font-size: 9pt; line-height: 1.5; }
.toc a { color: var(--fg); }
/* 目录里把文档标题本身也列进去了（和正上方的标题重复）——
   视觉上淡一点，不至于喧宾夺主。 */
.toc > ul > li > a { font-weight: 600; }
.toc-title {
  font-weight: 700;
  font-size: 11pt;
  margin-bottom: 0.5em;
  color: #12304f;
}

/* ---------------- 打印细节 ---------------- */
/* 避免段落首行孤零零留在页底，或末行单独跑到下一页 */
p, li, blockquote { orphans: 3; widows: 3; }

/* 默认不在 H1 前强制分页；--chapter-breaks 时启用 */
.chapter-breaks h1 { break-before: page; page-break-before: always; }
/* 第一个 H1 是文档标题 —— 它前面不能分页，否则第一页会全空。 */
.chapter-breaks h1:first-of-type { break-before: auto; page-break-before: auto; }

/* 页脚页码 */
.pagenum {
  width: 100%;
  font-size: 8pt;
  color: #888;
  text-align: center;
  font-family: "Segoe UI", sans-serif;
}

/* 文档信息放在页脚（由浏览器打印参数渲染），不占正文首行 ——
   放在正文最前面会让第一页只剩一行元信息，标题反而被挤到下一页。 */

/* ---------------- 自动标题块 ---------------- */
/* 当 Markdown 没有 H1 时，由 --title / 文件名补一个标题块。
   放在正文最前面会让第一页只剩一行元信息，标题反而被挤到下一页。 */
.doc-subtitle {
  font-size: 9.5pt;
  color: #6a7280;
  margin: -0.15em 0 1.3em;
  padding-bottom: 0.9em;
  border-bottom: 1px solid var(--border);
}
"""

# ======================================================================
# 可选主题：复刻「浏览器打印 + 自定义 CSS」那套既有排版
# ======================================================================
#: 用于面试题库这类文档。它和默认主题的区别不只是配色，还有几处结构性差异：
#:
#: * 标题块（标题 + 副标题 + 深色横线）由文件名/参数补出来；
#: * 答案写在 ``` 围栏里，渲染成**浅底 + 蓝色左条 + 红字**的引用式块；
#: * 页脚只有一个居中的「N / M」。
#:
#: 默认主题不这么做，因为它同时要服务带真实代码的设计文档 ——
#: 把 `pre` 里的文字全染成红色，代码就没法看了。
DOC_THEME_CSS = r"""
:root {
  --fg: #1f2328;
  --fg-soft: #50565d;
  --border: #e3e8ee;
  --bg-soft: #f6f8fa;
  --accent: #2f6fbf;
  --accent-soft: #f6f8fa;
  --code-bg: #f6f8fa;
}

body { font-size: 9.8pt; line-height: 1.7; }

h1 {
  font-size: 21pt;
  color: #1f3a5e;
  border-bottom: 1.5pt solid #1f3a5e;
  padding-bottom: 0.45em;
  margin-bottom: 0.35em;
}
h2 {
  font-size: 13.5pt;
  color: #1f3a5e;
  border-left: 3pt solid #2f6fbf;
  padding-left: 0.5em;
  /* 题库的竖向节奏比技术文档松：题与题之间留出明显的呼吸区 */
  margin: 1.05em 0 0.45em;
}
h3 { font-size: 11.6pt; color: #1f3a5e; }
h4 { font-size: 10.4pt; color: #1f3a5e; }

p { margin: 0.55em 0; }

/* 围栏块 = 答案块：浅底 + 蓝左条 + 红字 */
pre {
  background: #f6f8fa;
  border: 1px solid #e3e8ee;
  border-left: 3pt solid #2f6fbf;
  border-radius: 4px;
  font-size: 9pt;
  padding: 1.0em 1em;
  margin: 0.6em 0;
}
/* Pygments 会注入 `pre { line-height: 125% }`，而且它排在自定义样式**之后**，
   同优先级下把我们设的行高覆盖掉 —— 表现是答案块比参考文档矮一截。
   用 `.codehilite pre`（多一个类，优先级更高）把它压回去。 */
.codehilite pre { line-height: 14.4pt; }
pre code { color: #b31c28; }

blockquote {
  background: #f6f8fa;
  border-left: 3pt solid #2f6fbf;
  color: #1f2328;
}

table { font-size: 9pt; }
th { background: #f0f3f7; color: #1f3a5e; }
"""

#: 可选主题。``default`` 不追加任何东西（样式都在 BASE_CSS 里）。
THEME_CSS: dict[str, str] = {
    "default": "",
    "doc": DOC_THEME_CSS,
}


#: Pygments 的代码高亮样式（浅色，打印友好）
Pygments_styles: str = ""


def pygments_css() -> str:
    global Pygments_styles
    if not _PYGMENTS:
        return ""
    if not Pygments_styles:
        Pygments_styles = HtmlFormatter(style="friendly").get_style_defs(".codehilite")
    return Pygments_styles


# ======================================================================
# Markdown → HTML
# ======================================================================
MD_EXTENSIONS = [
    "extra",          # 表格、围栏代码、脚注、属性列表…
    "toc",            # 标题锚点 + [TOC]
    "sane_lists",
    "admonition",
    "codehilite",
    "nl2br",          # 单个换行也当换行 —— 中文长文里更符合直觉
]

MD_EXTENSION_CONFIGS = {
    "toc": {"permalink": False, "toc_depth": "1-2"},
    "codehilite": {
        "guess_lang": False,
        "css_class": "codehilite",
        "linenums": False,
        "noclasses": False,
    },
}

TOC_PLACEHOLDER = "<!-- TOC -->"


def markdown_to_body(text: str) -> str:
    """渲染 Markdown 正文。

    **不再往正文里插入文本目录。** 导航交给 PDF 的书签/大纲（左侧面板），
    它在阅读器里是可折叠、可跳转的，而正文里的目录页只是一张静态列表 ——
    既占版面，又要和分页规则搏斗（长目录压标题、短目录留空白）。

    ``toc`` 扩展仍然保留：它负责给每个标题生成 ``id``，让文档内部的
    ``[跳转](#锚点)`` 链接能工作；只是不再把 ``md.toc`` 拼进正文。
    如果某份文档确实想要文本目录，在 Markdown 里自己写 ``[TOC]`` 即可。
    """
    md = markdown.Markdown(
        extensions=MD_EXTENSIONS,
        extension_configs=MD_EXTENSION_CONFIGS,
        output_format="html5",
    )
    return mark_subtitle(md.convert(text))


#: 副标题的写法约定：以破折号开头（``## —— 传统风控与 Agent 的分层协作``）
_SUBTITLE_PREFIXES = ("——", "—", "–", "--")


def mark_subtitle(html: str) -> str:
    """给紧跟在 H1 后面、且以破折号开头的 H2 打上 ``subtitle`` 类。

    为什么不直接在 CSS 里写 ``h1 + h2``：CSS 选择不到文本内容。
    而「是不是副标题」恰恰是一个**文本**判断 —— 位置相同的情况下，
    ``## —— 副标题`` 和 ``## 一、项目深挖`` 是两种东西。
    只按位置判断，会把「H1 后面的第一个二级标题」当成副标题，
    于是同一份文档里第一个 H2 是灰色小字、第二个恢复成正常样式。
    """
    pattern = re.compile(r"</h1>\s*<h2\b([^>]*)>(.*?)</h2>", re.DOTALL)
    match = pattern.search(html)
    if match is None:
        return html

    text = re.sub(r"<[^>]+>", "", match.group(2)).strip()
    if not text.startswith(_SUBTITLE_PREFIXES):
        return html

    attrs = match.group(1)
    if re.search(r"class\s*=", attrs):
        attrs = re.sub(r'class\s*=\s*"([^"]*)"',
                       lambda m: f'class="{m.group(1)} subtitle"', attrs)
    else:
        attrs = f'{attrs} class="subtitle"'

    replacement = f"</h1>\n<h2{attrs}>{match.group(2)}</h2>"
    return html[: match.start()] + replacement + html[match.end():]


def extract_title(text: str, fallback: str) -> str:
    """取第一个 H1 当标题（去掉 Markdown 标记）。"""
    for line in text.splitlines():
        match = re.match(r"^#\s+(.+?)\s*#*$", line)
        if match:
            return re.sub(r"[`*_]{1,3}", "", match.group(1)).strip()
    return fallback


def has_h1(text: str) -> bool:
    return any(re.match(r"^#\s+\S", line) for line in text.splitlines())


def build_title_block(title: str, subtitle: str) -> str:
    """Markdown 没有 H1 时，补一个标题块。

    很多文档（尤其是「每题一段」的题库）直接从 ``## 1. …`` 开始，
    没有文档标题。原样渲染出来的 PDF 第一页就是一道题，看起来像缺了头 ——
    所以这里用文件名（或 ``--title``）补一个。
    """
    block = f'<h1 class="doc-title">{escape(title)}</h1>\n'
    if subtitle:
        block += f'<div class="doc-subtitle">{escape(subtitle)}</div>\n'
    return block


def build_html(
    body: str,
    *,
    title: str,
    css: str,
    chapter_breaks: bool,
) -> str:
    classes = "chapter-breaks" if chapter_breaks else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>
{css}
{pygments_css()}
</style>
</head>
<body class="{classes}">
{body}
</body>
</html>
"""


def escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


# ======================================================================
# HTML → PDF
# ======================================================================
def find_browser() -> Optional[str]:
    """找一个可用于 `--headless --print-to-pdf` 的浏览器。"""
    candidates = [
        shutil.which("chrome"),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("msedge"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def pdf_param_names() -> set[str]:
    """当前 Playwright 的 ``page.pdf()`` 支持哪些参数。

    ``outline`` / ``tagged`` 是较新版本才有的（Chrome 的
    ``generateDocumentOutline``）。老版本传了会直接 TypeError，
    所以先探测再传，而不是假定用户装的是最新版。
    """
    try:
        import inspect

        from playwright.sync_api import Page

        return set(inspect.signature(Page.pdf).parameters)
    except Exception:  # noqa: BLE001 - 探测失败就当作不支持
        return set()


def render_with_playwright(
    html_path: Path,
    pdf_path: Path,
    *,
    title: str,
    page_numbers: bool,
    outline: bool,
    margin_mm: tuple[float, float, float, float],
    landscape: bool,
    header_text: str = "",
    footer_style: str = "title-page",
) -> None:
    """用 Playwright 的 Chromium 打印。优先走这条 —— 它能控制页眉页脚和 PDF 大纲。"""
    from playwright.sync_api import sync_playwright

    top, right, bottom, left = margin_mm
    supported = pdf_param_names()
    # **``outline`` 必须和 ``tagged`` 一起开。**
    # 只传 ``outline=True`` 时 Chrome 会静默忽略 —— 生成的 PDF 一条书签都没有，
    # 而且不报错。实测：outline=True/tagged=False → 0 条；两者都开 → 51 条。
    want_outline = outline and {"outline", "tagged"} <= supported

    # 页眉页脚模板是独立渲染的，拿不到页面 CSS —— 样式必须内联。
    # doc 主题沿用题库既有排版的小字号灰色页眉页脚；默认主题保持稍大一点。
    if footer_style == "page":
        base_style = (
            "width:100%;font-size:5.6pt;color:#50565d;"
            "font-family:'Microsoft YaHei','Segoe UI',sans-serif;"
            "padding:0 14.8mm;"
        )
    else:
        base_style = (
            "width:100%;font-size:8pt;color:#888;"
            "font-family:'Microsoft YaHei','Segoe UI',sans-serif;"
            "padding:0 15mm;"
        )
    if footer_style == "page":
        # 只有居中的「N / M」—— 题库类文档的既有排版就是这样
        footer = (
            f'<div style="{base_style}text-align:center;">'
            '<span class="pageNumber"></span> / <span class="totalPages"></span>'
            "</div>"
        )
    else:
        footer = (
            f'<div style="{base_style}display:flex;justify-content:space-between;">'
            f"<span>{escape(title)}</span>"
            "<span>第 <span class=\"pageNumber\"></span> / "
            '<span class="totalPages"></span> 页</span>'
            "</div>"
        )
    header = (
        f'<div style="{base_style}text-align:right;">{escape(header_text)}</div>'
        if header_text else "<div></div>"
    )
    show_chrome = page_numbers or bool(header_text)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(html_path.resolve().as_uri(), wait_until="load")
            # 等字体真正加载完，否则第一页可能用回退字体
            page.wait_for_timeout(250)
            page.emulate_media(media="print")
            print_kwargs: dict = {
                "path": str(pdf_path),
                "format": "A4",
                "landscape": landscape,
                "print_background": True,
                "display_header_footer": show_chrome,
                "header_template": header,
                "footer_template": footer if page_numbers else "<div></div>",
                "margin": {
                    "top": f"{top}mm",
                    "right": f"{right}mm",
                    "bottom": f"{bottom}mm",
                    "left": f"{left}mm",
                },
            }
            if want_outline:
                print_kwargs["outline"] = True
                print_kwargs["tagged"] = True
            page.pdf(**print_kwargs)
        finally:
            browser.close()


def render_with_chrome(
    html_path: Path,
    pdf_path: Path,
    *,
    browser_path: str,
    landscape: bool,
) -> None:
    """退回方案：直接调浏览器的 `--print-to-pdf`。

    这条路径**不支持页码**（Chrome 的 CLI 没有页眉页脚选项），
    分页与边距由 `@page` 控制。只有在没有 Playwright 时才用它。
    """
    cmd = [
        browser_path,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--no-first-run",
        "--disable-extensions",
        "--no-pdf-header-footer",
        # 输出路径必须是绝对路径：浏览器的工作目录不是你想象的当前目录
        f"--print-to-pdf={pdf_path.resolve()}",
        html_path.resolve().as_uri(),
    ]
    if landscape:
        cmd.insert(1, "--print-to-pdf-landscape")

    # **encoding 必须显式指定**：Windows 上默认用系统编码（简体中文是 GBK），
    # 而 Chrome 的 stderr 里可能有非 GBK 字节 —— 解码失败会让 stdout/stderr
    # 变成 None，然后连报错信息本身都崩掉（原始错误被掩盖成 TypeError）。
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if not pdf_path.exists():
        raise RuntimeError(
            "浏览器打印失败：\n"
            f"  exit={result.returncode}\n"
            f"  stdout={(result.stdout or '')[-800:]}\n"
            f"  stderr={(result.stderr or '')[-800:]}"
        )


# ======================================================================
# 单个文件的完整流程
# ======================================================================
def convert_one(
    src: Path,
    *,
    out: Optional[Path],
    extra_css: str,
    chapter_breaks: bool,
    page_numbers: bool,
    landscape: bool,
    margin_mm: tuple[float, float, float, float],
    show_meta: bool,
    outline: bool,
    keep_html: bool,
    browser_path: Optional[str],
    verbose: bool,
    theme: str = "default",
    title_override: str = "",
    subtitle: str = "",
    header_text: str = "",
) -> Path:
    text = src.read_text(encoding="utf-8")
    body = markdown_to_body(text)

    # 文档没有 H1 时补一个标题块（标题取 --title，否则用文件名）
    if not has_h1(text):
        body = build_title_block(title_override or src.stem, subtitle) + body

    title = title_override or extract_title(text, src.stem)

    css_parts = [BASE_CSS, THEME_CSS.get(theme, ""), extra_css]
    html_doc = build_html(
        body,
        title=title,
        css="\n".join(part for part in css_parts if part),
        chapter_breaks=chapter_breaks,
    )

    # **中间 HTML 必须写在与源文件同目录**：Markdown 里的相对图片路径
    # 是相对源文件解析的。写到临时目录会让所有图片失效。
    html_path = src.with_name(f".{src.name}.md2pdf.html")
    html_path.write_text(html_doc, encoding="utf-8")

    pdf_path = out or src.with_suffix(".pdf")
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        try:
            render_with_playwright(
                html_path, pdf_path, title=title if show_meta else "",
                page_numbers=page_numbers, outline=outline,
                margin_mm=margin_mm, landscape=landscape,
                header_text=header_text,
                footer_style="page" if theme == "doc" else "title-page",
            )
            engine = "playwright/chromium"
            if outline and {"outline", "tagged"} <= pdf_param_names():
                engine += " + outline"
        except ImportError:
            if not browser_path:
                raise RuntimeError(
                    "没有可用的浏览器。请任选其一：\n"
                    "  1) pip install playwright && python -m playwright install chromium\n"
                    "  2) 安装 Chrome 或 Edge"
                )
            render_with_chrome(
                html_path, pdf_path, browser_path=browser_path, landscape=landscape
            )
            engine = f"chrome-cli ({Path(browser_path).name})（无 PDF 大纲）"
    finally:
        if not keep_html:
            html_path.unlink(missing_ok=True)

    size_kb = pdf_path.stat().st_size / 1024
    if verbose:
        print(f"  ✓ {pdf_path}  ({size_kb:,.0f} KB, {engine})")
    return pdf_path


# ======================================================================
# CLI
# ======================================================================
def collect_inputs(args) -> list[Path]:
    if args.all:
        base = Path(args.dir)
        if not base.is_dir():
            sys.exit(f"目录不存在：{base}")
        files = sorted(p for p in base.glob("*.md") if not p.name.startswith("."))
    else:
        files = [Path(item) for item in args.files]

    if not files:
        sys.exit("没有找到要转换的 .md 文件。用 --all 或直接给出文件名。")

    missing = [str(p) for p in files if not p.is_file()]
    if missing:
        sys.exit(f"文件不存在：{missing}")
    return files


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="md2pdf",
        description="Markdown → PDF（浏览器打印管线：Markdown → HTML → 无头浏览器打印）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python md2pdf.py 第十八章：风控 Agent 设计说明书.md\n"
            "  python md2pdf.py --all\n"
            "  python md2pdf.py --all --dir 技术面试题目答题模板\n"
            "  python md2pdf.py a.md -o build/a.pdf --chapter-breaks --toc\n"
        ),
    )
    parser.add_argument("files", nargs="*", help="要转换的 Markdown 文件")
    parser.add_argument("--all", action="store_true", help="转换目录下所有 .md")
    parser.add_argument("--dir", default=".", help="配合 --all 使用的目录（默认当前目录）")
    parser.add_argument("-o", "--output", help="输出路径（仅在转换单个文件时有效）")
    parser.add_argument("--outdir", help="把所有输出放到这个目录")
    parser.add_argument("--css", help="额外的 CSS 文件，追加在内置样式之后")
    parser.add_argument("--chapter-breaks", action="store_true",
                        help="每个 H1 另起一页")
    # 保留这个参数只为兼容旧命令行：正文里不再插入文本目录（改由 PDF 大纲承担）。
    parser.add_argument("--toc", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-page-numbers", action="store_true",
                        help="不输出页脚页码")
    parser.add_argument("--no-outline", action="store_true",
                        help="不生成 PDF 书签/大纲（默认生成，便于阅读器左侧导航）")
    parser.add_argument("--landscape", action="store_true", help="横向纸张")
    parser.add_argument("--margin", default="18,16,20,16",
                        help="页边距：上,右,下,左（毫米），默认 18,16,20,16")
    parser.add_argument("--no-meta", action="store_true",
                        help="页脚不显示文档标题（只保留页码）")
    parser.add_argument("--theme", default="default", choices=sorted(THEME_CSS),
                        help="排版主题：default（技术文档）/ doc（题库：标题块 + 红字答案块 + 居中页码）")
    parser.add_argument("--title", default="",
                        help="文档标题（默认取第一个 H1；没有 H1 时用文件名）")
    parser.add_argument("--subtitle", default="",
                        help="标题下方的一行说明（仅在文档没有 H1、生成了标题块时显示）")
    parser.add_argument("--header", default="",
                        help="页眉文字，显示在右上角")
    parser.add_argument("--keep-html", action="store_true",
                        help="保留中间 HTML（调样式时有用）")
    parser.add_argument("-q", "--quiet", action="store_true", help="静默模式")
    args = parser.parse_args(argv)

    files = collect_inputs(args)
    if args.output and len(files) > 1:
        sys.exit("-o/--output 只能用于单个文件；多个文件请用 --outdir")

    try:
        top, right, bottom, left = (float(x) for x in args.margin.split(","))
    except ValueError:
        sys.exit("--margin 需要四个数字：上,右,下,左（毫米）")

    extra_css = ""
    if args.css:
        css_path = Path(args.css)
        if not css_path.is_file():
            sys.exit(f"CSS 文件不存在：{css_path}")
        extra_css = css_path.read_text(encoding="utf-8")

    # 先找好退回方案（Playwright 不可用时才用得上）
    fallback_browser = find_browser()

    if not args.quiet:
        print(f"md2pdf: {len(files)} 个文件 → PDF")

    failures = 0
    for src in files:
        if args.outdir:
            out = Path(args.outdir) / src.with_suffix(".pdf").name
        elif args.output:
            out = Path(args.output)
        else:
            out = None
        try:
            convert_one(
                src,
                out=out,
                extra_css=extra_css,
                chapter_breaks=args.chapter_breaks,
                page_numbers=not args.no_page_numbers,
                landscape=args.landscape,
                margin_mm=(top, right, bottom, left),
                show_meta=not args.no_meta,
                outline=not args.no_outline,
                keep_html=args.keep_html,
                theme=args.theme,
                title_override=args.title,
                subtitle=args.subtitle,
                header_text=args.header,
                browser_path=fallback_browser,
                verbose=not args.quiet,
            )
        except Exception as exc:  # noqa: BLE001 —— CLI 要把失败摊开但继续下一个
            failures += 1
            print(f"  ✗ {src.name}: {exc}", file=sys.stderr)

    if failures:
        print(f"\n完成：{len(files) - failures} 成功，{failures} 失败", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"\n完成：{len(files)} 个 PDF")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
