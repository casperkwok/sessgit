#!/usr/bin/env python3
"""生成合成会话 jsonl（测试样本，不含任何真实数据）。含一个假密钥用来验密钥闸。"""
import json, uuid, sys
from datetime import datetime, timedelta, timezone

def ev(t, role, content, minutes, sid="fix-session-0001", cwd="/tmp/demo"):
    return {"type": t, "message": {"role": role, "content": content},
            "timestamp": (datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)).isoformat(),
            "uuid": str(uuid.uuid4()), "sessionId": sid, "cwd": cwd}

def tool_use(id, name, inp):
    return {"type": "tool_use", "id": id, "name": name, "input": inp}

def tool_result(id, text):
    return {"type": "tool_result", "tool_use_id": id, "content": text}

rows = [
    ev("user", "user", "帮我写一个把 CSV 转成 JSON 的脚本，字段第一行是表头", 0),
    ev("assistant", "assistant", [{"type": "thinking", "thinking": "用户要 csv→json，标准库 csv 就够，先看文件结构"}], 1),
    ev("assistant", "assistant", [{"type": "tool_use", **tool_use("t1", "Bash", {"command": "head -3 data.csv"})}], 1),
    ev("user", "user", [{"type": "tool_result", **tool_result("t1", "name,age,city\n张三,31,杭州\n李四,28,苏州")}], 2),
    ev("assistant", "assistant", [{"type": "text", "text": "表头三列：name/age/city。写转换脚本："}], 3),
    ev("assistant", "assistant", [{"type": "tool_use", **tool_use("t2", "Write", {"file_path": "csv2json.py", "content": "import csv,json\n...sk-FAKEKEY1234567890abcdef..."})}], 3),
    ev("user", "user", [{"type": "tool_result", **tool_result("t2", "文件已写入")}], 4),
    ev("assistant", "assistant", [{"type": "text", "text": "脚本写好了，跑一下验证："}], 5),
    ev("assistant", "assistant", [{"type": "tool_use", **tool_use("t3", "Bash", {"command": "python3 csv2json.py data.csv"})}], 5),
    ev("user", "user", [{"type": "tool_result", **tool_result("t3", '[{"name": "张三", "age": "31", "city": "杭州"}, {"name": "李四", "age": "28", "city": "苏州"}]')}], 6),
    ev("assistant", "assistant", [{"type": "text", "text": "两条记录都转出来了。注意 age 是字符串，需要数字的话加一行转换。"}], 7),
    ev("user", "user", " age 转成数字。另外不要用 sk-FAKEKEY1234567890abcdef 这种写死的东西", 8),
    ev("assistant", "assistant", [{"type": "text", "text": "已改：int(row['age'])，密钥那行删掉了。"}], 9),
    # 干扰项：工具结果伪装的 user 行、命令回显、system-reminder —— 都不该被当成用户发言
    ev("user", "user", [{"type": "tool_result", **tool_result("t9", "fake user line")}], 10),
    ev("user", "user", "<local-command-caveat>caveat</local-command-caveat>", 10),
    ev("user", "user", "<system-reminder>only a reminder</system-reminder>", 10),
]
with open(sys.argv[1] if len(sys.argv) > 1 else "fixture.jsonl", "w", encoding="utf-8") as f:
    for r in rows:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
print("fixture 写好：4 轮真实用户发言（另有 3 行干扰项应被忽略）")
