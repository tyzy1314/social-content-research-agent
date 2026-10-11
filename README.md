# 小红书评论分析与多平台内容研究

这个项目从小红书评论采集和情感分析做起，现在也包含 B 站等平台的公开内容读取、SQLite 数据管理和一个可选的 Agent 命令行入口。它更适合个人做小规模采集、整理和分析，不是面向全网的舆情监控服务。

## 先看平台限制

小红书的网页端会把未登录访问引到登录墙，笔记和评论的实时读取需要有效登录态。这不是简单换个接口就能解决的问题：项目不会绕过登录、验证码或风控，所以**实时采集小红书内容前，需要你在本机 Chrome 里手动登录**。如果登录过期或遇到安全验证，程序会停下来，不会尝试绕过。

没有登录时，小红书只能分析本地数据库里已经保存的数据；它不会凭空补出线上内容。数据库、浏览器登录会话和本机配置都不会随仓库提供。

按项目目前的验证结果，各平台情况如下：

| 平台 | 当前情况 |
| --- | --- |
| 小红书 | 实时采集需要登录；本地已有数据可以分析 |
| 哔哩哔哩 | 有免登录公开读取接口，但可用性会波动；本次上传前复验中，详情和评论都返回 HTTP 412，未重试 |
| 抖音 | 需要授权；不破解签名，也不把授权账号接口说成全站搜索 |
| 快手、微博 | 公开访问受限或需要授权 |
| 知乎、百度贴吧 | 当前没有实现可用的公开读取路径 |

遇到 401、403、412 或 429 等限制时，程序会记录原因并停止对应请求，不会换 IP、账号或伪造请求重试。详细状态和证据见[平台接入说明](docs/platform-access.md)。

## 环境准备

目前主要在 Windows + Python 3.10 环境下验证。小红书浏览器采集还需要安装 Chrome；分析已有数据和运行离线测试不需要登录平台。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt -r requirements-dev.txt
Copy-Item config.example.yaml config.yaml
```

`config.yaml`、采集数据库、导出文件和浏览器登录目录都是本地文件，已从 Git 跟踪中排除。默认 Chrome 用户目录是 `%USERPROFILE%\Desktop\xhs_chrome_profile`，与项目目录分开保存，也避免把登录会话混进仓库。如果你希望把登录会话放到别处，可设置 `XHS_CHROME_PROFILE` 环境变量；不要分享或上传这个目录。

## 常用命令

先看命令列表：

```powershell
python main.py --help
```

生成计划但不启动浏览器、不采集：

```powershell
python main.py crawl --platform xiaohongshu --dry-run --seed "露营" --limit 10
```

分析或导出本地数据库中的已有数据：

```powershell
python main.py analyze --from-db
python main.py discover-keywords --from-db --top 20
python main.py export --format csv --table comments --output outputs/comments.csv
```

小红书实时采集会打开 Chrome，并要求你手动完成登录或安全验证。可以先用小规模测试任务；是否能采集成功仍取决于平台当时的访问状态：

```powershell
python main.py crawl --platform xiaohongshu --mode test --seed "露营" --limit 10
```

更多参数和旧入口说明见 [`USAGE.md`](USAGE.md)。

## 可选：Agent 命令行

Agent 可以调用项目已有的查询、分析、导出和报告工具。连接真实模型需要一个 OpenAI-compatible API endpoint 和你自己的密钥：

```powershell
$env:AGENT_API_KEY = "你的 API key"
python main.py agent "分析本地数据库中露营相关评论的主要负面意见" `
  --provider openai `
  --base-url "https://你的服务商地址/v1" `
  --model "你的模型名" `
  --db data/xhs_crawler.db `
  --max-steps 10 `
  --max-tokens 20000
```

密钥只通过 `AGENT_API_KEY` 或 `--api-key` 传入，不要写进 `config.yaml`、README 或日志。模型调用可能产生费用；默认权限会拦截写文件等副作用操作。`--yes` 会自动批准所有副作用，只有在确认任务和数据安全时再使用。

## 验证与 Agent 测试

离线检查不需要模型密钥：

```powershell
.\scripts\engineering_review.ps1
```

需要真实网络和本机数据库时，再运行完整的平台冒烟检查：

```powershell
.\scripts\engineering_review.ps1 -Live
```

`-Live` 会访问真实平台，并读取 `data/xhs_crawler.db`；这个数据库不在仓库里，需要你先准备自己的数据。检查脚本会为每次 pytest 运行创建全新的临时目录，避免复用累积的测试文件。

Agent 的单元测试和脚本化调用也可以离线运行。脚本化调用只验证工具链路，不代表真实模型效果：

```powershell
python main.py agent "统计采集库各表行数" `
  --provider scripted `
  --script scripts/agent_db_script.json `
  --db data/xhs_crawler.db `
  --json
```

目前没有自动评分真实模型回答的评测器。测试通过说明代码路径符合测试预期，不代表平台访问始终可用，也不代表模型回答一定正确。Agent 的验证范围和限制见 [`docs/agent/evaluation.md`](docs/agent/evaluation.md)。

## 目前的局限

- 情感分析使用词典规则，不是训练好的模型；对否定、反讽等表达判断不稳。
- 平台返回的数据可能受登录状态、访问限制、时间和软限流影响；采样结果不能代表整个平台。
- 项目目前是工程交付候选，不等于已经部署或验证为稳定的生产服务。具体验收结论见[最终验收记录](docs/agent/acceptance-report-final.md)。

## 目录速览

- `main.py`：推荐的 CLI 入口。
- `platforms/`、`platform_dispatch.py`：平台适配和访问方式选择。
- `unified_store.py`、`db.py`：SQLite 数据存储。
- `agent/`、`agent_tools.py`：Agent 运行时和项目工具。
- `tests/`、`scripts/`：自动化测试、冒烟检查和辅助脚本。

旧采集入口仍有少量兼容封装；如果只是开始使用，优先从 `main.py` 进入。
