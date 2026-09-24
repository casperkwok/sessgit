#!/usr/bin/env python3
"""sessgit —— 给 AI 会话做版本管理：交接与分享，零账号零服务器。

    一次用户发言 = 一个 git commit。
    分享 = sessgit export（单文件 HTML，发文件不是发链接）。
    交接 = sessgit handoff（结构化轨迹，喂给下一个 Agent）。

只支持 Claude Code 会话（jsonl）。不联网、不上报、不要求注册。
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

STORE = ".sessgit"

# ---------------------------------------------------------------- 解析层

SKIP_PREFIXES = ("<local-command-caveat>", "<command-name>", "<command-message>",
                 "<command-args>", "<local-command-stdout>", "[Request interrupted",
                 "Caveat: the messages below")


def read_events(path):
    """逐行读 jsonl，容忍坏行。返回原始事件列表。"""
    events = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def event_text(ev):
    """取一个事件里的可读文本；没有返回 None。"""
    m = ev.get("message")
    if not isinstance(m, dict):
        return None
    c = m.get("content")
    out = []
    if isinstance(c, str):
        out.append(c)
    elif isinstance(c, list):
        for b in c:
            if isinstance(b, dict) and b.get("type") == "text":
                out.append(b.get("text", ""))
    t = "\n".join(x for x in out if x).strip()
    return t or None


def is_real_user(ev):
    """真正的用户发言（排除工具结果、命令回显、系统注入）。"""
    if ev.get("type") != "user" or ev.get("isMeta"):
        return False
    m = ev.get("message")
    if not isinstance(m, dict):
        return False
    c = m.get("content")
    if isinstance(c, list):
        kinds = {b.get("type") for b in c if isinstance(b, dict)}
        if kinds and kinds <= {"tool_result", "image"}:   # 工具结果不是用户说话
            return False
    t = event_text(ev)
    if not t:
        return False
    if t.startswith(SKIP_PREFIXES):
        return False
    # 整条都是 system-reminder 的不算
    stripped = clean_user_text(t)
    if not stripped or BARE_CMD.match(stripped):   # 裸斜杠命令不是对话
        return False
    return True


def clean_user_text(t):
    t = re.sub(r"<system-reminder>.*?</system-reminder>", "", t, flags=re.S)
    t = re.sub(r"<pasted_content[^>]*>.*?</pasted_content>", "", t, flags=re.S)
    return t.strip()


BARE_CMD = re.compile(r"^/[a-z][a-z0-9-]*\s*$")


def split_turns(events):
    """按真实用户发言切轮次。返回 [(用户事件, [后续事件...]), ...]"""
    turns, cur_user, buf = [], None, []
    for ev in events:
        if is_real_user(ev):
            if cur_user is not None:
                turns.append((cur_user, buf))
            cur_user, buf = ev, []
        elif cur_user is not None:
            buf.append(ev)
    if cur_user is not None:
        turns.append((cur_user, buf))
    return turns


def is_noise(ev):
    """命令回显、纯 system-reminder 等无信息事件。"""
    t = event_text(ev)
    if t and t.startswith(SKIP_PREFIXES):
        return True
    stripped = re.sub(r"<system-reminder>.*?</system-reminder>", "", t or "", flags=re.S).strip()
    return not stripped and not _has_blocks(ev, {"tool_use", "tool_result"})


def _has_blocks(ev, kinds):
    c = (ev.get("message") or {}).get("content")
    if not isinstance(c, list):
        return False
    return any(isinstance(b, dict) and b.get("type") in kinds for b in c)


def ev_ts(ev):
    ts = ev.get("timestamp")
    if not ts:
        return ""
    return str(ts)[:16].replace("T", " ")


# ---------------------------------------------------------------- 存储层（git）

def find_store():
    p = Path.cwd()
    while p != p.parent:
        if (p / STORE / "events").is_dir():
            return p / STORE
        p = p.parent
    sys.exit(f"没找到 {STORE}/（先 sessgit init，或 cd 到项目里）")


def git(store, *args, check=True):
    r = subprocess.run(["git", "-C", str(store), *args],
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.exit(f"git {' '.join(args)} 失败:\n{r.stderr}")
    return r.stdout.strip()


def cmd_init(args):
    d = Path.cwd() / STORE
    if d.exists():
        sys.exit(f"{d} 已存在")
    (d / "events").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True)
    meta = {"runtime": "claude-code", "created": dt.datetime.now().isoformat(timespec="seconds")}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", "sessgit init")
    print(f"✓ {d}")


def cmd_import(args):
    store = find_store()
    src = Path(args.file).expanduser().resolve()
    if not src.exists():
        sys.exit(f"文件不存在: {src}")
    events = read_events(src)
    turns = split_turns(events)
    if not turns:
        sys.exit("没识别出任何用户发言（确定是 Claude Code 的会话 jsonl？）")
    sid = next((e.get("sessionId") for e in events if e.get("sessionId")),
               hashlib.sha1(str(src).encode()).hexdigest()[:12])
    meta = json.loads((store / "meta.json").read_text(encoding="utf-8"))
    meta.update({"session_id": sid, "source": str(src),
                 "imported_at": dt.datetime.now().isoformat(timespec="seconds"),
                 "events": len(events), "turns": len(turns)})
    (store / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    t0 = dt.datetime.now()
    committed = 0
    for i, (u, rest) in enumerate(turns, 1):
        f = store / "events" / f"{i:06d}.json"
        f.write_text(json.dumps({"user": u, "rest": rest}, ensure_ascii=False), encoding="utf-8")
        title = clean_user_text(event_text(u) or "").splitlines()[0][:60]
        git(store, "add", "-A")
        if git(store, "diff", "--cached", "--name-only", check=False):
            git(store, "commit", "-q", "-m", f"#{i} {title}")
            committed += 1
    cost = (dt.datetime.now() - t0).total_seconds()
    print(f"✓ 导入 {len(turns)} 轮 / {len(events)} 条事件（新提交 {committed}，重复导入幂等），耗时 {cost:.1f}s")
    cost = (dt.datetime.now() - t0).total_seconds()
    print(f"✓ 导入 {len(turns)} 轮 / {len(events)} 条事件，提交耗时 {cost:.1f}s")
    print(f"  会话 {sid} ← {src.name}")


def load_turns(store):
    out = []
    for f in sorted((store / "events").glob("0*.json")):
        out.append(json.loads(f.read_text(encoding="utf-8")))
    return out


def turn_title(u, n=60):
    t = clean_user_text(event_text(u) or "").splitlines()[0]
    return t[:n]


# ---------------------------------------------------------------- 输出层

def _fmt_arg(key, val):
    """工具参数渲染成人能读的样子：多行字符串保留真换行，不再 json 转义。"""
    if isinstance(val, str) and "\n" in val:
        return f"{key}:\n{val[:6000]}"
    return f"{key}: {str(val)[:600]}"


def render_event(ev, sink):
    """把一个事件投递给 sink(kind, text, name, argsummary)。"""
    m = ev.get("message")
    if not isinstance(m, dict):
        return
    c = m.get("content")
    if isinstance(c, str):
        if c.strip():
            sink(m.get("role", "user"), c, None, None)
        return
    if not isinstance(c, list):
        return
    for b in c:
        if not isinstance(b, dict):
            continue
        k = b.get("type")
        if k == "text" and b.get("text", "").strip():
            sink(m.get("role", "assistant"), b["text"], None, None)
        elif k == "thinking":
            sink("thinking", b.get("thinking", b.get("text", "")), None, None)
        elif k == "tool_use":
            args = b.get("input") or {}
            brief = ", ".join(
                f"{kk}={str(vv).replace(chr(10), ' ⏎ ')[:40]}" for kk, vv in list(args.items())[:2])
            sink("tool", "  ".join(_fmt_arg(kk, vv) for kk, vv in args.items()), b.get("name", "?"), brief[:110])
        elif k == "tool_result":
            rc = b.get("content")
            txt = rc if isinstance(rc, str) else "\n".join(
                x.get("text", "") for x in rc if isinstance(x, dict)) if isinstance(rc, list) else ""
            sink("result", (txt or "")[:2000], None, None)


def cmd_log(args):
    store = find_store()
    meta = json.loads((store / "meta.json").read_text(encoding="utf-8"))
    turns = load_turns(store)
    print(f"会话 {meta.get('session_id','?')} · {len(turns)} 轮 · {meta.get('events','?')} 条事件")
    print(f"来源 {meta.get('source','?')}\n")
    for i, t in enumerate(turns, 1):
        ts = ev_ts(t["user"])
        print(f"#{i:<4d} {ts}  {turn_title(t['user'])}")


def cmd_show(args):
    store = find_store()
    turns = load_turns(store)
    upto = min(args.n, len(turns)) if args.n else len(turns)
    for i, t in enumerate(turns[:upto], 1):
        print("=" * 72)
        print(f"#{i} 用户 · {ev_ts(t['user'])}")
        print(clean_user_text(event_text(t["user"]) or ""))
        for ev in t["rest"]:
            if is_noise(ev):
                continue
            def sink(kind, text, name, brief, _i=i):
                if kind == "user":
                    print(f"  用户 · {text[:600]}")
                elif kind == "assistant":
                    print(f"  Claude · {text[:600]}")
                elif kind == "thinking":
                    print(f"  [思考] {text[:200]}")
                elif kind == "tool":
                    print(f"  [工具] {name}: {brief}")
                elif kind == "result":
                    print(f"  [结果] {text[:200].replace(chr(10),' ⏎ ')}")
            render_event(ev, sink)


# ---------------------------------------------------------------- 分享（HTML）

SECRET_PATTERNS = [
    (r"sk-[A-Za-z0-9_\-]{16,}", "API key (sk-…)"),
    (r"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}", "GitHub token"),
    (r"xox[abp]-[A-Za-z0-9\-]{10,}", "Slack token"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key"),
    (r"eyJhbGci[A-Za-z0-9_\-.]{20,}", "JWT"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key"),
]


def scan_secrets(text):
    found = []
    for pat, label in SECRET_PATTERNS:
        hits = re.findall(pat, text)
        if hits:
            found.append((label, len(hits)))
    return found


def esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,-apple-system,'PingFang SC',sans-serif;background:#F7F6F3;color:#1F1D1A;line-height:1.6;padding:40px 16px}
main{max-width:860px;margin:0 auto}
header{border-bottom:2px solid #1F1D1A;padding-bottom:18px;margin-bottom:26px}
h1{font-size:24px}
.meta{color:#7A746B;font-size:13px;margin-top:8px}
.badge{display:inline-block;background:#1F1D1A;color:#fff;border-radius:6px;font-size:12px;padding:2px 10px;margin-right:6px}
.turn{background:#fff;border:1px solid #E8E2D8;border-radius:14px;margin-bottom:22px;overflow:hidden}
.turn > summary{cursor:pointer;padding:14px 20px;font-weight:700;list-style:none;display:flex;gap:10px;align-items:baseline}
.turn > summary .no{color:#C8553D;min-width:44px}
.turn > summary .ts{color:#7A746B;font-weight:400;font-size:12px;margin-left:auto;white-space:nowrap}
.turn[open] > summary{border-bottom:1px solid #EEE9E0}
.body{padding:8px 20px 16px}
.msg{margin:12px 0;white-space:pre-wrap;word-break:break-word;font-size:14.5px}
.msg.user{background:#FBEDE8;border-radius:10px;padding:10px 14px}
.msg.assistant{padding:2px 2px}
.msg .who{font-size:12px;font-weight:700;color:#7A746B;display:block;margin-bottom:2px}
details.msg{border:1px solid #E8E2D8;border-radius:10px;padding:8px 14px;background:#FAFAF8}
details.msg summary{cursor:pointer;font-size:13px;color:#3A6B7E;font-family:Menlo,monospace}
details.msg pre{font-family:Menlo,monospace;font-size:12px;white-space:pre-wrap;word-break:break-word;margin-top:8px;color:#444}
footer{color:#9A948A;font-size:12px;text-align:center;margin-top:34px}
"""


def build_html(turns, meta):
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>sessgit · {esc(meta.get('session_id', '')[:12])}</title>",
        f"<style>{CSS}</style></head><body><main>",
        "<header><h1>💬 会话轨迹</h1>",
        f"<div class='meta'><span class='badge'>{len(turns)} 轮</span>"
        f"<span class='badge'>零 JS 单文件</span>"
        f"生成于 {dt.date.today()} · sessgit 导出 · "
        f"会话 <code>{esc(str(meta.get('session_id', ''))[:16])}</code></div></header>",
    ]
    for i, t in enumerate(turns, 1):
        open_ = " open" if i <= 1 else ""
        parts.append(f"<details class='turn'{open_}><summary>"
                     f"<span class='no'>#{i}</span>"
                     f"<span>{esc(turn_title(t['user'], 70))}</span>"
                     f"<span class='ts'>{esc(ev_ts(t['user']))}</span></summary>"
                     f"<div class='body'>")
        parts.append(f"<div class='msg user'><span class='who'>用户</span>"
                     f"{esc(clean_user_text(event_text(t['user']) or ''))}</div>")
        for ev in t["rest"]:
            if is_noise(ev):
                continue
            def sink(kind, text, name, brief):
                text = text or ""
                if kind == "assistant":
                    parts.append(f"<div class='msg assistant'><span class='who'>Claude</span>{esc(text)}</div>")
                elif kind == "thinking":
                    parts.append(f"<details class='msg'><summary>💭 思考过程</summary><pre>{esc(text)}</pre></details>")
                elif kind == "tool":
                    parts.append(f"<details class='msg'><summary>🔧 {esc(str(name))} · {esc(brief or '')}</summary><pre>{esc(text)}</pre></details>")
                elif kind == "result" and text.strip():
                    parts.append(f"<details class='msg'><summary>↩︎ 结果</summary><pre>{esc(text)}</pre></details>")
            render_event(ev, sink)
        parts.append("</div></details>")
    parts.append("<footer>由 sessgit 导出 · 人能读，Agent 也能接</footer></main></body></html>")
    return "\n".join(parts)


def cmd_export(args):
    store = find_store()
    meta = json.loads((store / "meta.json").read_text(encoding="utf-8"))
    turns = load_turns(store)
    if not turns:
        sys.exit("还没有导入任何会话")
    full_text = "\n".join(json.dumps(t, ensure_ascii=False) for t in turns)
    found = scan_secrets(full_text)
    if found and not args.yes:
        print("⚠️  扫到疑似敏感内容，默认拒绝导出：")
        for label, n in found:
            print(f"   · {label} × {n}")
        sys.exit("确认要带出去就加 --yes（自己把关）。")
    html = build_html(turns, meta)
    out = Path(args.o) if args.o else Path.cwd() / f"sessgit-{str(meta.get('session_id',''))[:8]}.html"
    out.write_text(html, encoding="utf-8")
    size = out.stat().st_size
    print(f"✓ {out}（{size/1024:.0f} KB · {len(turns)} 轮 · 单文件零 JS）")
    if found:
        print("  ⚠️ 你用了 --yes：导出内容含上面扫到的敏感项，分享前自己再核一遍")


# ---------------------------------------------------------------- 交接（MD）

def cmd_handoff(args):
    store = find_store()
    meta = json.loads((store / "meta.json").read_text(encoding="utf-8"))
    turns = load_turns(store)
    upto = min(args.n, len(turns)) if args.n else len(turns)
    lines = [f"# 交接文档 · 会话 {meta.get('session_id','?')}",
             f"> 由 sessgit 生成 · {upto} 轮 · 给下一个 Agent 读的轨迹摘要\n"]
    for i, t in enumerate(turns[:upto], 1):
        lines.append(f"## #{i} 用户 · {ev_ts(t['user'])}")
        lines.append(clean_user_text(event_text(t["user"]) or "")[:1500])
        tools, says = [], []
        for ev in t["rest"]:
            if is_noise(ev):
                continue
            def sink(kind, text, name, brief):
                if kind == "assistant" and text.strip():
                    says.append(text.strip())
                elif kind == "tool":
                    tools.append(f"{name}({brief or ''})")
            render_event(ev, sink)
        if tools:
            lines.append("**动过：** " + " · ".join(tools[:8]))
        if says:
            lines.append("**结论：** " + says[-1][:600].replace("\n", " "))
        lines.append("")
    out = Path(args.o) if args.o else Path.cwd() / "HANDOFF.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"✓ {out}（{upto} 轮）— 新会话直接让它读这个文件")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(prog="sessgit", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="在当前项目初始化 .sessgit")
    p = sub.add_parser("import", help="导入 Claude Code 会话 jsonl")
    p.add_argument("file")
    p = sub.add_parser("log", help="逐轮历史")
    p = sub.add_parser("show", help="读对话（默认全部，-n N 到第 N 轮）")
    p.add_argument("-n", type=int, default=None)
    p = sub.add_parser("export", help="导出单文件 HTML（分享）")
    p.add_argument("-o", default=None)
    p.add_argument("--yes", action="store_true", help="扫到敏感内容仍导出")
    p = sub.add_parser("handoff", help="生成交接 markdown（给下一个 Agent）")
    p.add_argument("-n", type=int, default=None)
    p.add_argument("-o", default=None)
    a = ap.parse_args()
    {"init": cmd_init, "import": cmd_import, "log": cmd_log,
     "show": cmd_show, "export": cmd_export, "handoff": cmd_handoff}[a.cmd](a)


if __name__ == "__main__":
    main()
