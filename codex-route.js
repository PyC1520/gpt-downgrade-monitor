// GPT降智监测 · CodexBar macOS 插件
// 只通过原有 loopback 服务读取日志状态，不产生模型请求。
// primary 继续作为告警指示：100=告警，0=无告警；并非账户额度。

function extractStatus(response) {
  if (!response || !response.json || !response.json.summary) {
    throw new Error("状态服务返回了非预期的数据结构");
  }
  return response.json;
}

function cleanText(value, limit) {
  var text = String(value == null ? "" : value)
    .replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim();
  var chars = Array.from(text);
  return chars.length > limit ? chars.slice(0, limit - 1).join("") + "…" : text;
}

function modelName(model) {
  if (!model) return "模型待记录";
  return cleanText(model, 60).replace(/^gpt-/i, "GPT-")
    .replace(/-(astra|sol|terra|luna)$/i, function (_, family) {
      return " " + family.charAt(0).toUpperCase() + family.slice(1).toLowerCase();
    });
}

function ageText(age, fallback) {
  if (typeof age !== "number" || !isFinite(age)) {
    return cleanText(fallback || "时间未知", 24).replace(/(\d+)s/g, "$1秒")
      .replace(/(\d+)m/g, "$1分钟").replace(/(\d+)h/g, "$1小时").replace(/(\d+)d/g, "$1天");
  }
  if (age < 10) return "刚刚";
  if (age < 60) return Math.floor(age) + "秒前";
  if (age < 3600) return Math.floor(age / 60) + "分钟前";
  if (age < 86400) return Math.floor(age / 3600) + "小时前";
  return Math.floor(age / 86400) + "天前";
}

function verdictName(verdict) {
  var names = { "astra:real": "Astra 记录", "substituted:luna": "Luna 替换信号",
    "chosen:luna": "Luna 自选", "healthy(290)": "健康信号 (290)",
    "sleep(311)": "睡眠信号 (311)", "unknown": "未判定" };
  return names[verdict] || cleanText(verdict || "未判定", 60);
}

function historyLimit(value) {
  var n = Number(value);
  return value == null || String(value).trim() === "" || !isFinite(n) ? 3 : Math.max(0, Math.min(9, Math.floor(n)));
}

function desktopStatus(w) {
  var names = { active: "运行中", activity: "有运行活动", idle: "空闲", unknown: "状态待更新" };
  return names[w.status] || names.unknown;
}

function toSnapshot(s, options) {
  var state = s.summary.state;
  var alarm = state === "luna" || state === "sleep";
  var emoji = { astra: "🟢", healthy: "🟢", luna: "🔴", sleep: "🟡", other: "⚪", unknown: "⚪" };
  var windows = (s.windows || []).slice().sort(function (a, b) {
    if (!!a.alert !== !!b.alert) return a.alert ? -1 : 1;
    return (typeof a.age === "number" ? a.age : Infinity) - (typeof b.age === "number" ? b.age : Infinity);
  });
  var alertCount = windows.filter(function (w) { return w.alert; }).length;
  var desktop = (s.desktop_windows || []).slice();
  var title = modelName(s.summary.label);
  if (state === "luna" && alertCount) title = alertCount + " 个会话有替换信号";
  var out = {
    dataConfidence: "exact",
    identity: {
      organization: (emoji[state] || "⚪") + " " + title,
      loginMethod: desktop.length ? "本地与 dots / 云端会话监视" : "本地日志监视",
    },
    // 保留 primary，宿主部分版本需要它才能稳定显示插件卡片。
    primary: { usedPercent: alarm ? 100 : 0,
      resetDescription: alarm ? "路由告警指示 · 非账户额度" : "路由状态指示 · 非账户额度" },
    details: [],
  };

  var overview = [];
  if (s.interactive) {
    var current = s.interactive;
    var activity = "最后活动：" + ageText(current.age, current.age_text);
    if (current.title) activity = cleanText(current.title, 20) + " · " + activity;
    overview.push({ label: "最近模型记录", value: modelName(current.model), secondaryValue: activity });
    if (current.turn_age_text || typeof current.turn_age === "number") {
      overview.push({ label: "本轮开始", value: ageText(current.turn_age, current.turn_age_text) });
    }
  } else {
    overview.push({ label: "模型记录", value: "暂无交互记录", secondaryValue: "等待会话写入模型日志" });
  }
  overview.push({ label: "近期会话", value: (windows.length + desktop.length) + " 个 · " + alertCount + " 个标记",
    secondaryValue: desktop.length ? "本地 " + windows.length + " 个 · dots / 云端 " + desktop.length + " 个" :
      "统计最近 2 小时有日志活动的会话" });
  out.details.push({ title: "模型概览", rows: overview });

  if (windows.length) {
    var titleCounts = {};
    windows.forEach(function (w) {
      var key = cleanText(w.title || "", 18);
      titleCounts[key] = (titleCounts[key] || 0) + 1;
    });
    out.details.push({ title: "近期会话 · " + windows.length, rows: windows.map(function (w) {
      var id = String(w.session_id || w.sid || "未知").slice(-8);
      var label = cleanText(w.title || "会话 " + id, 18);
      var secondary = "最后活动：" + ageText(w.age, w.age_text);
      if (w.source === "dots") secondary = "dots · 本地日志 · " + secondary;
      if (w.title && titleCounts[cleanText(w.title, 18)] > 1) secondary += " · " + id;
      return { label: label, value: (w.alert ? "🔴 " : "") + modelName(w.model),
        secondaryValue: (w.alert ? "替换信号 · " : "") + secondary };
    }) });
  }

  if (desktop.length) {
    var allDots = desktop.every(function (w) { return w.source === "dots"; });
    out.details.push({ title: (allDots ? "dots 会话" : "dots / 云端会话") + " · " + desktop.length,
      rows: desktop.map(function (w) {
        var id = String(w.session_id || w.sid || "未知").slice(-8);
        var label = cleanText(w.title || "会话 " + id, 24);
        var model = w.model ? modelName(w.model) + "（缓存）" : "模型未知";
        var status = desktopStatus(w);
        var project = String(w.cwd || "").replace(/\/+$/, "").split("/").pop();
        if (String(w.cwd || "").indexOf("/workspace/") === 0) project = "云端环境";
        var secondary = (w.source === "dots" ? "dots" : "云端") + (project ? " · " + cleanText(project, 24) : "");
        secondary += " · 最近活动：" + ageText(w.age);
        if (typeof w.status_age === "number") secondary += " · 状态确认：" + ageText(w.status_age);
        if (w.status === "unknown" && w.last_status === "active") secondary += "（上次运行中）";
        if (w.model) secondary += " · 模型记录：" + ageText(w.model_age);
        return { label: label, value: status + " · " + model, secondaryValue: cleanText(secondary, 120) };
      }) });
  }

  var limit = historyLimit(options && options.historyLimit);
  var entries = s.probe ? [s.probe].concat(s.history || []) : (s.history || []).slice();
  // 同一探测结果只出现一次；最新记录也应包含在历史中。
  var seen = {};
  entries = entries.filter(function (e) {
    var key = String(e.ts) + "|" + String(e.verdict) + "|" + (e.models || []).join(",");
    if (seen[key]) return false;
    seen[key] = true;
    return true;
  }).slice(0, limit);
  if (entries.length) {
    var stale = s.probe && typeof s.probe.age === "number" && s.probe.age > 3600;
    out.details.push({ title: "探测记录" + (stale ? " · 已过期" : ""), rows: entries.map(function (e) {
      var date = String(e.ts || "");
      var label = date ? date.slice(5, 10) + " " + date.slice(11, 16) : "时间未知";
      var secondary = (e.models || []).map(modelName).join("、") || "未记录模型";
      if (typeof e.age === "number" || e.age_text) secondary += " · " + ageText(e.age, e.age_text);
      return { label: cleanText(label, 24), value: verdictName(e.verdict), secondaryValue: cleanText(secondary, 100) };
    }) });
  }
  return out;
}

defineProvider({
  id: "codex-route",
  name: "GPT降智监测",
  icon: { monogram: "CR", tint: "#34C759" },
  topLevel: true,
  endpoints: [{ setting: "BASE_URL", policy: "https-or-loopback-http" }],
  settings: [
    { key: "BASE_URL", title: "状态服务地址",
      subtitle: "http://127.0.0.1:8791 (codex_status_server.py)", type: "plain" },
    { key: "HISTORY_LIMIT", title: "显示探测记录数",
      subtitle: "默认 3 条，可填 0–9；0 表示隐藏探测历史", type: "plain" },
  ],
  async fetchUsage(ctx) {
    var base = String(ctx.settings.get("BASE_URL") || "http://127.0.0.1:8791").replace(/\/+$/, "");
    var response;
    try {
      response = await ctx.http.getJSON(base + "/status");
    } catch (e) {
      if (e && e.transportClass === "cancelled") throw e;
      throw ctx.fail.providerUnavailable(
        "本地状态服务不可达 (" + (e && e.transportClass ? e.transportClass : "error") + "), " +
        "请检查 codex_status_server.py");
    }
    var s;
    try {
      s = extractStatus(response);
    } catch (e) {
      throw ctx.fail.parseFailure(e.message);
    }
    return toSnapshot(s, { historyLimit: ctx.settings.get("HISTORY_LIMIT") });
  },
});
