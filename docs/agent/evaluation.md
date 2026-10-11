# Agent 回归与评测

这里把不同类型的检查分开说明，避免把脚本跑通、测试通过和真实模型效果混为一谈。

## 离线检查

默认测试不需要模型 API 密钥，也不会调用真实模型。它们覆盖 Agent 的运行循环、工具调用、权限审批、状态恢复、预算和错误处理，也包含用本地 HTTP 服务验证 OpenAI-compatible provider 请求与响应解析的测试。

从项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -m ruff check .
$baseTemp = Join-Path (Get-Location) ('.pytest-tmp/run-' + [guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe -m pytest tests/ -q "--basetemp=$baseTemp"
```

每次都使用新的 `--basetemp` 目录，不要反复复用已积累大量文件的目录。

## 本地 Agent 调用

仓库提供了一个固定的 scripted provider 示例：

```powershell
.\.venv\Scripts\python.exe main.py agent "统计采集库各表行数" `
  --provider scripted `
  --script scripts/agent_db_script.json `
  --db data/xhs_crawler.db `
  --json
```

这会按脚本调用真实的本地数据库工具，不会访问模型服务；因此它只能验证 CLI、工具注册和本地数据链路，不能证明真实模型会作出同样的选择。命令需要本机已有 `data/xhs_crawler.db`，仓库不附带采集数据库。

## 平台联网检查

`python scripts/smoke_test.py` 会访问真实平台，并读取本机数据库。完整工程门禁可以用：

```powershell
.\scripts\engineering_review.ps1 -Live
```

联网结果会受平台当时的可用性、搜索风控和部分返回影响。出现访问限制时应记录并停止，不要把一次失败解释成代码单元测试失败，也不要尝试绕过平台限制。

## 真实模型评测的边界

真实模型需要用户自行提供 OpenAI-compatible 服务地址、模型名和 API key。当前仓库没有自动给真实模型回答打分的评测器；真实模型运行属于需要单独配置和人工复核的实验，不包含在默认离线测试中。模型调用可能产生费用。

历史运行记录和封箱时的证据见 [`acceptance-report-final.md`](acceptance-report-final.md)。那些结果描述的是当时的运行，不应代替本次环境中的测试或当前平台状态。
