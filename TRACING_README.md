# 轨迹记录使用说明

这套功能记录一次任务的**模型请求、模型响应、工具参数、工具结果和应用状态**。每个任务有独立的 `episode_id`。拿到这个 ID 后，可以检查原始记录、校验完整性、导出、离线回放，再归档到私有 Git 仓库。

“完整”指应用实际观察到的数据。模型内部隐藏状态不在范围内；API 没有返回的数据、未收到的远端结果不能补造。旧 Qwen 路径仍可使用，但不自动获得这套完整记录保证。

## 1. 安装

尚未合并全部 PR 时，使用完整功能分支：

```bash
git clone --branch codex/tracing-user-guide https://github.com/regulusv/membraneclaw.git
cd membraneclaw
python3 -m venv .venv
source .venv/bin/activate
python -m pip install '.[tools,http]'
umask 077
```

要求 Python 3.11 或以上。持久化与跨进程锁的正式 CI 在 Linux 上运行；文件后端只支持单机本地持久磁盘，不支持多机共享写入。`umask 077` 让后续私有导出默认只对当前操作员开放。

## 2. 先做一次不需要 API key 的检查

```bash
python -m membraneclaw_runtime.tracing.example
```

它生成**合成示例**，不会调用模型或工具。输出包含：

```text
episode=local/<run_id>/<episode_id> status=completed
```

复制完整 episode key：

```bash
EPISODE='把上面 local/run_id/episode_id 的完整值粘贴到这里'
python -m membraneclaw_runtime.tracing.cli validate --trace-dir ./var/traces --episode "$EPISODE"
python -m membraneclaw_runtime.tracing.cli inspect --trace-dir ./var/traces --episode "$EPISODE"
```

`validate` 应退出为 0，返回 revision、事件数、payload 数、状态和完整性。`inspect` 显示事件、状态以及完整 payload；payload 用显式 base64 编码保存展示，不是截断预览。

这个示例验证存储与检查命令，不验证真实 API，也不是执行回放 fixture。

## 3. 录制一次真实模型任务

先提供对应的环境变量，再明确选择 provider 和 model。当前代码允许的兼容配置如下；这些不是“最新模型”推荐：

| provider | model | key 环境变量 |
|---|---|---|
| `gemini` | `gemini-2.5-flash` | `GEMINI_API_KEY` |
| `openai` | `gpt-4.1-2025-04-14` 或 `o4-mini-2025-04-16` | `OPENAI_API_KEY` |
| `anthropic` | `claude-sonnet-4-20250514` | `ANTHROPIC_API_KEY` |

例如，先在当前 shell 设置自己的 `GEMINI_API_KEY`，再执行：

```bash
membraneclaw \
  --provider gemini --model gemini-2.5-flash \
  --trace-dir ./var/traces --no-mcp \
  -p '请调用 calculator 计算 27 * 19，并给出结果。'
```

**提示词用 `-p` / `--prompt` 传入**，不是位置参数。`--no-mcp` 关闭外部 MCP 发现，仍保留本地 calculator 等工具。模型是否选择工具由其响应决定，不能仅凭最终答案证明工具执行过；在轨迹中检查 `tool.started` 和 `tool.completed`。

OpenAI / Claude 只需更换 provider、model 和对应 key；不要在同一个 episode 中切换 provider。API key 来自环境，不进入请求 body 或配置快照。

开始时会输出 episode ID；结束时会输出最终回答及 `status` / `reason`。正常完成退出为 0，失败退出为非零。复制本次 ID，然后用第 2 节的检查命令查看记录。简短 episode ID 也可用，但必须在当前 trace 目录中唯一。

## 4. 记录在哪里，应该看什么

默认目录为当前工作目录下的 `./var/traces`。每个任务的权威数据包括：

```text
var/traces/<tenant>/<run_id>/<episode_id>/
  genesis.json
  manifest.json
  commits/000001.json ...
  payloads/<sha256>.bin ...
```

`commits` 是有 hash 链的不可变提交；payload 保存完整 bytes，包括超过 UI 预览长度的工具输出。`manifest.json` 可重建；检查与恢复以提交链为准。不要手工编辑原始文件。

| 要回答的问题 | 查看什么 |
|---|---|
| 模型实际收到什么？ | `attempt.started` 的 payload：最终序列化请求 body |
| 模型实际返回什么？ | `attempt.response` 的 payload；流式还要看全部 `stream.frame` |
| 是否重试？ | 同一 model call 下的独立 attempts 和 `attempt.retry_scheduled` |
| 工具参数是否有效？ | `tool.requested`、`tool.arguments_validated` / `tool.validation_failed` |
| 工具实际返回什么？ | `tool.completed` 的全量 payload；按 tool_call_id 配对 |
| 工具是否可能执行过，但结果不确定？ | `tool.outcome_unknown`；不能自动重复副作用 |
| 为什么停止？ | `runtime.finished`、episode 状态和最后的状态 checkpoint |

执行状态与记录完整性是两个维度：

| 字段 | 含义 |
|---|---|
| `status=completed` | 应用任务正常完成 |
| `status=awaiting_client_tool` | 已持久化交接，等待客户端工具结果，不是损坏 |
| `status=failed/cancelled/interrupted` | 执行失败、取消或中断；已提交记录仍可检查 |
| `capture=complete` | 在声明的采集边界内，已观察的数据被完整记录 |
| `capture=partial/unknown` | 有丢失或无法证明完整的部分，不能当成完整样本 |
| `external_outcome=unknown` | 无法确认远端模型或工具最终是否执行成功 |
| `replay=unsupported` | 生产记录没有自动认证执行回放能力；仍可检查/播放，执行回放需符合第 6 节条件 |

一次重试后任务可能完成，但先前未知的远端结果不会因此变成已知。存储写入失败会停止新模型/工具动作，不能把“回答出来了”当成“轨迹已保存”。

## 5. 导出给分析者，或导出完整私有记录

默认 analysis 导出只含事件 ID、顺序、类型和状态，排除提示词、工具内容、原生状态和签名：

```bash
python -m membraneclaw_runtime.tracing.cli export \
  --trace-dir ./var/traces --episode "$EPISODE" \
  --output ./var/traces/analysis.zip
```

需要保留全部原始数据、带到另一台机器检查/回放时，显式选择 private replay：

```bash
python -m membraneclaw_runtime.tracing.cli export \
  --trace-dir ./var/traces --episode "$EPISODE" \
  --mode private-replay --output ./var/traces/private-replay.zip
```

导出命令不覆盖已有文件；再次导出请使用新文件名。私有 bundle 以 0600 权限创建，包含私人输入、工具数据和 opaque continuation/signature，应只交给有权限的接收者。analysis bundle 标记为 derived/redacted，不能用来无损恢复执行。

## 6. 离线回放有两种

**记录播放**：校验 bundle，重建已经记录的事件和状态，不重新执行任何模型或工具。上面的合成示例也支持这种播放：

```bash
python -m membraneclaw_runtime.tracing.cli replay \
  --bundle ./var/traces/private-replay.zip --offline
```

**执行回放**：用 RecordedProvider / RecordedTool 驱动当前 runtime。它严格匹配请求 bytes、工具 ID/参数和原生历史，不允许回退到真实网络：

```bash
python -m membraneclaw_runtime.tracing.cli replay \
  --bundle ./var/traces/private-replay.zip --offline --execute
```

执行回放要求已完成的 runtime_v1/gateway_v1 记录及匹配的 adapter 版本。第 2 节的纯存储示例不满足这个条件；应使用第 3 节录制的完整 runtime 任务。跨 HTTP 客户端工具交接、中断记录目前只支持记录播放。并行完成的真实时间不会重演。参数/请求发生分歧时返回 mismatch 并停止，不会悄悄请求模型补齐。

## 7. 流式与 HTTP

CLI 加 `--stream` 即使用原生 SSE：

```bash
membraneclaw --provider gemini --model gemini-2.5-flash \
  --stream --no-mcp --trace-dir ./var/traces -p '解释一下反渗透的基本原理。'
```

每个 SSE frame 先持久化再交给消费者；CLI 在流完成后打印最终回答。工具参数完整且响应结束前不会执行工具。断流保留已提交 frames，并标 partial；已转发数据的 attempt 不会自动拼接重试。

HTTP 启动示例：

```bash
export AGENT_PROVIDER=gemini
export AGENT_MODEL=gemini-2.5-flash
export TRACE_DIR=./var/traces
export AGENT_HTTP_NO_MCP=on
uvicorn server:app --host 127.0.0.1 --port 8001
```

本机单请求示例：

```bash
curl http://127.0.0.1:8001/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemini-2.5-flash","messages":[{"role":"user","content":"计算 27 * 19"}],"stream":false}'
```

配置了 `AGENT_API_KEY` 时，客户端还需发送 `Authorization: Bearer <AGENT_API_KEY>`。共享 API key 不代表具体用户身份。客户端工具续接要求现有 Open WebUI 签名 JWT 身份；匿名请求不能声明客户端工具或恢复私人 continuation。

HTTP 返回 trace schema、episode ID、request ID 和 locator。后续客户端工具结果必须保留工具 handle 和完全一致的历史前缀；部分结果先保存，全部到齐后才继续模型。重复相同结果幂等，跨用户、过期、编辑历史或冲突结果会拒绝。

`stream=true` 会在已提交 frame 后发送 keepalive，再返回完整可见内容/工具声明、finish/usage 和 `[DONE]`。**当前 HTTP 不逐字展示模型文本**，不向 UI 暴露原始 frame 或签名。普通下一轮聊天创建新 episode，不凭 user ID 猜测跨任务隐藏上下文。完整协议见 [HTTP 使用说明](docs/http-runtime.md)。

## 8. 归档到私有 GitHub 仓库

本地保存不会自动 push。先准备一个已存在的**私有轨迹仓库**、分支和正常 Git 认证；另外设置 `TRACE_ARCHIVE_GITHUB_TOKEN`，用于验证仓库确实为 private。

```bash
python -m membraneclaw_runtime.archives.cli publish \
  --trace-dir ./var/traces --episode "$EPISODE" \
  --remote git@github.com:YOUR_ACCOUNT/YOUR_PRIVATE_TRACE_REPO.git \
  --branch trace-data --state-dir ./var/trace-archive
```

| archive status | 含义 / 下一步 |
|---|---|
| `published` | 已验证远端 ref 包含 snapshot commit 和全部预期 blobs |
| `pending` | 本地仍在，归档未确认；修复网络/认证后重试同一命令，不重跑模型 |
| `not_configured` | 没有目标；不会自行创建仓库或上传 |

重试按 snapshot hash 幂等；只提交快照的确切路径，不 force push。远端有别人新增的提交时，重建独立 checkout 后追加自己的快照。receipt 与运行时 trace 分开保存。

归档恢复、访问权限与留存说明见 [归档运维说明](docs/archive-operations.md)。删除当前文件不等于删除 Git 历史；不要先公开原始记录再尝试补救。

## 9. 评估与健康检查

评估安装与角色配置见 [评估记录说明](docs/evaluation-tracing.md)。solver/router/teacher/judge/recovery 走同一 gateway，但可分别配置 provider；judge 和工具禁用角色不获得工具。

```bash
python -m pip install -e experiments/auto-evaluate
python -m auto_evaluate auto --help
python -m membraneclaw_runtime.operations \
  --trace-dir ./var/traces --archive-state ./var/trace-archive
```

评估结果保留 canonical event IDs 和私有 bundle；judge 视图裁剪会列出省略的事件 ID，原始记录不裁剪。复用已有结果不计作新 API 请求。研究导出需明确 dataset split，不能把测试轨迹自动混入训练集。

健康命令输出记录大小、incomplete episodes、unknown tool outcomes、请求/重试数、pending continuations 和归档状态。先区分执行失败、采集不完整和归档积压，再判断需要重试哪一层。

## 10. 验证范围与排错

离线验证覆盖合成三家协议、真实本地 MCP、文件故障/锁、HTTP 续接、无网络回放和本地 bare Git 归档；CI 覆盖 Python 3.11–3.13。真实三家 API、WaterTAP、Open WebUI 和私有 GitHub trace 归档 smoke 的状态需单独记录，不能用离线通过代替。

| 现象 | 检查 / 处理 |
|---|---|
| 缺少 model/key 或 ConfigurationError | 明确选择上表的 provider/model，确认对应 key；不支持的参数会在请求前拒绝 |
| trace_storage_error | 检查磁盘、目录权限和本地文件系统；停止新调用，保留已有文件 |
| continuation_busy | 原 worker 可能仍在运行；不要把正在等待网络的任务判死 |
| outcome_unknown | 远端动作可能已经发生；先检查记录和远端事实，不自动重复副作用 |
| bundle 已存在 | 换一个输出名；命令不会覆盖原文件 |
| replay mismatch / 不支持的版本 | 保留原 bundle，使用记录播放检查分歧；不要自动切真实 API |
| archive pending | 重试归档，不重跑原任务；检查 Git 认证、目标私有性与网络 |

格式、协议和实现边界分别见 [存储格式](docs/tracing.md)、[runtime](docs/runtime.md)、[streaming](docs/streaming.md)、[replay](docs/replay.md)。

实际 API smoke 必须显式开启。OpenAI / Claude 各自提供 key 和上表中对应的 `LIVE_OPENAI_MODEL` / `LIVE_ANTHROPIC_MODEL` 后，可运行：

```bash
RUN_LIVE_PROVIDER_SMOKE=1 python -m unittest discover \
  -s tests/providers/contract -p test_live_native.py -v
```

未提供开关、key 或 model 的 provider 显示 skipped，不计为 passed。真实调用会产生 API 用量；这个检查仅验证无工具任务，不代表 WaterTAP 或 Open WebUI 端到端测试通过。

## PR 对应关系

这些 PR 按依赖堆叠，每个 diff 只包含本阶段修改；不会自动合并。

| 计划范围 | PR |
|---|---|
| PR 1：存储基础，已合并上游 | [rtong/membraneclaw #41](https://github.com/rtong/membraneclaw/pull/41) |
| PR 2：Gemini + gateway | [#1](https://github.com/regulusv/membraneclaw/pull/1) |
| PR 5：通用 runtime + CLI | [#2](https://github.com/regulusv/membraneclaw/pull/2) |
| PR 3：OpenAI Responses | [#3](https://github.com/regulusv/membraneclaw/pull/3) |
| PR 4：Claude Messages | [#4](https://github.com/regulusv/membraneclaw/pull/4) |
| PR 6：持久化流式 | [#5](https://github.com/regulusv/membraneclaw/pull/5) |
| PR 7：HTTP + 客户端工具续接 | [#6](https://github.com/regulusv/membraneclaw/pull/6) |
| PR 8a：导出 + 离线回放 | [#7](https://github.com/regulusv/membraneclaw/pull/7) |
| PR 8b：评估集成 | [#8](https://github.com/regulusv/membraneclaw/pull/8) |
| PR 8c：Git 归档 + 运维 | [#9](https://github.com/regulusv/membraneclaw/pull/9) |
| 使用手册 + 显式 live 检查入口 | 当前 `codex/tracing-user-guide` 分支 |
