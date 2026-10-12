# 多平台接入模式与免登录能力矩阵（第五轮）

> 本文件的状态**由真实运行时探测生成**，不是手写常量。
> 生成命令：`python scripts/generate_access_matrix.py`
> 产物：`outputs/access-matrix-round5.json`
>
> 硬约束：探测遇到 401/403/412/429 **立即停止该能力请求并记录原因**，
> 绝不换 IP / 换 UA / 换账号重试，绝不伪造请求或破解签名，绝不用模拟数据冒充真实采集。

## 统一实时采集入口

```powershell
python main.py collect-live --platform all --keyword "露营" --limit 10
```

该命令按平台逐个采集并分别显示结果；一个平台受限不会中断其他平台。小红书走现有 Chrome 浏览器流程，需要用户手动登录，每轮最多处理 3 条笔记、每条最多 20 条评论；数据仍由原采集表保存，再按 legacy 来源标记同步到统一表。B 站走公开 API，遇到 401/403/412/429 就停止该平台。其他平台只按现有适配器的能力执行；无合法实时路径时会显示原因，不会用本地旧数据替代实时结果。

---

> **状态说明：** 下方矩阵是第五轮验收时的运行快照，不保证当前实时可用。2026-10-11 上传前复验时，B 站视频详情与评论请求均返回 HTTP 412；本次未重试。当前平台状态以单次运行时探测结果为准。

---

## 一、六统一接入模式

| 模式 | 含义 | 是否需要账号 |
|------|------|------------|
| `PUBLIC_READONLY` | 平台允许的**免登录公开只读**接口 | 否 |
| `LOCAL_DATABASE` | 本地统一库中**已有的真实采集数据** | 否 |
| `USER_IMPORT` | 用户主动提供链接/文件导入 | 否（用户自备） |
| `OFFICIAL_API` | 官方开放平台 API（需应用凭据） | 是（应用级） |
| `AUTHORIZED_SESSION` | 已授权账号会话 / 浏览器登录态 | 是（用户级） |
| `UNAVAILABLE` | 无合法可用路径 | — |

降级顺序（可在调用时覆盖）：

```
PUBLIC_READONLY  →  LOCAL_DATABASE  →  OFFICIAL_API  →  AUTHORIZED_SESSION  →  USER_IMPORT  →  UNAVAILABLE
   免登录公开          本地已有真实数据        官方 API           授权会话           用户导入         标注缺失
```

**关键设计**：免登录公开优先（数据最新）；公开被限流/拒绝时回退本地已有真实数据；
两者都不行才判定需要授权，并给出可操作指引；最终无路径时明确标注"数据缺失"，
绝不静默产出空分析。

---

## 二、逐平台核查结论（游客能访问什么）

核查方式：每个平台每个能力**只发一次请求**，依据真实响应判定。

### 哔哩哔哩（bilibili）— ✅ 免登录可用

| 能力 | 运行时状态 | 接入模式 | 依据 |
|------|-----------|---------|------|
| 视频详情 `content_detail` | `AVAILABLE` | PUBLIC_READONLY | `/x/web-interface/view` HTTP 200, `code=0` |
| 评论 `comments` | `AVAILABLE` | PUBLIC_READONLY | `/x/v2/reply` HTTP 200, `code=0` |
| 二级回复 `replies` | `AVAILABLE` | PUBLIC_READONLY | `/x/v2/reply/reply` HTTP 200, `code=0` |
| 弹幕 `barrage` | `AVAILABLE` | PUBLIC_READONLY | `comment.bilibili.com/<cid>.xml`，raw deflate |
| 搜索 `search_content` | **随时间波动** `AVAILABLE` / `RATE_LIMITED` | PUBLIC_READONLY | 实测出现过 HTTP 412 风控 |
| UP主作品 `creator_content` | `NOT_SUPPORTED` | — | 需 wbi 签名 + 登录态，不实现 |

**实测到的三条真实限制（均如实记录、未绕过）**：

1. 搜索接口会返回 **HTTP 412**（风控）。同一次作业内先观测到 412，稍后同一端点又返回 200 ——
   说明状态随时间变化，因此状态必须运行时探测，不能写死。
2. 评论接口 `ps` 需 **≥ 20**，较小值会返回空 `replies`（接口行为，非风控）。
3. 评论接口存在**平台侧软限流**：`page.count` 声明 768 条，实际只返回 3 条。
   代码中记为 `partial=True` 并在消息里写明"疑似平台侧限流，未做绕过"。

### 小红书（xiaohongshu）— ❌ 线上不可免登录，✅ 本地数据可分析

| 能力 | 运行时状态 | 接入模式 | 依据 |
|------|-----------|---------|------|
| 搜索 / 详情 / 评论 | `AUTH_REQUIRED` | AUTHORIZED_SESSION | Web 端需登录态，未登录跳转登录墙 |
| 评论（本地路径） | `AVAILABLE` | **LOCAL_DATABASE** | 本地库 90 条内容 / 419 条真实评论 |

不绕过登录墙。免登录分析走 `LOCAL_DATABASE`。
（本轮修复：适配器改为惰性导入 selenium/browser_utils，
否则无浏览器环境下连本地数据路径都会因导入失败而不可用。）

### 抖音（douyin）— ❌ 无合法免登录路径

| 能力 | 运行时状态 | 接入模式 | 依据 |
|------|-----------|---------|------|
| 详情/评论/回复 | `AUTH_REQUIRED` | OFFICIAL_API | Web/APP 接口需 a_bogus / X-Bogus 签名或授权会话 |
| 全站搜索 | `NOT_SUPPORTED` | — | 官方不提供面向全站的公开搜索接口 |
| 弹幕 | `NOT_SUPPORTED` | — | 短视频平台无弹幕能力 |

**不破解签名、不伪造请求、不使用第三方解密服务。** 既有 OAuth 保留为独立模式
（`build_oauth_url` / `exchange_code_for_token` / `refresh_token` 均已实现）。
授权后也**只能**取得授权账号自己的视频与评论，不含全站任意视频评论 —— 这一点已写进适配器说明，不冒充。

### 微博（weibo）— ❌ 公开端点被访客系统拦截

| 能力 | 运行时状态 | 依据 |
|------|-----------|------|
| 搜索 `search_content` | `BLOCKED` | `m.weibo.cn/api/container/getIndex` HTTP 200 但返回 **Sina Visitor System** 访客页 |
| 热搜 Ajax | `BLOCKED` | `weibo.com/ajax/side/hotSearch` → **HTTP 403** `{"error":"Forbidden"}` |
| 评论 | `AUTH_REQUIRED` | 需开放平台应用审核 + 用户授权 |

### 快手（kuaishou）— ❌ 无免登录端点（未发起请求）

无面向公众的免登录只读数据接口。
**记录为"未发起请求"**，原因：Web/APP 接口需登录态与签名，不破解签名、不伪造会话。

### 知乎（zhihu）— ❌ 无合法免登录路径（未发起请求）

无官方开放数据接口，内容需登录态且存在风控。
**记录为"未发起请求"**，原因：不绕过登录态与风控。

### 百度贴吧（tieba）— ❌ 公开页无数据

| 能力 | 运行时状态 | 依据 |
|------|-----------|------|
| 搜索 `search_content` | `BLOCKED` | `tieba.baidu.com/hottopic/browse/hotall` HTTP 200，但仅约 2.5KB 占位页，`topic_name`/`topic_id` 命中数为 0 |

---

## 三、每平台验收结论（五类）

| 平台 | 分类 | 说明 |
|------|------|------|
| 哔哩哔哩 | **免登录可用** | 详情/评论/回复/弹幕真实可取；搜索受风控波动，412 时自动降级 |
| 小红书 | **本地数据可用（线上需登录）** | 免登录只能用本地已有真实数据（90/419） |
| 抖音 | **需登录/授权** | OAuth 已实现，缺凭据时准确报错 + 指引 |
| 微博 / 快手 | **平台限制** | 公开端点被拦截或无端点，需官方授权 |
| 知乎 / 贴吧 | **无合法路径** | 无官方接口或页面无数据，仅支持用户导入/本地数据 |

---

## 四、统一接入记录（九要素）

每次访问写入 `access_records` 表（append-only）：

| 字段 | 说明 |
|------|------|
| `task_id` | 任务 ID（串起一次调度的所有尝试） |
| `platform` / `capability` | 平台 / 能力 |
| `access_mode` | 本次尝试的接入模式 |
| `status` / `ok` | 结果状态 / 是否成功 |
| `source` | 来源（接口 URL / `sqlite:...#table`） |
| `record_count` | 取得记录数 |
| `failure_reason` | 失败原因（失败时必有） |
| `citation` | 原始引用 JSON（样本 ID + 文本片段 + HTTP 状态） |
| `checked_at` | 检查时间 |

查询：`python -c "from unified_store import list_access_records; print(list_access_records(DB))"`

---

## 五、契约测试通过 ≠ 真实访问成功

两者在本系统中**分别记录**，不混为一谈：

* 契约层：`tests/test_platform_access.py` 用打桩响应验证状态映射逻辑（412→RATE_LIMITED、403→BLOCKED 等），
  打桩只用于**验证我们的处理逻辑**，绝不写入报告冒充真实数据；
* 真实层：设置 `PLATFORM_LIVE_TESTS=1` 后运行真实联网测试，
  以及 `scripts/generate_access_matrix.py` 生成的矩阵、`outputs/e2e_round5_access.json` 的真实模型运行记录。

报告中凡标注"真实"的数据，都能追溯到 `citation` 里的评论 ID 与来源端点。
