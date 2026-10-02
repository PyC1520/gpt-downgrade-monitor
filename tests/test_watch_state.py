#!/usr/bin/env python3
"""watch_state.py 的单元测试 (exec trace → 记录 → 告警门控)"""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
WS = str(SOURCE_ROOT / "watch_state.py")
spec = importlib.util.spec_from_file_location("watch_state", WS)
ws = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ws)


class TestRecord(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.hist = os.path.join(self.tmp, "hist.jsonl")
        ws.HIST = self.hist  # 重定向到临时历史
        self.notified = []
        ws.notify = lambda t, m: self.notified.append((t, m))

    def trace(self, text):
        p = os.path.join(self.tmp, "trace.err")
        with open(p, "w") as fh:
            fh.write(text)
        return p

    def lines(self):
        return [ln for ln in open(self.hist).read().splitlines() if ln.strip()]

    def test_failed_capture_not_recorded(self):
        # 回归 (2026-09-23): 探测请求 400 → trace 无任何模型信号,
        # 被当成 unknown 落历史 + 假告警 "astra:real → unknown"。
        # 捕获失败 ≠ 路由变化: 不落历史、不告警。
        with open(self.hist, "w") as fh:
            fh.write(json.dumps({"ts": "2026-09-23T05:43:16", "verdict": "astra:real"}) + "\n")
        entry = ws.record(self.trace(
            'error: {"code":"unsupported_value","message":"minimal is not supported",'
            '"param":"reasoning.effort"} status 400'))
        self.assertEqual(entry["verdict"], "capture_failed")
        self.assertEqual(len(self.lines()), 1)      # 未追加
        self.assertEqual(self.notified, [])         # 未告警

    def test_chosen_luna_not_substitution(self):
        # 2026-09-23 gpt-6-luna 公售: exec -m gpt-6-luna 主动选它 ≠ 替换
        entry = ws.record(self.trace('... "model":"gpt-6-luna" ...'),
                          requested="gpt-6-luna")
        self.assertEqual(entry["verdict"], "chosen:luna")

    def test_substitution_when_astra_requested(self):
        # 请求 astra 被服务任何代 luna (5.6 或 6) → 替换
        entry = ws.record(self.trace('... "model":"gpt-6-luna" ...'),
                          requested="gpt-6-astra")
        self.assertEqual(entry["verdict"], "substituted:luna")

    def test_capture_recorded_and_flip_notifies(self):
        with open(self.hist, "w") as fh:
            fh.write("")
        entry = ws.record(self.trace('... "model":"gpt-5.6-luna" ...'))
        self.assertEqual(entry["verdict"], "substituted:luna")
        self.assertEqual(len(self.lines()), 1)      # 正常捕获落历史
        self.assertEqual(self.notified, [])         # 无前条不告警
        ws.record(self.trace('... "model":"gpt-6-astra" ...'))
        self.assertEqual(len(self.notified), 1)     # luna→astra 翻转告警一次


if __name__ == "__main__":
    unittest.main()
