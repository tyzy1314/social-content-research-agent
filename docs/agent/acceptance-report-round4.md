# 第四轮验收报告：多平台社交媒体内容研究系统

---

## 平台状态总览（逐平台分级，不使用"已支持"笼统描述）

| 平台 | 能力 | 接入方式 | 验收等级 | 证据 |
|---|---|---|---|---|
| **小红书** xiaohongshu | 搜索/详情/评论 | 浏览器自动化（需登录态）+ 已有本地数据 | **DATA_VERIFIED** | 库内真实数据 90 内容 / 419 评论；本轮未做新实时抓取 |
| **B站** bilibili | 搜索/详情/评论/二级回复/弹幕 | 平台公开 Web 接口（只读，无需登录） | **LIVE_VERIFIED** | 5 项能力真实请求全部通过；采集入库 2 视频 / 38 评论 / 100 弹幕 |
| **抖音** douyin | 详情/评论（**仅授权账号**） | 抖音开放平台 OAuth | **BLOCKED** | OAuth 路径已实现，无 client_key/secret/token |
| 快手 kuaishou | 详情/评论 | 开放平台（企业资质 + OAuth） | **BLOCKED** | 无凭据 |
| 微博 weibo | 搜索/评论 | 开放平台（App Key + 应用审核） | **BLOCKED** | 无凭据 |
| 知乎 zhihu | — | 无官方开放接口 | **NOT_SUPPORTED** | 未实现抓取（合规考虑） |
| 百度贴吧 tieba | — | 无官方开放接口 | **NOT_SUPPORTED** | 未实现抓取 |

### 整体等级：**B**

理由（按证据，不自降 A 门槛）：统一架构 + 跨平台报告 + B站真实链路均已通过，
但**第一优先级中的抖音仍为 BLOCKED**（无授权凭据），三个首选平台只验证了 2 个，
故不满足 A 的"必需功能全部验收完成"。

---

## 一、现在有哪些平台？

注册表 `platforms/__init__.py::PLATFORM_ORDER`：`xiaohongshu, bilibili, douyin, kuaishou, weibo, zhihu, tieba`（7 个）。
状态由 `platform_info()` 逐个声明，见上表。

## 二、哪些平台真实接通？

**仅 B站（bilibili）为 LIVE_VERIFIED。** 真实请求验证：

| 能力 | 真实结果 |
|---|---|
| 视频详情 | `BV1GJ411x7h7` 播放 107,108,584 / 点赞 2,951,003 / 评论 237,242 |
| 一级评论 | 真实评论（含点赞 765,701 的热评） |
| 二级回复 | 真实嵌套回复链（`root`→`parent` 关系正确） |
| 弹幕 | raw deflate 解码，1,200 条真实弹幕 |
| 搜索 | 「南京美食」返回 5 条真实视频（真实 UP 主与播放量） |

小红书为 **DATA_VERIFIED**（有真实历史数据，未做新的实时抓取）。

## 三、哪些平台需要授权？

抖音（client_key/secret + 用户 OAuth）、快手（企业资质）、微博（App Key + 审核）。
三者均返回 `AUTH_REQUIRED`，**不返回空数据伪装成功**。

## 四、每个平台实际支持哪些功能？

见上表"能力"列。不支持的能力显式返回 `NOT_SUPPORTED`
（例如 B站 UP主作品列表需 wbi 签名 → `NOT_SUPPORTED`；抖音全站搜索 → `NOT_SUPPORTED`）。
抖音**未**把"授权账号评论管理接口"冒充为"任意视频评论接口"。

## 五、数据是否统一存储？**是。**

新增三张表（统一库即原 `data/xhs_crawler.db`）：

- `platform_content`（`UNIQUE(platform, platform_content_id)`）
- `platform_comments`（含 `parent_comment_id` 表达一级/二级）
- `platform_runs`（采集审计：seen/inserted/updated/error_type）

已验证：
- 不同平台相同原始 ID **不冲突**（测试覆盖）；
- 重复采集**幂等**（第二次 `inserted=0, updated=N`）；
- 历史小红书数据**保留并导入**（90/419），原表未改动；
- 迁移前**自动备份** `data/xhs_crawler.backup_*.db`。

## 六、Agent 能否自动选择平台？**能。**

真实模型（glm-5.1）运行证据 `outputs/e2e_round4_platform.json`：
模型先调用 `check_platform_access(bilibili, comments)` 确认可用，
再调用 `analyze_platform_content(platform="bilibili", keyword="南京美食")`，
**正确选择 B站而非小红书工具**，并基于真实数据生成报告。

## 七、能否完成跨平台内容分析？**能。**

同主题「南京美食」真实对比（`cross_platform_*` 报告）：

| 平台 | 样本评论 | 正面 | 负面 | 中性 | 负面占比 |
|---|---:|---:|---:|---:|---:|
| 小红书 | 233 | 81 | 7 | 145 | 3.0% |
| B站 | 38 | 13 | 1 | 24 | 2.6% |

- 共同主题：南京、好吃
- 平台独有：小红书＝交通/地铁/地址/夫子庙/新街口；B站＝李记/汤包/烤鸭/烧麦/皮肚面

**严格遵守的限制**：不同平台互动指标（播放 vs 浏览）不合并、不排名；
负面占比差异**不解读为**平台用户整体情绪差异；不可用的抖音如实列出、不以他平台数据推测。

## 八、报告能否追溯到真实内容？**能。**

报告含 `comment_id` + 原文片段（`evidence` / `negative_examples`），
并含**自动核验**环节：重算占比并检查计数一致性
（`counts_sum_consistent=true`、`ratios_match_recomputation=true`、`evidence_present=true`）。
输出两类文件：结构化 JSON + 可读 Markdown。

## 九、是否保留原有小红书功能？**是。**

原表（`content_items` / `content_comments` / `comments` / `keywords`）**未修改、未删除**；
原有 9 个业务工具 + 98 项测试全部保留并通过。新增 7 个平台工具（共 16 个）。

## 十、全部测试是否通过？**是。**

- ruff：`All checks passed`
- pytest：**118 通过 / 0 失败**（74 原有 + 24 业务评测 + 20 平台适配）
- 新增 `tests/test_platform_adapters.py`：契约（能力声明、不支持能力显式化、抖音不冒充、归一化字段映射）+ 统一库（ID 不冲突、幂等、父子评论、按平台筛选）+ 分析（数据不足诚实、报告数字可复算）

## 十一、当前有哪些阻塞？

1. **抖音/快手/微博**：无授权凭据 → BLOCKED（OAuth 路径已实现，待提供凭据）。
2. **B站搜索接口风控**：高频调用返回 HTTP 412；**未绕过**（不伪造 cookie/代理），已降级为"用直接 BVID 采集"，并把 412 如实记为 `UNAVAILABLE`。
3. **B站 UP主作品列表**：需 wbi 签名/登录态 → `NOT_SUPPORTED`。
4. **知乎/贴吧**：无官方开放接口 → `NOT_SUPPORTED`（不做登录态抓取）。
5. **情感分析**仍为词典规则基线（标注样本 9/10），非训练模型。
6. **无前端 WebUI**：仍为 CLI + JSON；未用虚假进度冒充。
7. 未做真实小红书**新**抓取（需登录态），仅用库内既有真实数据。

## 十二、与 MediaCrawler 等工程相比

**优势**：
- 与既有 Agent Runtime / 权限 / 事件存储**深度集成**，平台数据直接进入受控工具链与审批门；
- 每个平台有**显式能力声明与分级状态**（MediaCrawler 类工程通常默认"能抓"）；
- 采集经 `platform_runs` 审计、幂等写入、错误隔离；
- 分析链路带**自动数字核验**与来源引用；
- 不为凑平台数而创建空适配器。

**缺口（诚实）**：
- 覆盖面远小于 MediaCrawler（其支持多平台登录态抓取）；本项目**刻意**不实现登录态/风控绕过；
- 无断点续接队列、无并发调度、无 WebUI；
- 平台能力以"公开/授权"为界，因此可用平台数少但每个都真实。

---

## 关键文件

| 文件 | 作用 |
|---|---|
| `platforms/capabilities.py` | 能力与状态词汇表（OK/AUTH_REQUIRED/UNAVAILABLE/NOT_SUPPORTED） |
| `platforms/base.py` | `BaseAdapter` 扩展能力层（默认 NOT_SUPPORTED） |
| `platforms/bilibili_api.py` | B站公开接口适配器（LIVE_VERIFIED） |
| `platforms/douyin.py` | 抖音 OAuth 路径 + 诚实 BLOCKED |
| `platforms/pending.py` | 快手/微博/知乎/贴吧 阻塞声明 |
| `unified_store.py` | 统一数据层 + 备份迁移 |
| `platform_service.py` | 采集编排（审计/幂等/错误隔离） |
| `cross_platform.py` | 单平台与跨平台分析 + 报告 + 自核验 |
| `tests/test_platform_adapters.py` | 20 项平台测试 |

## 运行示例

```bash
python main.py agent "分析B站关于南京美食的评论" --provider openai      # 自动选平台
python main.py agent "对比小红书和B站对南京美食的讨论" --provider openai
python main.py agent "..." --provider openai --yes                     # 允许采集/写报告
```
