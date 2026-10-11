# 第五轮验收报告：多平台公开内容免登录接入与智能体自动降级

日期：2026-10-11
范围：让用户在**不配置任何平台账号**的前提下，尽可能分析各平台公开内容与评论；
对无合法免登录路径的平台准确报错并自动降级。

---

## 一、总体结论

**等级：B+**

理由：核心目标达成 —— 7 个平台中 **B站可真实免登录采集**（4 项能力），
**小红书可免登录用本地已有真实数据分析**（90 内容 / 419 评论），
抖音/微博/快手/知乎/贴吧的阻塞原因全部来自**实测证据**并给出准确错误与降级指引；
统一调度、统一记录、10 类测试、能力矩阵均已落地并经真实模型端到端验证。

未给 A 的原因：
1. 7 个平台中仅 **1 个**（B站）能真实在线免登录采集，小红书走的是本地数据路径；
2. B站搜索接口风控不稳定（同一次作业内 412 与 200 交替出现），
   导致"仅凭关键词"的输入无法稳定走通公开路径，需提供 BV 号；
3. B站评论接口存在平台侧软限流（声明 768 条实际返回 3 条），采集量不稳定。

---

## 二、六统一接入模式（已实现）

`platforms/capabilities.py` 中定义：`PUBLIC_READONLY` / `OFFICIAL_API` /
`AUTHORIZED_SESSION` / `USER_IMPORT` / `LOCAL_DATABASE` / `UNAVAILABLE`。

降级链（`DEFAULT_MODE_ORDER`）：

```
PUBLIC_READONLY → LOCAL_DATABASE → OFFICIAL_API → AUTHORIZED_SESSION → USER_IMPORT → UNAVAILABLE
```

既有 OAuth（抖音）保留为**独立模式** `OFFICIAL_API`，未被免登录路径覆盖或污染。

---

## 三、逐平台核查（游客可访问什么 —— 实测）

核查原则：每平台每能力**只发一次请求**，依真实响应判定；无合法端点的平台**不发起请求**并写明原因。

| 平台 | 免登录能力 | 实测证据 | 结论分类 |
|------|-----------|---------|---------|
| **bilibili** | 详情/评论/回复/弹幕 ✅，搜索 ⚠️波动 | `/x/web-interface/view` 200 code=0；`/x/v2/reply` 200 code=0；弹幕 XML raw deflate 解压成功；搜索同日先观测 **HTTP 412**，稍后同一端点 **HTTP 200** | **免登录可用** |
| **xiaohongshu** | 线上 ❌，本地 ✅ | Web 端需登录态（登录墙）；本地库 90 内容 / 419 评论 | **本地数据可用（线上需登录）** |
| **douyin** | ❌ | Web/APP 接口需 a_bogus / X-Bogus 签名或授权会话。**不探测、不破解** | **需登录/授权** |
| **weibo** | ❌ | `m.weibo.cn/api/container/getIndex` → **Sina Visitor System** 访客页；`weibo.com/ajax/side/hotSearch` → **HTTP 403** | **平台限制** |
| **kuaishou** | ❌（未发起请求） | 无面向公众的免登录只读接口；不破解签名 | **平台限制** |
| **zhihu** | ❌（未发起请求） | 无官方开放接口，需登录态 + 风控；不绕过 | **无合法路径** |
| **tieba** | ❌ | `hottopic/browse/hotall` 200 但仅 2.5KB 占位页，`topic_name`/`topic_id` 命中 0 | **无合法路径** |

### 能力级运行时状态（非硬编码）

B站实测矩阵（`scripts/generate_access_matrix.py` 生成，2026-10-11T09:13:52Z）：

| 能力 | 状态 | 接入模式 |
|------|------|---------|
| `content_detail` | AVAILABLE | PUBLIC_READONLY |
| `comments` | AVAILABLE | PUBLIC_READONLY |
| `replies` | AVAILABLE | PUBLIC_READONLY |
| `barrage` | AVAILABLE | PUBLIC_READONLY |
| `search_content` | AVAILABLE / RATE_LIMITED（随时间波动） | PUBLIC_READONLY |
| `creator_content` | NOT_SUPPORTED | UNAVAILABLE |

**状态波动本身就是"非硬编码"的证据**：同一天同一端点先返回 412、后返回 200。
系统如实记录，不做任何绕过。

### 限制信号处理（停止并记录，绝不绕过）

| 信号 | 映射 | 行为 |
|------|------|------|
| HTTP 412 / 429 | `RATE_LIMITED` | 立即停止该能力请求，消息中写明"不换 IP / 不换 UA / 不伪造参数"，并给出降级建议 |
| HTTP 401 / 403 | `BLOCKED` | 判定为平台拒绝访客访问 |
| api code -101/-111/-403 | `AUTH_REQUIRED` | 需登录态 |
| 网络异常 / 5xx | `UNAVAILABLE` | 记录异常类型 |

代码中不存在任何重试换 UA、IP 轮换、账号切换、签名破解或请求伪造逻辑。

### 两个额外发现的真实限制（已如实标注）

1. **评论接口 `ps` 需 ≥ 20**：`ps=3` 会返回空 `replies`（接口行为，非风控）。
2. **平台侧软限流**：`page.count` 声明 768 条，实际仅返回 3 条。
   代码新增 `partial=True` 并在消息中写明"疑似平台侧限流，已如实标注为部分返回，未做绕过"。

---

## 四、智能体统一调度（已实现）

`platform_dispatch.py` → `resolve_access()`，Agent 工具 `resolve_platform_access`。

决策链：
1. 平台/能力识别 → 2. 盘点本地已有真实数据（不发网络请求）→
3. 尝试免登录公开（含"关键词→搜索定位内容 ID"，1 次请求）→
4. 公开失败则回退本地数据 → 5. 判定是否需要授权并给出指引 →
6. 数据不足/无路径时明确标注缺失。

输出 `DispatchOutcome` 含：选定模式、逐模式尝试记录（含失败原因）、
可回溯引用、充分性判定、缺失标注、授权指引。

**充分性阈值** `MIN_SUFFICIENT_COMMENTS = 20`，阈值本身写进结果（`missing_note`），不做隐藏假设。

---

## 五、统一记录（九要素）

新增 `access_records` 表（schema v3，迁移前自动备份）：

`task_id` / `platform` / `capability` / `access_mode` / `status` / `ok` /
`source` / `record_count` / `failure_reason` / `citation` / `checked_at`。

append-only，成功与失败都留痕。实测一次 B站弹幕调度写入 6 条尝试记录，
含 `PUBLIC_READONLY:UNAVAILABLE`（SSL 异常）、`LOCAL_DATABASE:OK`、`OFFICIAL_API:NOT_SUPPORTED` 等。

---

## 六、测试（10 类，26 项，全部通过）

文件：`tests/test_platform_access.py`

| # | 类别 | 覆盖点 |
|---|------|-------|
| 1 | 无凭据启动 | 系统可启动；抖音不得声称可用；每平台声明合法接入模式 |
| 2 | 本地数据分析 | 免登录用本地数据；小红书线上不误报免登录；样本不足须标注 |
| 3 | 真实公开访问 | 真实取到 B站数据；评论可回溯；离线不得谎报 AVAILABLE |
| 4 | 缺 OAuth 降级 | 抖音返回 AUTH_REQUIRED + 指引，且**不产出任何数据**；抖音不声明免登录 |
| 5 | 限制信号 | 412→RATE_LIMITED、403→BLOCKED、搜索限流不返回空成功 |
| 6 | 错误隔离 | 抖音失败不影响 B站；各平台状态不统一硬编码 |
| 7 | 去重幂等 | 重复播种不产生重复记录；接入记录逐条留痕且字段完整；失败必带原因 |
| 8 | 模式选择 | 公开可用优先；不可用回退本地；不支持能力明确标注；弹幕不被评论数冒充 |
| 9 | 引用回溯 | 引用 ID 必须能在库中查到；全路径失败时不产出数据或引用 |
| 10 | 多平台报告 | 一次调度 5 平台，可用/不可用分别记录，失败项记录数为 0 |

全量回归：**145 passed**（含前四轮 119 项 + 本轮 26 项）。
真实联网测试：`PLATFORM_LIVE_TESTS=1` 下 26/26 通过。
静态检查：`ruff check .` All checks passed。

---

## 七、真实端到端确认

`outputs/e2e_round5_access.json`（真实模型 `glm-5.1`，6 次工具调用，32,322 tokens）：

模型自主选择并调用了
`probe_platform_capabilities` → `resolve_platform_access` → `analyze_platform_content`，
产出结论包含：

* 小红书：PUBLIC_READONLY 失败（AUTH_REQUIRED / 登录墙）→ 降级 LOCAL_DATABASE（419 条评论）；
* B站：未给 BV 号时 PUBLIC_READONLY 无法定位 → 降级 LOCAL_DATABASE（38 条评论）；
* 情感对比：小红书 正面 28.0% / 中性 72.0% / 负面 0.0%（50 条样本）；
  B站 正面 34.2% / 中性 63.2% / 负面 2.6%（38 条样本，含可回溯 comment_id）；
* 明确指出哪些平台无法免登录获取及原因。

结论与系统实际行为一致，**未出现把不可用平台写成"0 条成功"的情况**。

---

## 八、本轮修复的真实缺陷

1. `PendingAdapter` 的类属性 `access_modes` / `default_access_mode` 遮蔽了基类同名方法，
   导致 `adapter.access_modes()` 抛 `TypeError` → 改名为 `declared_*` 并加方法覆写。
2. 小红书适配器顶层导入 selenium / browser_utils，
   在无浏览器环境下**连本地数据路径都不可用** → 改为惰性导入。
3. `LOCAL_DATABASE` 模式曾用"评论条数"冒充弹幕可用 → 改为按能力匹配表，本地不落库的能力返回 0。
4. 最终失败原因曾取自缓存探测结论（滞后）→ 改为取自真实尝试记录。
5. B站评论探测曾为判断 partial 重复发请求 → 改为单次请求同时取状态与条数。
6. **新增**：关键词输入时先用搜索定位内容 ID（1 次请求），搜索被限流则如实记录并继续降级。

---

## 九、契约测试通过 ≠ 真实访问成功

* 契约层：打桩响应只用于验证**我们自己的状态映射逻辑**（412→RATE_LIMITED 等），
  打桩数据绝不写入报告或数据库；
* 真实层：`PLATFORM_LIVE_TESTS=1` 的联网测试、`outputs/access-matrix-round5.json`
  的实测矩阵、`outputs/e2e_round5_access.json` 的真实模型运行记录；
* 报告中所有"真实"数据均可回溯到 `citation` 中的评论 ID 与来源端点。

---

## 十、已知限制（不掩饰）

1. B站是唯一能真实在线免登录采集的平台，且搜索风控不稳定；
2. B站评论存在平台侧软限流，单次采集量可能远小于声明总数；
3. 小红书线上仍需登录态，免登录只能分析本地已有数据；
4. 抖音即使完成 OAuth，也只能取得**授权账号自己**的视频与评论，不含全站任意视频评论；
5. 知乎/贴吧无合法路径，只能靠用户导入或本地数据。

---

## 十一、可复现命令

```bash
# 生成真实能力矩阵
python scripts/generate_access_matrix.py

# 全量测试
python -m pytest tests/ -q

# 真实联网测试
PLATFORM_LIVE_TESTS=1 python -m pytest tests/test_platform_access.py -q

# 统一调度（小红书：免登录走本地数据）
python -c "from platform_service import resolve_platform_access; \
print(resolve_platform_access('data/xhs_crawler.db','xiaohongshu','comments',keyword='南京美食'))"

# 查看接入记录
python -c "from unified_store import list_access_records; print(list_access_records('data/xhs_crawler.db', limit=20))"
```
