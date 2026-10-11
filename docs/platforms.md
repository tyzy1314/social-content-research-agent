# 平台接入说明

## 统一适配层设计

在**既有** `platforms/base.py` 的 `BaseAdapter` 之上扩展能力层，不新建第二套注册系统。

```python
class BaseAdapter:
    def capabilities(self) -> tuple[str, ...]: ...        # 实际实现的能力
    def platform_info(self) -> PlatformInfo: ...          # 能力 + 接入方式 + 验收等级
    def check_access(capability) -> AccessResult: ...     # OK / AUTH_REQUIRED / UNAVAILABLE / NOT_SUPPORTED
    def search_content(keyword, limit): ...
    def get_content_detail(content_id): ...
    def list_comments(content_id, limit): ...
    def list_replies(content_id, parent_comment_id, limit): ...
    def list_creator_content(creator_id, limit): ...
    def collect_barrage(content_id, limit): ...
    def normalize_content(raw) -> UnifiedContent: ...
    def normalize_comment(raw, *, platform_content_id) -> UnifiedComment: ...
```

**默认实现一律返回 `NOT_SUPPORTED`**：适配器必须显式覆写才能"声明"能力，
因此不存在"看起来支持、实际返回空数据"的空适配器。

## 状态语义（严格遵守）

| 状态 | 含义 |
|---|---|
| `OK` | 能力可用且已验证 |
| `AUTH_REQUIRED` | 需账号/应用授权（抖音、快手、微博） |
| `PERMISSION_DENIED` | 无该接口权限 |
| `UNAVAILABLE` | 平台不可达、被风控或网络失败（B站搜索 412 即属此类） |
| `NOT_SUPPORTED` | 该平台不具备此能力 |

**禁止**：用空数组/空结果表示"成功但无内容"。

## 平台清单

| 平台 | 适配器 | 能力 | 接入方式 | 等级 |
|---|---|---|---|---|
| 小红书 | `platforms/xiaohongshu.py` | 搜索/详情/评论 | 浏览器自动化（需登录态）+ 本地库 | DATA_VERIFIED |
| B站 | `platforms/bilibili_api.py` | 搜索/详情/评论/二级回复/弹幕 | **公开 Web 接口，只读，无需登录** | LIVE_VERIFIED |
| 抖音 | `platforms/douyin.py` | 详情/评论（仅授权账号） | 抖音开放平台 OAuth | BLOCKED |
| 快手 | `platforms/pending.py` | 详情/评论 | 开放平台（企业资质） | BLOCKED |
| 微博 | `platforms/pending.py` | 搜索/评论 | 开放平台（App Key + 审核） | BLOCKED |
| 知乎 | `platforms/pending.py` | — | 无官方接口 | NOT_SUPPORTED |
| 贴吧 | `platforms/pending.py` | — | 无官方接口 | NOT_SUPPORTED |

旧浏览器版 B站适配器保留为 `bilibili_browser`，避免破坏既有流程。

## B站接口细节（真实验证）

| 能力 | 端点 | 备注 |
|---|---|---|
| 视频详情 | `/x/web-interface/view?bvid=` | 统计在 `data.stat`（view/reply/like/coin/favorite/share） |
| 一级评论 | `/x/v2/reply?type=1&oid=<aid>` | `oid` 是 **aid** 不是 bvid；总数在 `data.page.count` |
| 二级回复 | `/x/v2/reply/reply?type=1&oid=<aid>&root=<rpid>` | `root`/`parent` 决定层级 |
| 弹幕 | `comment.bilibili.com/<cid>.xml` | 响应为 **raw deflate**，需 `zlib.decompress(raw, -15)` |
| 搜索 | `/x/web-interface/search/type?search_type=video` | **有风控**，高频调用返回 HTTP 412 |

### 已采取的风控应对（不绕过）

* 请求间最小间隔（默认 1s）；
* 搜索 412 时**如实返回 `UNAVAILABLE`**，降级为"按直接 BVID 采集"；
* **不伪造 cookie / 不使用代理 / 不破解 wbi 签名**。

## 抖音授权路径（已实现，待凭据）

```python
adapter.build_oauth_url()                 # 授权 URL
adapter.exchange_code_for_token(code)     # 换取 access_token
adapter.refresh_token(refresh_token)      # 刷新
```

凭据经构造参数或环境变量（`DOUYIN_CLIENT_KEY` / `DOUYIN_CLIENT_SECRET` / `DOUYIN_ACCESS_TOKEN`）
提供，**不写死在代码或文档**。未配置时所有能力返回 `AUTH_REQUIRED`。

**重要**：抖音开放平台接口面向**已授权账号**，
本项目**不**将其冒充为"全站任意视频评论接口"，故 `search_content` 返回 `NOT_SUPPORTED`。
