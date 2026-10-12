# 多平台社交内容研究 Agent

这是一个本地运行的命令行工具，用来查看多个社交平台的访问能力、整理本地已有内容，并做基础分析。项目包含小红书、哔哩哔哩、抖音、快手、微博、知乎和百度贴吧的适配入口，但**不代表这七个平台都能实时采集**：每个平台实际能读什么，要看登录要求、授权和平台当时的访问限制。

目前主要提供三类功能：

- **平台访问判断**：区分公开访问、本地数据、需要授权和暂不支持的能力；遇到登录墙或限流会说明原因，不伪装成采集成功。
- **本地内容分析**：从 SQLite 读取已有数据，做统计、关键词提取、词典情感分析、跨平台比较和导出。
- **可选 Agent**：用自然语言调用项目已有的查询、分析和报告工具；也可以不配置模型，直接使用普通 CLI 命令。

## 各个平台目前能做什么

以下是项目最近一次整理时确认的情况。实际访问状态会变化，运行时返回的信息优先于这张表。

| 平台 | 当前情况 |
| --- | --- |
| 小红书 | 实时读取笔记和评论需要登录；没有登录时只能分析本地数据库里已有的数据。 |
| 哔哩哔哩 | 有公开接口适配。详情、评论、回复和弹幕曾通过免登录验证；最近一次在线复验（2026-10-11）中，详情和评论请求返回 HTTP 412，未重试。本次功能更新未重做在线采集。 |
| 抖音 | 需要官方授权；不破解签名，也不把授权账号接口说成全站搜索。 |
| 快手 | 当前没有确认可用的免登录公开读取路径。 |
| 微博 | 公开请求会遇到访客系统或访问限制。 |
| 知乎、百度贴吧 | 当前没有实现可用的公开读取路径。 |

遇到 401、403、412 或 429 等响应时，程序会记录结果并停止对应请求，不会轮换 IP、账号或伪造请求。更详细的探测结果见[平台接入说明](docs/platform-access.md)。

## 为什么小红书采集需要登录

小红书未登录访问笔记和评论时会遇到登录墙，实时读取需要有效的登录态。这是平台对网页访问的限制，不是少填了一个参数或换个接口就能解决的问题。项目不会绕过登录、验证码或风控。

运行多平台采集时，程序会打开本机 Chrome。若页面要求登录，你需要自己登录并在终端确认；程序会复核一次，仍被拦截就停止小红书这一项。没有登录时，仍可分析本地数据库里已有的内容，但不会把旧数据当作本次实时采集结果。

默认 Chrome 用户目录是 `%USERPROFILE%\Desktop\xhs_chrome_profile`，与项目分开保存。可以用 `XHS_CHROME_PROFILE` 环境变量改到其他本地目录。不要分享或上传这个目录。

## 环境准备

目前主要在 Windows + Python 3.10 环境下验证。浏览器采集需要安装 Chrome；分析本地数据库和运行离线测试不需要登录任何平台。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt -r requirements-dev.txt
Copy-Item config.example.yaml config.yaml
```

`config.yaml`、采集数据库、导出文件和浏览器登录状态都只放在本机，不随仓库提供。密钥不能写入配置、代码或文档。

## 常用命令

先查看 CLI 支持的命令：

```powershell
python main.py --help
```

查看某个平台的计划，不启动浏览器、不采集：

```powershell
python main.py crawl --platform bilibili --dry-run --seed "露营" --limit 10
```

分析或导出本地已有数据：

```powershell
python main.py analyze --from-db
python main.py discover-keywords --from-db --top 20
python main.py export --format csv --table all --output outputs/all.csv
```

统一实时采集入口会按平台逐个处理；某个平台需要登录、被限流或暂不支持，不会阻止其他平台继续：

```powershell
python main.py collect-live --platform all --keyword "露营" --limit 10
```

也可以指定一个平台，例如 `--platform bilibili`。`--content-id` 只适用于单个平台，不适用于 `all`；小红书当前实时流程使用关键词。小红书每次最多处理 3 条笔记，单条评论上限最多 20 条；B 站搜索或评论遇到 HTTP 412 等限制时会停止。其他平台会如实显示需登录/授权、受限或暂不支持，不会拿本地旧数据冒充实时结果。 结果写入 `config.yaml` 的 `output.db_path`，默认是 `data/xhs_crawler.db`。

旧的单平台 XHS 兼容入口仍可使用；新任务建议从 `collect-live` 开始：

```powershell
python main.py crawl --platform xiaohongshu --mode test --seed "露营" --limit 10
```

更多命令和旧入口说明见 [`USAGE.md`](USAGE.md)。

## 可选 Agent

Agent 可以结合平台访问状态与本地数据调用分析工具。连接真实模型需要你自己的 OpenAI-compatible API endpoint 和密钥：

```powershell
$env:AGENT_API_KEY = "你的 API key"
python main.py agent "检查小红书和哔哩哔哩当前可用的访问方式，并比较本地已有评论" `
  --provider openai `
  --base-url "https://你的服务商地址/v1" `
  --model "你的模型名" `
  --db data/xhs_crawler.db `
  --max-steps 10 `
  --max-tokens 20000
```

密钥只通过 `AGENT_API_KEY` 或 `--api-key` 传入。模型调用可能产生费用。默认权限会拦截写文件等副作用操作；`--yes` 会自动批准所有副作用，只有确认任务和数据安全时再使用。

## 测试与验证

离线工程检查不需要模型密钥：

```powershell
.\scripts\engineering_review.ps1
```

需要真实网络和本机数据库时，可以运行在线检查：

```powershell
.\scripts\engineering_review.ps1 -Live
```

`-Live` 会访问真实平台并读取 `data/xhs_crawler.db`；数据库不会随仓库提供。平台可能临时限流或返回部分数据，在线检查结果不等同于离线单元测试结果。

Agent 的脚本化测试可以验证工具调用链路，但不能代替真实模型评测。目前项目没有自动给真实模型回答打分的评测器。详见[Agent 评测说明](docs/agent/evaluation.md)。

## 目前的局限

- 不同平台的访问能力差异很大；适配入口不等于平台当前允许实时采集。
- 小红书实时读取需要登录；B 站公开接口也可能返回 412 或部分结果。
- 情感分析使用词典规则，不是训练好的模型；对否定、反讽等表达判断不稳。
- 采集样本受时间、访问权限和平台推荐机制影响，不能代表整个平台。
- 这是本地研究和工程验证项目，不等于已经部署或验证为稳定的生产服务。封箱时的记录见[最终验收说明](docs/agent/acceptance-report-final.md)。

## 目录速览

- `main.py`：推荐的 CLI 入口。
- `platforms/`、`platform_dispatch.py`：七个平台的适配入口和访问方式选择。
- `unified_store.py`、`db.py`：SQLite 数据存储。
- `agent/`、`agent_tools.py`：Agent 运行时和可调用工具。
- `tests/`、`scripts/`：自动化测试、联网 smoke 和工程检查脚本。

旧版采集入口仍保留少量兼容封装；新用法优先从 `main.py` 开始。