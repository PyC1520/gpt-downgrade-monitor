#!/usr/bin/env python3
"""codex 路由状态核心解析 (2026-09-23, 自 swiftbar/codexroute.5m.py 迁出)

被 codex_status_server.py (CodexBar 插件数据桥) 与单元测试共用。只读、无副作用。
数据源:
  ~/.codex/route_history.jsonl   codex-watch exec 探测历史 (ts=本地时间)
  ~/.codex/sessions/**/*.jsonl   rollout turn_context = 每轮实际生效模型 (ts=UTC)
"""
import glob
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path

HOME = os.path.expanduser("~")
HIST = HOME + "/.codex/route_history.jsonl"
SESSIONS = HOME + "/.codex/sessions"


def configured_model(config_path):
    """config.toml 顶层 model = 用户配置的默认请求模型 (交互会话的"请求侧")。

    只认第一个 model 赋值 (顶层; 写在 [profiles] 之前的那个)。
    2026-09-23 gpt-6-luna 公售后, "请求的是什么"成为区分 自选/替换 的关键。"""
    try:
        with open(config_path, errors="ignore") as fh:
            m = re.search(r'^model\s*=\s*"([^"]+)"', fh.read(), re.M)
    except OSError:
        return None
    return m.group(1) if m else None


def verdict(models, lens):
    """与 watch_state.py 同一套判据"""
    if "gpt-6-astra" in models:
        return "astra:real"
    if any("luna" in m for m in models):
        return "substituted:luna"
    if any(288 <= n <= 294 for n in lens):
        return "healthy(290)"
    if any(308 <= n <= 314 for n in lens):
        return "sleep(311)"
    return "unknown"


def newest_rollout(sessions_dir):
    files = glob.glob(sessions_dir + "/**/*.jsonl", recursive=True)
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def last_turn_context(path):
    """返回单个 rollout 里最后一条 turn_context 的 (生效模型, UTC 时间戳)
    codex-auto-review 是后台审查模型, 不代表用户对话的实际模型, 跳过"""
    model, ts = None, None
    with open(path, errors="ignore") as fh:
        for line in fh:
            if '"turn_context"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            m = (d.get("payload") or {}).get("model")
            if m and m != "codex-auto-review":
                model, ts = m, (d.get("timestamp") or "")[:19]
    return model, ts


def last_event_ts(path):
    """文件里最后一条可解析记录的 timestamp (UTC)。

    rollout 在整个轮次内持续追加事件 (token_count 等), 与只在轮首写的
    turn_context 不同 — 这是'最后活动'的正确口径 (2026-09-23 长轮次 bug)。"""
    ts = None
    with open(path, errors="ignore") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t = d.get("timestamp")
            if t:
                ts = t[:19]
    return ts


def session_profile(path):
    """只读日志元数据、模型和事件时间；不返回对话正文。

    桌面客户端可以给同一 session 写多份 rollout，身份以 session_meta.id 为准。
    """
    model, tc_ts, act_ts, session_id, cwd = None, None, None, None, None
    with open(path, errors="ignore") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            payload = d.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {}
            timestamp = (d.get("timestamp") or "")[:19]
            if timestamp and (act_ts is None or timestamp > act_ts):
                act_ts = timestamp
            if d.get("type") == "session_meta":
                session_id = payload.get("id") or session_id
                cwd = payload.get("cwd") or cwd
            elif d.get("type") == "turn_context":
                m = payload.get("model")
                if m and m != "codex-auto-review" and (tc_ts is None or timestamp >= tc_ts):
                    model, tc_ts = m, timestamp
    parts = os.path.basename(path).removesuffix(".jsonl").split("-")
    sid = str(session_id)[:8] if session_id else (parts[6][:8] if len(parts) > 6 else parts[-1][:8])
    return {"path": path, "session_id": session_id, "sid": sid, "cwd": cwd,
            "model": model, "tc_ts": tc_ts, "act_ts": act_ts}


def scan_sessions(sessions_dir, max_files=8):
    """最近会话画像，合并同一会话的多份日志；保留最新模型和最后活动时间。"""
    files = glob.glob(sessions_dir + "/**/*.jsonl", recursive=True)
    out, identities = [], {}
    # 多扫少量候选，避免同一会话的重复文件挤掉其他近期会话。
    for f in sorted(files, key=os.path.getmtime, reverse=True)[:max_files * 4]:
        try:
            profile = session_profile(f)
        except OSError:  # 并发归档/迁移时文件可能消失
            continue
        if profile["model"] is None and profile["act_ts"] is None:
            continue
        key = profile["session_id"] or f
        if key in identities:
            current = identities[key]
            if profile["tc_ts"] and (not current["tc_ts"] or profile["tc_ts"] > current["tc_ts"]):
                current["model"], current["tc_ts"] = profile["model"], profile["tc_ts"]
            if profile["act_ts"] and (not current["act_ts"] or profile["act_ts"] > current["act_ts"]):
                current["act_ts"] = profile["act_ts"]
        elif len(out) < max_files:
            identities[key] = profile
            out.append(profile)
        if len(out) >= max_files and all(s["model"] for s in out):
            break
    return out


def session_titles(session_ids, codex_home):
    """按完整 session ID 查询本地聊天标题；数据库不可用时退回短 ID。

    mode=ro 禁止写入，短超时避免标题查询阻塞状态服务。
    只使用 name（侧边栏标题）；title 可能保存整段原始提示词，禁止回退。
    """
    ids = [sid for sid in session_ids if sid]
    if not ids:
        return {}
    databases = sorted(Path(codex_home).glob("state_*.sqlite"),
                       key=lambda p: int(re.search(r"state_(\d+)", p.name).group(1))
                       if re.search(r"state_(\d+)", p.name) else -1, reverse=True)
    for database in databases:
        try:
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.2)) as con:
                placeholders = ",".join("?" for _ in ids)
                columns = {row[1] for row in con.execute("PRAGMA table_info(threads)")}
                if "name" not in columns:
                    return {}
                rows = con.execute(
                    f"SELECT id, NULLIF(TRIM(name), '') FROM threads WHERE id IN ({placeholders})", ids)
                return {sid: name for sid, name in rows if name}
        except (sqlite3.Error, OSError):
            continue
    return {}


def latest_effective_model(sessions_dir, max_files=8):
    """跨最近活跃的 rollout 文件, 返回 (生效模型, 轮首tc时间, 最后活动时间), 均 UTC。

    - 模型/轮首: 取时间戳最大的 turn_context。不能只看 mtime 最新的那一个文件:
      后台 auto-review / resume 会触碰旧会话文件刷新其 mtime, 而它最后一条
      turn_context 可能早已过时 (2026-09-23 年龄虚高根因之一)。
    - 最后活动: 所选模型所属会话的最后事件。turn_context 只在轮首写，
      长轮次按活动计算年龄；不能把另一个会话的活动挂到该模型上。"""
    best_model, best_tc, best_act = None, None, None
    for s in scan_sessions(sessions_dir, max_files):
        if s["model"] and s["tc_ts"] and (best_tc is None or s["tc_ts"] > best_tc):
            best_model, best_tc, best_act = s["model"], s["tc_ts"], s["act_ts"]
    return best_model, best_tc, best_act


def last_history(path):
    entry = None
    if os.path.exists(path):
        with open(path, errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
    return entry


def age_of(ts, utc=False):
    """utc=True 用于 rollout 时间戳 (实测为 UTC); route_history 的 ts 是本地时间"""
    try:
        dt = datetime.fromisoformat(ts)
        if utc:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, time.time() - dt.timestamp())
    except (ValueError, TypeError):
        return None


def fmt_age(sec):
    if sec < 60:
        return f"{int(sec)}s"
    if sec < 3600:
        return f"{int(sec // 60)}m"
    if sec < 86400:
        return f"{int(sec // 3600)}h"
    return f"{int(sec // 86400)}d"
