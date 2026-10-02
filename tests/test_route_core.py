#!/usr/bin/env python3
"""route_status_core.py 的单元测试 (核心解析: 判据 / rollout / 历史 / 年龄)"""
import importlib.util
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
CORE = str(SOURCE_ROOT / "route_status_core.py")
spec = importlib.util.spec_from_file_location("route_status_core", CORE)
cr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cr)


def write(path, lines):
    with open(path, "w") as fh:
        for line in lines:
            fh.write(line + "\n")


class TestVerdict(unittest.TestCase):
    def test_astra_real(self):
        self.assertEqual(cr.verdict(["gpt-6-astra"], []), "astra:real")

    def test_luna_substituted(self):
        self.assertEqual(cr.verdict(["gpt-5.6-luna"], [311]), "substituted:luna")

    def test_healthy_290(self):
        self.assertEqual(cr.verdict(["gpt-5.6-sol"], [290]), "healthy(290)")

    def test_sleep_311(self):
        self.assertEqual(cr.verdict(["gpt-5.6-sol"], [311]), "sleep(311)")

    def test_unknown(self):
        self.assertEqual(cr.verdict([], []), "unknown")


class TestRollout(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_last_turn_context_only(self):
        # turn_context 才是生效模型; 其他 model 字段(thread_settings 等)不采信
        f = os.path.join(self.dir, "rollout-a.jsonl")
        write(f, [
            json.dumps({"timestamp": "2026-09-22T10:00:00", "type": "turn_context",
                        "payload": {"model": "gpt-5.6-luna"}}),
            json.dumps({"timestamp": "2026-09-22T10:05:00", "type": "event_msg",
                        "payload": {"model": "gpt-6-astra"}}),
            json.dumps({"timestamp": "2026-09-22T10:06:00", "type": "turn_context",
                        "payload": {"model": "gpt-6-astra"}}),
        ])
        model, ts = cr.last_turn_context(f)
        self.assertEqual(model, "gpt-6-astra")
        self.assertEqual(ts, "2026-09-22T10:06:00")

    def test_skip_background_auto_review(self):
        # codex-auto-review 是后台审查模型, 不代表用户对话的实际生效模型
        f = os.path.join(self.dir, "rollout-r.jsonl")
        write(f, [
            json.dumps({"timestamp": "2026-09-22T10:00:00", "type": "turn_context",
                        "payload": {"model": "gpt-6-astra"}}),
            json.dumps({"timestamp": "2026-09-22T10:06:00", "type": "turn_context",
                        "payload": {"model": "codex-auto-review"}}),
        ])
        model, ts = cr.last_turn_context(f)
        self.assertEqual(model, "gpt-6-astra")
        self.assertEqual(ts, "2026-09-22T10:00:00")

    def test_cross_file_latest_turn_context(self):
        # 回归: mtime 最新的文件不一定含最新 turn_context
        # (后台 auto-review / resume 会触碰旧会话文件刷新其 mtime,
        #  单看 mtime 会读到旧文件里过时的 turn_context → 年龄显示过老)
        a = os.path.join(self.dir, "rollout-a.jsonl")
        b = os.path.join(self.dir, "rollout-b.jsonl")
        write(a, [json.dumps({"timestamp": "2026-09-22T10:00:00", "type": "turn_context",
                              "payload": {"model": "gpt-6-astra"}})])
        write(b, [json.dumps({"timestamp": "2026-09-22T11:00:00", "type": "turn_context",
                              "payload": {"model": "gpt-5.6-sol"}})])
        os.utime(a, (time.time(),) * 2)                      # a: mtime 最新
        os.utime(b, (time.time() - 3600,) * 2)               # b: mtime 较旧但 tc 更新
        model, ts, act = cr.latest_effective_model(self.dir)
        self.assertEqual(model, "gpt-5.6-sol")
        self.assertEqual(ts, "2026-09-22T11:00:00")

    def test_long_turn_activity_exceeds_turn_start(self):
        # 回归: turn_context 只在轮首写; agent 长轮次持续追加事件时,
        # "年龄"必须按最后活动算, 否则越刷新越老 (2026-09-23 实测 27m vs 1.2m)
        f = os.path.join(self.dir, "rollout-a.jsonl")
        write(f, [
            json.dumps({"timestamp": "2026-09-23T03:09:47", "type": "turn_context",
                        "payload": {"model": "gpt-6-astra"}}),
            json.dumps({"timestamp": "2026-09-23T03:36:05", "type": "event_msg",
                        "payload": {"type": "token_count"}}),
        ])
        os.utime(f, (time.time(),) * 2)
        model, tc_ts, act_ts = cr.latest_effective_model(self.dir)
        self.assertEqual(model, "gpt-6-astra")
        self.assertEqual(tc_ts, "2026-09-23T03:09:47")   # 模型确立 = 轮首
        self.assertEqual(act_ts, "2026-09-23T03:36:05")  # 最后活动 = 最后事件

    def test_newest_file_by_mtime(self):
        old = os.path.join(self.dir, "rollout-old.jsonl")
        new = os.path.join(self.dir, "rollout-new.jsonl")
        write(old, ['{"x":1}'])
        write(new, ['{"x":1}'])
        os.utime(old, (time.time() - 3600,) * 2)
        self.assertEqual(cr.newest_rollout(self.dir), new)

    def test_scan_sessions_per_file(self):
        # 多窗口: 逐文件画像 (sid/模型/轮首tc/最后活动), 无信号的文件跳过
        a = os.path.join(self.dir, "rollout-2026-09-23T01-00-00-aaaabbbb-1111.jsonl")
        b = os.path.join(self.dir, "rollout-2026-09-23T02-00-00-ccccdddd-2222.jsonl")
        c = os.path.join(self.dir, "rollout-empty.jsonl")
        write(a, [json.dumps({"timestamp": "2026-09-23T03:09:00", "type": "turn_context",
                              "payload": {"model": "gpt-6-astra"}}),
                  json.dumps({"timestamp": "2026-09-23T03:20:00", "type": "event_msg"})])
        write(b, [json.dumps({"timestamp": "2026-09-23T03:15:00", "type": "turn_context",
                              "payload": {"model": "gpt-5.6-luna"}}),
                  json.dumps({"timestamp": "2026-09-23T03:25:00", "type": "event_msg"})])
        write(c, ["not json"])
        os.utime(b, (time.time(),) * 2)              # b: mtime 最新
        os.utime(a, (time.time() - 60,) * 2)
        os.utime(c, (time.time() - 120,) * 2)
        rows = cr.scan_sessions(self.dir)
        self.assertEqual([r["sid"] for r in rows], ["ccccdddd", "aaaabbbb"])  # mtime 降序, c 无信号跳过
        self.assertEqual(rows[0]["model"], "gpt-5.6-luna")
        self.assertEqual(rows[0]["act_ts"], "2026-09-23T03:25:00")

    def test_missing_dir(self):
        self.assertIsNone(cr.newest_rollout(os.path.join(self.dir, "nope")))

    def test_same_session_multiple_rollouts_merge(self):
        sid = "00000000-0000-4000-8000-000000000004"
        metadata = json.dumps({"type": "session_meta", "payload": {"id": sid}})
        a = os.path.join(self.dir, "rollout-a.jsonl")
        b = os.path.join(self.dir, "rollout-b.jsonl")
        write(a, [metadata, json.dumps({"timestamp": "2026-10-01T14:30:00", "type": "event_msg"})])
        write(b, [metadata, json.dumps({"timestamp": "2026-10-01T14:20:00", "type": "turn_context",
                                       "payload": {"model": "gpt-6.1-sol"}})])
        os.utime(a, (time.time(),) * 2)
        os.utime(b, (time.time() - 60,) * 2)
        rows = cr.scan_sessions(self.dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session_id"], sid)
        self.assertEqual(rows[0]["model"], "gpt-6.1-sol")
        self.assertEqual(rows[0]["act_ts"], "2026-10-01T14:30:00")

    def test_identical_short_prefix_different_sessions(self):
        for name, sid in [("a", "00000000-a"), ("b", "00000000-b")]:
            write(os.path.join(self.dir, f"rollout-{name}.jsonl"), [
                json.dumps({"type": "session_meta", "payload": {"id": sid}}),
                json.dumps({"timestamp": "2026-10-01T14:30:00", "type": "turn_context",
                            "payload": {"model": "gpt-6.1-sol"}})])
        self.assertEqual(len(cr.scan_sessions(self.dir)), 2)

    def test_model_activity_stays_in_its_own_session(self):
        write(os.path.join(self.dir, "rollout-old.jsonl"), [
            json.dumps({"timestamp": "2026-10-01T12:00:00", "type": "turn_context",
                        "payload": {"model": "gpt-6-astra"}}),
            json.dumps({"timestamp": "2026-10-01T15:00:00", "type": "event_msg"})])
        write(os.path.join(self.dir, "rollout-new.jsonl"), [
            json.dumps({"timestamp": "2026-10-01T14:00:00", "type": "turn_context",
                        "payload": {"model": "gpt-6.1-sol"}})])
        model, _, act = cr.latest_effective_model(self.dir)
        self.assertEqual(model, "gpt-6.1-sol")
        self.assertEqual(act, "2026-10-01T14:00:00")


class TestSessionTitles(unittest.TestCase):
    def test_sidebar_name_preferred_over_original_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            with sqlite3.connect(os.path.join(directory, "state_5.sqlite")) as con:
                con.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, title TEXT, name TEXT)")
                con.executemany("INSERT INTO threads VALUES (?, ?, ?)", [
                    ("abc", "<codex_delegation>长段原始内容</codex_delegation>", "科研方案"),
                    ("def", "旧版本标题", " ")])
            self.assertEqual(cr.session_titles(["abc", "def"], directory),
                             {"abc": "科研方案"})

    def test_empty_sidebar_name_never_returns_original_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            with sqlite3.connect(os.path.join(directory, "state_5.sqlite")) as con:
                con.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, title TEXT, name TEXT)")
                con.execute("INSERT INTO threads VALUES (?, ?, ?)",
                            ("abc", "SYNTHETIC_PRIVATE_PROMPT\nsecond line", " "))
            self.assertEqual(cr.session_titles(["abc"], directory), {})

    def test_legacy_title_only_schema_never_returns_original_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            with sqlite3.connect(os.path.join(directory, "state_5.sqlite")) as con:
                con.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, title TEXT)")
                con.execute("INSERT INTO threads VALUES (?, ?)",
                            ("abc", "SYNTHETIC_PRIVATE_PROMPT"))
            self.assertEqual(cr.session_titles(["abc"], directory), {})

    def test_readonly_title_lookup_and_missing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(cr.session_titles(["abc"], directory), {})
            database = os.path.join(directory, "state_5.sqlite")
            with sqlite3.connect(database) as con:
                con.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, title TEXT, name TEXT)")
                con.execute("INSERT INTO threads VALUES (?, ?, ?)",
                            ("abc", "SYNTHETIC_PRIVATE_PROMPT", "示例聊天"))
            before = Path(database).read_bytes()
            self.assertEqual(cr.session_titles(["abc", "missing"], directory), {"abc": "示例聊天"})
            self.assertEqual(Path(database).read_bytes(), before)


class TestHistory(unittest.TestCase):
    def test_last_entry(self):
        f = tempfile.mktemp(suffix=".jsonl")
        write(f, [
            json.dumps({"ts": "2026-09-22T13:00:00", "verdict": "substituted:luna",
                        "models": ["gpt-5.6-luna"], "turn_state_lens": [311], "safety_buffering": True}),
            json.dumps({"ts": "2026-09-22T14:00:00", "verdict": "astra:real",
                        "models": ["gpt-6-astra"], "turn_state_lens": [], "safety_buffering": False}),
        ])
        entry = cr.last_history(f)
        self.assertEqual(entry["verdict"], "astra:real")
        os.unlink(f)

    def test_empty(self):
        self.assertIsNone(cr.last_history(tempfile.mktemp(suffix=".jsonl")))


class TestAge(unittest.TestCase):
    def test_fmt(self):
        self.assertEqual(cr.fmt_age(30), "30s")
        self.assertEqual(cr.fmt_age(45 * 60), "45m")
        self.assertEqual(cr.fmt_age(5 * 3600), "5h")
        self.assertEqual(cr.fmt_age(3 * 86400), "3d")

    def test_utc_vs_local(self):
        # rollout 时间戳是 UTC, route_history 是本地时间 — age_of 必须区分
        self.assertGreater(cr.age_of("2000-01-01T00:00:00", utc=True), 9 * 365 * 86400)
        # 远过去的时间在两种口径下都应是"很久以前"而非 0 (回归: UTC 被当本地 → 负数钳 0)
        self.assertGreater(cr.age_of("2000-01-01T00:00:00"), 9 * 365 * 86400)


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_configured_model(self):
        # config.toml 顶层 model = 用户配置的默认请求模型 (交互会话的"请求侧")
        f = os.path.join(self.dir, "config.toml")
        write(f, ['model = "gpt-6-astra"', "", "[profiles.fast]", 'model = "gpt-5.6-sol"'])
        self.assertEqual(cr.configured_model(f), "gpt-6-astra")

    def test_missing_config(self):
        self.assertIsNone(cr.configured_model(os.path.join(self.dir, "nope.toml")))


if __name__ == "__main__":
    unittest.main()
