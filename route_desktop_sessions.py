"""只读 dots/云端会话显示桥：桌面缓存、任务关联和有时间戳的活动。

缓存模型仅为模型记录，不能证明当前服务端实际模型。无模型时保留 None。
不读取凭据、不发送模型请求，不把文件修改时间当成任务状态更新时间。
"""
import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path

FRESH_STATUS_SEC = 120
WINDOW_SEC = 7200
UUID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.I)
LOG_ID = re.compile(r"(?:threadId|conversationId)=([0-9a-f-]{36})\b", re.I)


def read_json(path):
    try:
        if not path or Path(path).stat().st_size > 4 * 1024 * 1024:
            return {}
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def epoch(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        stamp = value / 1000 if value > 1e11 else value
        return stamp if math.isfinite(stamp) and 0 < stamp < 32503680000 else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
        except ValueError:
            pass
    return None


def activity_from_logs(directory, ids, now):
    """有界读取近期日志。浏览器打开、thread/read 等 UI 操作不算运行活动。"""
    observations = {}
    if not directory or not ids:
        return observations
    root = Path(directory)
    dates = {datetime.fromtimestamp(now - offset, timezone.utc).strftime("%Y/%m/%d")
             for offset in (0, 86400)}
    files = []
    for date in dates:
        files.extend((root / date).glob("codex-desktop-*.log"))
    try:
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return observations
    for path in files[:6]:
        try:
            with path.open("rb") as fh:
                size = path.stat().st_size
                start = max(0, size - 2 * 1024 * 1024)
                fh.seek(start)
                if start:
                    fh.readline()  # 丢掉不完整的首行
                lines = fh.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for line in lines:
            # 这些是应用记录的运行事件，不解析摘要正文或工具参数。
            if "Reasoning summary item completed" in line:
                kind = "activity"
            elif "Received turn/completed" in line:
                kind = "idle"
            elif "Received turn/started" in line:
                kind = "active"
            else:
                continue
            match = LOG_ID.search(line)
            if not match or match[1] not in ids:
                continue
            timestamp = epoch(line.split(" ", 1)[0])
            if timestamp is None or timestamp > now + 60 or now - timestamp > WINDOW_SEC:
                continue
            old = observations.get(match[1])
            if old is None or timestamp > old["timestamp"]:
                observations[match[1]] = {"timestamp": timestamp, "status": kind}
    return observations


def desktop_sessions(state_path, metadata_path=None, logs_dir=None, now=None):
    """返回白名单元数据。dots 归属以桌面任务关联为准，不泛化 durable。"""
    now = time.time() if now is None else now
    state = read_json(state_path)
    atoms = state.get("electron-persisted-atom-state", {})
    if not isinstance(atoms, dict):
        atoms = {}
    cache = atoms.get("cloud-aeon-sidebar-cache-v1", {})
    if not isinstance(cache, dict):
        cache = {}
    account = cache.get("accountId")
    account = account if isinstance(account, str) else None
    profiles = cache.get("profilesByThreadId", {})
    profiles = profiles if isinstance(profiles, dict) else {}
    dots_ids = set(profiles)
    links = atoms.get("aeon-subtasks-by-account-v1", {})
    links = links.get(account, {}) if isinstance(links, dict) else {}
    if isinstance(links, dict):
        for locator, children in links.items():
            try:
                host, parent = json.loads(locator)
            except (ValueError, TypeError):
                continue
            if host == "durable" and isinstance(parent, str):
                dots_ids.add(parent)
            if isinstance(children, list):
                dots_ids.update(sid for sid in children if isinstance(sid, str))

    rows = {}
    def row(sid):
        if not isinstance(sid, str) or not UUID.fullmatch(sid):
            return None
        return rows.setdefault(sid, {"session_id": sid, "sid": sid[-8:], "title": None,
                                    "model": None, "model_at": None, "cwd": None,
                                    "activity_at": None, "status_at": None,
                                    "status": "unknown", "tracked": False})

    for sid in dots_ids:
        row(sid)
    seed = read_json(metadata_path)
    # 工具提供的标题/状态补充缓存中没有的项目会话；保留其实际采集时间。
    if account and seed.get("account_id") == account:
        for entry in seed.get("threads", []) if isinstance(seed.get("threads"), list) else []:
            if not isinstance(entry, dict) or entry.get("host_id") != "durable":
                continue
            out = row(entry.get("session_id"))
            if out is None:
                continue
            for key in ("title", "cwd", "status"):
                value = entry.get(key)
                if isinstance(value, str):
                    out[key] = value
            out["status_at"] = epoch(entry.get("verified_at"))
            out["activity_at"] = epoch(entry.get("updated_at"))
            out["tracked"] = entry.get("tracked") is True

    cached_threads = cache.get("threads", [])
    for thread in cached_threads if isinstance(cached_threads, list) else []:
        if not isinstance(thread, dict):
            continue
        out = row(thread.get("id"))
        if out is None:
            continue
        if isinstance(thread.get("name"), str) and thread["name"].strip():
            out["title"] = thread["name"]
        if isinstance(thread.get("cwd"), str):
            out["cwd"] = thread["cwd"]
        model = thread.get("model")
        if isinstance(model, str) and model and model != "codex-auto-review":
            out["model"] = model
            out["model_at"] = epoch(thread.get("updatedAt"))
        stamp = epoch(thread.get("updatedAt"))
        if stamp and (out["activity_at"] is None or stamp > out["activity_at"]):
            out["activity_at"] = stamp
        # cache refresh / 文件 mtime 不证明 status 新鲜，不采用缓存的 active 标志。

    for sid, out in rows.items():
        profile = profiles.get(sid, {})
        if not out["title"] or out["title"] == sid:
            name = profile.get("display_name") if isinstance(profile, dict) else None
            out["title"] = (name + " · 主会话") if isinstance(name, str) and name else None
        stamp = epoch(atoms.get("aeon-last-activity-v1:" + sid))
        if stamp and (out["activity_at"] is None or stamp > out["activity_at"]):
            out["activity_at"] = stamp

    outputs = atoms.get("orbit-outputs-v1", [])
    for artifact in outputs if isinstance(outputs, list) else []:
        if not isinstance(artifact, dict) or artifact.get("hostId") != "durable":
            continue
        out = rows.get(artifact.get("threadId"))
        produced = artifact.get("artifact") or {}
        stamp = epoch(produced.get("producedAtMs")) if isinstance(produced, dict) else None
        if out and stamp and (out["activity_at"] is None or stamp > out["activity_at"]):
            out["activity_at"] = stamp

    for sid, event in activity_from_logs(logs_dir, set(rows), now).items():
        out = rows[sid]
        stamp = event["timestamp"]
        if out["activity_at"] is None or stamp > out["activity_at"]:
            out["activity_at"] = stamp
        if out["status_at"] is None or stamp > out["status_at"]:
            out["status"], out["status_at"] = event["status"], stamp

    result = []
    for sid, out in rows.items():
        age = max(0, now - out["activity_at"]) if out["activity_at"] else None
        if not out["tracked"] and (age is None or age > WINDOW_SEC):
            continue
        status_age = max(0, now - out["status_at"]) if out["status_at"] and out["status_at"] <= now + 60 else None
        previous = out["status"]
        if status_age is None or status_age > FRESH_STATUS_SEC:
            out["status"] = "unknown"
        out.update({"source": "dots" if sid in dots_ids else "cloud", "age": age,
                    "status_age": status_age, "last_status": previous, "alert": False,
                    "model_source": "desktop_cache" if out["model"] else None,
                    "model_age": max(0, now - out["model_at"]) if out["model_at"] else None})
        result.append(out)
    result.sort(key=lambda s: (not s["tracked"], s["age"] if s["age"] is not None else float("inf")))
    return result[:8]
