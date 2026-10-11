# GitHub 再对标（第二轮）——基于真实实现的差距复核

与上一轮的通用 README 调研不同，本文只针对**本项目已落地的 Runtime 与 Tool System**
逐项复核差距，并对每个差距回答五个判断问题：

1. 本项目是否真正需要？
2. 已有代码能否实现？
3. 引入成熟依赖是否更可靠？
4. 成本与维护负担是否合理？
5. 直接修改 / 保留现状？

参考：langgraph、openai-agents-python、pydantic-ai、crewAI、OpenHands、microsoft/agent-framework。

---

## 1. Runtime 稳定性

| 项 | 参考做法 | 本项目现状 | 判定 |
|---|---|---|---|
| 步骤预算 | langgraph 递归上限 | `RuntimeConfig.max_steps`，已在循环入口强制 | 已具备 |
| 卡死检测 | OpenHands iteration guard | `(tool,args)` 哈希窗口计数 | 已具备 |
| 墙钟超时 | 各框架均有 | **本轮新增** `max_wall_clock_seconds` | ✅ 已补齐 |
| Token 预算 | agent-framework / pydantic-ai usage limits | **本轮新增** `max_total_tokens` | ✅ 已补齐 |
| 取消/中断 | OpenHands 可中断 agent | **本轮新增** `AgentRuntime.cancel()` + `--cancel` | ✅ 已补齐 |
| 失败重试 | langgraph RetryPolicy（0.5s/×2/上限128s） | provider 内指数退避 + 抖动 | 已具备 |

**结论**：不需要引入框架。缺的三项已用约 30 行自有代码补齐，且被
`test_eval_22/23/24` 故障注入覆盖。引入 langgraph 只会带来图抽象与状态模式重写成本。

## 2. Tool Calling 正确性

- 参数 Schema 由 `inspect.signature` + `get_type_hints` 生成，不支持的类型**在注册期**就 `TypeError`（不是调用期）。
- 校验失败 → `error_kind="invalid_input"`，模型可见并可重试（`test_eval_09`）。
- 模型幻觉工具名 → **第二轮修复**：原先 `ToolNotFound` 未被捕获会**崩溃整个 run**，现在降级为可恢复的 tool_result 并提示可用工具（`test_eval_13`）。

**判定**：对齐 pydantic-ai 的"类型即 schema"思路，但保持零依赖。不需要改。

## 3. 多步骤任务编排

- 现状：单智能体 + 单循环，每步一个工具调用；真实模型已实测 8 步 8 次调用自主完成多步分析并自我纠错。
- 是否需要 Multi-Agent？**否**。业务是"查库 → 分析 → 出报告"的线性链路，单智能体 + 工具集已可靠完成；拆成多角色只会增加状态同步与权限边界复杂度，却没有新的能力。
- 结论：**保持轻量**，符合"不为框架而框架"。

## 4. 状态持久化

- 追加式事件日志为真相源、`agent_runs` 为派生状态（OpenHands 模式）。
- `resume` 从事件日志重建消息，**不依赖客户端传入的对话历史**（防篡改）。
- **本轮验证**：resume 不会重放被阻断的副作用（`test_eval_19`）。

**判定**：与参考工程同一层次，保留现状。

## 5. Human-in-the-loop

| 机制 | 参考 | 本项目 | 判定 |
|---|---|---|---|
| 审批绑定参数哈希 | agent-framework | `approval_hash(tool, args)`，改参即失效 | 已具备 |
| `deny_mutations` 默认 | 默认最小权限 | 默认策略，副作用工具暂停 | 已具备 |
| 审批拒绝真阻断 | — | `test_eval_16` 验证未写文件 | 已具备 |
| resume 绕过审批？ | — | 不可能：resume 仍走同一 `invoke` 管线与策略 | 已具备 |

**新增副作用工具**（`write_report`）同样纳入审批门（`test_eval_14`），并做了路径净化
（`test_eval_18`：目录穿越被中和，比直接拒绝更强）。

## 6. 失败恢复

- 工具内部异常 → 对模型不透明（`"tool X failed"`，不泄漏路径/堆栈），循环仍走到 final（`test_eval_11`）。
- 工具超时 → 工作线程 + `future.result(timeout)`，映射为 `failed`（`test_eval_12`）。
- 4xx 不重试、429/5xx 退避（`OpenAICompatibleProvider`）。

## 7. Tracing / 可观测性

- 可插拔 `TraceSink`（SQLite / JSONL / Multi），未硬编码 OTel——与 OpenHands 的 pluggable sink 思路一致。
- 每次模型调用落 `provider_call` 事件（含耗时、attempts、usage）。
- **判定**：够用。引入 OTel/LangSmith 会增加部署依赖，当前规模不划算。

## 8. Evaluation

- 参考做法（pydantic-ai / OpenHands 的 eval harness）：分层、可复现、区分 mock 与真实模型。
- **本轮补齐**：`tests/test_agent_evaluation.py`，24 个任务，明确分层，且**不把 scripted 结果伪装成真实模型结果**。
- 缺口：尚无自动化"工具选择正确率 / 参数正确率"的批量打分器——人工从 trace 判定。记为已知不足。

## 9. 安全与权限

| 风险 | 处置 | 证据 |
|---|---|---|
| `db_query` 越权写库 | 仅允许 `SELECT\|WITH` 开头 + SQLite `mode=ro` | `test_eval_10`（DELETE 被拒且库未变） |
| 导出/报告路径越界 | 文件名净化 + resolve 复核 | `test_eval_18` |
| Prompt Injection 提权 | 工具结果只作为 `role=tool` 内容，不授予任何权限 | `test_eval_17`（注入文本未触发写文件） |
| 密钥泄露 | 密钥仅走 `--api-key`/环境变量，不入 config 与文档 | CLI 实现 |

**判定**：权限模型与 agent-framework 的 allowlist 思路一致，无需引入依赖。

## 10. 工程部署

- 零新增第三方依赖（provider 用 stdlib urllib），Python 3.10 可跑。
- CLI 单一入口 `python main.py agent`，可 `--json` 供程序消费。
- 缺口：**无前端**。当前以 CLI + JSON + 生成报告文件满足"查看任务/步骤/报告"需求；
  按第七节要求"不得为了增加界面而重写稳定的业务底层"，故**保持 CLI 优先**。

---

## 总体结论

**不引入任何新框架。** 九个维度中，真正缺的只有"墙钟超时 / Token 预算 / 取消"三项，
已用自有代码补齐并被故障注入测试覆盖；其余维度本项目已与参考工程处于同一层次，
引入 langgraph/crewAI 只会增加抽象与维护负担而没有新能力。

唯一诚实保留的不足：**eval 缺自动打分器**、**无前端 UI**、**情感仍是词典基线**。
