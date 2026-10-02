#!/usr/bin/env python3
"""codex_status_server.py 的单元测试 (loopback JSON 状态服务)"""
import importlib.util
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

SOURCE_ROOT = Path(__file__).resolve().parents[1]
SERVER = str(SOURCE_ROOT / "codex_status_server.py")
spec = importlib.util.spec_from_file_location("codex_status_server", SERVER)
srv = importlib.util.module_from_spec(spec)


def write(path, lines):
    with open(path, "w") as fh:
        for line in lines:
            fh.write(line + "\n")


class ServerBase(unittest.TestCase):
    """起一个临时服务器 (独立 fixture 数据, 随机端口)"""

    def start(self, rollout_lines=None, hist_lines=None, config_model="gpt-6-astra",
              extra_rollouts=None):
        """extra_rollouts: [(文件名, 行列表)] — 多窗口 fixture"""
        self.dir = tempfile.mkdtemp()
        sessions = os.path.join(self.dir, "sessions")
        os.makedirs(sessions)
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        write(os.path.join(sessions, "rollout-x.jsonl"), rollout_lines if rollout_lines is not None else [
            json.dumps({"timestamp": now, "type": "turn_context",
                        "payload": {"model": "gpt-6-astra"}})])
        for name, lines in (extra_rollouts or []):
            write(os.path.join(sessions, name), lines)
        self.hist = os.path.join(self.dir, "route_history.jsonl")
        write(self.hist, hist_lines if hist_lines is not None else [
            json.dumps({"ts": now, "verdict": "astra:real", "models": ["gpt-6-astra"],
                        "turn_state_lens": [], "safety_buffering": False})])
        self.config = os.path.join(self.dir, "config.toml")
        write(self.config, [f'model = "{config_model}"'])
        spec.loader.exec_module(srv)  # 重载以拿到干净模块
        self.httpd = srv.make_server(0, self.hist, sessions, self.config)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def get(self, path="/status"):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return r.status, r.headers.get("Content-Type"), json.loads(r.read())

    def tearDown(self):
        if hasattr(self, "httpd"):
            self.httpd.shutdown()
            self.httpd.server_close()
            self.thread.join(timeout=2)


class TestStatus(ServerBase):
    def test_astra_snapshot(self):
        self.start()
        status, ctype, data = self.get()
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        self.assertEqual(data["summary"]["state"], "astra")
        self.assertEqual(data["interactive"]["model"], "gpt-6-astra")
        self.assertEqual(data["probe"]["verdict"], "astra:real")
        self.assertIsInstance(data["history"], list)
        self.assertIn("age_text", data["interactive"])  # 年龄由服务端预格式化, JS 端零逻辑

    def test_luna_rollout_wins_when_newer(self):
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.start(
            rollout_lines=[json.dumps({"timestamp": now, "type": "turn_context",
                                       "payload": {"model": "gpt-5.6-luna"}})],
            hist_lines=[json.dumps({"ts": "2026-09-20T00:00:00", "verdict": "astra:real",
                                    "models": ["gpt-6-astra"], "turn_state_lens": [],
                                    "safety_buffering": False})])
        _, _, data = self.get()
        self.assertEqual(data["summary"]["state"], "luna")

    def test_unknown_when_no_data(self):
        self.start(rollout_lines=[], hist_lines=[])
        _, _, data = self.get()
        self.assertEqual(data["summary"]["state"], "unknown")

    def test_long_turn_age_uses_last_activity(self):
        # 回归: 长轮次下卡片年龄按"最后活动"而非"轮次开始", 否则越刷新越老
        self.start(rollout_lines=[
            json.dumps({"timestamp": "2026-09-23T03:09:47", "type": "turn_context",
                        "payload": {"model": "gpt-6-astra"}}),
            json.dumps({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                        "type": "event_msg",
                        "payload": {"type": "token_count"}}),   # 刚刚 (UTC) 的活动
        ])
        _, _, data = self.get()
        self.assertEqual(data["interactive"]["model"], "gpt-6-astra")
        self.assertIn("age_text", data["interactive"])
        self.assertTrue(data["interactive"]["age"] < 120)         # 按最后活动算, 秒级
        self.assertIn("turn_age_text", data["interactive"])       # 轮首时长另行暴露
        self.assertGreater(data["interactive"]["turn_age"],
                           data["interactive"]["age"])            # 轮次更长

    def _tc(self, model):
        # 交互 turn_context fixture (ts=UTC 口径)
        return [json.dumps({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                            "type": "turn_context", "payload": {"model": model}})]

    def _stale_hist(self):
        return [json.dumps({"ts": "2026-09-20T00:00:00", "verdict": "astra:real",
                            "models": ["gpt-6-astra"], "turn_state_lens": [],
                            "safety_buffering": False})]

    def test_gpt6_luna_substitution_when_astra_configured(self):
        # 2026-09-23 gpt-6-luna 公售后: 配置请求 astra 却被服务 gpt-6-luna → 仍是替换 (红)
        self.start(rollout_lines=self._tc("gpt-6-luna"), hist_lines=self._stale_hist())
        _, _, data = self.get()
        self.assertEqual(data["summary"]["state"], "luna")
        self.assertEqual(data["summary"]["label"], "Luna 替换中")

    def test_gpt6_luna_chosen_is_neutral(self):
        # 主动配置 gpt-6-luna (公开发售模型) → 中性 ⚪️, 不是降智
        self.start(rollout_lines=self._tc("gpt-6-luna"), hist_lines=self._stale_hist(),
                   config_model="gpt-6-luna")
        _, _, data = self.get()
        self.assertEqual(data["summary"]["state"], "other")

    def test_gpt6_sol_label_shows_model_name(self):
        # gpt-6-sol 公售: 中性, 且标签直接显示模型名而非笼统"其他模型"
        self.start(rollout_lines=self._tc("gpt-6-sol"), hist_lines=self._stale_hist())
        _, _, data = self.get()
        self.assertEqual(data["summary"]["state"], "other")
        self.assertEqual(data["summary"]["label"], "gpt-6-sol")

    def _win(self, model, act_age_sec):
        ts_tc = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - act_age_sec - 30))
        ts_ev = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - act_age_sec))
        return [json.dumps({"timestamp": ts_tc, "type": "turn_context",
                            "payload": {"model": model}}),
                json.dumps({"timestamp": ts_ev, "type": "event_msg"})]

    def test_multi_window_display_and_escalation(self):
        # 窗口A astra 刚活跃; 窗口B 5m前被替换成 luna (配置请求 astra)
        # → windows 两行、B 标红、整卡汇总升级红 (不能只看最新一轮)
        self.start(rollout_lines=self._tc("gpt-6-astra"), hist_lines=self._stale_hist(),
                   extra_rollouts=[("rollout-2026-09-23T01-00-00-ccccdddd-2222.jsonl",
                                    self._win("gpt-5.6-luna", 300))])
        _, _, data = self.get()
        self.assertEqual(len(data["windows"]), 2)
        self.assertTrue(any(w["alert"] for w in data["windows"]))
        self.assertEqual(data["summary"]["state"], "luna")

    def test_multi_window_chosen_luna_no_escalation(self):
        # 配置主动选 luna 时, luna 窗口不标红、不升级
        self.start(rollout_lines=self._tc("gpt-6-astra"), hist_lines=self._stale_hist(),
                   config_model="gpt-6-luna",
                   extra_rollouts=[("rollout-2026-09-23T01-00-00-ccccdddd-2222.jsonl",
                                    self._win("gpt-6-luna", 300))])
        _, _, data = self.get()
        self.assertFalse(any(w["alert"] for w in data["windows"]))
        self.assertEqual(data["summary"]["state"], "astra")

    def test_404_other_paths(self):
        self.start()
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/etc/passwd")
        self.assertEqual(cm.exception.code, 404)

    def test_session_title_and_duplicate_rollout(self):
        sid = "00000000-0000-4000-8000-000000000004"
        meta = json.dumps({"type": "session_meta", "payload": {"id": sid}})
        self.start(rollout_lines=[meta] + self._tc("gpt-6.1-sol"),
                   hist_lines=self._stale_hist(), extra_rollouts=[
                       ("rollout-copy.jsonl", [meta, json.dumps({
                           "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                           "type": "event_msg"})])])
        with sqlite3.connect(os.path.join(self.dir, "state_5.sqlite")) as con:
            con.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, title TEXT, name TEXT)")
            con.execute("INSERT INTO threads VALUES (?, ?, ?)",
                        (sid, "SYNTHETIC_PRIVATE_PROMPT", "示例聊天"))
        _, _, data = self.get()
        self.assertEqual(len(data["windows"]), 1)
        self.assertEqual(data["windows"][0]["title"], "示例聊天")
        self.assertEqual(data["interactive"]["title"], "示例聊天")
        self.assertEqual(data["interactive"]["model"], "gpt-6.1-sol")

    def test_http_snapshot_never_exposes_original_prompt(self):
        sid = "00000000-0000-4000-8000-000000000004"
        marker = "SYNTHETIC_PRIVATE_PROMPT"
        meta = json.dumps({"type": "session_meta", "payload": {"id": sid}})
        self.start(rollout_lines=[meta] + self._tc("gpt-6.1-sol"),
                   hist_lines=self._stale_hist())
        with sqlite3.connect(os.path.join(self.dir, "state_5.sqlite")) as con:
            con.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, title TEXT, name TEXT)")
            con.execute("INSERT INTO threads VALUES (?, ?, ?)",
                        (sid, marker + "\nsecond line", ""))
        _, _, data = self.get()
        self.assertNotIn(marker, json.dumps(data))
        self.assertIsNone(data["windows"][0]["title"])
        self.assertIsNone(data["interactive"]["title"])

    def test_dots_without_model_survives_http_and_does_not_change_alarm(self):
        self.start(rollout_lines=self._tc("gpt-6-sol"), hist_lines=self._stale_hist())
        sid = "00000000-0000-4000-8000-000000000002"
        atom = {"cloud-aeon-sidebar-cache-v1": {"accountId": "current", "threads": []},
                "aeon-subtasks-by-account-v1": {"current": {'["durable","parent"]': [sid]}}}
        Path(self.dir, ".codex-global-state.json").write_text(json.dumps({
            "electron-persisted-atom-state": atom}))
        Path(self.dir, "route_desktop_metadata.json").write_text(json.dumps({
            "account_id": "current", "threads": [{"session_id": sid, "host_id": "durable",
            "title": "示例项目任务", "status": "active", "tracked": True,
            "verified_at": time.time(), "updated_at": time.time()}]}))
        _, _, data = self.get()
        self.assertEqual(data["summary"]["state"], "other")
        self.assertEqual(len(data["desktop_windows"]), 1)
        self.assertEqual(data["desktop_windows"][0]["status"], "active")
        self.assertIsNone(data["desktop_windows"][0]["model"])

    def test_dots_task_with_local_log_is_not_duplicated(self):
        sid = "00000000-0000-4000-8000-000000000002"
        self.start(rollout_lines=[json.dumps({"type": "session_meta", "payload": {"id": sid}})] +
                   self._tc("gpt-6-sol"), hist_lines=self._stale_hist())
        with patch.object(srv.desktop, "desktop_sessions", return_value=[{
                "session_id": sid, "source": "dots", "model": None}]):
            _, _, data = self.get()
        self.assertEqual(len(data["windows"]), 1)
        self.assertEqual(data["windows"][0]["source"], "dots")
        self.assertEqual(data["windows"][0]["model"], "gpt-6-sol")
        self.assertEqual(data["desktop_windows"], [])


if __name__ == "__main__":
    unittest.main()
