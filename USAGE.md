# 使用说明

## 安装依赖

```bash
pip install -r requirements.txt
```

## 推荐 CLI

```bash
python main.py collect-live --platform all --keyword "露营" --limit 10
python main.py crawl --platform xiaohongshu --mode test --seed "露营" --limit 10
python main.py plan-crawl --dry-run --seed "户外露营" --limit 10
python main.py profile-site --platform xiaohongshu
python main.py discover-keywords --from-db --top 20
python main.py analyze --from-db
python main.py export --format csv --table content
python main.py export --format xlsx --table all
```

`main.py` 是推荐的 CLI 入口。`collect-live --platform all` 会按平台逐个运行，显示本次真实采集数量和各平台原因；单个平台失败不会中断整轮。小红书使用现有 Chrome 浏览器采集流程，需要你手动登录；程序不会代登，也不会处理或绕过验证码。B 站走公开 API，遇到 HTTP 401/403/412/429 等限制即停止该平台。其他平台按当前适配器返回需授权、受限或暂不支持。`all` 不代表七个平台都会取得数据，本地历史数据也不会算作本次实时结果。详见 [平台接入说明](docs/platform-access.md)。

关键词采集：

```bash
python main.py collect-live --platform all --keyword "露营" --limit 10
```

指定单个平台和内容 ID（仅该平台适配器支持时可用）：

```bash
python main.py collect-live --platform bilibili --content-id BVxxxxxxxxxx --limit 10
```

小红书实时流程使用关键词，不支持 `--content-id`。XHS 每轮最多处理 3 条笔记，每条评论最多 20 条；没有有效登录态时，程序会提示手动登录并复核一次，仍被登录墙或验证拦截就停止。 结果写入 `config.yaml` 的 `output.db_path`，默认路径为 `data/xhs_crawler.db`。

## XHS 兼容主流程

```bash
python search_stream_scrape.py
```

这是保留的兼容入口。流程为：

关键词搜索页
→ 点击搜索结果卡片
→ 进入真实笔记详情页
→ 抓取笔记正文
→ 抓取评论
→ 生成规则总结
→ 保存 CSV / SQLite / Markdown

当前主流程不再依赖 `urls.txt`。

### 测试模式

```bash
python search_stream_scrape.py --test
```

默认值：

- `success_target = 3`
- `max_candidates = 15`
- `max_comments_per_note = 10`
- `sleep_between_notes_seconds = 5`
- `fast_skip_sleep_seconds = 1`
- `write_db = false`
- `generate_summary = false`

### 正式模式

```bash
python search_stream_scrape.py --prod
```

默认值：

- `success_target = 20`
- `max_candidates = 80`
- `max_comments_per_note = 50`
- `sleep_between_notes_seconds = 60`
- `fast_skip_sleep_seconds = 2`
- `write_db = true`
- `generate_summary = true`

即使使用 `--test` 或 `--prod`，程序仍会保留交互输入；直接回车即可接受对应默认值。

## 单条调试

```bash
python run.py
```

`run.py` 只保留为单链接调试入口，适合检查：

- 当前浏览器登录状态是否正常
- 详情页是否能识别正文和评论区
- `debug_page.html` / `debug_page_before_scroll.html` 是否仍是登录页、验证码页或结构变化

## 输出文件

流式主流程会输出：

- `outputs/csv/stream_notes_时间.csv`
- `outputs/csv/stream_comments_时间.csv`
- `outputs/summaries/summary_YYYYMMDD.md`
- `outputs/logs/stream_failed_时间.txt`
- `data/xhs_crawler.db`

其中：

- `stream_notes_*.csv` 保存笔记正文和元数据
- `stream_comments_*.csv` 保存评论明细
- `summary_YYYYMMDD.md` 保存当天追加式日报

## 当前会抓取什么

现在主流程会同时抓取：

- 笔记标题
- 笔记正文
- 作者显示名
- 发布时间
- 点赞 / 收藏 / 评论 / 分享展示值
- 图片数量
- 是否视频
- 评论内容
- 规则总结

默认不会下载图片，也不会采集手机号、私信、用户主页详细资料等敏感信息。

## 自动关键词发现

默认配置不写死具体业务关键词：

```yaml
keywords: []
discovery:
  mode: auto
  seed_keywords: []
```

如果没有用户输入入口，系统不会凭空启动采集；可以先用合法关键词、URL、账号或导入数据作为种子。采集完成后运行：

```bash
python main.py discover-keywords --from-db --top 20
```

系统会从通用内容表和评论表中抽取高频主题、实体词、标签式短语，并写入 `discovered_keywords` 表，同时输出 `reports/discovered_keywords_YYYYMMDD.md` 和 CSV。

## AI 与 Agent

采集流程中的规则总结默认关闭 AI 调用：

- `config.yaml -> ai`

Agent 是单独的可选 CLI 功能，可通过 OpenAI-compatible provider 调用模型。它需要你自行配置服务地址和 API key；不要把密钥写入仓库。默认权限会拦截写报告、导出等副作用操作。快速示例和权限说明见 [README](README.md#可选agent-命令行)。

## 自主采集规划、画像与去重

自主模式默认只在授权白名单内工作，不绕过登录、验证码、访问限制或平台风控。

```bash
python main.py plan-crawl --dry-run --seed "户外露营" --limit 10
python main.py crawl --engine autonomous --mode test --platform all --seed "户外露营" --limit 5
python main.py profile-site --platform bilibili
python main.py export --format xlsx --table all --output outputs/csv/full_export.xlsx
```

- `plan-crawl --dry-run` 只打印计划，不打开浏览器、不采集、不写采集数据。
- `crawl --engine autonomous --mode test` 会先生成计划，再调用现有 XHS/Bilibili 小规模 test 采集入口；浏览器型任务因共享 Chrome profile 默认串行执行。
- `profile-site` 维护每个平台的专精工作区画像，包括支持通道、健康分、推荐速率和调优备注。
- 去重层保留 URL 与 `(platform, content_id)` 唯一键，并为内容/评论写入规范化文本哈希和 SimHash。`dedupe_groups` 记录精确重复组。
- `export --format xlsx --table all` 会导出内容、评论、关键词、采集运行、站点画像和去重组等 sheet。

## 历史方案

以下文件属于历史方案，不再推荐继续作为主流程使用：

- `legacy/collect_urls.py`
- `legacy/batch_scrape.py`
- `legacy/crawler_daemon.py`

根目录中的：

- `collect_urls.py`
- `batch_scrape.py`
- `crawler_daemon.py`

现在只是兼容包装入口，会提示 Legacy，不建议继续使用。

旧的 `urls.txt` 批量方案已经不推荐，当前主流程不再依赖 `urls.txt`。

## 登录与安全验证

- 小红书未登录访问会被登录墙拦截，因此实时搜索笔记和读取评论需要有效登录态；本机 Chrome 中的登录和后续验证码都由用户手动完成。
- 登录、短信验证码、安全验证、滑块验证都需要你手动完成。
- 程序不会绕过验证码、不会破解接口、不会使用批量账号或规避访问限制的手段。
- 所有采集仅基于浏览器中公开可见的页面内容。
- 如果检测到登录、验证码、访问过于频繁、账号异常、403、429 或登录跳转，程序会标记为 `blocked`、`login_required`、`captcha_required` 或 `rate_limited`，并按配置降速、暂停或停止当前平台。
- 默认 Chrome 登录目录为 `%USERPROFILE%\Desktop\xhs_chrome_profile`，可用 `XHS_CHROME_PROFILE` 指定其他本地目录。不要分享或提交浏览器会话目录。
- 如果使用 HTTP 请求采集公开页面，默认启用 `respect_robots_txt: true`，robots 不允许的路径会标记为 `skipped_robots`。
- 建议优先使用官方 API、授权数据源、用户导出文件或自有数据。

## 迁移说明（v3 → v4 多平台框架）

v4 起项目改为多平台内容采集框架，数据库做了平滑迁移：

- 物理表 `notes` 自动重命名为 `content_items`，并新增 `platform`、`content_id`、`source_url`、`body`、`view_count`、`favorite_count`、`raw_metadata_json`、`last_error`、`blocked_reason`、`retry_count`、`next_retry_at` 等列。
- 老数据回填 `platform = 'xiaohongshu'`、`content_id` 由 `clean_url` 自动推导（兼容 `/explore/` 与 `/discovery/item/`）。
- `(platform, content_id)` 加唯一索引；`comments` / `summaries` 的外键自动指向 `content_items`。
- `data/xhs_crawler.db` 路径保留，无需重新采集；老 Python API（`upsert_note` / `get_note_by_clean_url` / `mark_note_*` 等）作为兼容包装继续可用。

迁移由 `python -c "from db import init_db; init_db()"` 或任意调用 `init_db()` 的入口（包括 `python inspect_db.py`）触发，幂等可重复执行。建议升级前先备份：

```powershell
Copy-Item .\data\xhs_crawler.db .\data\xhs_crawler.db.bak
```

## 频率建议

- 建议先用 `--test` 跑 3 篇做最小验证
- 每篇评论先采 10 到 30 条
- 避免高频、大规模采集
- 如果出现登录或验证码页面，需要先在浏览器中手动处理

## 排障建议

- 如果搜索页异常，先看 `debug_search_page.html`
- 如果详情页异常，先看 `debug_stream_序号.html`
- 如果评论抓不到，先看 `debug_page.html` 和 `debug_page_before_scroll.html`
- 如果页面仍是登录页、验证码页或安全验证页，需要先在浏览器中手动处理
