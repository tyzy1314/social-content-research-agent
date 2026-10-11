# 最终验收报告：项目封箱（RELEASE CANDIDATE）

封箱日期：2026-10-11
范围：第五轮工程改造完成后的**最终封箱检查与交付**。本轮不新增功能、不做架构重构。

> **状态声明（重要）**
> 本项目标记为 **RELEASE CANDIDATE / ENGINEERING COMPLETE**（工程完成、可交付候选）。
> 这**不等于**已生产部署，**也不代表**生产环境稳定性已通过验证。
> 尚未取得平台授权的部分（抖音/微博/快手/知乎/贴吧）**不再继续尝试**，
> 相关能力如实保持"需授权 / 无合法路径"状态。

---

## 一、Git diff 检查（无误修改 / 无临时代码 / 无意外删除）

| 检查项 | 结果 |
|---|---|
| 删除的文件 | **0**（`git diff --filter=D` 为空） |
| 误改的既有文件 | 无。改动集中在 15 个既有文件（db.py/README.md/config.example.yaml 等），均为前几轮有意为之的功能扩展 |
| 未跟踪的新增文件 | 41 项（`agent/`、`platforms/`、`tests/`、`docs/`、`scripts/`、`cross_platform.py`、`platform_dispatch.py` 等）—— 均为第 1～5 轮新增子系统，**非临时代码** |
| 临时/调试残留 | 仅 `粘贴的文本 (1).txt`（早期改造计划文本，18KB，无敏感内容），已加入 `.gitignore`，不纳入交付 |
| 分支 / 提交 | `main`，最后一次提交 `029a18a` |

**说明**：第 1～5 轮的新增子系统此前从未提交，封箱时一并纳入版本控制，
使仓库状态与实际代码一致（否则 41 个文件处于"未跟踪"的空悬状态）。

## 二、敏感信息检查（无泄漏）

| 检查项 | 结果 |
|---|---|
| 源码中的 `sk-` / `sk_tr_` / `api_key` / `Bearer` / `client_secret` / `access_token` 实值 | **0 命中**（仅 `tests/` 中 `test-key`、`test_key`/`test_secret` 占位符） |
| Cookie / `SESSDATA` / `bili_jct` / `DedeUserID` / 口令明文 | **0 命中** |
| Git 历史中的密钥（`git log -S`） | **0 命中** |
| 被跟踪的敏感文件（`.env` / `.provider_key` / `*.key`） | **0**（`git ls-files` 无匹配） |
| `config.yaml` | 已 gitignore；`ai.api_key` **故意留空**，改用 `--api-key` 或环境变量 `AGENT_API_KEY` |
| 数据库 / 输出目录 | `data/`、`*.db`、`outputs/`、`reports/`、`logs/` 均已 gitignore |
| 日志 | `outputs/logs/` 仅 2 个早期失败记录文本，无凭据 |

**保留的安全凭据处理说明**（写在 `.gitignore` 第 73-76 行与 `config.yaml` 注释中）：

```
# 密钥（不得提交）
.provider_key
.env
*.key
```

```
ai:
  api_key_env: "AGENT_API_KEY"
  # api_key 故意留空：密钥不写入本文件/文档，改用 --api-key 或环境变量 AGENT_API_KEY。
```

密钥只经 `--api-key` / `AGENT_API_KEY` 传入，保存在 `.provider_key`（已 gitignore），
**不写入 config、文档或日志**。

## 三、最后一次质量门禁

| 门禁 | 结果 |
|---|---|
| `ruff check .` | **All checks passed** |
| `pytest tests/ -q` | **145 passed**（前四轮 119 + 第五轮 26） |
| `PLATFORM_LIVE_TESTS=1 pytest tests/test_platform_access.py` | **26/26 passed**（真实联网） |
| `python scripts/smoke_test.py` | **全部通过**（5 组 27 项断言） |

Smoke Test 覆盖：① Agent 工具注册 = 18；② B站真实公开分析（详情+评论+可回溯）；
③ 跨平台报告（含自动数字复核与可比性说明）；④ 免登录降级链（小红书→本地 / 抖音→需授权 / 微博→BLOCKED）；
⑤ 统一接入记录九要素留痕。

## 四、数据库备份 / 迁移版本 / 历史数据完整性

| 检查项 | 结果 |
|---|---|
| `PRAGMA integrity_check` | **ok** |
| 迁移版本 | `SCHEMA_VERSION = 3`（第四轮 2 → 第五轮 3，新增 `access_records`） |
| 迁移前自动备份 | 4 份：`data/xhs_crawler.backup_20261011_{161951,170216,170404,170637}.db` |
| 统一表 | `platform_content` 92、`platform_comments` 457、`platform_runs` 5、`access_records` 43 |
| 按平台 | 内容 B站 2 / 小红书 90；评论 B站 38 / 小红书 419 |
| **原小红书历史表未改动** | `content_items` 90、`content_comments` 419、`comments` 419（与导入前一致） |
| 二级评论关系 | 32 条 `parent_comment_id` 非空 |

## 五、核心证据一致性核对

| 证据 | 内容 | 状态 |
|---|---|---|
| `outputs/access-matrix-round5.json` | 7 平台 × 6 能力的真实运行时探测矩阵 | ✅ 有效 JSON |
| `outputs/e2e_round5_access.json` | 真实模型免登录降级链路，6 次工具调用 / 32,322 tokens | ✅ finished |
| `outputs/e2e_round4_platform.json` | 真实模型平台分析链路，2 次工具调用 / 6,079 tokens | ✅ finished（封箱时重跑复验） |
| `outputs/e2e_business_report.json` | 真实模型业务闭环（分析→写报告），3 次调用 / 10,275 tokens | ✅ finished（封箱时重跑复验） |
| `outputs/e2e_real_model_1.json` | 单工具真实调用 / 2,061 tokens | ✅ finished |
| `outputs/e2e_real_model_2.json` | 8 次调用含自我纠错 / 13,419 tokens | ✅ finished |
| 全部 6 个证据文件 | JSON 解析 | **ALL JSON VALID** |

**封箱时的两处证据修复（如实记录）**：
1. `e2e_business_report.json` 与 `e2e_round4_platform.json` 原先带有修复前的
   jieba stdout banner 前缀，导致不是严格 JSON。封箱时用**当前代码重新跑了一遍真实链路**，
   替换为干净且可解析的证据（不是手工篡改）。
2. 重跑 `e2e_round4_platform.json` 时，第一次尝试模型轨迹发散、
   在 11 次工具调用后触发步数上限（`status=exceeded`，36,279 tokens）。
   改用明确分步指令后正常完成（2 次调用 / 6,079 tokens）。
   **该现象已记入已知限制：真实模型步数与成本存在波动。**

## 六、平台最终状态（真实在线 / 仅历史数据 / 需授权）

| 平台 | 最终状态 | 免登录可用 | 说明 |
|---|---|---|---|
| **哔哩哔哩** | **LIVE_VERIFIED（真实在线可用）** | ✅ | 详情/评论/二级回复/弹幕免登录真实可取；UP主作品列表 NOT_SUPPORTED（需 wbi 签名，不实现） |
| **小红书** | **DATA_VERIFIED（仅历史数据）** | ✅ 本地数据 | 线上需登录态（登录墙，不绕过）；免登录可分析本地已有真实数据 90 内容 / 419 评论 |
| **抖音** | **BLOCKED（需授权）** | ❌ | OAuth 路径已实现；Web 接口需 a_bogus/X-Bogus 签名，**不破解**；授权后仅能取授权账号自己的视频与评论 |
| **微博** | **BLOCKED（平台限制）** | ❌ | 公开端点被 Sina Visitor System 拦截；热搜 Ajax 返回 403；封箱复测另观测到 HTTP 432 |
| **快手** | **BLOCKED（需授权）** | ❌ | 无免登录端点，未发起请求（不破解签名） |
| **知乎** | **NOT_SUPPORTED（无合法路径）** | ❌ | 无官方开放接口，需登录态 + 风控，未发起请求 |
| **百度贴吧** | **NOT_SUPPORTED（无合法路径）** | ❌ | 公开页仅占位内容，无数据 |

**不会因为尚未取得授权而无限尝试**：上述状态已固化在适配器中，
新增探测只在用户明确触发时执行，且遇 401/403/412/429 立即停止。

## 七、启动命令与真实使用示例

### 环境准备

```bash
# 依赖（建议用项目虚拟环境）
.venv/Scripts/python.exe -m pip install -r requirements.txt -r requirements-dev.txt

# 密钥（不写入任何文件/文档）
export AGENT_API_KEY="<你的 key>"          # 或用 --api-key 传入
```

### 核心命令

```bash
# 1) 质量门禁（每次改动后必跑）
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m pytest tests/ -q --basetemp=".pytest-tmp/run-$(date +%s%N)"

# 2) 业务冒烟（5 组断言，含真实网络）
.venv/Scripts/python.exe scripts/smoke_test.py

# 3) 生成免登录能力矩阵（真实探测，非手写）
.venv/Scripts/python.exe scripts/generate_access_matrix.py

# 4) 数据库迁移（自动备份 + 幂等）
.venv/Scripts/python.exe -c "from unified_store import migrate; print(migrate('data/xhs_crawler.db'))"
```

### 真实使用示例

```bash
# A) 免登录：不配置任何平台账号，分析小红书 + B站（自动降级）
python main.py agent \
  "请在不配置任何平台账号的前提下，分析小红书和B站关于'南京美食'的公开评论" \
  --provider openai --yes --json

# B) B站真实在线采集（提供明确 BV 号最稳）
python main.py agent "采集B站视频 BV1YtAoz3Er3 的评论并分析" --provider openai --yes

# C) 先看能力再分析（推荐：模型会自行校验可用性）
python main.py agent "帮我分析B站关于南京美食的视频评论" --provider openai --json

# D) 业务闭环：分析并写出报告文件
python main.py agent "分析数据库中南京美食相关的评论情感分布，并把分析结果写成报告文件" \
  --provider openai --yes --json

# E) 跨平台对比（同主题；不混算不同平台互动指标）
python main.py agent "对比小红书和B站对南京美食的讨论差异" --provider openai

# F) 纯本地分析（完全不联网）
python -c "from platform_service import resolve_platform_access; \
print(resolve_platform_access('data/xhs_crawler.db','xiaohongshu','comments',keyword='南京美食'))"
```

> 默认 `deny_mutations`：只读工具直接执行，会写库/写文件的工具需批准（运行会暂停并说明原因）。
> 加 `--yes` 为无头模式（自动批准副作用工具）。

## 八、最终文件清单

### 核心子系统

| 文件 | 职责 |
|---|---|
| `platforms/capabilities.py` | 统一能力词汇表 + 六统一接入模式 + `CapabilityStatus` |
| `platforms/base.py` | `BaseAdapter`（能力声明 / 运行时探测 / 能力矩阵） |
| `platforms/bilibili_api.py` | **B站公开接口适配器（LIVE_VERIFIED）** |
| `platforms/xiaohongshu.py` | 小红书适配器（本地数据 + 授权会话） |
| `platforms/douyin.py` | 抖音适配器（OAuth 独立模式，BLOCKED） |
| `platforms/pending.py` | 快手/微博/知乎/贴吧（如实阻塞） |
| `platform_dispatch.py` | **统一接入调度与自动降级** |
| `platform_service.py` | 采集编排 + 统一记录落库 |
| `unified_store.py` | 跨平台统一数据层（schema v3，迁移前备份） |
| `cross_platform.py` | 跨平台分析与报告（含自动数字复核） |
| `agent_tools.py` | 18 个 Agent 工具注册 |
| `agent/` | Runtime / 状态 / 权限 / Provider / CLI |

### 测试与脚本

| 文件 | 说明 |
|---|---|
| `tests/test_platform_access.py` | 第五轮 26 项（10 个类别） |
| `tests/test_platform_adapters.py` | 第四轮 20 项 |
| `tests/test_agent_evaluation.py` | 评测集 24 项 |
| `tests/test_agent_*.py` / `test_audit_regressions.py` / `test_smoke.py` | 其余 75 项 |
| `scripts/generate_access_matrix.py` | 生成真实能力矩阵 |
| `scripts/smoke_test.py` | 封箱冒烟测试 |

### 文档

| 文件 | 说明 |
|---|---|
| `README.md` | 项目说明、启动命令、使用示例、平台状态 |
| `AGENTS.md` | 工程约定、封箱门禁、各轮现状与踩坑 |
| `docs/platform-access.md` | **免登录接入能力矩阵** |
| `docs/agent/acceptance-report-round2.md` / `round4.md` / `round5.md` | 各轮验收证据 |
| `docs/agent/acceptance-report-final.md` | **本文件：封箱最终验收** |
| `docs/agent/architecture.md` / `evaluation.md` / `github-comparison.md` | 架构契约 / 证据分层 / 基准对比 |
| `docs/db-migration.md` / `docs/platforms.md` | 迁移说明 / 平台清单 |

### 证据产物（`outputs/`，已 gitignore）

`access-matrix-round5.json`、`e2e_real_model_1.json`、`e2e_real_model_2.json`、
`e2e_business_report.json`、`e2e_round4_platform.json`、`e2e_round5_access.json`、
`reports/`、`csv/`、`summaries/`

## 九、已知限制（不掩饰）

1. **B站搜索 HTTP 412 风控**：同一天同一端点会在 412 与 200 之间波动。
   412 时立即停止请求并记录原因，**不换 IP / 不换 UA / 不伪造参数**；
   降级建议为"改用明确 BV 号"或"先用本地已有数据"。
2. **B站评论部分返回**：接口 `code=0` 但 `page.count` 声明 768 条时实际可能只返回 3 条
   （平台侧软限流）。已记为 `partial=True` 并在消息中写明，**未做绕过**，采集量因此不稳定。
   另注：评论接口 `ps` 需 ≥ 20，较小值会返回空 `replies`（接口行为，非风控）。
3. **抖音等平台授权阻塞**：抖音需 client_key/secret + 用户授权（且授权后仅覆盖授权账号自己的内容）；
   微博/快手需官方应用审核；知乎/贴吧无合法路径。**本轮不再为此无限尝试。**
4. **情感分析为词典规则基线**：非训练模型，无法识别否定（"不好不坏"）、反讽、
   子串误命中（"差不多"）等；标注样本实测 9/10，失败样例已记录。
   报告统一附 `caveats` 声明该局限。
5. **真实模型运行成本与轨迹波动**：单次真实运行 2,061 ～ 36,279 tokens 不等。
   封箱复验时观察到一次模型轨迹发散、11 次工具调用后触发步数上限（`status=exceeded`）。
   生产使用前应设 `--max-steps` / `--max-tokens` / `--max-seconds` 预算约束。
6. **样本代表性**：B站仅 2 条内容 / 38 条评论，小红书 90 / 419，
   受采集时机与平台推荐机制影响，**不代表平台整体舆情**；报告中已声明。
7. **不混算跨平台互动指标**：点赞/播放等不可跨平台比较，只比较情感占比与话题差异，
   且明确声明"负面占比差异 ≠ 平台用户情绪差异"。

## 十、最终结论

**状态：RELEASE CANDIDATE / ENGINEERING COMPLETE**

判定依据（全部一致）：
- Git diff 无删除、无误改、无临时代码残留；
- 敏感信息扫描零泄漏，凭据处理说明保留完整；
- `ruff` 全绿、`pytest` 145 通过、真实联网 26/26、Smoke Test 全通过；
- 数据库 `integrity_check: ok`，schema v3，4 份备份，历史数据 90/419 未改动；
- 6 个核心证据文件全部可解析，Agent / B站分析 / 跨平台报告 / 免登录降级四条链路均已真实复验；
- 平台状态划分明确：1 个真实在线可用（B站）、1 个仅历史数据可分析（小红书）、
  2 个需授权（抖音/快手）、1 个平台限制（微博）、2 个无合法路径（知乎/贴吧）。

**不宣称的内容**：
- 不宣称已生产部署；
- 不宣称生产稳定性已验证；
- 不把契约测试通过等同于真实访问成功；
- 不把本地历史数据分析等同于实时采集能力。

本轮开发到此正式停止，不再新增功能或重构。
