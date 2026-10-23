[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$SkipInstall,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Root = [IO.Path]::GetFullPath($PSScriptRoot)
$BackendDir = Join-Path $Root 'backend'
$FrontendDir = Join-Path $Root 'frontend'
$RuntimeDir = Join-Path $Root '.local-dev'
$LogDir = Join-Path $RuntimeDir 'logs'
$ManifestPath = Join-Path $RuntimeDir 'processes.json'
$DependencyMarkerPath = Join-Path $RuntimeDir 'dependencies.json'
$VenvDir = Join-Path $BackendDir '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$BackendEnvPath = Join-Path $BackendDir '.env'
$EnvExamplePath = Join-Path $Root '.env.example'
$script:Services = @()

function Write-Title([string]$Message) {
    Write-Host "`n=== $Message ===" -ForegroundColor Cyan
}

function Invoke-Checked([string]$Executable, [string[]]$Arguments, [string]$WorkingDirectory) {
    Push-Location -LiteralPath $WorkingDirectory
    try {
        & $Executable @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "命令失败（exit code $LASTEXITCODE）：$Executable $($Arguments -join ' ')"
        }
    }
    finally {
        Pop-Location
    }
}

function Write-Manifest {
    if (-not (Test-Path -LiteralPath $RuntimeDir)) {
        New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
    }

    $snapshot = @($script:Services)
    if ($snapshot.Count -eq 0) {
        $json = '[]'
    }
    else {
        $json = ConvertTo-Json -InputObject $snapshot -Depth 6
    }
    [IO.File]::WriteAllText(
        $ManifestPath,
        $json,
        ([System.Text.UTF8Encoding]::new($false))
    )
}

function Stop-ManagedEntries([object[]]$Entries) {
    $orderedEntries = @($Entries)
    [array]::Reverse($orderedEntries)

    foreach ($entry in $orderedEntries) {
        if ($null -eq $entry) {
            continue
        }

        $processId = 0
        try {
            $processId = [int]$entry.pid
        }
        catch {
            Write-Host '跳过一条无效的进程记录。' -ForegroundColor Yellow
            continue
        }

        $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if (-not $process) {
            Write-Host "$($entry.name) 已停止。" -ForegroundColor DarkGray
            continue
        }

        $processPath = $null
        $processStartTicks = 0L
        try {
            $processPath = [IO.Path]::GetFullPath([string]$process.Path)
        }
        catch {
        }
        try {
            $processStartTicks = [long]$process.StartTime.ToUniversalTime().Ticks
        }
        catch {
        }

        $entryPath = $null
        try {
            if (-not [string]::IsNullOrWhiteSpace([string]$entry.executable)) {
                $entryPath = [IO.Path]::GetFullPath([string]$entry.executable)
            }
        }
        catch {
        }

        $entryStartTicks = 0L
        $hasEntryStart = $false
        try {
            if ($null -ne $entry.startTicks) {
                $entryStartTicks = [long]$entry.startTicks
                $hasEntryStart = $entryStartTicks -gt 0
            }
        }
        catch {
        }
        if (-not $hasEntryStart) {
            try {
                if (-not [string]::IsNullOrWhiteSpace([string]$entry.startTime)) {
                    $entryStartTicks = [DateTime]::Parse([string]$entry.startTime).ToUniversalTime().Ticks
                    $hasEntryStart = $entryStartTicks -gt 0
                }
            }
            catch {
            }
        }

        $samePath = $processPath -and $entryPath -and ($processPath -ieq $entryPath)
        $sameStart = $hasEntryStart -and ($processStartTicks -gt 0) -and ($processStartTicks -eq $entryStartTicks)
        if ($samePath -and $sameStart) {
            try {
                Stop-Process -Id $processId -Force -ErrorAction Stop
                Write-Host "已停止 $($entry.name) (PID $processId)" -ForegroundColor DarkGray
                Start-Sleep -Milliseconds 150
            }
            catch {
                Write-Host "停止 $($entry.name) (PID $processId) 失败：$($_.Exception.Message)" -ForegroundColor Yellow
            }
        }
        else {
            Write-Host "跳过 PID $processId：进程路径或启动时间与记录不符，未作更改。" -ForegroundColor Yellow
        }
    }
}

function Stop-FromManifest([switch]$Silent) {
    if (-not (Test-Path -LiteralPath $ManifestPath)) {
        if (-not $Silent) {
            Write-Host '没有找到本脚本记录的开发服务进程。' -ForegroundColor Yellow
        }
        return
    }

    $raw = Get-Content -LiteralPath $ManifestPath -Raw
    $entries = @()
    if (-not [string]::IsNullOrWhiteSpace($raw)) {
        $parsed = ConvertFrom-Json -InputObject $raw
        if ($null -ne $parsed) {
            $entries = @($parsed)
        }
    }

    Stop-ManagedEntries -Entries $entries
    $script:Services = @()
    Write-Manifest

    if (-not $Silent) {
        Write-Host '本地开发服务已停止；日志保留在 .local-dev/logs。' -ForegroundColor Green
    }
}

function Quote-Argument([string]$Value) {
    if ($null -eq $Value) {
        $Value = ''
    }
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') {
        return $Value
    }

    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append([char]34)
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq [char]92) {
            $backslashes++
            continue
        }
        if ($character -eq [char]34) {
            for ($index = 0; $index -lt (($backslashes * 2) + 1); $index++) {
                [void]$builder.Append([char]92)
            }
            [void]$builder.Append([char]34)
            $backslashes = 0
            continue
        }
        for ($index = 0; $index -lt $backslashes; $index++) {
            [void]$builder.Append([char]92)
        }
        [void]$builder.Append($character)
        $backslashes = 0
    }
    for ($index = 0; $index -lt ($backslashes * 2); $index++) {
        [void]$builder.Append([char]92)
    }
    [void]$builder.Append([char]34)
    return $builder.ToString()
}

function Start-ManagedProcess(
    [string]$Name,
    [string]$Executable,
    [string[]]$Arguments,
    [string]$WorkingDirectory
) {
    if (-not (Test-Path -LiteralPath $LogDir)) {
        New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    }

    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $stdout = Join-Path $LogDir "$stamp-$Name.out.log"
    $stderr = Join-Path $LogDir "$stamp-$Name.err.log"
    $argumentLine = ($Arguments | ForEach-Object { Quote-Argument ([string]$_) }) -join ' '

    $process = Start-Process `
        -FilePath $Executable `
        -ArgumentList $argumentLine `
        -WorkingDirectory $WorkingDirectory `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru

    Start-Sleep -Milliseconds 500
    $process.Refresh()
    if ($process.HasExited) {
        $details = @()
        foreach ($logPath in @($stdout, $stderr)) {
            if (Test-Path -LiteralPath $logPath) {
                try {
                    $details += Get-Content -LiteralPath $logPath -Tail 40
                }
                catch {
                }
            }
        }
        throw "$Name 启动后立即退出。`n$($details -join "`n")"
    }

    $startTime = $process.StartTime.ToUniversalTime()
    $script:Services += [pscustomobject]@{
        name = $Name
        pid = [int]$process.Id
        executable = [IO.Path]::GetFullPath($Executable)
        startTime = $startTime.ToString('o')
        startTicks = [long]$startTime.Ticks
        workingDirectory = [IO.Path]::GetFullPath($WorkingDirectory)
        arguments = @($Arguments)
        stdout = $stdout
        stderr = $stderr
    }
    Write-Manifest
    Write-Host "已创建 $Name 进程 (PID $($process.Id))" -ForegroundColor Green
}

function Get-ExecutablePath([string[]]$Names) {
    foreach ($name in $Names) {
        $command = Get-Command -Name $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command -and $command.Source) {
            return [IO.Path]::GetFullPath([string]$command.Source)
        }
    }
    return $null
}

function Ensure-BackendEnv {
    if (Test-Path -LiteralPath $BackendEnvPath) {
        return
    }
    if (-not (Test-Path -LiteralPath $EnvExamplePath)) {
        throw "缺少环境变量样例文件：$EnvExamplePath"
    }

    Copy-Item -LiteralPath $EnvExamplePath -Destination $BackendEnvPath
    Write-Host '已从 .env.example 创建 backend/.env；后续启动不会覆盖它。' -ForegroundColor Green
}

function Get-DotEnvValue([string]$Name, [string]$DefaultValue = '') {
    $processValue = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if ($null -ne $processValue) {
        return $processValue.Trim()
    }
    if (-not (Test-Path -LiteralPath $BackendEnvPath)) {
        return $DefaultValue
    }

    foreach ($line in Get-Content -LiteralPath $BackendEnvPath) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) {
            continue
        }
        $parts = $trimmed -split '=', 2
        if ($parts.Count -ne 2 -or $parts[0].Trim() -ne $Name) {
            continue
        }

        $value = $parts[1].Trim()
        if ($value.Length -ge 2) {
            $first = $value[0]
            $last = $value[$value.Length - 1]
            if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        return $value
    }
    return $DefaultValue
}

function Import-BackendEnvironment([string]$Path) {
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) {
            continue
        }
        $parts = $trimmed -split '=', 2
        if ($parts.Count -ne 2) {
            continue
        }

        $name = $parts[0].Trim()
        $value = $parts[1].Trim()
        if ($name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
            continue
        }
        if ($value.Length -ge 2) {
            $first = $value[0]
            $last = $value[$value.Length - 1]
            if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }

        # 已存在的进程环境变量优先，避免脚本覆盖用户显式传入的配置。
        if ($null -eq [Environment]::GetEnvironmentVariable($name, 'Process')) {
            [Environment]::SetEnvironmentVariable($name, $value, 'Process')
        }
    }
}

function Resolve-BackendPath([string]$Value, [string]$DefaultValue) {
    if ([string]::IsNullOrWhiteSpace($Value)) {
        $Value = $DefaultValue
    }
    $Value = [Environment]::ExpandEnvironmentVariables($Value.Trim().Trim('"').Trim("'"))
    if ([IO.Path]::IsPathRooted($Value)) {
        return [IO.Path]::GetFullPath($Value)
    }
    return [IO.Path]::GetFullPath((Join-Path $BackendDir $Value))
}

function Get-SqliteDatabasePath([string]$DatabaseUrl) {
    $DatabaseUrl = $DatabaseUrl.Trim()
    if ($DatabaseUrl -notmatch '^sqlite:///') {
        throw '本地一键启动脚本目前要求使用 SQLite（APP_DB_URL=sqlite:///...）。'
    }

    $path = $DatabaseUrl.Substring(10) -replace '[?#].*$', ''
    if ($path -eq ':memory:' -or [string]::IsNullOrWhiteSpace($path)) {
        throw '一键启动需要持久化 SQLite 文件，不能使用内存数据库。'
    }
    $path = [Uri]::UnescapeDataString($path).Replace('/', [IO.Path]::DirectorySeparatorChar)
    if ([IO.Path]::IsPathRooted($path)) {
        return [IO.Path]::GetFullPath($path)
    }
    return [IO.Path]::GetFullPath((Join-Path $BackendDir $path))
}

function Get-PythonVersion([string]$PythonExe) {
    $value = ((& $PythonExe -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>$null) | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $value -notmatch '^\d+\.\d+\.\d+$') {
        throw "无法读取 Python 版本：$PythonExe"
    }
    return $value
}

function Assert-PythonVersion([string]$PythonExe) {
    $version = Get-PythonVersion -PythonExe $PythonExe
    $parts = $version -split '\.'
    $major = [int]$parts[0]
    $minor = [int]$parts[1]
    if (-not (($major -gt 3) -or (($major -eq 3) -and ($minor -ge 11)))) {
        throw "当前 Python 版本为 $version；本项目需要 Python 3.11 或更高版本。"
    }
    return $version
}

function Get-NodeVersion([string]$NodeExe) {
    $value = ((& $NodeExe --version 2>$null) | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $value -notmatch '^v?\d+\.\d+\.\d+') {
        throw "无法读取 Node.js 版本：$NodeExe"
    }
    return $value.TrimStart('v')
}

function Assert-NodeVersion([string]$NodeExe) {
    $version = Get-NodeVersion -NodeExe $NodeExe
    $parts = $version -split '\.'
    $major = [int]$parts[0]
    $minor = [int]$parts[1]
    $supported = (($major -eq 20) -and ($minor -ge 19)) -or (($major -eq 22) -and ($minor -ge 12)) -or ($major -gt 22)
    if (-not $supported) {
        throw "当前 Node.js 版本为 $version；Vite 7 需要 Node.js 20.19+ 或 22.12+。"
    }
    return $version
}

function Get-DependencyFingerprint([string]$PythonExe, [string]$NodeExe) {
    $inputPaths = @(
        (Join-Path $BackendDir 'pyproject.toml'),
        (Join-Path $FrontendDir 'package.json'),
        (Join-Path $FrontendDir 'package-lock.json')
    )
    $components = foreach ($inputPath in $inputPaths) {
        if (-not (Test-Path -LiteralPath $inputPath)) {
            throw "缺少依赖清单：$inputPath"
        }
        (Get-FileHash -LiteralPath $inputPath -Algorithm SHA256).Hash
    }

    $pythonVersion = Get-PythonVersion -PythonExe $PythonExe
    $nodeVersion = Get-NodeVersion -NodeExe $NodeExe
    $payload = (@($components) + @($pythonVersion, $nodeVersion)) -join "`n"
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return [System.BitConverter]::ToString(
            $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($payload))
        ).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Test-DependencyMarker([string]$Fingerprint) {
    if ([string]::IsNullOrWhiteSpace($Fingerprint) -or -not (Test-Path -LiteralPath $DependencyMarkerPath)) {
        return $false
    }
    try {
        $marker = Get-Content -LiteralPath $DependencyMarkerPath -Raw | ConvertFrom-Json
        return ([string]$marker.fingerprint -eq $Fingerprint)
    }
    catch {
        return $false
    }
}

function Write-DependencyMarker([string]$Fingerprint, [string]$PythonExe, [string]$NodeExe) {
    if (-not (Test-Path -LiteralPath $RuntimeDir)) {
        New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
    }
    $marker = [pscustomobject]@{
        fingerprint = $Fingerprint
        pythonVersion = Get-PythonVersion -PythonExe $PythonExe
        nodeVersion = Get-NodeVersion -NodeExe $NodeExe
        checkedAt = (Get-Date).ToUniversalTime().ToString('o')
    }
    $json = ConvertTo-Json -InputObject $marker -Depth 4
    [IO.File]::WriteAllText(
        $DependencyMarkerPath,
        $json,
        ([System.Text.UTF8Encoding]::new($false))
    )
}

function Test-BackendDependencies([string]$PythonExe) {
    if (-not (Test-Path -LiteralPath $PythonExe)) {
        return $false
    }

    Push-Location -LiteralPath $BackendDir
    try {
        & $PythonExe -m pip check 2>$null | Out-Null
        $pipExit = $LASTEXITCODE
        if ($pipExit -ne 0) {
            return $false
        }

        $importCode = 'import app.cli, app.config, app.health, app.main, app.worker_main, argon2, alembic, cryptography, fastapi, httpx, multipart, numpy, PIL, pydantic, pydantic_settings, sqlalchemy, uvicorn'
        & $PythonExe -c $importCode 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    }
    finally {
        Pop-Location
    }
}

function Test-FrontendDependencies([string]$NodeExe, [string]$NpmExe, [string]$ViteCli) {
    $requiredPackages = @(
        'vite/bin/vite.js',
        'react/package.json',
        'react-dom/package.json',
        'react-router-dom/package.json',
        '@tanstack/react-query/package.json',
        'typescript/package.json',
        'vitest/package.json'
    )
    foreach ($package in $requiredPackages) {
        if (-not (Test-Path -LiteralPath (Join-Path $FrontendDir (Join-Path 'node_modules' $package)))) {
            return $false
        }
    }
    if (-not (Test-Path -LiteralPath $NpmExe) -or -not (Test-Path -LiteralPath $ViteCli)) {
        return $false
    }

    $npmExit = 1
    Push-Location -LiteralPath $FrontendDir
    try {
        & $NpmExe ls --depth=0 2>$null | Out-Null
        $npmExit = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    if ($npmExit -ne 0) {
        return $false
    }

    & $NodeExe $ViteCli --version 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function New-BackendVirtualEnvironment([string]$PythonLauncher) {
    if (Test-Path -LiteralPath $VenvPython) {
        return
    }
    if ($SkipInstall) {
        throw "缺少 Python 虚拟环境：$VenvDir；-SkipInstall 不会创建或安装依赖。"
    }

    Write-Host '正在创建 backend/.venv …' -ForegroundColor Yellow
    if ([IO.Path]::GetFileNameWithoutExtension($PythonLauncher).ToLowerInvariant() -eq 'py') {
        Invoke-Checked -Executable $PythonLauncher -Arguments @('-3', '-m', 'venv', $VenvDir) -WorkingDirectory $BackendDir
    }
    else {
        Invoke-Checked -Executable $PythonLauncher -Arguments @('-m', 'venv', $VenvDir) -WorkingDirectory $BackendDir
    }
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        throw "Python 虚拟环境创建失败：$VenvPython"
    }
}

function Ensure-Dependencies([string]$PythonLauncher, [string]$NodeExe, [string]$NpmExe) {
    New-BackendVirtualEnvironment -PythonLauncher $PythonLauncher
    $pythonVersion = Assert-PythonVersion -PythonExe $VenvPython
    Write-Host "使用 Python $pythonVersion。" -ForegroundColor DarkGray

    $fingerprint = Get-DependencyFingerprint -PythonExe $VenvPython -NodeExe $NodeExe
    $markerValid = Test-DependencyMarker -Fingerprint $fingerprint
    $backendReady = Test-BackendDependencies -PythonExe $VenvPython
    $viteCli = Join-Path $FrontendDir 'node_modules\vite\bin\vite.js'
    $frontendReady = Test-FrontendDependencies -NodeExe $NodeExe -NpmExe $NpmExe -ViteCli $viteCli

    if ($SkipInstall) {
        if (-not $markerValid) {
            throw '依赖标记不存在或已失效；-SkipInstall 不会安装依赖，请先不带 -SkipInstall 运行一次。'
        }
        if (-not $backendReady -or -not $frontendReady) {
            throw '依赖实际检查未通过；-SkipInstall 不会修复依赖，请先不带 -SkipInstall 运行一次。'
        }
        Write-Host '依赖标记有效，Python 与前端依赖检查通过；跳过安装。' -ForegroundColor Green
        return
    }

    $markerChanged = -not $markerValid
    if (-not $backendReady -or $markerChanged) {
        Write-Title '安装后端依赖'
        Invoke-Checked -Executable $VenvPython -Arguments @('-m', 'pip', 'install', '-e', '.[dev]') -WorkingDirectory $BackendDir
    }
    if (-not $frontendReady -or $markerChanged) {
        Write-Title '安装前端依赖'
        Invoke-Checked -Executable $NpmExe -Arguments @('install', '--no-audit', '--no-fund') -WorkingDirectory $FrontendDir
    }

    $backendReady = Test-BackendDependencies -PythonExe $VenvPython
    $frontendReady = Test-FrontendDependencies -NodeExe $NodeExe -NpmExe $NpmExe -ViteCli $viteCli
    if (-not $backendReady -or -not $frontendReady) {
        throw '依赖安装完成，但实际依赖检查仍未通过；请查看上方输出。'
    }

    # npm install 可能更新 package-lock.json，必须在安装并复检后重新计算指纹。
    $fingerprint = Get-DependencyFingerprint -PythonExe $VenvPython -NodeExe $NodeExe
    Write-DependencyMarker -Fingerprint $fingerprint -PythonExe $VenvPython -NodeExe $NodeExe
    Write-Host 'Python 与前端依赖已准备完成。' -ForegroundColor Green
}

function Get-AdminState([string]$PythonExe, [string]$DatabasePath) {
    if (-not (Test-Path -LiteralPath $DatabasePath)) {
        return $false
    }

    $checkCode = 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); names={r[0] for r in c.execute("select name from sqlite_master where type=''table''")}; print("yes" if "api_auth_users" in names and c.execute("select 1 from api_auth_users limit 1").fetchone() else "no"); c.close()'
    $result = ((& $PythonExe -c $checkCode $DatabasePath 2>&1) | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $result -notin @('yes', 'no')) {
        throw "无法检查本地管理员账户状态：$result"
    }
    return ($result -eq 'yes')
}

function Initialize-LocalData([string]$PythonExe) {
    Write-Title '初始化本地数据'
    Invoke-Checked -Executable $PythonExe -Arguments @('-m', 'app.cli', 'init') -WorkingDirectory $BackendDir
    Invoke-Checked -Executable $PythonExe -Arguments @('-m', 'app.cli', 'migrate') -WorkingDirectory $BackendDir

    $databaseUrl = Get-DotEnvValue -Name 'APP_DB_URL' -DefaultValue 'sqlite:///./data/app.sqlite3'
    $databasePath = Get-SqliteDatabasePath -DatabaseUrl $databaseUrl
    $hasAdmin = Get-AdminState -PythonExe $PythonExe -DatabasePath $databasePath

    $bootstrapFileValue = Get-DotEnvValue -Name 'APP_BOOTSTRAP_PASSWORD_FILE' -DefaultValue './data/bootstrap_password'
    $bootstrapFile = Resolve-BackendPath -Value $bootstrapFileValue -DefaultValue './data/bootstrap_password'
    $configuredPassword = Get-DotEnvValue -Name 'APP_BOOTSTRAP_PASSWORD'
    $configuredPassword = $configuredPassword.Trim()
    $bootstrapPassword = ''
    $generatedBootstrapPassword = $false
    if (Test-Path -LiteralPath $bootstrapFile) {
        $bootstrapPassword = (Get-Content -LiteralPath $bootstrapFile -Raw).Trim()
    }

    if (-not $hasAdmin -and [string]::IsNullOrWhiteSpace($configuredPassword) -and [string]::IsNullOrWhiteSpace($bootstrapPassword)) {
        if (Test-Path -LiteralPath $bootstrapFile) {
            throw "初始管理员密码文件为空：$bootstrapFile；请修复该文件后重试，脚本不会覆盖已有文件。"
        }
        Invoke-Checked -Executable $PythonExe -Arguments @('-m', 'app.cli', 'init', '--with-password') -WorkingDirectory $BackendDir
        if (-not (Test-Path -LiteralPath $bootstrapFile)) {
            throw "未能生成初始管理员密码文件：$bootstrapFile"
        }
        $bootstrapPassword = (Get-Content -LiteralPath $bootstrapFile -Raw).Trim()
        $generatedBootstrapPassword = $true
    }

    if (-not $hasAdmin) {
        if (-not [string]::IsNullOrWhiteSpace($configuredPassword)) {
            if ($configuredPassword.Length -lt 12) {
                throw 'APP_BOOTSTRAP_PASSWORD 至少需要 12 个字符。'
            }
            $username = Get-DotEnvValue -Name 'APP_BOOTSTRAP_USERNAME' -DefaultValue 'admin'
            Write-Host "将使用 backend/.env 中配置的初始管理员密码，用户名：$username" -ForegroundColor Yellow
        }
        elseif (-not [string]::IsNullOrWhiteSpace($bootstrapPassword)) {
            if ($bootstrapPassword.Length -lt 12) {
                throw '初始管理员密码至少需要 12 个字符。'
            }
            $username = Get-DotEnvValue -Name 'APP_BOOTSTRAP_USERNAME' -DefaultValue 'admin'
            if ($generatedBootstrapPassword) {
                Write-Host "初始管理员用户名：$username`n初始管理员密码：$bootstrapPassword" -ForegroundColor Yellow
                Write-Host '请登录后修改并妥善保存密码；不要分享终端输出。' -ForegroundColor Yellow
            }
            else {
                Write-Host "管理员尚未初始化；使用已配置的密码文件（$bootstrapFile），不会在终端显示其内容。用户名：$username" -ForegroundColor Yellow
            }
        }
        else {
            throw '管理员尚未初始化，且没有可用的初始密码。'
        }
    }
}

function Get-PortListener([int]$Port) {
    if (Get-Command -Name Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        return Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    }
    return $null
}

function Assert-PortAvailable([int]$Port) {
    $listener = Get-PortListener -Port $Port
    if ($listener) {
        $owner = "PID $($listener.OwningProcess)"
        try {
            $ownerProcess = Get-Process -Id ([int]$listener.OwningProcess) -ErrorAction Stop
            $owner = "$owner ( $($ownerProcess.ProcessName) )"
        }
        catch {
        }
        throw "端口 $Port 已被其他进程占用（$owner）。脚本不会关闭其他进程，请先处理端口占用后重试。"
    }
}

function Get-ProcessLogTail([string]$Name) {
    $entry = @($script:Services | Where-Object { $_.name -eq $Name }) | Select-Object -Last 1
    if ($null -eq $entry) {
        return ''
    }
    $lines = @()
    foreach ($logPath in @($entry.stdout, $entry.stderr)) {
        if (Test-Path -LiteralPath $logPath) {
            try {
                $lines += Get-Content -LiteralPath $logPath -Tail 30
            }
            catch {
            }
        }
    }
    return ($lines -join "`n")
}

function Wait-HttpReady([string]$Uri, [int]$TimeoutSeconds, [string]$Name) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400) {
                Write-Host "$Name 已就绪：$Uri" -ForegroundColor Green
                return
            }
        }
        catch {
        }

        $entry = @($script:Services | Where-Object { $_.name -eq $Name.ToLowerInvariant() -or $_.name -eq $Name }) | Select-Object -Last 1
        if ($entry) {
            $process = Get-Process -Id ([int]$entry.pid) -ErrorAction SilentlyContinue
            if (-not $process -or $process.HasExited) {
                $tail = Get-ProcessLogTail -Name $entry.name
                throw "$Name 进程已退出，未能访问 $Uri。`n$tail"
            }
        }
        Start-Sleep -Seconds 1
    }
    throw "$Name 在 $TimeoutSeconds 秒内未就绪：$Uri"
}

try {
    if ($Stop) {
        Stop-FromManifest
        exit 0
    }

    if (-not (Test-Path -LiteralPath $BackendDir) -or -not (Test-Path -LiteralPath $FrontendDir)) {
        throw '未找到 backend 或 frontend 目录；请从仓库根目录运行此脚本。'
    }

    # 只停止上一轮由本脚本记录且身份完全匹配的进程；不会按端口杀进程。
    Stop-FromManifest -Silent
    Ensure-BackendEnv
    Import-BackendEnvironment -Path $BackendEnvPath

    $pythonLauncher = Get-ExecutablePath -Names @('py.exe', 'py', 'python.exe', 'python')
    $nodeExe = Get-ExecutablePath -Names @('node.exe', 'node')
    $npmExe = Get-ExecutablePath -Names @('npm.cmd', 'npm')
    if (-not $pythonLauncher) {
        throw '未找到 Python。请安装 Python 3.11+ 并确保 py.exe 或 python.exe 在 PATH 中。'
    }
    if (-not $nodeExe -or -not $npmExe) {
        throw '未找到 Node.js 或 npm。请安装 Node.js 20.19+ / 22.12+ 并确保 node 与 npm 在 PATH 中。'
    }
    $nodeVersion = Assert-NodeVersion -NodeExe $nodeExe
    Write-Host "使用 Node.js $nodeVersion。" -ForegroundColor DarkGray

    Ensure-Dependencies -PythonLauncher $pythonLauncher -NodeExe $nodeExe -NpmExe $npmExe
    Initialize-LocalData -PythonExe $VenvPython

    Assert-PortAvailable -Port 8000
    Assert-PortAvailable -Port 5173

    Write-Title '启动 API 与后台服务'
    Start-ManagedProcess -Name 'api' -Executable $VenvPython -Arguments @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000') -WorkingDirectory $BackendDir
    Wait-HttpReady -Uri 'http://127.0.0.1:8000/health/ready' -TimeoutSeconds 45 -Name 'api'

    Start-ManagedProcess -Name 'worker' -Executable $VenvPython -Arguments @('-m', 'app.cli', 'worker') -WorkingDirectory $BackendDir
    Start-ManagedProcess -Name 'scheduler' -Executable $VenvPython -Arguments @('-m', 'app.cli', 'scheduler') -WorkingDirectory $BackendDir
    Start-ManagedProcess -Name 'capture' -Executable $VenvPython -Arguments @('-m', 'app.cli', 'capture') -WorkingDirectory $BackendDir

    $viteCli = Join-Path $FrontendDir 'node_modules\vite\bin\vite.js'
    Start-ManagedProcess -Name 'frontend' -Executable $nodeExe -Arguments @($viteCli, '--host', '127.0.0.1', '--port', '5173', '--strictPort') -WorkingDirectory $FrontendDir
    Wait-HttpReady -Uri 'http://127.0.0.1:5173' -TimeoutSeconds 30 -Name 'frontend'

    Write-Title '本地开发环境已就绪'
    Write-Host '前端：  http://localhost:5173' -ForegroundColor Green
    Write-Host 'API：   http://localhost:8000/docs' -ForegroundColor Green
    Write-Host '日志：  ' -NoNewline -ForegroundColor DarkGray
    Write-Host $LogDir -ForegroundColor DarkGray
    Write-Host '停止：  .\start-local.ps1 -Stop' -ForegroundColor Cyan
    Write-Host 'Worker / Scheduler / Capture：仅确认进程已创建，未进行服务级健康探测。' -ForegroundColor Yellow
    if (-not $NoBrowser) {
        Start-Process 'http://localhost:5173'
    }
}
catch {
    Write-Host "`n启动失败：$($_.Exception.Message)" -ForegroundColor Red
    if ($script:Services.Count -gt 0) {
        Write-Host '正在停止本次已启动且身份匹配的服务……' -ForegroundColor Yellow
        Stop-ManagedEntries -Entries $script:Services
        $script:Services = @()
        Write-Manifest
    }
    exit 1
}