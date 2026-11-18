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

$PackageRoot = (Resolve-Path -LiteralPath $PackageRoot).Path
$Installer = Join-Path $PackageRoot "install.ps1"
$Uninstaller = Join-Path $PackageRoot "uninstall.ps1"
if ([string]::IsNullOrWhiteSpace($EvidencePath)) {
    $EvidencePath = Join-Path $PackageRoot "windows-smoke-evidence.json"
}

$Evidence = [ordered]@{
    started_at = (Get-Date).ToUniversalTime().ToString("o")
    finished_at = $null
    status = "running"
    package_root = $PackageRoot
    uninstall_mode = "default preserves data; -Purge is not run by this smoke test"
    steps = @()
    os = [ordered]@{}
    before = [ordered]@{}
    after_install = [ordered]@{}
    backups = @()
    files = @()
    service = [ordered]@{}
    data = [ordered]@{}
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

function Save-Evidence {
    $Evidence.finished_at = (Get-Date).ToUniversalTime().ToString("o")
    $parent = Split-Path -Parent $EvidencePath
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    $Evidence | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $EvidencePath -Encoding UTF8
}

function Get-ServiceSnapshot {
    $service = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction SilentlyContinue
    if (-not $service) { return $null }
    return [ordered]@{
        name = $service.Name
        display_name = $service.DisplayName
        state = $service.State
        start_mode = $service.StartMode
        delayed_auto_start = [bool]$service.DelayedAutoStart
        process_id = $service.ProcessId
        binary_path = $service.PathName
    }
}

function Wait-ServiceState {
    param(
        [ValidateSet("Running", "Stopped", "Absent")]
        [string]$Expected,
        [int]$TimeoutSeconds = 45
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
        if ($Expected -eq "Absent" -and $null -eq $service) { return $null }
        if ($null -ne $service -and $service.Status.ToString() -eq $Expected) { return $service }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw "服务未在 $TimeoutSeconds 秒内进入 $Expected 状态"
}

function Get-DataSnapshot {
    if (-not (Test-Path -LiteralPath $DataDir)) {
        return [ordered]@{
            exists = $false
            files = @()
            protected_files = @()
        }
    }
    $files = @(Get-ChildItem -LiteralPath $DataDir -Recurse -File -ErrorAction SilentlyContinue)
    $protected = @()
    foreach ($file in $files) {
        if ($file.Name -in @("secrets.json", "auth_profiles.json", "identity.json", "device_key.json")) {
            try {
                $text = [System.IO.File]::ReadAllText($file.FullName, [System.Text.Encoding]::UTF8)
                if ($text -match '^KAIWU-DPAPI-V1\r?\n') { $protected += $file.FullName }
            } catch {}
        }
    }
    return [ordered]@{
        exists = $true
        files = @($files | ForEach-Object { [ordered]@{ path = $_.FullName; size = $_.Length } })
        protected_files = $protected
    }
}

function Get-BackupSnapshot {
    $backupRoot = Join-Path $DataDir "backups"
    if (-not (Test-Path -LiteralPath $backupRoot)) { return @() }
    return @(
        Get-ChildItem -LiteralPath $backupRoot -Directory -Filter "*-pre-upgrade" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime |
        ForEach-Object {
            [ordered]@{
                path = $_.FullName
                created_at = $_.LastWriteTimeUtc.ToString("o")
                entries = @(Get-ChildItem -LiteralPath $_.FullName -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
            }
        }
    )
}

function Assert-PrivateAcl {
    param([Parameter(Mandatory = $true)][string]$Path)
    $items = @(Get-Item -LiteralPath $Path -Force) + @(Get-ChildItem -LiteralPath $Path -Force -Recurse -ErrorAction Stop)
    foreach ($item in $items) {
        $acl = Get-Acl -LiteralPath $item.FullName
        if (-not $acl.AreAccessRulesProtected) {
            throw "ACL 仍会继承上级权限: $($item.FullName)"
        }
        foreach ($rule in @($acl.Access)) {
            $sid = $rule.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
            if ($sid -notin @("S-1-5-18", "S-1-5-32-544")) {
                throw "ACL 含有未授权主体 ${sid}: $($item.FullName)"
            }
            if ($rule.AccessControlType -ne "Allow") {
                throw "ACL 含有拒绝规则 ${sid}: $($item.FullName)"
            }
        }
    }
}

function Assert-PathPresent {
    param([string]$Path, [string]$Label)
    if (-not (Test-Path -LiteralPath $Path)) { throw "$Label 不存在: $Path" }
}

function Assert-PathAbsent {
    param([string]$Path, [string]$Label)
    if (Test-Path -LiteralPath $Path) { throw "$Label 仍然存在: $Path" }
}

function Invoke-PackageInstaller {
    if (-not (Test-Path -LiteralPath $Installer)) { throw "找不到安装脚本: $Installer" }
    & $Installer
}

function Invoke-PackageUninstaller {
    param([switch]$KeepProgramDirectory)
    if (-not (Test-Path -LiteralPath $Uninstaller)) { throw "找不到卸载脚本: $Uninstaller" }
    if ($KeepProgramDirectory) {
        & $Uninstaller -KeepProgramDirectory
    } else {
        & $Uninstaller
    }
}

function Get-UiToken {
    $deadline = (Get-Date).AddSeconds(30)
    $page = $null
    do {
        try {
            $page = Invoke-WebRequest -UseBasicParsing -Uri "$BaseURL/" -TimeoutSec 5
            if ($page.StatusCode -eq 200) { break }
        } catch {
            Start-Sleep -Milliseconds 500
        }
    } while ((Get-Date) -lt $deadline)
    if (-not $page -or $page.StatusCode -ne 200) { throw "本机配置页面未就绪" }
    if ($page.Content -notmatch "开物") { throw "本机配置页面内容不符合预期" }
    $tokenMatch = [regex]::Match($page.Content, 'const TOKEN = "([0-9a-f]{48})";')
    if (-not $tokenMatch.Success) { throw "无法从页面中取得本机会话令牌" }
    return $tokenMatch.Groups[1].Value
}

function Get-ConnectorStatus {
    $status = Invoke-RestMethod -Uri "$BaseURL/api/status" -Method Get -TimeoutSec 10
    if (-not $status.ok -or -not $status.data) { throw "本机状态接口返回无效响应" }
    return $status
}

function Assert-ConnectorState {
    param([string]$Expected, [string]$Context)
    $status = Get-ConnectorStatus
    if ($status.data.state -ne $Expected) {
        throw "$Context 后状态应为 $Expected，实际为 $($status.data.state)"
    }
    return $status
}

function Assert-SecretsProtected {
    foreach ($name in @("secrets.json", "auth_profiles.json", "identity.json", "device_key.json")) {
        $path = Join-Path $DataDir $name
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $text = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
        if ($text -notmatch '^KAIWU-DPAPI-V1\r?\n') {
            throw "本机秘密文件未使用 Windows DPAPI: $name"
        }
        if ($text.Contains("windows-ci-secret")) {
            throw "本机秘密文件中出现明文测试凭据: $name"
        }
    }
}

try {
    $os = Get-CimInstance Win32_OperatingSystem
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $Evidence.os = [ordered]@{
        caption = $os.Caption
        version = $os.Version
        build = $os.BuildNumber
        architecture = $os.OSArchitecture
        user = $identity.Name
        is_system = $identity.IsSystem
    }
    Add-Step -Name "collect_os" -Status "passed" -Detail "$($os.Caption) $($os.Version)"

    $required = @(
        "install.ps1",
        "uninstall.ps1",
        "windows-smoke.ps1",
        "SHA256SUMS",
        "bin\kaiwu-connector.exe",
        "bin\frpc.exe",
        "docs\交付与运维说明.md"
    )
    foreach ($relative in $required) {
        $path = Join-Path $PackageRoot $relative
        if (-not (Test-Path -LiteralPath $path)) { throw "交付包缺少文件: $relative" }
        $item = Get-Item -LiteralPath $path
        $Evidence.files += [ordered]@{
            path = $relative
            size = $item.Length
            sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
        }
    }
    Add-Step -Name "verify_package_files" -Status "passed" -Detail "$($required.Count) files"

    $versionOutput = (& (Join-Path $PackageRoot "bin\kaiwu-connector.exe") version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($versionOutput)) {
        throw "kaiwu-connector.exe version 执行失败: $versionOutput"
    }
    if ($versionOutput -notmatch '1\.3\.1') { throw "候选包版本不是 1.3.1: $versionOutput" }
    Add-Step -Name "binary_version" -Status "passed" -Detail $versionOutput

    $frpcPath = Join-Path $PackageRoot "bin\frpc.exe"
    $frpcVersion = (& $frpcPath -v 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $frpcVersion -notmatch '0\.61\.1') {
        throw "官方 frpc 版本检查失败: $frpcVersion"
    }
    Add-Step -Name "frpc_binary_version" -Status "passed" -Detail $frpcVersion

    $beforeService = Get-ServiceSnapshot
    $beforeData = Get-DataSnapshot
    $Evidence.before = [ordered]@{
        service = $beforeService
        data = $beforeData
    }
    Add-Step -Name "before_snapshot" -Status "passed" -Detail "service=$([bool]$beforeService); data_files=$($beforeData.files.Count)"

    Invoke-PackageInstaller
    $service = Wait-ServiceState -Expected "Running"
    $afterService = Get-ServiceSnapshot
    $Evidence.after_install.service = $afterService
    Add-Step -Name "install_and_start_service" -Status "passed" -Detail "$($service.Status); pid=$($afterService.process_id)"

    $installState = Join-Path $DataDir "install-state.json"
    Assert-PathPresent -Path $installState -Label "安装状态文件"
    $installStateJson = Get-Content -LiteralPath $installState -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([string]::IsNullOrWhiteSpace($installStateJson.version) -or $installStateJson.version -notmatch '1\.3\.1') {
        throw "安装状态版本不正确: $($installStateJson.version)"
    }
    Add-Step -Name "install_state" -Status "passed" -Detail $installStateJson.version

    $token = Get-UiToken
    Add-Step -Name "local_ui" -Status "passed" -Detail $BaseURL

    $beforeHadSensitive = @($beforeData.files | Where-Object { $_.path -match '\\(config|secrets|auth_profiles|identity|device_key)\.json$' }).Count -gt 0
    if ($beforeService -or $beforeHadSensitive) {
        $backups = @(Get-BackupSnapshot)
        $Evidence.backups = $backups
        if ($backups.Count -eq 0) { throw "升级前存在服务或配置，但没有生成 pre-upgrade 备份" }
        $latestBackup = $backups[$backups.Count - 1].path
        Assert-PathPresent -Path $latestBackup -Label "升级前备份目录"
        if ($beforeService) {
            Assert-PathPresent -Path (Join-Path $latestBackup "service.json") -Label "服务快照"
        }
        Assert-PrivateAcl -Path $latestBackup
        Add-Step -Name "upgrade_backup" -Status "passed" -Detail $latestBackup
    } else {
        Add-Step -Name "upgrade_backup" -Status "not_applicable" -Detail "首次安装，没有旧服务或配置"
    }

    if (-not $beforeHadSensitive) {
        Assert-ConnectorState -Expected "UNPAIRED" -Context "安装" | Out-Null
        Add-Step -Name "status_api" -Status "passed" -Detail "UNPAIRED"
    } else {
        Add-Step -Name "status_api" -Status "passed" -Detail "旧配置保留，未强制要求 UNPAIRED"
    }

    $missingTokenStatus = 0
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "$BaseURL/api/import" -Method Post `
            -ContentType "application/json" -Body '{"config":"{}"}' -TimeoutSec 10 | Out-Null
    } catch {
        if ($_.Exception.Response) { $missingTokenStatus = [int]$_.Exception.Response.StatusCode }
    }
    if ($missingTokenStatus -ne 403) { throw "无本机会话令牌的写请求应返回 403，实际为 $missingTokenStatus" }
    Add-Step -Name "csrf_token_guard" -Status "passed" -Detail "missing-token=403"

    $mcpConfig = '{"mcpServers":{"ci-local":{"type":"http","url":"http://127.0.0.1:18765/mcp","headers":{"Authorization":"Bearer windows-ci-secret"}}}}'
    $importBody = @{ config = $mcpConfig } | ConvertTo-Json -Compress
    $importResult = Invoke-RestMethod -Uri "$BaseURL/api/import" -Method Post `
        -Headers @{ "X-Connector-Token" = $token } -ContentType "application/json" `
        -Body $importBody -TimeoutSec 15
    if (-not $importResult.ok -or $importResult.data.HasErrors) { throw "测试来源导入失败" }
    foreach ($name in @("config.json", "secrets.json")) {
        Assert-PathPresent -Path (Join-Path $DataDir $name) -Label "导入后本机文件 $name"
    }
    Assert-ConnectorState -Expected "CONFIGURING" -Context "测试来源导入" | Out-Null
    Add-Step -Name "import_and_local_storage" -Status "passed" -Detail $DataDir

    Assert-SecretsProtected
    Add-Step -Name "dpapi_secret_files" -Status "passed" -Detail "prefix marker present; no test bearer plaintext"
    Assert-PrivateAcl -Path $DataDir
    Add-Step -Name "data_acl" -Status "passed" -Detail "SYSTEM and Administrators only; inheritance disabled"

    Restart-Service -Name $ServiceName -Force
    Wait-ServiceState -Expected "Running" | Out-Null
    Start-Sleep -Seconds 1
    Assert-ConnectorState -Expected "CONFIGURING" -Context "服务重启" | Out-Null
    Add-Step -Name "service_restart_recovery" -Status "passed" -Detail "CONFIGURING"

    $savedSecrets = Join-Path $DataDir "secrets.json"
    $configPath = Join-Path $DataDir "config.json"
    $configAfterRestart = [System.IO.File]::ReadAllText($configPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
    $sourceID = $configAfterRestart.sources[0].source_id
    if ([string]::IsNullOrWhiteSpace($sourceID)) { throw "迁移测试无法取得来源编号" }
    Stop-Service -Name $ServiceName -Force
    Wait-ServiceState -Expected "Stopped" | Out-Null
    $legacySecrets = @{}
    $legacySecrets[$sourceID] = @{ Authorization = "Bearer windows-ci-secret" }
    [System.IO.File]::WriteAllText($savedSecrets, ($legacySecrets | ConvertTo-Json -Depth 5 -Compress), [System.Text.UTF8Encoding]::new($false))
    Start-Service -Name $ServiceName
    Wait-ServiceState -Expected "Running" | Out-Null
    Start-Sleep -Seconds 1
    Assert-SecretsProtected
    Add-Step -Name "legacy_plaintext_migration" -Status "passed" -Detail $sourceID

    Invoke-PackageInstaller
    Wait-ServiceState -Expected "Running" | Out-Null
    Assert-ConnectorState -Expected "CONFIGURING" -Context "覆盖升级" | Out-Null
    Add-Step -Name "upgrade_reinstall" -Status "passed" -Detail "configuration retained"

    Invoke-PackageUninstaller -KeepProgramDirectory
    Wait-ServiceState -Expected "Absent" | Out-Null
    Assert-PathPresent -Path $InstallDir -Label "KeepProgramDirectory 后的程序目录"
    Assert-PathPresent -Path (Join-Path $DataDir "config.json") -Label "默认卸载后配置"
    Assert-PathPresent -Path (Join-Path $DataDir "secrets.json") -Label "默认卸载后本机身份"
    Add-Step -Name "uninstall_keep_program" -Status "passed" -Detail "service removed; program and data retained"

    Invoke-PackageInstaller
    Wait-ServiceState -Expected "Running" | Out-Null
    Assert-ConnectorState -Expected "CONFIGURING" -Context "卸载后重新安装" | Out-Null
    Add-Step -Name "reinstall_after_uninstall" -Status "passed" -Detail "configuration retained"

    Invoke-PackageUninstaller
    Wait-ServiceState -Expected "Absent" | Out-Null
    Assert-PathAbsent -Path $InstallDir -Label "默认卸载后的程序目录"
    Assert-PathPresent -Path $DataDir -Label "默认卸载后的数据目录"
    Assert-PathPresent -Path (Join-Path $DataDir "config.json") -Label "默认卸载后配置"
    Assert-PathPresent -Path (Join-Path $DataDir "secrets.json") -Label "默认卸载后本机身份"
    Add-Step -Name "uninstall_default_preserves_data" -Status "passed" -Detail "service and program removed; data retained"

    Invoke-PackageInstaller
    $finalService = Wait-ServiceState -Expected "Running"
    Assert-ConnectorState -Expected "CONFIGURING" -Context "最终恢复安装" | Out-Null
    Assert-PrivateAcl -Path $DataDir
    $Evidence.service = [ordered]@{
        name = $ServiceName
        state = $finalService.Status.ToString()
        start_type = (Get-Service -Name $ServiceName).StartType.ToString()
        snapshot = Get-ServiceSnapshot
        sc_qc = @(& sc.exe qc $ServiceName 2>&1)
    }
    $Evidence.data = Get-DataSnapshot
    Add-Step -Name "final_install" -Status "passed" -Detail "service running; configuration preserved"

    $Evidence.status = "passed"
    Save-Evidence
    exit 0
} catch {
    $Evidence.status = "failed"
    $Evidence.error = $_.Exception.Message
    Add-Step -Name "failure" -Status "failed" -Detail $_.Exception.Message
    try {
        $service = Get-ServiceSnapshot
        if ($service) { $Evidence.service = $service }
        if (Test-Path -LiteralPath $DataDir) { $Evidence.data = Get-DataSnapshot }
        if (Test-Path -LiteralPath $LogPath) { $Evidence.service.log_tail = @(Get-Content -LiteralPath $LogPath -Tail 200) }
        $Evidence.backups = @(Get-BackupSnapshot)
    } catch {}
    Save-Evidence
    Write-Error $_
    exit 1
}
