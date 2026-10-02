#!/usr/bin/env python3
"""本地路由插件的纯 JS 测试；用 Node 沙箱运行，不操作 GUI、不请求模型。"""
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN = str(SOURCE_ROOT / "codex-route.js")
ASTRA = {"summary": {"state": "astra", "label": "Astra 正常"},
         "interactive": {"model": "gpt-6-astra", "title": "示例聊天", "age": 19,
                         "age_text": "19s前", "turn_age": 1620, "turn_age_text": "27m前"},
         "probe": {"ts": "2026-09-23T13:56:26", "verdict": "astra:real", "age": 15*60,
                   "age_text": "15m前", "models": ["gpt-6-astra"]},
         "windows": [{"sid": "11111111", "title": "示例聊天", "model": "gpt-6-astra", "age": 19,
                      "age_text": "19s前", "alert": False},
                     {"sid": "22222222", "title": "示例任务", "model": "gpt-5.6-luna", "age": 120,
                      "age_text": "2m前", "alert": True}],
         "history": [{"ts": "2026-09-23T13:56:26", "verdict": "astra:real", "models": ["gpt-6-astra"]},
                     {"ts": "2026-09-23T13:55:57", "verdict": "substituted:luna", "models": ["gpt-5.6-luna"]}]}
LUNA = {"summary": {"state": "luna", "label": "Luna 替换中"},
        "interactive": {"model": "gpt-5.6-luna", "age_text": "2m前"},
        "probe": {"verdict": "substituted:luna", "age_text": "3m前", "models": ["gpt-5.6-luna"]},
        "history": []}
EMPTY = {"summary": {"state": "unknown", "label": "无数据"},
         "interactive": None, "probe": None, "history": []}
OTHER = {"summary": {"state": "other", "label": "gpt-6.1-sol"},
         "interactive": {"model": "gpt-6.1-sol", "age_text": "1m前"},
         "probe": None, "windows": [], "history": []}

JS = r"""
var __manifest = null;
function defineProvider(m) { __manifest = m; }
%(plugin_src)s
function T(name, cond) { return {name: name, ok: !!cond}; }
var fixture = %(astra)s;
var a = toSnapshot(fixture), l = toSnapshot(%(luna)s), e = toSnapshot(%(empty)s), o = toSnapshot(%(other)s);
var w = extractStatus({status: 200, headers: {}, json: fixture});
var badWrap = null;
try { extractStatus({status: 200, headers: {}}); } catch (err) { badWrap = err.message; }
var results = [
  T("manifest-id", __manifest.id === "codex-route"),
  T("loopback-policy-unchanged", __manifest.endpoints.length === 1 &&
    __manifest.endpoints[0].policy === "https-or-loopback-http" && !__manifest.capabilities),
  T("unwrap-getjson", w.summary.state === "astra"),
  T("reject-missing-json", badWrap !== null),
  T("astra-0pct", a.primary.usedPercent === 0),
  T("luna-100pct", l.primary.usedPercent === 100),
  T("unknown-primary-0", e.primary.usedPercent === 0),
  T("other-primary-0", o.primary.usedPercent === 0),
  T("model-version-preserved", o.details[0].rows[0].value === "GPT-6.1 Sol"),
  T("human-time", a.details[0].rows.some(function(r) { return r.label === "本轮开始" && r.value === "27分钟前"; })),
  T("conversation-title", a.details[1].rows.some(function(r) { return r.label === "示例聊天"; })),
  T("alert-first", a.details[1].rows[0].label === "示例任务" && a.details[1].rows[0].value.indexOf("🔴") >= 0),
  T("activity-secondary", a.details[1].rows[0].secondaryValue.indexOf("2分钟前") >= 0),
  T("history-dedup", a.details[2].rows.length === 2),
  T("history-date", a.details[2].rows[0].label === "09-23 13:56"),
  T("verdict-human-readable", a.details[2].rows[1].value === "Luna 替换信号"),
  T("history-hidden", toSnapshot(fixture, {historyLimit: "0"}).details.length === 2),
  T("ring-not-quota", a.primary.resetDescription.indexOf("非账户额度") >= 0),
  T("sanitize-control-and-length", cleanText("a\nb\tc", 10) === "a b c" && cleanText("😀😀😀😀", 3) === "😀😀…"),
  T("empty-state-readable", e.details[0].rows[0].value === "暂无交互记录"),
  T("history-limit-input", historyLimit(undefined) === 3 && historyLimit(" ") === 3 &&
     historyLimit("oops") === 3 && historyLimit("-2") === 0 && historyLimit("100") === 9),
  T("short-rows", [a, l, e, o].every(function(s) { return s.details.every(function(sec) {
    return sec.rows.every(function(r) { return [r.label, r.value, r.secondaryValue || ""].every(function(v) {
      return Array.from(v).length <= 120;
    }); });
  }); })),
];
fixture.probe.age = 8 * 86400;
T("dummy", true);
results.push(T("stale-probe-marked", toSnapshot(fixture).details[2].title.indexOf("已过期") >= 0));
fixture.history = Array.from({length: 9}, function(_, i) {
  return {ts: "2026-09-22T10:0" + i + ":00", verdict: "astra:real", models: ["gpt-6-astra"]};
});
results.push(T("default-history-three", toSnapshot(fixture).details[2].rows.length === 3));
fixture.windows[0].title = fixture.windows[1].title = "重复标题";
fixture.windows[0].session_id = "same-prefix-distinct-A";
fixture.windows[1].session_id = "same-prefix-distinct-B";
var dup = toSnapshot(fixture).details[1].rows;
results.push(T("duplicate-titles-disambiguated", dup[0].secondaryValue !== dup[1].secondaryValue &&
  dup.every(function(r) { return r.secondaryValue.indexOf("stinct-") >= 0; })));
fixture.desktop_windows = [{session_id: "dots-project", title: "示例项目任务",
  source: "dots", status: "active", age: 35, status_age: 5, cwd: "/Users/example/demo-project", model: null}];
var dots = toSnapshot(fixture);
var dotSection = dots.details.filter(function(s) { return s.title.indexOf("dots 会话") === 0; })[0];
results.push(T("dots-without-model-visible", dotSection.rows[0].label === "示例项目任务" &&
  dotSection.rows[0].value === "运行中 · 模型未知"));
results.push(T("dots-project-and-observation-time", dotSection.rows[0].secondaryValue.indexOf("dots · demo-project") >= 0 &&
  dotSection.rows[0].secondaryValue.indexOf("状态确认") >= 0));
results.push(T("dots-count-in-overview", dots.details[0].rows.some(function(r) {
  return r.label === "近期会话" && r.value.indexOf("3 个") === 0;
})));
fixture.desktop_windows[0].status = "unknown";
fixture.desktop_windows[0].last_status = "active";
fixture.desktop_windows[0].model = "gpt-6-astra";
fixture.desktop_windows[0].model_age = 3600;
var staleDots = toSnapshot(fixture).details.filter(function(s) { return s.title.indexOf("dots 会话") === 0; })[0];
results.push(T("dots-stale-status-and-cached-model-explicit", staleDots.rows[0].value ===
  "状态待更新 · GPT-6 Astra（缓存）" && staleDots.rows[0].secondaryValue.indexOf("上次运行中") >= 0));
delete fixture.desktop_windows;
(async function() {
  var urls = [];
  var fetched = await __manifest.fetchUsage({
    settings: { get: function(key) { return key === "BASE_URL" ? "http://127.0.0.1:8791/" : "1"; } },
    http: { getJSON: async function(url) { urls.push(url); return {json: fixture}; } },
  });
  results.push(T("fetch-loopback-only", urls.length === 1 && urls[0] === "http://127.0.0.1:8791/status"));
  results.push(T("fetch-respects-history-setting", fetched.details[2].rows.length === 1));
  var cancelled = {transportClass: "cancelled"}, caught;
  try { await __manifest.fetchUsage({settings:{get:function(){return null;}},
    http:{getJSON:async function(){throw cancelled;}}}); } catch(err) {caught = err;}
  results.push(T("cancellation-preserved", caught === cancelled));
  process.stdout.write(JSON.stringify(results));
})().catch(function(err) { console.error(err); process.exitCode = 1; });
"""


class TestCodexBarPlugin(unittest.TestCase):
    def test_all(self):
        with open(PLUGIN) as fh:
            src = fh.read()
        code = JS % {"plugin_src": src, "astra": json.dumps(ASTRA), "luna": json.dumps(LUNA),
                     "empty": json.dumps(EMPTY), "other": json.dumps(OTHER)}
        node = shutil.which("node")
        self.assertIsNotNone(node, "需要 Node.js 执行纯 JS 验证")
        proc = subprocess.run([node, "-"], input=code, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        results = json.loads(proc.stdout)
        self.assertEqual([r["name"] for r in results if not r["ok"]], [])
        self.assertEqual(len(results), 32)


if __name__ == "__main__":
    unittest.main()
