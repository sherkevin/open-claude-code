#Requires -RunAsAdministrator

param(
    [Parameter(Mandatory = $true)]
    [string]$PackageRoot,
    [string]$EvidencePath = ""
)

$ErrorActionPreference = "Stop"
$ServiceName = "KaiwuConnector"
$BaseURL = "http://127.0.0.1:17872"
$InstallDir = Join-Path $env:ProgramFiles "Kaiwu\Connector"
$DataDir = Join-Path $env:ProgramData "Kaiwu\Connector"
$LogPath = Join-Path $env:TEMP "kaiwu-connector-windows-smoke.log"

if ([string]::IsNullOrWhiteSpace($EvidencePath)) {
    $EvidencePath = Join-Path $PackageRoot "windows-smoke-evidence.json"
}

$Evidence = [ordered]@{
    started_at = (Get-Date).ToUniversalTime().ToString("o")
    finished_at = $null
    status = "running"
    steps = @()
    os = [ordered]@{}
    service = [ordered]@{}
    files = @()
    error = $null
}

function Add-Step {
    param(
        [string]$Name,
        [string]$Status,
        [string]$Detail = ""
    )
    $script:Evidence.steps += [ordered]@{
        name = $Name
        status = $Status
        detail = $Detail
        at = (Get-Date).ToUniversalTime().ToString("o")
    }
    Write-Host "[$Status] $Name $Detail"
}

function Wait-ServiceState {
    param(
        [ValidateSet("Running", "Stopped")]
        [string]$Expected,
        [int]$TimeoutSeconds = 45
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
        if ($service -and $service.Status.ToString() -eq $Expected) {
            return $service
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw "服务未在 $TimeoutSeconds 秒内进入 $Expected 状态"
}

function Save-Evidence {
    $Evidence.finished_at = (Get-Date).ToUniversalTime().ToString("o")
    $Evidence | ConvertTo-Json -Depth 10 | Set-Content -Path $EvidencePath -Encoding UTF8
}

function Collect-FailureContext {
    try {
        $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
        if ($service) {
            $Evidence.service.state = $service.Status.ToString()
        }
    } catch {}
    if (Test-Path $LogPath) {
        $Evidence.service.log_tail = @(Get-Content $LogPath -Tail 200)
    }
    try {
        $Evidence.service.sc_query = @(& sc.exe query $ServiceName 2>&1)
    } catch {}
    try {
        $Evidence.service.sc_qc = @(& sc.exe qc $ServiceName 2>&1)
    } catch {}
}

try {
    $os = Get-CimInstance Win32_OperatingSystem
    $Evidence.os = [ordered]@{
        caption = $os.Caption
        version = $os.Version
        build = $os.BuildNumber
        architecture = $os.OSArchitecture
    }
    Add-Step -Name "collect_os" -Status "passed" -Detail "$($os.Caption) $($os.Version)"

    $required = @(
        "install.ps1",
        "uninstall.ps1",
        "bin\kaiwu-connector.exe",
        "bin\frpc.exe"
    )
    foreach ($relative in $required) {
        $path = Join-Path $PackageRoot $relative
        if (-not (Test-Path $path)) {
            throw "交付包缺少文件: $relative"
        }
        $item = Get-Item $path
        $Evidence.files += [ordered]@{
            path = $relative
            size = $item.Length
            sha256 = (Get-FileHash $path -Algorithm SHA256).Hash
        }
    }
    Add-Step -Name "verify_package_files" -Status "passed" -Detail "$($required.Count) files"

    $versionOutput = (& (Join-Path $PackageRoot "bin\kaiwu-connector.exe") version 2>&1) -join " "
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($versionOutput)) {
        throw "kaiwu-connector.exe version 执行失败: $versionOutput"
    }
    Add-Step -Name "binary_version" -Status "passed" -Detail $versionOutput

    $uninstaller = Join-Path $PackageRoot "uninstall.ps1"
    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        & $uninstaller -Purge
    }

    & (Join-Path $PackageRoot "install.ps1")
    $service = Wait-ServiceState -Expected "Running"
    Add-Step -Name "install_and_start_service" -Status "passed" -Detail $service.Status.ToString()

    $deadline = (Get-Date).AddSeconds(30)
    $page = $null
    do {
        try {
            $page = Invoke-WebRequest -UseBasicParsing -Uri "$BaseURL/" -TimeoutSec 5
            if ($page.StatusCode -eq 200) {
                break
            }
        } catch {
            Start-Sleep -Milliseconds 500
        }
    } while ((Get-Date) -lt $deadline)
    if (-not $page -or $page.StatusCode -ne 200) {
        throw "本机配置页面未就绪"
    }
    if ($page.Content -notmatch "开物") {
        throw "本机配置页面内容不符合预期"
    }
    $tokenMatch = [regex]::Match($page.Content, 'const TOKEN = "([0-9a-f]{48})";')
    if (-not $tokenMatch.Success) {
        throw "无法从页面中取得本机会话令牌"
    }
    $Token = $tokenMatch.Groups[1].Value
    Add-Step -Name "local_ui" -Status "passed" -Detail $BaseURL

    $status = Invoke-RestMethod -Uri "$BaseURL/api/status" -Method Get -TimeoutSec 10
    if (-not $status.ok -or -not $status.data) {
        throw "本机状态接口返回无效响应"
    }
    if ($status.data.state -ne "UNPAIRED") {
        throw "全新安装状态应为 UNPAIRED，实际为 $($status.data.state)"
    }
    Add-Step -Name "status_api" -Status "passed" -Detail $status.data.state

    $missingTokenStatus = 0
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "$BaseURL/api/import" -Method Post `
            -ContentType "application/json" -Body '{"config":"{}"}' -TimeoutSec 10 | Out-Null
    } catch {
        $missingTokenStatus = [int]$_.Exception.Response.StatusCode
    }
    if ($missingTokenStatus -ne 403) {
        throw "无本机会话令牌的写请求应返回 403，实际为 $missingTokenStatus"
    }
    Add-Step -Name "csrf_token_guard" -Status "passed" -Detail "missing-token=403"

    $mcpConfig = '{"mcpServers":{"ci-local":{"type":"http","url":"http://127.0.0.1:18765/mcp","headers":{"Authorization":"Bearer windows-ci-secret"}}}}'
    $importBody = @{ config = $mcpConfig } | ConvertTo-Json -Compress
    $importResult = Invoke-RestMethod -Uri "$BaseURL/api/import" -Method Post `
        -Headers @{ "X-Connector-Token" = $Token } -ContentType "application/json" `
        -Body $importBody -TimeoutSec 15
    if (-not $importResult.ok -or $importResult.data.HasErrors) {
        throw "测试来源导入失败"
    }
    foreach ($name in @("config.json", "secrets.json")) {
        $path = Join-Path $DataDir $name
        if (-not (Test-Path $path)) {
            throw "导入后缺少本机文件: $name"
        }
    }
    Add-Step -Name "import_and_local_storage" -Status "passed" -Detail $DataDir

    foreach ($name in @("secrets.json", "auth_profiles.json", "identity.json", "device_key.json")) {
        $path = Join-Path $DataDir $name
        if (-not (Test-Path $path)) { continue }
        $text = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
        if (-not $text.StartsWith("KAIWU-DPAPI-V1`n")) {
            throw "本机秘密文件未使用 Windows DPAPI: $name"
        }
        if ($text.Contains("windows-ci-secret")) {
            throw "本机秘密文件中出现明文客户凭据: $name"
        }
    }
    Add-Step -Name "dpapi_secret_files" -Status "passed" -Detail "prefix marker present; no test bearer plaintext"

    Restart-Service -Name $ServiceName -Force
    $service = Wait-ServiceState -Expected "Running"
    Start-Sleep -Seconds 1
    $statusAfterRestart = Invoke-RestMethod -Uri "$BaseURL/api/status" -Method Get -TimeoutSec 10
    if (-not $statusAfterRestart.ok -or $statusAfterRestart.data.state -ne "CONFIGURING") {
        throw "重启后未恢复已绑定/待配置状态，实际为 $($statusAfterRestart.data.state)"
    }
    Add-Step -Name "service_restart_recovery" -Status "passed" -Detail $statusAfterRestart.data.state

    $savedSecrets = Join-Path $DataDir "secrets.json"
    $configPath = Join-Path $DataDir "config.json"
    $configAfterRestart = [System.IO.File]::ReadAllText($configPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
    $sourceID = $configAfterRestart.sources[0].source_id
    if ([string]::IsNullOrWhiteSpace($sourceID)) {
        throw "迁移测试无法取得来源编号"
    }
    Stop-Service -Name $ServiceName -Force
    Wait-ServiceState -Expected "Stopped" | Out-Null
    $legacySecrets = @{}
    $legacySecrets[$sourceID] = @{ Authorization = "Bearer windows-ci-secret" }
    [System.IO.File]::WriteAllText(
        $savedSecrets,
        ($legacySecrets | ConvertTo-Json -Depth 5 -Compress),
        [System.Text.UTF8Encoding]::new($false)
    )
    Start-Service -Name $ServiceName
    Wait-ServiceState -Expected "Running" | Out-Null
    Start-Sleep -Seconds 1
    $migrated = [System.IO.File]::ReadAllText($savedSecrets, [System.Text.Encoding]::UTF8)
    if (-not $migrated.StartsWith("KAIWU-DPAPI-V1`n")) {
        throw "旧明文 secrets.json 未在服务启动后迁移为 DPAPI"
    }
    if ($migrated.Contains("windows-ci-secret")) {
        throw "迁移后的 secrets.json 仍包含明文"
    }
    Add-Step -Name "legacy_plaintext_migration" -Status "passed" -Detail $sourceID

    & (Join-Path $PackageRoot "install.ps1")
    $service = Wait-ServiceState -Expected "Running"
    $upgradeStatus = Invoke-RestMethod -Uri "$BaseURL/api/status" -Method Get -TimeoutSec 10
    if (-not $upgradeStatus.ok -or $upgradeStatus.data.state -ne "CONFIGURING") {
        throw "升级重装后本机配置未恢复"
    }
    Add-Step -Name "upgrade_reinstall" -Status "passed" -Detail "configuration retained"

    $Evidence.service = [ordered]@{
        name = $ServiceName
        state = $service.Status.ToString()
        start_type = (Get-Service -Name $ServiceName).StartType.ToString()
        sc_qc = @(& sc.exe qc $ServiceName 2>&1)
    }

    & $uninstaller -Purge
    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        throw "卸载后服务仍然存在"
    }
    if (Test-Path $InstallDir) {
        throw "卸载后程序目录仍然存在"
    }
    if (Test-Path $DataDir) {
        throw "彻底卸载后数据目录仍然存在"
    }
    Add-Step -Name "uninstall" -Status "passed" -Detail "service and directories removed"

    $Evidence.status = "passed"
    Save-Evidence
    exit 0
} catch {
    $Evidence.status = "failed"
    $Evidence.error = $_.Exception.Message
    Add-Step -Name "failure" -Status "failed" -Detail $_.Exception.Message
    Collect-FailureContext
    Save-Evidence
    Write-Error $_
    exit 1
}
