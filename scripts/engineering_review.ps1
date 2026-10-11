[CmdletBinding()]
param(
    [switch]$Live
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "找不到项目虚拟环境：$Python。先创建 .venv 并安装 requirements.txt 与 requirements-dev.txt。"
}

function Invoke-Step {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$Command
    )

    Write-Host "`n==> $Name"
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Name 失败，退出码：$LASTEXITCODE"
    }
    Write-Host "PASS: $Name"
}

function New-PytestBaseTemp {
    $directory = Join-Path $ProjectRoot ".pytest-tmp\run-$([guid]::NewGuid().ToString('N'))"
    if (Test-Path -LiteralPath $directory) {
        throw "pytest 临时目录意外已存在：$directory"
    }
    return $directory
}

$compileTargets = @("agent", "discovery", "legacy", "platforms", "scripts", "tests")
$compileTargets += @(Get-ChildItem -LiteralPath $ProjectRoot -File -Filter "*.py" | ForEach-Object { $_.FullName })
Invoke-Step "Python syntax" { & $Python -m compileall -q $compileTargets }
Invoke-Step "ruff" { & $Python -m ruff check . }

$testBaseTemp = New-PytestBaseTemp
Invoke-Step "pytest (offline regression)" {
    & $Python -m pytest tests/ -q "--basetemp=$testBaseTemp"
}

if ($Live) {
    $database = Join-Path $ProjectRoot "data\xhs_crawler.db"
    if (-not (Test-Path -LiteralPath $database)) {
        throw "联网冒烟检查需要本机数据库：$database。先准备自己的数据，或不加 -Live 运行离线检查。"
    }

    Invoke-Step "live smoke test" { & $Python scripts/smoke_test.py }

    $liveBaseTemp = New-PytestBaseTemp
    $previousPlatformLiveTests = $env:PLATFORM_LIVE_TESTS
    try {
        $env:PLATFORM_LIVE_TESTS = "1"
        Invoke-Step "live platform tests" {
            & $Python -m pytest tests/test_platform_access.py -q "--basetemp=$liveBaseTemp"
        }
    }
    finally {
        if ($null -eq $previousPlatformLiveTests) {
            Remove-Item Env:PLATFORM_LIVE_TESTS -ErrorAction SilentlyContinue
        }
        else {
            $env:PLATFORM_LIVE_TESTS = $previousPlatformLiveTests
        }
    }
}

Write-Host "`n所有已请求的检查均通过。"
