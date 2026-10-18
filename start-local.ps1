param(
    [switch]$Install,
    [switch]$Stop
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not $Stop) {
    $newVenv = -not (Test-Path -LiteralPath $venvPython)
    if ($newVenv) {
        & python -m venv (Join-Path $projectRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Python 虚拟环境创建失败，请检查 Python 3.11 或更新版本。' }
    }
    if ($Install -or $newVenv) {
        & $venvPython -m pip install -r (Join-Path $projectRoot 'backend\requirements.lock')
        if ($LASTEXITCODE -ne 0) { throw '后端依赖安装失败。' }
    }
    if ($Install -or -not (Test-Path -LiteralPath (Join-Path $projectRoot 'frontend\node_modules\vite\package.json'))) {
        Push-Location (Join-Path $projectRoot 'frontend')
        try {
            & npm.cmd ci
            if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败。' }
        } finally { Pop-Location }
    }
}

if (-not (Test-Path -LiteralPath $venvPython)) { throw '未找到本项目的 Python 虚拟环境。' }
Push-Location (Join-Path $projectRoot 'backend')
try {
    $localArguments = @('-u', '-m', 'scripts.run_local')
    if ($Stop) { $localArguments += '--stop' }
    & $venvPython @localArguments
    if ($LASTEXITCODE -ne 0) { throw '本机工作台运行失败，请查看 data/local/logs 中的日志。' }
} finally { Pop-Location }
