#Requires -RunAsAdministrator

param(
    [Parameter(Mandatory = $true)]
    [string]$PackageRoot,
    [string]$EvidencePath = "",
    [string]$SetupPath = ""
)

$ErrorActionPreference = "Stop"
$ServiceName = "KaiwuConnector"
$BaseURL = "http://127.0.0.1:17872"
$InstallDir = Join-Path $env:ProgramFiles "Kaiwu\Connector"
$DataDir = Join-Path $env:ProgramData "Kaiwu\Connector"
$LogPath = Join-Path $env:TEMP "kaiwu-connector-windows-smoke.log"
$StateBackupRoot = Join-Path $env:ProgramData "Kaiwu\ConnectorSmokeBackups"
$UninstallKey = "HKLM\Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector"
$StateBackup = $null
$ExpectedVersion = $null
$SupportedEnvironmentNames = @("KAIWU_CLOUD_BASE", "KAIWU_EDGE_BASE")

$PackageRoot = (Resolve-Path -LiteralPath $PackageRoot).Path
$Installer = Join-Path $PackageRoot "install.ps1"
$Uninstaller = Join-Path $PackageRoot "uninstall.ps1"
$UseSetup = -not [string]::IsNullOrWhiteSpace($SetupPath)
if ($UseSetup) {
    $SetupPath = (Resolve-Path -LiteralPath $SetupPath).Path
}
if ([string]::IsNullOrWhiteSpace($EvidencePath)) {
    $EvidencePath = Join-Path $PackageRoot "windows-smoke-evidence.json"
}

$Evidence = [ordered]@{
    started_at = (Get-Date).ToUniversalTime().ToString("o")
    finished_at = $null
    status = "running"
    package_root = $PackageRoot
    setup_path = $SetupPath
    installer_mode = if ($UseSetup) { "nsis-setup" } else { "portable-package" }
    uninstall_mode = "default uninstall preserves data; the smoke test separately restores the protected pre-test snapshot"
    steps = @()
    os = [ordered]@{}
    before = [ordered]@{}
    after_install = [ordered]@{}
    backups = @()
    files = @()
    service = [ordered]@{}
    data = [ordered]@{}
    synthetic_mcp = [ordered]@{}
    state_backup = [ordered]@{}
    restoration = [ordered]@{}
    error = $null
}

$SyntheticPort = 18765
$SyntheticBearer = "windows-ci-secret"
$SyntheticHmacKey = "short-key"
$SyntheticAppID = "synthetic-app"
$SyntheticChannel = "synthetic-channel"
$SyntheticUserID = "synthetic-user"
$SyntheticTool = "lookup_order"
$SyntheticResult = "SYNTHETIC-ORDER-42"
$SyntheticLog = Join-Path ([System.IO.Path]::GetTempPath()) "kaiwu-synthetic-mcp-$PID.jsonl"
$SyntheticJob = $null

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

function Test-RegistryKey {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][ValidateSet(32, 64)][int]$View
    )
    $subPath = $Path -replace '^HKLM\\', ''
    $registryView = if ($View -eq 64) {
        [Microsoft.Win32.RegistryView]::Registry64
    } else {
        [Microsoft.Win32.RegistryView]::Registry32
    }
    $baseKey = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
        [Microsoft.Win32.RegistryHive]::LocalMachine,
        $registryView
    )
    $key = $null
    try {
        $key = $baseKey.OpenSubKey($subPath)
        return ($null -ne $key)
    } finally {
        if ($null -ne $key) { $key.Dispose() }
        $baseKey.Dispose()
    }
}

function Get-SyntheticMcpEvents {
    if (-not (Test-Path -LiteralPath $SyntheticLog)) { return @() }
    return @(
        Get-Content -LiteralPath $SyntheticLog -Encoding UTF8 -ErrorAction SilentlyContinue |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        ForEach-Object { $_ | ConvertFrom-Json }
    )
}

function Start-SyntheticMcp {
    Remove-Item -LiteralPath $SyntheticLog -Force -ErrorAction SilentlyContinue
    $script:SyntheticJob = Start-Job -ArgumentList @(
        $SyntheticPort,
        $SyntheticBearer,
        $SyntheticHmacKey,
        $SyntheticAppID,
        $SyntheticChannel,
        $SyntheticUserID,
        $SyntheticTool,
        $SyntheticResult,
        $SyntheticLog
    ) -ScriptBlock {
        param(
            $Port,
            $Bearer,
            $HmacKey,
            $AppID,
            $Channel,
            $UserID,
            $ToolName,
            $ToolResult,
            $LogPath
        )
        $ErrorActionPreference = "Stop"

        function Write-SyntheticEvent {
            param([hashtable]$Event)
            $line = ($Event | ConvertTo-Json -Depth 8 -Compress) + "`r`n"
            [System.IO.File]::AppendAllText($LogPath, $line, [System.Text.UTF8Encoding]::new($false))
        }

        function Send-SyntheticResponse {
            param(
                [System.Net.HttpListenerContext]$Context,
                [int]$StatusCode,
                $Payload = $null
            )
            $Context.Response.StatusCode = $StatusCode
            $Context.Response.ContentType = "application/json; charset=utf-8"
            if ($null -eq $Payload) {
                $Context.Response.ContentLength64 = 0
            } else {
                $bytes = [System.Text.Encoding]::UTF8.GetBytes(($Payload | ConvertTo-Json -Depth 12 -Compress))
                $Context.Response.ContentLength64 = $bytes.Length
                $Context.Response.OutputStream.Write($bytes, 0, $bytes.Length)
            }
            $Context.Response.OutputStream.Close()
        }

        function Test-DynamicHeaders {
            param([System.Net.HttpListenerRequest]$Request)
            $appid = [string]$Request.Headers["appid"]
            $channel = [string]$Request.Headers["channel"]
            $userid = [string]$Request.Headers["userid"]
            $requestID = [string]$Request.Headers["requestId"]
            $timestampRaw = [string]$Request.Headers["timestamp"]
            $sign = [string]$Request.Headers["sign"]
            if ($appid -ne $AppID -or $channel -ne $Channel -or $userid -ne $UserID) { return $false }
            if ($requestID -notmatch '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$') { return $false }
            [int64]$timestamp = 0
            if (-not [int64]::TryParse($timestampRaw, [ref]$timestamp)) { return $false }
            if ([Math]::Abs([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - $timestamp) -gt 120) { return $false }
            $canonical = "appid=$AppID&channel=$Channel&userid=$UserID&requestId=$requestID&timestamp=$timestampRaw"
            $hmac = [System.Security.Cryptography.HMACSHA256]::new([System.Text.Encoding]::UTF8.GetBytes($HmacKey))
            try {
                $expected = (($hmac.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($canonical)) | ForEach-Object { $_.ToString("x2") }) -join "")
            } finally {
                $hmac.Dispose()
            }
            if ([string]::Equals($expected, $sign, [System.StringComparison]::Ordinal)) { return $true }
            return $false
        }

        $listener = [System.Net.HttpListener]::new()
        $listener.Prefixes.Add("http://127.0.0.1:$Port/")
        try {
            $listener.Start()
        } catch {
            Write-SyntheticEvent @{ event = "start_failed"; error = $_.Exception.Message; at = (Get-Date).ToUniversalTime().ToString("o") }
            return
        }
        Write-SyntheticEvent @{ event = "started"; port = $Port; at = (Get-Date).ToUniversalTime().ToString("o") }

        try {
            while ($listener.IsListening) {
                $context = $listener.GetContext()
                $method = ""
                $status = 500
                $bearerOK = $false
                $dynamicOK = $null
                $returnedMarker = $null
                try {
                    $reader = [System.IO.StreamReader]::new($context.Request.InputStream, [System.Text.Encoding]::UTF8)
                    try {
                        $body = $reader.ReadToEnd()
                    } finally {
                        $reader.Dispose()
                    }
                    $rpc = $body | ConvertFrom-Json
                    $method = [string]$rpc.method
                    $bearerOK = [string]::Equals([string]$context.Request.Headers["Authorization"], "Bearer $Bearer", [System.StringComparison]::Ordinal)
                    if (-not $bearerOK) {
                        $status = 401
                        Send-SyntheticResponse -Context $context -StatusCode $status -Payload @{ error = "unauthorized" }
                        continue
                    }

                    if ([string]::IsNullOrWhiteSpace($method) -or $method -eq "notifications/initialized") {
                        $status = 202
                        Send-SyntheticResponse -Context $context -StatusCode $status
                        continue
                    }

                    switch ($method) {
                        "initialize" {
                            $result = @{
                                protocolVersion = "2025-06-18"
                                capabilities = @{ tools = @{} }
                                serverInfo = @{ name = "synthetic-windows-mcp"; version = "1.0.0" }
                            }
                            $status = 200
                        }
                        "tools/list" {
                            $result = @{
                                tools = @(@{
                                    name = $ToolName
                                    description = "固定合成订单查询"
                                    inputSchema = @{
                                        type = "object"
                                        properties = @{ order_id = @{ type = "string" } }
                                        required = @("order_id")
                                    }
                                })
                            }
                            $status = 200
                        }
                        "tools/call" {
                            $dynamicOK = Test-DynamicHeaders -Request $context.Request
                            if (-not $dynamicOK) {
                                $status = 403
                                Send-SyntheticResponse -Context $context -StatusCode $status -Payload @{ error = "dynamic authentication failed" }
                                continue
                            }
                            if ([string]$rpc.params.name -ne $ToolName) {
                                $status = 404
                                Send-SyntheticResponse -Context $context -StatusCode $status -Payload @{ error = "unknown tool" }
                                continue
                            }
                            $result = @{
                                content = @(@{ type = "text"; text = $ToolResult })
                                isError = $false
                            }
                            $returnedMarker = $ToolResult
                            $status = 200
                        }
                        default {
                            $status = 404
                            Send-SyntheticResponse -Context $context -StatusCode $status -Payload @{ error = "unsupported method" }
                            continue
                        }
                    }

                    Send-SyntheticResponse -Context $context -StatusCode $status -Payload @{
                        jsonrpc = "2.0"
                        id = $rpc.id
                        result = $result
                    }
                } catch {
                    $status = 500
                    try {
                        Send-SyntheticResponse -Context $context -StatusCode $status -Payload @{ error = $_.Exception.Message }
                    } catch {}
                } finally {
                    Write-SyntheticEvent @{
                        event = "request"
                        method = $method
                        http_status = $status
                        bearer_ok = $bearerOK
                        dynamic_headers_ok = $dynamicOK
                        returned_marker = $returnedMarker
                        at = (Get-Date).ToUniversalTime().ToString("o")
                    }
                }
            }
        } finally {
            if ($listener.IsListening) { $listener.Stop() }
            $listener.Close()
        }
    }

    $deadline = (Get-Date).AddSeconds(15)
    do {
        Start-Sleep -Milliseconds 250
        $events = @(Get-SyntheticMcpEvents)
        if ($events | Where-Object { $_.event -eq "started" }) { return }
        if ($script:SyntheticJob.State -eq "Completed") {
            $jobOutput = Receive-Job -Job $script:SyntheticJob -Keep -ErrorAction SilentlyContinue | Out-String
            throw "合成 MCP 服务未启动: $jobOutput"
        }
    } while ((Get-Date) -lt $deadline)
    throw "合成 MCP 服务未在 15 秒内监听 127.0.0.1:$SyntheticPort"
}

function Stop-SyntheticMcp {
    if ($null -eq $script:SyntheticJob) { return }
    Stop-Job -Job $script:SyntheticJob -ErrorAction SilentlyContinue
    Remove-Job -Job $script:SyntheticJob -Force -ErrorAction SilentlyContinue
    $script:SyntheticJob = $null
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

function Assert-NativeWindowsInstall {
    param([Parameter(Mandatory = $true)]$ServiceSnapshot)
    if ([string]::IsNullOrWhiteSpace($env:ProgramW6432)) {
        throw "当前进程没有读取到 64 位 Program Files 路径"
    }
    $expectedDir = [System.IO.Path]::GetFullPath((Join-Path $env:ProgramW6432 "Kaiwu\Connector"))
    $actualDir = [System.IO.Path]::GetFullPath($InstallDir)
    if (-not [string]::Equals($expectedDir, $actualDir, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "安装目录不在原生 64 位 Program Files: $actualDir"
    }
    $legacyX86Dir = Join-Path ${env:ProgramFiles(x86)} "Kaiwu\Connector"
    if (Test-Path -LiteralPath $legacyX86Dir) {
        throw "旧 32 位程序目录仍然存在: $legacyX86Dir"
    }
    Assert-PathPresent -Path (Join-Path $expectedDir "kaiwu-connector.exe") -Label "64 位连接器可执行文件"
    $binaryPath = ([string]$ServiceSnapshot.binary_path).Trim('"')
    if (-not $binaryPath.StartsWith($expectedDir + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Windows 服务没有从原生 64 位安装目录启动: $binaryPath"
    }

    if (-not (Test-RegistryKey -Path $UninstallKey -View 64)) {
        throw "缺少原生 64 位卸载登记"
    }
    if (Test-RegistryKey -Path $UninstallKey -View 32) {
        throw "旧 32 位卸载登记仍然存在"
    }

    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort 17872 -ErrorAction Stop)
    if ($listeners.Count -eq 0) { throw "17872 端口没有监听进程" }
    foreach ($listener in $listeners) {
        if ($listener.LocalAddress -ne "127.0.0.1") {
            throw "17872 监听地址不是回环地址: $($listener.LocalAddress)"
        }
    }
    return [ordered]@{
        install_dir = $actualDir
        legacy_x86_dir_absent = $true
        service_binary = $binaryPath
        uninstall_registry_native = $true
        uninstall_registry_x86 = $false
        listeners = @($listeners | ForEach-Object { [ordered]@{ address = $_.LocalAddress; port = $_.LocalPort; pid = $_.OwningProcess } })
    }
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
            # Files created after installation inherit the protected parent
            # ACL and therefore normally do not carry a protected DACL bit
            # themselves. Reject only when that parent is not protected too.
            $parentPath = Split-Path -Parent $item.FullName
            if ([string]::IsNullOrWhiteSpace($parentPath)) {
                throw "无法确认 ACL 上级目录: $($item.FullName)"
            }
            $parentAcl = Get-Acl -LiteralPath $parentPath
            if (-not $parentAcl.AreAccessRulesProtected) {
                throw "ACL 仍会继承未受保护的上级权限: $($item.FullName)"
            }
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
    if ($UseSetup) {
        if (-not (Test-Path -LiteralPath $SetupPath)) { throw "找不到安装程序: $SetupPath" }
        $process = Start-Process -FilePath $SetupPath -ArgumentList "/S" -Wait -PassThru
        if ($process.ExitCode -ne 0) { throw "NSIS 安装程序退出码为 $($process.ExitCode)" }
        return
    }
    if (-not (Test-Path -LiteralPath $Installer)) { throw "找不到安装脚本: $Installer" }
    & $Installer
}

function Invoke-PackageUninstaller {
    param([switch]$KeepProgramDirectory)
    if ($UseSetup) {
        if ($KeepProgramDirectory) { throw "NSIS 安装模式不支持 KeepProgramDirectory 中间态" }
        $setupUninstaller = Join-Path $InstallDir "uninstall.exe"
        if (-not (Test-Path -LiteralPath $setupUninstaller)) { throw "找不到 NSIS 卸载程序: $setupUninstaller" }
        $process = Start-Process -FilePath $setupUninstaller -ArgumentList "/S" -Wait -PassThru
        if ($process.ExitCode -ne 0) { throw "NSIS 卸载程序退出码为 $($process.ExitCode)" }
        return
    }
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

function Get-OptionalConnectorState {
    try {
        $status = Get-ConnectorStatus
        return [string]$status.data.state
    } catch {
        return $null
    }
}

function Get-FileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-TextSha256 {
    param([AllowNull()][string]$Text)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes([string]$Text)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes)) -replace '-', '').ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Get-ConfigurationFingerprint {
    $entries = @()
    if (Test-Path -LiteralPath $DataDir) {
        foreach ($name in @(
            "config.json",
            "secrets.json",
            "auth_profiles.json",
            "identity.json",
            "device_key.json",
            "frpc.toml",
            "revoked.json"
        )) {
            $path = Join-Path $DataDir $name
            $entries += [ordered]@{
                name = $name
                sha256 = if (Test-Path -LiteralPath $path) { Get-FileSha256 -Path $path } else { $null }
            }
        }
    }
    $environment = @()
    foreach ($name in @("KAIWU_CLOUD_BASE", "KAIWU_EDGE_BASE")) {
        $value = [Environment]::GetEnvironmentVariable($name, "Machine")
        $environment += [ordered]@{
            name = $name
            sha256 = if ([string]::IsNullOrWhiteSpace($value)) { $null } else { Get-TextSha256 -Text $value }
        }
    }
    $canonical = [ordered]@{ files = $entries; environment = $environment } | ConvertTo-Json -Depth 5 -Compress
    return [ordered]@{
        sha256 = Get-TextSha256 -Text $canonical
        files = $entries
        environment = $environment
    }
}

function Set-PrivateAcl {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Force -Path $Path | Out-Null
    }
    & icacls.exe $Path /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" /T /C | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "设置受保护目录 ACL 失败，icacls 退出码为 ${LASTEXITCODE}: $Path"
    }
    $systemSid = [System.Security.Principal.SecurityIdentifier]::new("S-1-5-18")
    $adminSid = [System.Security.Principal.SecurityIdentifier]::new("S-1-5-32-544")
    $items = @(Get-Item -LiteralPath $Path -Force) + @(Get-ChildItem -LiteralPath $Path -Force -Recurse -ErrorAction Stop)
    foreach ($item in $items) {
        $acl = Get-Acl -LiteralPath $item.FullName
        $acl.SetAccessRuleProtection($true, $false)
        foreach ($rule in @($acl.Access)) {
            $acl.RemoveAccessRuleSpecific($rule) | Out-Null
        }
        if ($item.PSIsContainer) {
            $inheritance = (
                [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
                [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
            )
        } else {
            $inheritance = [System.Security.AccessControl.InheritanceFlags]::None
        }
        foreach ($sid in @($systemSid, $adminSid)) {
            $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
                $sid,
                [System.Security.AccessControl.FileSystemRights]::FullControl,
                $inheritance,
                [System.Security.AccessControl.PropagationFlags]::None,
                [System.Security.AccessControl.AccessControlType]::Allow
            )
            $acl.AddAccessRule($rule)
        }
        Set-Acl -LiteralPath $item.FullName -AclObject $acl
    }
    Assert-PrivateAcl -Path $Path
}

function Copy-DirectoryContents {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    foreach ($item in @(Get-ChildItem -LiteralPath $Source -Force -ErrorAction Stop)) {
        Copy-Item -LiteralPath $item.FullName -Destination $Destination -Recurse -Force
    }
}

function Remove-RegistryKey {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][ValidateSet(32, 64)][int]$View
    )
    $subPath = $Path -replace '^HKLM\\', ''
    $registryView = if ($View -eq 64) {
        [Microsoft.Win32.RegistryView]::Registry64
    } else {
        [Microsoft.Win32.RegistryView]::Registry32
    }
    $baseKey = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
        [Microsoft.Win32.RegistryHive]::LocalMachine,
        $registryView
    )
    try {
        $baseKey.DeleteSubKeyTree($subPath, $false)
    } finally {
        $baseKey.Dispose()
    }
}

function Get-RegistryNodeSnapshot {
    param(
        [Parameter(Mandatory = $true)][Microsoft.Win32.RegistryKey]$Key
    )
    $values = @()
    foreach ($name in @($Key.GetValueNames() | Sort-Object)) {
        $kind = $Key.GetValueKind($name).ToString()
        $raw = $Key.GetValue(
            $name,
            $null,
            [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames
        )
        switch ($kind) {
            "String" { $value = [string]$raw }
            "ExpandString" { $value = [string]$raw }
            "DWord" { $value = [Convert]::ToString([uint32]$raw, 10) }
            "QWord" { $value = [Convert]::ToString([uint64]$raw, 10) }
            "Binary" { $value = [Convert]::ToBase64String([byte[]]$raw) }
            "MultiString" { $value = @($raw | ForEach-Object { [string]$_ }) }
            "None" { $value = $null }
            default { throw "不支持的注册表值类型 ${kind}: $name" }
        }
        $values += [ordered]@{
            name = [string]$name
            kind = $kind
            value = $value
        }
    }

    $subkeys = @()
    foreach ($subkeyName in @($Key.GetSubKeyNames() | Sort-Object)) {
        $child = $Key.OpenSubKey($subkeyName, $false)
        if ($null -eq $child) {
            throw "无法读取注册表子项: $subkeyName"
        }
        try {
            $subkeys += [ordered]@{
                name = [string]$subkeyName
                node = (Get-RegistryNodeSnapshot -Key $child)
            }
        } finally {
            $child.Dispose()
        }
    }
    return [ordered]@{
        values = $values
        subkeys = $subkeys
    }
}

function Get-RegistryKeySnapshot {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][ValidateSet(32, 64)][int]$View
    )
    $subPath = $Path -replace '^HKLM\\', ''
    $registryView = if ($View -eq 64) {
        [Microsoft.Win32.RegistryView]::Registry64
    } else {
        [Microsoft.Win32.RegistryView]::Registry32
    }
    $baseKey = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
        [Microsoft.Win32.RegistryHive]::LocalMachine,
        $registryView
    )
    $key = $null
    try {
        $key = $baseKey.OpenSubKey($subPath, $false)
        if ($null -eq $key) {
            return [ordered]@{
                exists = $false
                node = $null
            }
        }
        return [ordered]@{
            exists = $true
            node = (Get-RegistryNodeSnapshot -Key $key)
        }
    } finally {
        if ($null -ne $key) { $key.Dispose() }
        $baseKey.Dispose()
    }
}

function Set-RegistryNodeSnapshot {
    param(
        [Parameter(Mandatory = $true)][Microsoft.Win32.RegistryKey]$Key,
        [Parameter(Mandatory = $true)]$Snapshot
    )
    foreach ($entry in @($Snapshot.values)) {
        $name = [string]$entry.name
        switch ([string]$entry.kind) {
            "String" { $value = [string]$entry.value; $kind = [Microsoft.Win32.RegistryValueKind]::String }
            "ExpandString" { $value = [string]$entry.value; $kind = [Microsoft.Win32.RegistryValueKind]::ExpandString }
            "DWord" { $value = [uint32]::Parse([string]$entry.value); $kind = [Microsoft.Win32.RegistryValueKind]::DWord }
            "QWord" { $value = [uint64]::Parse([string]$entry.value); $kind = [Microsoft.Win32.RegistryValueKind]::QWord }
            "Binary" { $value = [Convert]::FromBase64String([string]$entry.value); $kind = [Microsoft.Win32.RegistryValueKind]::Binary }
            "MultiString" { $value = [string[]]@($entry.value); $kind = [Microsoft.Win32.RegistryValueKind]::MultiString }
            "None" { $value = $null; $kind = [Microsoft.Win32.RegistryValueKind]::None }
            default { throw "不支持的注册表快照值类型: $($entry.kind)" }
        }
        $Key.SetValue($name, $value, $kind)
    }
    foreach ($subkey in @($Snapshot.subkeys)) {
        $child = $Key.CreateSubKey([string]$subkey.name)
        if ($null -eq $child) { throw "无法创建注册表子项: $($subkey.name)" }
        try {
            Set-RegistryNodeSnapshot -Key $child -Snapshot $subkey.node
        } finally {
            $child.Dispose()
        }
    }
}

function Get-UninstallRegistrySnapshot {
    return [ordered]@{
        key = $UninstallKey
        views = [ordered]@{
            "32" = (Get-RegistryKeySnapshot -Path $UninstallKey -View 32)
            "64" = (Get-RegistryKeySnapshot -Path $UninstallKey -View 64)
        }
    }
}

function Get-UninstallRegistryFingerprint {
    param([Parameter(Mandatory = $true)]$Snapshot)
    $canonical = $Snapshot | ConvertTo-Json -Depth 20 -Compress
    return Get-TextSha256 -Text $canonical
}

function Restore-UninstallRegistrySnapshot {
    param([Parameter(Mandatory = $true)]$Snapshot)
    if ([string]$Snapshot.key -ne $UninstallKey) {
        throw "卸载登记快照目标不匹配: $($Snapshot.key)"
    }
    foreach ($view in @(64, 32)) {
        $viewKey = [string]$view
        $viewSnapshot = $Snapshot.views[$viewKey]
        Remove-RegistryKey -Path $UninstallKey -View $view
        if (-not $viewSnapshot.exists) { continue }

        $subPath = $UninstallKey -replace '^HKLM\\', ''
        $registryView = if ($view -eq 64) {
            [Microsoft.Win32.RegistryView]::Registry64
        } else {
            [Microsoft.Win32.RegistryView]::Registry32
        }
        $baseKey = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
            [Microsoft.Win32.RegistryHive]::LocalMachine,
            $registryView
        )
        $key = $null
        try {
            $key = $baseKey.CreateSubKey($subPath)
            if ($null -eq $key) { throw "无法恢复卸载登记，注册表视图: $view" }
            Set-RegistryNodeSnapshot -Key $key -Snapshot $viewSnapshot.node
        } finally {
            if ($null -ne $key) { $key.Dispose() }
            $baseKey.Dispose()
        }
    }
}

function New-TestStateBackup {
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
    $backup = Join-Path $StateBackupRoot ($stamp + "-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
    New-Item -ItemType Directory -Force -Path $backup | Out-Null
    $service = Get-ServiceSnapshot
    $dataExisted = Test-Path -LiteralPath $DataDir
    $programExisted = Test-Path -LiteralPath $InstallDir
    $fingerprint = Get-ConfigurationFingerprint
    $uninstallRegistry = Get-UninstallRegistrySnapshot
    $uninstallRegistryFingerprint = Get-UninstallRegistryFingerprint -Snapshot $uninstallRegistry
    $environment = [ordered]@{}
    foreach ($name in $SupportedEnvironmentNames) {
        $environment[$name] = [Environment]::GetEnvironmentVariable($name, "Machine")
    }
    $state = [ordered]@{
        created_at = (Get-Date).ToUniversalTime().ToString("o")
        service = $service
        data_existed = $dataExisted
        program_existed = $programExisted
        configuration_fingerprint = $fingerprint
        uninstall_registry = $uninstallRegistry
        uninstall_registry_fingerprint = $uninstallRegistryFingerprint
        environment = $environment
        connector_state = if ($service -and $service.state -eq "Running") { Get-OptionalConnectorState } else { $null }
    }
    $serviceWasRunning = $false
    if ($service -and $service.state -eq "Running") {
        Stop-Service -Name $ServiceName -Force -ErrorAction Stop
        Wait-ServiceState -Expected "Stopped" | Out-Null
        $serviceWasRunning = $true
    }
    try {
        if ($dataExisted) {
            Copy-DirectoryContents -Source $DataDir -Destination (Join-Path $backup "data")
        }
        if ($programExisted) {
            Copy-DirectoryContents -Source $InstallDir -Destination (Join-Path $backup "program")
        }
    } finally {
        if ($serviceWasRunning) {
            Start-Service -Name $ServiceName -ErrorAction Stop
            Wait-ServiceState -Expected "Running" | Out-Null
        }
    }
    $state | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath (Join-Path $backup "state.json") -Encoding UTF8
    Set-PrivateAcl -Path $backup
    $script:StateBackup = [ordered]@{
        path = $backup
        state = $state
    }
    $Evidence.state_backup = [ordered]@{
        path = $backup
        protected_acl = $true
        data_existed = $dataExisted
        program_existed = $programExisted
        configuration_sha256 = $fingerprint.sha256
        uninstall_registry_sha256 = $uninstallRegistryFingerprint
        uninstall_registry_present_32 = [bool]$uninstallRegistry.views["32"].exists
        uninstall_registry_present_64 = [bool]$uninstallRegistry.views["64"].exists
        service_state = if ($service) { $service.state } else { "Absent" }
        service_paused_for_backup = $serviceWasRunning
    }
    return $script:StateBackup
}

function Remove-TestInstallArtifacts {
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($service) {
        Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
        sc.exe delete $ServiceName | Out-Null
        Wait-ServiceState -Expected "Absent" | Out-Null
    }
    foreach ($path in @(
        $InstallDir,
        (Join-Path ${env:ProgramFiles(x86)} "Kaiwu\Connector"),
        (Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\开物内网连接器"),
        (Join-Path $env:PUBLIC "Desktop\开物内网连接器配置.lnk")
    )) {
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Recurse -Force -Confirm:$false
        }
    }
    Remove-RegistryKey -Path $UninstallKey -View 64
    Remove-RegistryKey -Path $UninstallKey -View 32
}

function Restore-OriginalState {
    if ($null -eq $script:StateBackup) {
        throw "没有可用于恢复的测试前状态快照"
    }
    $backup = [string]$script:StateBackup.path
    $state = $script:StateBackup.state
    $currentService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($currentService) {
        Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
        sc.exe delete $ServiceName | Out-Null
        Wait-ServiceState -Expected "Absent" | Out-Null
    }

    if ($state.program_existed) {
        if (Test-Path -LiteralPath $InstallDir) {
            Remove-Item -LiteralPath $InstallDir -Recurse -Force -Confirm:$false
        }
        New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
        Copy-DirectoryContents -Source (Join-Path $backup "program") -Destination $InstallDir
    } elseif (Test-Path -LiteralPath $InstallDir) {
        Remove-Item -LiteralPath $InstallDir -Recurse -Force -Confirm:$false
    }

    if ($state.data_existed) {
        if (Test-Path -LiteralPath $DataDir) {
            Remove-Item -LiteralPath $DataDir -Recurse -Force -Confirm:$false
        }
        New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
        Copy-DirectoryContents -Source (Join-Path $backup "data") -Destination $DataDir
        Set-PrivateAcl -Path $DataDir
    } elseif (Test-Path -LiteralPath $DataDir) {
        Remove-Item -LiteralPath $DataDir -Recurse -Force -Confirm:$false
    }

    $environmentState = $state.environment
    foreach ($name in $SupportedEnvironmentNames) {
        $restoreValue = $null
        if ($environmentState -is [System.Collections.IDictionary]) {
            foreach ($key in $environmentState.Keys) {
                if ([string]::Equals([string]$key, $name, [System.StringComparison]::OrdinalIgnoreCase)) {
                    $restoreValue = $environmentState[$key]
                    break
                }
            }
        } elseif ($null -ne $environmentState) {
            $property = $environmentState.PSObject.Properties[$name]
            if ($null -ne $property) {
                $restoreValue = $property.Value
            }
        }
        [Environment]::SetEnvironmentVariable($name, $restoreValue, "Machine")
    }

    if ($null -ne $state.service) {
        $snapshot = $state.service
        $startupType = switch ($snapshot.start_mode) {
            "Auto" { "Automatic" }
            "Manual" { "Manual" }
            "Disabled" { "Disabled" }
            default { "Automatic" }
        }
        $displayName = [string]$snapshot.display_name
        if ([string]::IsNullOrWhiteSpace($displayName)) { $displayName = "Kaiwu Intranet Connector" }
        New-Service -Name $ServiceName -BinaryPathName ([string]$snapshot.binary_path) `
            -StartupType $startupType -DisplayName $displayName -ErrorAction Stop | Out-Null
        if ($snapshot.delayed_auto_start -and $startupType -eq "Automatic") {
            sc.exe config $ServiceName start= delayed-auto | Out-Null
        }
        sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/15000/restart/60000 | Out-Null
        if ($snapshot.state -eq "Running") {
            Start-Service -Name $ServiceName -ErrorAction Stop
            Wait-ServiceState -Expected "Running" | Out-Null
            Start-Sleep -Seconds 1
        }
    } else {
        Remove-TestInstallArtifacts
    }

    Restore-UninstallRegistrySnapshot -Snapshot $state.uninstall_registry
    $restoredRegistry = Get-UninstallRegistrySnapshot
    $restoredRegistryFingerprint = Get-UninstallRegistryFingerprint -Snapshot $restoredRegistry
    if ($restoredRegistryFingerprint -ne [string]$state.uninstall_registry_fingerprint) {
        throw "恢复后的卸载登记与测试前不一致"
    }
    $restoredFingerprint = Get-ConfigurationFingerprint
    if ($restoredFingerprint.sha256 -ne $state.configuration_fingerprint.sha256) {
        throw "恢复后的配置指纹与测试前不一致"
    }
    $restoredService = Get-ServiceSnapshot
    if ($null -eq $state.service -and $null -ne $restoredService) {
        throw "测试前服务不存在，但恢复后服务仍存在"
    }
    if ($null -ne $state.service) {
        if ($null -eq $restoredService) { throw "测试前服务存在，但恢复后服务不存在" }
        if ($restoredService.state -ne $state.service.state) {
            throw "恢复后的服务状态不一致：测试前=$($state.service.state)，恢复后=$($restoredService.state)"
        }
    }
    $restoredConnectorState = $null
    if ($restoredService -and $restoredService.state -eq "Running") {
        $deadline = (Get-Date).AddSeconds(20)
        do {
            $restoredConnectorState = Get-OptionalConnectorState
            if ($null -ne $restoredConnectorState) { break }
            Start-Sleep -Milliseconds 500
        } while ((Get-Date) -lt $deadline)
        if ($null -ne $state.connector_state -and $restoredConnectorState -ne $state.connector_state) {
            throw "恢复后的连接器状态不一致：测试前=$($state.connector_state)，恢复后=$($restoredConnectorState)"
        }
    }
    if ($state.data_existed) {
        Assert-PrivateAcl -Path $DataDir
    }
    return [ordered]@{
        backup_path = $backup
        configuration_sha256_before = $state.configuration_fingerprint.sha256
        configuration_sha256_after = $restoredFingerprint.sha256
        service_before = if ($state.service) { $state.service.state } else { "Absent" }
        service_after = if ($restoredService) { $restoredService.state } else { "Absent" }
        connector_state_before = $state.connector_state
        connector_state_after = $restoredConnectorState
        uninstall_registry_sha256_before = $state.uninstall_registry_fingerprint
        uninstall_registry_sha256_after = $restoredRegistryFingerprint
        uninstall_registry_present_32 = [bool]$restoredRegistry.views["32"].exists
        uninstall_registry_present_64 = [bool]$restoredRegistry.views["64"].exists
        uninstall_registry_verified = $true
        acl_verified = $state.data_existed
    }
}

function Restore-TestStateAndRecord {
    if ($null -eq $script:StateBackup) {
        return $true
    }
    try {
        $result = Restore-OriginalState
        $Evidence.restoration = $result
        Add-Step -Name "original_configuration_restored" -Status "passed" -Detail "fingerprint/service/ACL/uninstall-registry verified"
        try {
            Remove-Item -LiteralPath $script:StateBackup.path -Recurse -Force -Confirm:$false
            $Evidence.restoration.backup_removed = $true
        } catch {
            $Evidence.restoration.backup_removed = $false
            $Evidence.restoration.backup_cleanup_warning = $_.Exception.Message
            Add-Step -Name "test_backup_cleanup" -Status "warning" -Detail $script:StateBackup.path
        }
        return $true
    } catch {
        $Evidence.status = "failed"
        $Evidence.restoration = [ordered]@{
            status = "failed"
            backup_path = if ($script:StateBackup) { $script:StateBackup.path } else { $null }
            error = $_.Exception.Message
            backup_retained = $true
        }
        Add-Step -Name "original_configuration_restored" -Status "failed" -Detail $_.Exception.Message
        return $false
    }
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
        powershell_version = $PSVersionTable.PSVersion.ToString()
        powershell_edition = $PSVersionTable.PSEdition
    }
    Add-Step -Name "collect_os" -Status "passed" -Detail "$($os.Caption) $($os.Version)"

    $required = @(
        "install.ps1",
        "uninstall.ps1",
        "windows-smoke.ps1",
        "SHA256SUMS",
        "版本信息.txt",
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

    $versionInfo = Get-Content -LiteralPath (Join-Path $PackageRoot "版本信息.txt") -Raw -Encoding UTF8
    $versionInfoMatch = [regex]::Match($versionInfo, '(?m)^开物内网连接器\s+([0-9]+\.[0-9]+\.[0-9]+)\s*$')
    if (-not $versionInfoMatch.Success) {
        throw "版本信息.txt 缺少有效版本号"
    }
    $ExpectedVersion = $versionInfoMatch.Groups[1].Value

    $versionOutput = (& (Join-Path $PackageRoot "bin\kaiwu-connector.exe") version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($versionOutput)) {
        throw "kaiwu-connector.exe version 执行失败: $versionOutput"
    }
    $binaryVersionMatch = [regex]::Match($versionOutput, '^([0-9]+\.[0-9]+\.[0-9]+)(?:\s|$)')
    if (-not $binaryVersionMatch.Success -or $binaryVersionMatch.Groups[1].Value -ne $ExpectedVersion) {
        throw "候选包版本不是 $ExpectedVersion`: $versionOutput"
    }
    Add-Step -Name "binary_version" -Status "passed" -Detail "$versionOutput; metadata=$ExpectedVersion"

    $frpcPath = Join-Path $PackageRoot "bin\frpc.exe"
    $frpcVersion = (& $frpcPath -v 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $frpcVersion -notmatch '0\.61\.1') {
        throw "官方 frpc 版本检查失败: $frpcVersion"
    }
    Add-Step -Name "frpc_binary_version" -Status "passed" -Detail $frpcVersion

    Start-SyntheticMcp
    Add-Step -Name "synthetic_mcp_start" -Status "passed" -Detail "http://127.0.0.1:$SyntheticPort/mcp"

    $beforeService = Get-ServiceSnapshot
    $beforeData = Get-DataSnapshot
    $beforeFingerprint = Get-ConfigurationFingerprint
    $beforeConnectorState = if ($beforeService -and $beforeService.state -eq "Running") { Get-OptionalConnectorState } else { $null }
    $Evidence.before = [ordered]@{
        service = $beforeService
        data = $beforeData
        configuration_fingerprint = $beforeFingerprint
        connector_state = $beforeConnectorState
    }
    Add-Step -Name "before_snapshot" -Status "passed" -Detail "service=$([bool]$beforeService); data_files=$($beforeData.files.Count)"

    $stateBackup = New-TestStateBackup
    Add-Step -Name "protected_state_backup" -Status "passed" -Detail $stateBackup.path

    Invoke-PackageInstaller
    $service = Wait-ServiceState -Expected "Running"
    $afterService = Get-ServiceSnapshot
    $Evidence.after_install.service = $afterService
    Add-Step -Name "install_and_start_service" -Status "passed" -Detail "$($service.Status); pid=$($afterService.process_id)"
    if ($UseSetup) {
        $Evidence.after_install.native_layout = Assert-NativeWindowsInstall -ServiceSnapshot $afterService
        Add-Step -Name "native_x64_layout" -Status "passed" -Detail "Program Files, service path, registry, loopback listener"
    } else {
        Add-Step -Name "native_x64_layout" -Status "not_applicable" -Detail "portable install.ps1 does not register uninstall entry"
    }

    $installState = Join-Path $DataDir "install-state.json"
    Assert-PathPresent -Path $installState -Label "安装状态文件"
    if ((Get-Item -LiteralPath $installState).Length -gt 65536) {
        throw "安装状态文件异常膨胀，可能写入了 PowerShell 管道包装对象"
    }
    $installStateJson = Get-Content -LiteralPath $installState -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($installStateJson.version -isnot [string]) {
        throw "安装状态 version 字段不是字符串"
    }
    $expectedInstallVersion = "开物内网连接器 $ExpectedVersion"
    if ($installStateJson.version -ne $expectedInstallVersion) {
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

    $wrongBearer = "windows-ci-wrong"
    $wrongConfig = '{"mcpServers":{"ci-local":{"type":"http","url":"http://127.0.0.1:18765/mcp","headers":{"Authorization":"Bearer windows-ci-wrong"}}}}'
    $wrongImportBody = @{ config = $wrongConfig } | ConvertTo-Json -Compress
    $wrongImport = Invoke-RestMethod -Uri "$BaseURL/api/import" -Method Post `
        -Headers @{ "X-Connector-Token" = $token } -ContentType "application/json" `
        -Body $wrongImportBody -TimeoutSec 15
    if (-not $wrongImport.ok -or @($wrongImport.data.issues | Where-Object { $_.severity -eq "error" }).Count -gt 0) {
        throw "错误 Bearer 测试配置导入失败"
    }
    $wrongSourceID = [string](@($wrongImport.data.sources)[0].source_id)
    if ([string]::IsNullOrWhiteSpace($wrongSourceID)) { throw "错误 Bearer 测试没有取得来源编号" }
    $wrongDiagnoseBody = @{ source_id = $wrongSourceID; tool_probe = "" } | ConvertTo-Json -Compress
    $wrongDiagnosis = Invoke-RestMethod -Uri "$BaseURL/api/diagnose" -Method Post `
        -Headers @{ "X-Connector-Token" = $token } -ContentType "application/json" `
        -Body $wrongDiagnoseBody -TimeoutSec 30
    $wrongReport = @($wrongDiagnosis.data | Where-Object { $_.source_id -eq $wrongSourceID })[0]
    if ($null -eq $wrongReport -or $wrongReport.ok) { throw "错误 Bearer 不应通过上游认证" }
    if (@($wrongReport.checks | Where-Object { $_.code -eq "UPSTREAM_AUTH_FAILED" }).Count -eq 0) {
        throw "错误 Bearer 没有返回认证失败结果"
    }
    $wrongAuthEvent = @(Get-SyntheticMcpEvents | Where-Object {
        $_.event -eq "request" -and $_.method -eq "initialize" -and $_.http_status -eq 401 -and $_.bearer_ok -eq $false
    })
    if ($wrongAuthEvent.Count -eq 0) { throw "合成 MCP 没有记录错误 Bearer 的 HTTP 401" }
    Add-Step -Name "wrong_bearer_rejected" -Status "passed" -Detail "initialize HTTP 401; UPSTREAM_AUTH_FAILED"

    $mcpConfig = '{"mcpServers":{"ci-local":{"type":"http","url":"http://127.0.0.1:18765/mcp","headers":{"Authorization":"Bearer windows-ci-secret"}}}}'
    $importBody = @{ config = $mcpConfig } | ConvertTo-Json -Compress
    $importResult = Invoke-RestMethod -Uri "$BaseURL/api/import" -Method Post `
        -Headers @{ "X-Connector-Token" = $token } -ContentType "application/json" `
        -Body $importBody -TimeoutSec 15
    if (-not $importResult.ok -or @($importResult.data.issues | Where-Object { $_.severity -eq "error" }).Count -gt 0) {
        throw "测试来源导入失败"
    }
    $sourceID = [string](@($importResult.data.sources)[0].source_id)
    if ([string]::IsNullOrWhiteSpace($sourceID)) { throw "测试来源导入后没有来源编号" }
    foreach ($name in @("config.json", "secrets.json")) {
        Assert-PathPresent -Path (Join-Path $DataDir $name) -Label "导入后本机文件 $name"
    }
    Assert-ConnectorState -Expected "CONFIGURING" -Context "测试来源导入" | Out-Null
    Add-Step -Name "import_and_local_storage" -Status "passed" -Detail $DataDir

    $authProfileBody = @{
        source_id = $sourceID
        app_id = $SyntheticAppID
        channel = $SyntheticChannel
        user_id = $SyntheticUserID
        hmac_key = $SyntheticHmacKey
        tools = $SyntheticTool
    } | ConvertTo-Json -Compress
    $authProfileResult = Invoke-RestMethod -Uri "$BaseURL/api/auth-profile" -Method Post `
        -Headers @{ "X-Connector-Token" = $token } -ContentType "application/json" `
        -Body $authProfileBody -TimeoutSec 15
    if (-not $authProfileResult.ok) { throw "本机动态认证配置保存失败" }
    $configuredProfile = @($authProfileResult.data.auth_profiles | Where-Object { $_.source_id -eq $sourceID })[0]
    if (-not $configuredProfile.configured -or $SyntheticTool -notin @($configuredProfile.allowed_tools)) {
        throw "动态认证配置或工具允许列表没有保存"
    }
    Add-Step -Name "dynamic_auth_profile" -Status "passed" -Detail $SyntheticTool

    $diagnoseBody = @{ source_id = $sourceID; tool_probe = $SyntheticTool } | ConvertTo-Json -Compress
    $diagnosis = Invoke-RestMethod -Uri "$BaseURL/api/diagnose" -Method Post `
        -Headers @{ "X-Connector-Token" = $token } -ContentType "application/json" `
        -Body $diagnoseBody -TimeoutSec 30
    $report = @($diagnosis.data | Where-Object { $_.source_id -eq $sourceID })[0]
    if ($null -eq $report -or -not $report.ok) { throw "合成 MCP 分层诊断没有通过" }
    $toolCheck = @($report.checks | Where-Object { $_.name -eq "tool" })[0]
    if ($null -eq $toolCheck -or $toolCheck.status -ne "pass") { throw "合成工具试调用没有通过" }
    $validCalls = @(Get-SyntheticMcpEvents | Where-Object {
        $_.event -eq "request" -and $_.method -eq "tools/call" -and $_.http_status -eq 200 -and
        $_.bearer_ok -eq $true -and $_.dynamic_headers_ok -eq $true -and $_.returned_marker -eq $SyntheticResult
    })
    if ($validCalls.Count -eq 0) {
        throw "没有记录到 Bearer、六项 HMAC 头和固定业务结果均正确的 tools/call"
    }
    $Evidence.synthetic_mcp = [ordered]@{
        source_id = $sourceID
        tool = $SyntheticTool
        fixed_result = $SyntheticResult
        hmac_key_length = $SyntheticHmacKey.Length
        wrong_bearer_rejected = $true
        hmac_verified_by_server = $true
        diagnosis = $report
        successful_calls = @($validCalls | ForEach-Object {
            [ordered]@{ method = $_.method; http_status = $_.http_status; returned_marker = $_.returned_marker }
        })
    }
    Add-Step -Name "synthetic_mcp_call" -Status "passed" -Detail "$SyntheticTool -> $SyntheticResult; Bearer/HMAC verified"

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

    if ($UseSetup) {
        Invoke-PackageUninstaller
        Wait-ServiceState -Expected "Absent" | Out-Null
        Assert-PathAbsent -Path $InstallDir -Label "NSIS 默认卸载后的程序目录"
        Assert-PathPresent -Path $DataDir -Label "NSIS 默认卸载后的数据目录"
        Assert-PathPresent -Path (Join-Path $DataDir "config.json") -Label "NSIS 默认卸载后配置"
        Assert-PathPresent -Path (Join-Path $DataDir "secrets.json") -Label "NSIS 默认卸载后本机身份"
        if (Test-RegistryKey -Path $UninstallKey -View 64) { throw "NSIS 卸载后仍有 64 位卸载登记" }
        if (Test-RegistryKey -Path $UninstallKey -View 32) { throw "NSIS 卸载后仍有 32 位卸载登记" }
        Add-Step -Name "uninstall_default_preserves_data" -Status "passed" -Detail "service/program/registry removed; data retained"

        Invoke-PackageInstaller
        $reinstalledService = Wait-ServiceState -Expected "Running"
        Assert-ConnectorState -Expected "CONFIGURING" -Context "NSIS 卸载后重新安装" | Out-Null
        $Evidence.after_reinstall = [ordered]@{
            service = Get-ServiceSnapshot
            native_layout = Assert-NativeWindowsInstall -ServiceSnapshot (Get-ServiceSnapshot)
        }
        Add-Step -Name "reinstall_after_uninstall" -Status "passed" -Detail "NSIS setup; configuration retained; x64 layout verified"
    } else {
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
    }
    $finalService = Wait-ServiceState -Expected "Running"
    Assert-ConnectorState -Expected "CONFIGURING" -Context "最终恢复安装" | Out-Null
    Assert-PrivateAcl -Path $DataDir
    if ($UseSetup) {
        $Evidence.final_native_layout = Assert-NativeWindowsInstall -ServiceSnapshot (Get-ServiceSnapshot)
    }
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
    Stop-SyntheticMcp
    $restoreOk = Restore-TestStateAndRecord
    Save-Evidence
    if (-not $restoreOk) {
        Write-Error "测试主体完成，但测试前配置或服务状态恢复失败；受保护备份已保留。"
        exit 1
    }
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
        $Evidence.synthetic_mcp.events = @(Get-SyntheticMcpEvents)
    } catch {}
    Stop-SyntheticMcp
    $restoreOk = Restore-TestStateAndRecord
    Save-Evidence
    if (-not $restoreOk) {
        Write-Error "测试失败，并且测试前配置或服务状态恢复也失败；受保护备份已保留。"
    }
    Write-Error $_
    exit 1
}
