# 数据库迁移说明（第四轮：跨平台统一数据层）

## 原则

1. **迁移前自动备份**（`data/xhs_crawler.backup_<时间戳>.db`），可回滚。
2. **原表不动**：`content_items` / `content_comments` / `comments` / `keywords` 全部保留，
   既有分析工具继续可用。
3. **幂等**：`CREATE TABLE IF NOT EXISTS` + UPSERT，可重复执行。
4. 统一表建在**同一个**库文件（`data/xhs_crawler.db`），无需新建库。

## 新增表

```sql
platform_content(
  id, platform, platform_content_id, content_type, title, text, source_url,
  author_ref, published_at, collected_at, engagement_metrics(JSON),
  raw_metadata(JSON), source_method, collection_run_id,
  UNIQUE(platform, platform_content_id)        -- 不同平台相同原始 ID 不冲突
)

platform_comments(
  id, platform, platform_comment_id, platform_content_id, parent_comment_id,
  content, author_ref, published_at, collected_at,
  engagement_metrics(JSON), provenance(JSON),
  UNIQUE(platform, platform_comment_id)        -- 重复采集不产生重复记录
)

platform_runs(
  run_id, platform, task_type, access_method, started_at, finished_at,
  status, records_seen, records_inserted, records_updated, error_type
)
```

`parent_comment_id` 为空＝一级评论；非空＝指向被回复的评论（二级/嵌套）。

## 执行方式

```python
from unified_store import migrate, import_xhs_history

report = migrate("data/xhs_crawler.db")        # 自动备份 + 建表
print(report["backup"])                        # 备份文件路径
import_xhs_history("data/xhs_crawler.db")      # 把既有小红书数据复制进统一表
```

幂等性验证（实测）：

| 执行 | inserted | updated | 总行数 |
|---|---:|---:|---:|
| 第 1 次 | 内容 90 / 评论 419 | 0 | 90 / 419 |
| 第 2 次 | **0** | 90 / 419 | **仍为 90 / 419** |

## 各平台统计字段不混用

`engagement_metrics` 以 JSON 按平台原样保存：

* B站：`view`（播放）、`danmaku`、`reply`、`like`、`coin`、`favorite`、`share`
* 小红书：`like`、`comment_display`

**分析层明确不合并、不跨平台排名**（播放量 ≠ 浏览量），
详见 `cross_platform.py` 的 `comparability_notes`。

## 重复内容的处理原则

* 同平台重复采集 → 由 `UNIQUE(platform, platform_content_id)` UPSERT 去重；
* **跨平台语义相似内容（转载/同一话题）不删除**——不同平台的记录各自保留，
  仅在比较时按主题聚合，不做基于文本相似的删除。

## 回滚

统一表为新增，删除即回滚；原数据另有备份文件：

```bash
cp data/xhs_crawler.backup_<时间戳>.db data/xhs_crawler.db
```
