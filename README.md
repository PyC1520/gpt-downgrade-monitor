# GPT降智监测

本项目基于“292 打票”思路的启发：把响应 trace 中不透明的 turn-state 字符串长度视为可观察特征，结合记录中的模型字段，辅助发现模型或路由状态的变化。字符串长度只是启发式信号，不能直接代表模型能力，也不能证明服务端实际执行了哪个模型。

当前代码沿用两组历史观察规则：长度 **288–294** 归为 `healthy(290)` 信号，**308–314** 归为 `sleep(311)` 信号；“292”落在前一组区间中。解析器仅统计字符串长度，不解密 turn-state。trace 解析器优先识别 `gpt-6-astra` 和含 `luna` 的模型字段，再使用长度判据；Luna 是否触发替换标记，还会参考请求的模型参数。

这些 trace 规则只用于已有探测历史。日常显示主要读取本地会话日志的 `turn_context.model`、轮首与最后活动时间；dots / 云端会话则读取桌面缓存、任务关联和带时间戳的运行事件，并明确区分缓存模型、模型未知和状态过期。插件不会主动发起模型探测请求，也不会建立真实的后台请求路由。

**上传包仅适用于 macOS。** 这是供 macOS 上的 Codex 桌面客户端与 CodexBar 使用的源码安装包，包含 Python 状态服务、JavaScript 插件、测试和 launchd 模板。安装需要本机已有 CodexBar 与 Codex 桌面客户端。

默认只读状态服务地址为 `http://127.0.0.1:8791/status`。本目录经过脱敏：测试会话 ID、任务名、昵称和项目路径均为合成示例，真实账号、聊天记录、配置和历史备份不包含在上传包中。GitHub 仓库使用英文名称 `gpt-downgrade-monitor`。

## 环境与启动

本上传包仅支持 macOS。需要 Python 3.9+。状态服务只使用 Python 标准库；运行插件测试还需要 Node.js。CodexBar 必须支持 JavaScript 用户插件。

在本目录运行：

```sh
python3 codex_status_server.py
```

默认从当前系统用户的 `~/.codex` 读取会话元数据、显示名称、模型配置和已有探测历史。从同目录加载 Python 模块，不依赖开发者的安装路径。

安装 CodexBar 插件文件：

```sh
mkdir -p ~/.config/codexbar/providers
cp codex-route.js ~/.config/codexbar/providers/codex-route.js
```

在 CodexBar 中加载“GPT降智监测”插件并刷新。插件的 `BASE_URL` 默认是上面的本机地址；`HISTORY_LIMIT` 默认显示 3 条历史，可填 0–9。

后台启动可参考 `launchd/com.example.codex-route-monitor.plist.template`：将 `__PYTHON3__` 替换为 Python 可执行文件的绝对路径，将 `__PROJECT_DIR__` 替换为本目录的绝对路径，另存到本机 `~/Library/LaunchAgents/com.example.codex-route-monitor.plist` 后加载。填写后的文件包含本机路径，应留在本机。

## 显示含义

- “最近模型记录”来自本地 `turn_context.model`；后台 `codex-auto-review` 记录会被跳过。
- “最后活动”和“本轮开始”分别按最后事件和轮首时间计算。没有新事件时，“几分钟前”会随时间自然增加。
- dots / 云端会话从桌面缓存、任务关联和有时间戳的运行事件中发现；缺少模型时显示“模型未知”。缓存模型会明确标注“缓存”。
- 状态记录超过 120 秒时显示“状态待更新”；最近 2 小时内的会话通常会显示，私有元数据中标记 `tracked` 的会话可以持续保留。
- `primary` 的 0/100 是插件卡片的告警指示，表示无告警/有告警，不是账户额度。

模型记录和缓存不能独立证明服务器实际执行了哪个模型。“替换信号”比较本地默认模型配置与记录，可能受会话单独选模影响；历史中的 290/311 判据为旧的启发式规则。该项目不会创建真正的请求路由，也不会主动发起模型探测请求。

## 私有运行数据

`route_desktop_metadata.json` 是可选的本机补充数据，用于提供缓存缺失的会话标题和带采集时间的状态。格式参考 `examples/route_desktop_metadata.example.json`。只有补充数据的 `account_id` 与桌面缓存中的当前账号一致时，才会采用其中的条目。

示例中的账号和会话 ID 均为虚构值，不能直接用于监视真实会话。需要时将自己的运行配置放到 `~/.codex/route_desktop_metadata.json`；真实配置不属于源码包。

`watch_state.py` 是可选的旧 trace 解析器，读取已有 trace 后向本机历史文件追加模型、判据及时间记录，不保存 trace 正文。调用格式为 `python3 watch_state.py TRACE_PATH [REQUESTED_MODEL]`。未经审查的原始 trace 可能包含敏感信息，应留在本机。

本机 `/status` 仍会返回用于显示和去重的聊天名称、会话 ID、时间以及 dots 项目路径。这些运行数据可能是私密的，请勿将接口快照或日志加入公开仓库。详情见 [PRIVACY.md](PRIVACY.md)。

## 验证

在本目录运行：

```sh
python3 -m unittest discover -s tests -v
python3 tools/check_public_tree.py
```

测试使用临时合成数据，并加载本源码目录中的模块；不会读取原开发者安装的源码。HTTP 测试使用随机本机端口，JS 测试使用 Node 沙箱。脱敏复核结果见 [SANITIZATION.md](SANITIZATION.md)。
