#!/usr/bin/env python3
"""统计目录下各类文件的代码行数，按行数从多到少输出。

用法:
    python3 countloc.py [目录] [--top N] [--exclude-comments]

代码行 = 去掉空行后的行数；加 --exclude-comments 后，纯注释行也不再计入
（首行 shebang 例外，算代码行）。排除 .git 和 node_modules。注释语法按
扩展名/文件名识别，不认识的类型（如 .md、.json、.csv）原样统计，不做剔除。
"""

import argparse
import os
import sys
import unicodedata
from collections import defaultdict

EXCLUDE_DIRS = {".git", "node_modules"}

# 无扩展名时按文件名归类
SPECIAL_NAMES = {
    "Dockerfile": "Dockerfile",
    "Makefile": "Makefile",
    "Makefile.am": "Makefile",
    "CMakeLists.txt": "CMakeLists.txt",
    "BUILD": "BUILD",
    "Rakefile": "Rakefile",
    "Gemfile": "Gemfile",
}

# ---- 注释语法表 ----------------------------------------------------------
# 值 = (行注释标记, 块注释 (开始, 结束) 对)

HASH_LANGS = {
    ".sh", ".bash", ".zsh", ".ksh", ".rb", ".pl", ".pm", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".r", ".jl", ".ps1", ".tcl", ".awk",
    ".tf", ".tfvars", ".properties", ".gitignore", ".editorconfig",
}
SLASH_LANGS = {
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".m", ".mm", ".java",
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs", ".swift",
    ".kt", ".kts", ".cs", ".php", ".scala", ".dart", ".groovy", ".proto",
    ".sol", ".zig", ".vala", ".v", ".sv", ".d",
}
DASH_LANGS = {".sql", ".hs", ".elm", ".ada", ".lua"}
SEMI_LANGS = {".lisp", ".clj", ".cljs", ".scm", ".el", ".rkt"}
HTML_LANGS = {".html", ".htm", ".xml", ".vue", ".svelte", ".svg"}


def _syntax(line=(), block=()):
    return (tuple(line), tuple(block))


COMMENT_SYNTAX = {}
for _e in HASH_LANGS:
    COMMENT_SYNTAX[_e] = _syntax(["#"])
for _e in SLASH_LANGS:
    COMMENT_SYNTAX[_e] = _syntax(["//"], [("/*", "*/")])
for _e in DASH_LANGS:
    COMMENT_SYNTAX[_e] = _syntax(["--"], [("--[[", "]]")])
for _e in SEMI_LANGS:
    COMMENT_SYNTAX[_e] = _syntax([";"], [("#|", "|#")])
for _e in HTML_LANGS:
    COMMENT_SYNTAX[_e] = _syntax([], [("<!--", "-->")])

COMMENT_SYNTAX[".py"] = _syntax(["#"], [('"""', '"""'), ("'''", "'''")])
COMMENT_SYNTAX[".css"] = _syntax([], [("/*", "*/")])
COMMENT_SYNTAX[".scss"] = _syntax(["//"], [("/*", "*/")])
COMMENT_SYNTAX[".less"] = _syntax(["//"], [("/*", "*/")])
for _n in ("Dockerfile", "Makefile", "Makefile.am", "CMakeLists.txt",
           "Rakefile", "Gemfile", "BUILD"):
    COMMENT_SYNTAX[_n] = _syntax(["#"])

TRIPLE_QUOTES = ('"""', "'''")
STRING_QUOTES = "\"'`"
SHEBANG = "#!"  # 首行 shebang 是给内核看的可执行声明，算代码行


def skip_string(text, i, quote):
    """i 指向起始引号，返回字符串结束后的下标。"""
    i += 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
        elif text[i] == quote:
            return i + 1
        else:
            i += 1
    return i


def analyze_line(text, syntax, block_end, lineno=1):
    """剔除注释，返回 (本行是否还有代码, 跨行块注释的新状态)。

    只按标记切分，不解析语法：字符串字面量会整体跳过（所以 `"http://x"`
    里的 `//` 不会误判成注释），但正则字面量、heredoc 之类仍可能误判。
    """
    line_tokens, block_pairs = syntax
    i, n = 0, len(text)
    has_code = False
    while i < n:
        if block_end is not None:  # 已在块注释里，只找结束标记
            if text.startswith(block_end, i):
                i += len(block_end)
                block_end = None
            else:
                i += 1
            continue

        entered_block = False
        for start, end in block_pairs:
            if text.startswith(start, i):
                # Python 三引号只在行首（前面还没有代码）时算 docstring；
                # 赋值语句里的三引号是字符串，交给下面的字符串分支。
                if start in TRIPLE_QUOTES and has_code:
                    break
                block_end = end
                i += len(start)
                entered_block = True
                break
        if entered_block:
            continue

        for tok in line_tokens:  # 行注释：本行剩下的都不要了
            if text.startswith(tok, i):
                if lineno == 1 and i == 0 and text.startswith(SHEBANG):
                    return True, block_end
                return has_code, block_end
        if text[i] in STRING_QUOTES:
            has_code = True
            i = skip_string(text, i, text[i])
            continue
        if not text[i].isspace():
            has_code = True
        i += 1

    return has_code, block_end


def ext_of(filename):
    """返回文件的类型标识，如 .py / .js；无扩展名返回文件名本身。"""
    base = os.path.basename(filename)
    if base in SPECIAL_NAMES:
        return SPECIAL_NAMES[base]
    _, ext = os.path.splitext(base)
    return ext.lower() or base


def count_lines(path, key, exclude_comments):
    """统计单个文件的代码行数；二进制文件返回 None。"""
    syntax = COMMENT_SYNTAX.get(key) if exclude_comments else None
    lines = 0
    block_end = None
    try:
        with open(path, "r", encoding="utf-8", errors="strict") as f:
            for lineno, line in enumerate(f, start=1):
                if syntax is None:
                    if line.strip():
                        lines += 1
                else:
                    has_code, block_end = analyze_line(line, syntax,
                                                       block_end, lineno)
                    if has_code:
                        lines += 1
    except (UnicodeDecodeError, OSError):
        return None
    return lines


def scan(root, exclude_comments):
    """遍历 root，返回 {类型: [文件数, 代码行数]}。"""
    stats = defaultdict(lambda: [0, 0])
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for name in filenames:
            key = ext_of(name)
            n = count_lines(os.path.join(dirpath, name), key, exclude_comments)
            if n is None:  # 二进制或不可读，跳过
                continue
            stat = stats[key]
            stat[0] += 1
            stat[1] += n
    return stats


def display_width(s):
    """终端显示宽度：中日韩等宽字符占 2 列。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def pad(s, width):
    return s + " " * max(0, width - display_width(s))


def rpad(s, width):
    return " " * max(0, width - display_width(s)) + s


def render(rows, width):
    print(f"{pad('类型', width)}  {rpad('文件数', 6)}  {rpad('代码行数', 8)}")
    print("-" * (width + 18))
    for label, files, lines in rows:
        print(f"{pad(label, width)}  {rpad(str(files), 6)}  {rpad(str(lines), 8)}")


def main():
    ap = argparse.ArgumentParser(
        description="统计目录下各类文件的代码行数，按行数从多到少输出")
    ap.add_argument("dir", nargs="?", default=".", help="要统计的目录（默认当前目录）")
    ap.add_argument("--top", type=int, metavar="N", help="只显示前 N 类")
    ap.add_argument("--exclude-comments", action="store_true",
                    help="剔除注释行后再统计（只对认识的注释语法生效）")
    args = ap.parse_args()

    if args.top is not None and args.top < 1:
        ap.error("--top 必须 >= 1")
    if not os.path.isdir(args.dir):
        sys.exit(f"错误: 不是目录: {args.dir}")

    stats = scan(args.dir, args.exclude_comments)
    if not stats:
        print(f"{args.dir} 下没有可统计的文本文件")
        return

    ranked = sorted(stats.items(), key=lambda kv: kv[1][1], reverse=True)
    shown = ranked[: args.top] if args.top else ranked
    rest = ranked[len(shown):]

    rows = [(k, v[0], v[1]) for k, v in shown]
    if rest:  # 被 --top 截掉的部分单独汇总，不混进合计
        rows.append((f"其余 {len(rest)} 类", sum(v[0] for _, v in rest),
                     sum(v[1] for _, v in rest)))
    width = max(display_width(label) for label, _, _ in rows)
    width = max(width, display_width("合计"))
    render(rows, width)

    print("-" * (width + 18))
    total_files = sum(v[0] for v in stats.values())
    total_lines = sum(v[1] for v in stats.values())
    print(f"{pad('合计', width)}  {rpad(str(total_files), 6)}  "
          f"{rpad(str(total_lines), 8)}")


if __name__ == "__main__":
    main()
