"""dots 显示桥：身份、缺失模型、状态过期和只读活动采集。"""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone

SOURCE_ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location(
    "route_desktop_sessions", str(SOURCE_ROOT / "route_desktop_sessions.py"))
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

ROOT = "00000000-0000-4000-8000-000000000001"
TASK = "00000000-0000-4000-8000-000000000002"
NEW = "00000000-0000-4000-8000-000000000003"
NOW = 1790870000


class TestDesktopSessions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "state.json"
        self.metadata_path = Path(self.tmp.name) / "metadata.json"
        self.logs = Path(self.tmp.name) / "logs"
        self.cache = {"accountId": "current", "profilesByThreadId": {ROOT: {"display_name": "示例助手"}},
                      "refreshStartedAtMs": NOW * 1000, "threads": []}
        self.atoms = {"cloud-aeon-sidebar-cache-v1": self.cache,
                      "aeon-subtasks-by-account-v1": {
                          "current": {json.dumps(["durable", ROOT]): [TASK]}},
                      "orbit-outputs-v1": []}
        self.metadata = {"account_id": "current", "threads": [{
            "session_id": TASK, "host_id": "durable", "title": "示例项目任务",
            "cwd": "/Users/example/demo-project", "status": "active", "verified_at": NOW,
            "updated_at": NOW - 1000, "tracked": True}]}

    def read(self, now=NOW):
        self.state_path.write_text(json.dumps({"electron-persisted-atom-state": self.atoms}))
        self.metadata_path.write_text(json.dumps(self.metadata))
        return bridge.desktop_sessions(self.state_path, self.metadata_path, self.logs, now)

    def target(self, **kwargs):
        return next(r for r in self.read(**kwargs) if r["session_id"] == TASK)

    def test_dots_project_without_model_is_visible(self):
        row = self.target()
        self.assertEqual(row["source"], "dots")
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["title"], "示例项目任务")
        self.assertIsNone(row["model"])
        self.assertFalse(row["alert"])

    def test_snapshot_status_expires_and_tracked_chat_stays_visible(self):
        row = self.target(now=NOW + 8000)
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["last_status"], "active")
        self.assertEqual(row["status_age"], 8000)

    def test_cache_status_and_file_refresh_do_not_claim_running(self):
        self.cache["threads"] = [{"id": TASK, "name": "新标题", "model": "gpt-6-astra",
                                  "updatedAt": NOW - 600, "status": {"type": "active"},
                                  "preview": "private text", "secret": "not exported"}]
        self.metadata["threads"][0]["verified_at"] = NOW - 600
        row = self.target()
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["model"], "gpt-6-astra")
        self.assertEqual(row["model_source"], "desktop_cache")
        self.assertEqual(row["model_age"], 600)
        self.assertNotIn("preview", row)
        self.assertNotIn("secret", row)
        self.assertEqual(row["title"], "新标题")

    def test_new_child_discovered_and_deduplicated_from_artifact(self):
        self.atoms["aeon-subtasks-by-account-v1"]["current"][json.dumps(["durable", ROOT])] += [NEW, NEW]
        self.atoms["orbit-outputs-v1"] = [{"hostId": "durable", "threadId": NEW,
                                           "artifact": {"producedAtMs": (NOW - 10) * 1000}}]
        rows = [r for r in self.read() if r["session_id"] == NEW]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "dots")
        self.assertIsNone(rows[0]["model"])
        self.assertEqual(rows[0]["status"], "unknown")
        self.assertEqual(rows[0]["age"], 10)

    def test_other_account_metadata_is_not_used(self):
        self.metadata["account_id"] = "previous"
        self.assertEqual(self.read(), [])

    def test_durable_alone_is_not_dots(self):
        self.atoms["aeon-subtasks-by-account-v1"] = {}
        self.assertEqual(self.target()["source"], "cloud")

    def log(self, text, age=0):
        stamp = datetime.fromtimestamp(NOW - age, timezone.utc).isoformat().replace("+00:00", "Z")
        directory = self.logs / datetime.fromtimestamp(NOW, timezone.utc).strftime("%Y/%m/%d")
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "codex-desktop-fixture.log").open("a") as fh:
            fh.write(stamp + " info " + text + " threadId=" + TASK + "\n")

    def test_ui_navigation_is_not_task_activity(self):
        self.metadata["threads"][0]["verified_at"] = NOW - 600
        self.log("response_routed method=thread/read hostId=durable")
        self.log("IAB_LIFECYCLE ownerRoutePath=/local/" + TASK)
        row = self.target()
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["age"], 1000)

    def test_log_activity_and_completion_supersede_older_snapshot(self):
        self.metadata["threads"][0]["verified_at"] = NOW - 600
        self.log("Reasoning summary item completed", age=20)
        self.assertEqual(self.target()["status"], "activity")
        self.log("Received turn/completed", age=10)
        row = self.target()
        self.assertEqual(row["status"], "idle")
        self.assertEqual(row["status_age"], 10)

    def test_malformed_files_fail_closed(self):
        self.state_path.write_text("{partial")
        self.metadata_path.write_text("{partial")
        self.assertEqual(bridge.desktop_sessions(self.state_path, self.metadata_path, self.logs, NOW), [])

    def test_timestamp_offset(self):
        self.assertEqual(bridge.epoch("2026-10-01T12:00:00-04:00"),
                         bridge.epoch("2026-10-01T16:00:00Z"))


if __name__ == "__main__":
    unittest.main()
