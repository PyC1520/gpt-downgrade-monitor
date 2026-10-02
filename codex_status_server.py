#!/usr/bin/env python3
"""codex 路由状态 loopback 服务 (2026-09-23)

只绑 127.0.0.1, 供 CodexBar 插件 (QuickJS 沙箱无本地文件权限) HTTP 拉取:
  GET /status → {"summary": {state, label}, "interactive": {...}, "probe": {...}, "history": [...]}
数据层与 SwiftBar 插件共用 codexroute.5m.py 的解析逻辑 (只读, 不产生请求)。
launchd 配置由安装时生成，源码不包含个人服务标识或绝对路径。
"""
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = os.path.expanduser("~")
SOURCE_DIR = Path(__file__).resolve().parent
CORE = str(SOURCE_DIR / "route_status_core.py")
DEFAULT_HIST = HOME + "/.codex/route_history.jsonl"
DEFAULT_SESSIONS = HOME + "/.codex/sessions"
DEFAULT_CONFIG = HOME + "/.codex/config.toml"
DEFAULT_PORT = 8791
WINDOW_SEC = 7200   # 「各窗口」只列最近 2h 内有活动的会话
ESCALATE_SEC = 1800  # 半小时内仍活跃的被替换窗口 → 整卡升级红

_spec = importlib.util.spec_from_file_location("route_status_core", CORE)
cr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cr)
_desktop_spec = importlib.util.spec_from_file_location(
    "route_desktop_sessions", str(SOURCE_DIR / "route_desktop_sessions.py"))
desktop = importlib.util.module_from_spec(_desktop_spec)
_desktop_spec.loader.exec_module(desktop)


def build_status(hist_path, sessions_dir, config_path=DEFAULT_CONFIG,
                 desktop_state_path=None, desktop_metadata_path=None, desktop_logs_dir=None):
    """三个信号 + 汇总裁定，附只读聊天标题和完整会话身份。"""
    sessions = cr.scan_sessions(sessions_dir)
    titles = cr.session_titles([s["session_id"] for s in sessions], os.path.dirname(sessions_dir))
    candidates = [s for s in sessions if s["model"] and s["tc_ts"]]
    latest = max(candidates, key=lambda s: s["tc_ts"]) if candidates else None
    rollout = None
    model, ts, act_ts = ((latest["model"], latest["tc_ts"], latest["act_ts"])
                         if latest else (None, None, None))
    if model:
        # 年龄按"最后活动"算 (长轮次下轮首 tc 会越来越老); 轮首时长另行暴露
        age = cr.age_of(act_ts or ts, utc=True)
        turn_age = cr.age_of(ts, utc=True)
        rollout = {"model": model, "ts": ts, "age": age,
                   "session_id": latest["session_id"], "sid": latest["sid"],
                   "title": titles.get(latest["session_id"]),
                   "age_text": (cr.fmt_age(age) + "前") if age is not None else "?",
                   "turn_age": turn_age,
                   "turn_age_text": (cr.fmt_age(turn_age) + "前") if turn_age is not None else "?"}
    history = cr.last_history(hist_path)
    if history:
        age = cr.age_of(history.get("ts"))
        history["age"] = age
        history["age_text"] = (cr.fmt_age(age) + "前") if age is not None else "?"
    extra = []
    if os.path.exists(hist_path):
        with open(hist_path, errors="ignore") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        for ln in lines[:-1][-9:][::-1]:
            try:
                extra.append(json.loads(ln))
            except ValueError:
                continue

    # 汇总: 更新的信号说了算 (与 SwiftBar 渲染层同一规则)
    if rollout and history:
        sig = rollout if (history.get("age") is None or
                          (rollout.get("age") is not None and rollout["age"] <= history["age"])) else history
    else:
        sig = rollout or history
    # 判定: "请求的是什么"决定 luna 是 自选(中性) 还是 替换(红)。
    # 2026-09-23 gpt-6-sol / gpt-6-luna 公售, 主动选它们是正常用法。
    requested = cr.configured_model(config_path) or ""

    # 各窗口 (多 session): 每个近期活跃会话一行, 被替换的标红;
    # 汇总不能只看最新一轮 — 你没盯着的窗口被降智也要红牌
    windows = []
    for s in sessions:
        if not s["act_ts"]:
            continue
        w_age = cr.age_of(s["act_ts"], utc=True)
        if w_age is None or w_age > WINDOW_SEC:
            continue
        windows.append({"sid": s["sid"], "session_id": s["session_id"],
                        "title": titles.get(s["session_id"]),
                        "model": s["model"], "age": w_age,
                        "age_text": cr.fmt_age(w_age) + "前",
                        "alert": bool(s["model"]) and "luna" in s["model"]
                        and "luna" not in requested})
        if len(windows) >= 8:
            break
    windows.sort(key=lambda w: w["age"])  # 最近活动的排前面

    if sig is None:
        state = "unknown"
    elif "model" in sig:
        m = sig["model"]
        if "astra" in m:
            state = "astra"
        elif "luna" in m and "luna" not in requested:
            state = "luna"          # 请求的不是 luna 却被服务 luna → 替换
        else:
            state = "other"         # 自选 luna / sol / terra / 其他 → 中性
    else:
        v = sig.get("verdict", "unknown")
        state = {"astra:real": "astra", "substituted:luna": "luna",
                 "chosen:luna": "other", "healthy(290)": "healthy",
                 "sleep(311)": "sleep"}.get(v, "unknown")
    labels = {"astra": "Astra 正常", "luna": "Luna 替换中", "sleep": "311 睡眠态",
              "healthy": "290 健康态", "unknown": "无数据"}
    # 升级: 任一仍在活跃 (≤ESCALATE_SEC) 的窗口被替换 → 整卡红, 即便最新一轮正常
    if state != "luna" and any(w["alert"] and w["age"] <= ESCALATE_SEC for w in windows):
        state = "luna"
    if state == "other":
        # 中性模型直接显示模型名 (gpt-6-sol / 自选 luna), 不再笼统"其他模型"
        label = sig.get("model") or {"chosen:luna": "Luna 自选"}.get(
            sig.get("verdict", ""), "其他模型")
    else:
        label = labels[state]
    codex_home = os.path.dirname(sessions_dir)
    desktop_windows = desktop.desktop_sessions(
        desktop_state_path or os.path.join(codex_home, ".codex-global-state.json"),
        desktop_metadata_path or os.path.join(codex_home, "route_desktop_metadata.json"),
        desktop_logs_dir or (HOME + "/Library/Logs/com.openai.codex"
                             if os.path.abspath(sessions_dir) == DEFAULT_SESSIONS else None))
    # dots 创建的本地任务可能同时有 rollout；以完整 ID 去重并保留来源。
    local_by_id = {w["session_id"]: w for w in windows if w["session_id"]}
    unique_desktop = []
    for window in desktop_windows:
        local = local_by_id.get(window["session_id"])
        if local is not None:
            local["source"] = window["source"]
        else:
            unique_desktop.append(window)
    return {"summary": {"state": state, "label": label},
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "interactive": rollout, "probe": history, "history": extra,
            "windows": windows, "desktop_windows": unique_desktop}


def make_server(port, hist_path, sessions_dir, config_path=DEFAULT_CONFIG):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] != "/status":
                self.send_error(404)
                return
            body = json.dumps(build_status(hist_path, sessions_dir, config_path),
                              ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass  # 静默, launchd 下不刷日志

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    httpd = make_server(port, DEFAULT_HIST, DEFAULT_SESSIONS)
    print(f"codex status server on http://127.0.0.1:{port}/status", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
