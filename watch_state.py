#!/usr/bin/env python3
"""codex 路由状态监视：解析 tungstenite trace → 记录 → 变化告警。

判据（本机 2026-09-22 实测标定）：
  响应 model 字段含 gpt-6-astra      → astra:real（真旗舰）
  响应 model 字段含 luna              → substituted:luna（被代服）
  turn-state 长度 288–294             → healthy(290)（健康态会话）
  turn-state 长度 308–314             → sleep(311)（睡眠/降级态会话）
历史记录: ~/.codex/route_history.jsonl（每次 exec 追加一行）
状态翻转时: 终端黄字告警 + macOS 通知
"""
import json
import os
import re
import subprocess
import sys
import time

HIST = os.path.expanduser("~/.codex/route_history.jsonl")


def parse_trace(text):
    models = sorted(set(re.findall(r'"model":"([^"]+)"', text)))
    tokens = re.findall(r"gAAAAAB[A-Za-z0-9_-]+", text)
    lens = sorted({len(t) for t in tokens if len(t) <= 400})
    return models, lens, ("x-codex-safety-buffering-enabled" in text)


def verdict(models, lens, requested=None):
    if "gpt-6-astra" in models:
        return "astra:real"
    if any("luna" in m for m in models):
        # 2026-09-23 gpt-6-luna 公售: 主动请求 luna 是正常用法, 不是降智
        return "chosen:luna" if (requested and "luna" in requested) else "substituted:luna"
    if any(288 <= n <= 294 for n in lens):
        return "healthy(290)"
    if any(308 <= n <= 314 for n in lens):
        return "sleep(311)"
    return "unknown"


def notify(title, msg):
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{msg}" with title "{title}"'],
            timeout=5, capture_output=True)
    except Exception:
        pass


def record(trace_path, requested=None):
    text = open(trace_path, errors="ignore").read()
    models, lens, buffering = parse_trace(text)
    state = verdict(models, lens, requested)
    if not models and not lens:
        # 捕获失败 (请求 400/网络断, trace 无任何模型信号) ≠ 路由变化:
        # 不落历史、不告警, 否则假翻转会污染记录并狼来了 (2026-09-23 实测)
        return {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "verdict": "capture_failed",
                "models": models, "turn_state_lens": lens,
                "safety_buffering": buffering}
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "verdict": state,
             "models": models, "turn_state_lens": lens,
             "safety_buffering": buffering}
    prev = None
    if os.path.exists(HIST):
        lines = [line for line in open(HIST).read().splitlines() if line.strip()]
        if lines:
            try:
                prev = json.loads(lines[-1])
            except ValueError:
                prev = None
    with open(HIST, "a") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    if prev and prev.get("verdict") != state:
        msg = f"{prev.get('verdict')} → {state}"
        print(f"\033[33m⚠ codex-watch 路由状态变化: {msg}\033[0m", file=sys.stderr)
        notify("Codex 路由状态变化", msg)
    return entry


if __name__ == "__main__":
    # argv: trace 路径 [请求的模型 (codex-watch 从命令行 -m 提取)]
    print(json.dumps(record(sys.argv[1],
                            sys.argv[2] if len(sys.argv) > 2 else None),
                     ensure_ascii=False))
