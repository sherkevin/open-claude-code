# 开物内网连接器 Windows 安装脚本。
# 用法：以管理员身份运行 PowerShell：.\install.ps1
#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"

$Source = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallDir = Join-Path $env:ProgramFiles "Kaiwu\Connector"
$DataDir = Join-Path $env:ProgramData "Kaiwu\Connector"
$ServiceName = "KaiwuConnector"
$Listen = "127.0.0.1:17872"

$ChecksumFile = Join-Path $Source "SHA256SUMS"
if (-not (Test-Path $ChecksumFile)) {
  throw "安装包缺少 SHA256SUMS，拒绝安装"
}
foreach ($line in [System.IO.File]::ReadAllLines($ChecksumFile, [System.Text.Encoding]::UTF8)) {
  if ($line -notmatch '^([0-9a-fA-F]{64})\s+(.+)$') { continue }
  $expected = $matches[1].ToLowerInvariant()
  $relative = $matches[2].TrimStart('.', '\', '/')
  $target = Join-Path $Source $relative
  if (-not (Test-Path $target)) { throw "安装包缺少文件: $relative" }
  $actual = (Get-FileHash -Path $target -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($actual -ne $expected) { throw "安装包文件校验失败: $relative" }
}

function Wait-ServiceAbsent {
  param([int]$TimeoutSeconds = 15)
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  do {
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($null -eq $service) { return }
    Start-Sleep -Milliseconds 500
  } while ((Get-Date) -lt $deadline)
  throw "服务 $ServiceName 未能及时删除"
}

function Set-PrivateDataAcl {
  param([Parameter(Mandatory = $true)][string]$Path)

  & icacls.exe $Path /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" /T /C | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "设置数据目录 ACL 失败，icacls 退出码为 ${LASTEXITCODE}: $Path"
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

  $verifiedItems = @(Get-Item -LiteralPath $Path -Force) + @(Get-ChildItem -LiteralPath $Path -Force -Recurse -ErrorAction Stop)
  foreach ($verifiedItem in $verifiedItems) {
    $verified = Get-Acl -LiteralPath $verifiedItem.FullName
    if (-not $verified.AreAccessRulesProtected) {
      throw "数据目录 ACL 仍会继承上级权限: $($verifiedItem.FullName)"
    }
    foreach ($rule in @($verified.Access)) {
      $sid = $rule.IdentityReference.Translate(
        [System.Security.Principal.SecurityIdentifier]
      ).Value
      if ($sid -notin @("S-1-5-18", "S-1-5-32-544")) {
        throw "数据目录 ACL 含有未授权主体 ${sid}: $($verifiedItem.FullName)"
      }
    }
  }
}

function Wait-ServiceRunning {
  param([int]$TimeoutSeconds = 45)
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  do {
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($null -ne $service -and $service.Status -eq "Running") { return $service }
    Start-Sleep -Milliseconds 500
  } while ((Get-Date) -lt $deadline)
  throw "服务 $ServiceName 未在 $TimeoutSeconds 秒内进入 Running 状态"
}

function Stop-AndDeleteService {
  $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
  if (-not $service) { return }
  Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
  sc.exe delete $ServiceName | Out-Null
  Wait-ServiceAbsent
}

function Get-ServiceStartupType {
  param([string]$StartMode)
  switch ($StartMode) {
    "Auto" { return "Automatic" }
    "Manual" { return "Manual" }
    "Disabled" { return "Disabled" }
    default { return "Automatic" }
  }
}

$SensitiveFiles = @(
  "identity.json",
  "device_key.json",
  "secrets.json",
  "auth_profiles.json",
  "config.json",
  "frpc.toml",
  "state.json",
  "revoked.json"
)
$ManagedEnvNames = @("KAIWU_CLOUD_BASE", "KAIWU_EDGE_BASE")
$ExistingService = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction SilentlyContinue
$HadExistingService = $null -ne $ExistingService
$InstallDirExisted = Test-Path -LiteralPath $InstallDir
$ExistingSensitive = @($SensitiveFiles | ForEach-Object { Join-Path $DataDir $_ } | Where-Object { Test-Path -LiteralPath $_ })
$ExistingBinaries = @("kaiwu-connector.exe", "frpc.exe") | ForEach-Object { Join-Path $InstallDir $_ } | Where-Object { Test-Path -LiteralPath $_ }
$BackupDir = $null
$EnvironmentBefore = @{}
$InstalledByThisRun = $false

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
Set-PrivateDataAcl -Path $DataDir
foreach ($name in $ManagedEnvNames) {
  $EnvironmentBefore[$name] = [Environment]::GetEnvironmentVariable($name, "Machine")
}

$NeedsBackup = $HadExistingService -or ($ExistingSensitive.Count -gt 0) -or ($ExistingBinaries.Count -gt 0)
if ($NeedsBackup) {
  $BackupDir = Join-Path $DataDir ("backups\" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-pre-upgrade")
  New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
  New-Item -ItemType Directory -Force -Path (Join-Path $BackupDir "program") | Out-Null
  foreach ($sourceFile in $ExistingSensitive) {
    Copy-Item -LiteralPath $sourceFile -Destination $BackupDir -Force
  }
  foreach ($binary in $ExistingBinaries) {
    Copy-Item -LiteralPath $binary -Destination (Join-Path $BackupDir "program") -Force
  }
  $ServiceSnapshot = $null
  if ($HadExistingService) {
    $ServiceSnapshot = [ordered]@{
      name = $ServiceName
      display_name = $ExistingService.DisplayName
      binary_path = $ExistingService.PathName
      start_mode = $ExistingService.StartMode
      delayed_auto_start = [bool]$ExistingService.DelayedAutoStart
      was_running = $ExistingService.State -eq "Running"
    }
    $ServiceSnapshot | ConvertTo-Json -Depth 4 | Set-Content -Path (Join-Path $BackupDir "service.json") -Encoding UTF8
  }
  $EnvironmentBefore | ConvertTo-Json -Depth 4 | Set-Content -Path (Join-Path $BackupDir "environment.json") -Encoding UTF8
  [System.IO.File]::WriteAllText(
    (Join-Path $BackupDir "backup-info.txt"),
    "created_at=$((Get-Date).ToUniversalTime().ToString("o"))`r`nprevious_install=$InstallDir`r`nservice=$ServiceName`r`nrestore_script=restore.ps1`r`n",
    [System.Text.Encoding]::UTF8
  )
  $RestoreScript = @'
# 仅供本次安装前备份使用。由管理员在 PowerShell 中运行。
$ErrorActionPreference = "Stop"
$BackupDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServiceName = "KaiwuConnector"
$InstallDir = Join-Path $env:ProgramFiles "Kaiwu\Connector"
$DataDir = Join-Path $env:ProgramData "Kaiwu\Connector"
$snapshotPath = Join-Path $BackupDir "service.json"
if (-not (Test-Path -LiteralPath $snapshotPath)) { throw "备份缺少 service.json" }
$snapshot = Get-Content -LiteralPath $snapshotPath -Encoding UTF8 | ConvertFrom-Json
$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($service) {
  Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
  sc.exe delete $ServiceName | Out-Null
  $deadline = (Get-Date).AddSeconds(15)
  do {
    Start-Sleep -Milliseconds 500
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
  } while ($null -ne $service -and (Get-Date) -lt $deadline)
  if ($null -ne $service) { throw "旧服务无法恢复：当前服务未能删除" }
}
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item -Path (Join-Path $BackupDir "program\*.exe") -Destination $InstallDir -Force
foreach ($file in Get-ChildItem -LiteralPath $BackupDir -File) {
  if ($file.Name -in @("service.json", "environment.json", "backup-info.txt", "restore.ps1")) { continue }
  Copy-Item -LiteralPath $file.FullName -Destination $DataDir -Force
}
$environment = Get-Content -LiteralPath (Join-Path $BackupDir "environment.json") -Encoding UTF8 | ConvertFrom-Json
foreach ($property in $environment.PSObject.Properties) {
  [Environment]::SetEnvironmentVariable($property.Name, $property.Value, "Machine")
}
$startupType = switch ($snapshot.start_mode) {
  "Auto" { "Automatic" }
  "Manual" { "Manual" }
  "Disabled" { "Disabled" }
  default { "Automatic" }
}
$displayName = $snapshot.display_name
if ([string]::IsNullOrWhiteSpace($displayName)) { $displayName = "Kaiwu Intranet Connector" }
New-Service -Name $ServiceName -BinaryPathName $snapshot.binary_path -StartupType $startupType -DisplayName $displayName -ErrorAction Stop | Out-Null
if ($snapshot.delayed_auto_start -and $startupType -eq "Automatic") {
  sc.exe config $ServiceName start= delayed-auto | Out-Null
}
if ($snapshot.was_running) { Start-Service -Name $ServiceName }
Write-Host "已恢复安装前版本。"
'@
  [System.IO.File]::WriteAllText(
    (Join-Path $BackupDir "restore.ps1"),
    $RestoreScript,
    [System.Text.UTF8Encoding]::new($true)
  )
  Set-PrivateDataAcl -Path $BackupDir
  Write-Host "升级前程序、配置和本机秘密已备份到 $BackupDir"
}

$OriginalError = $null
try {
  Stop-AndDeleteService

  New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
  New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir "docs") | Out-Null
  New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir "compliance") | Out-Null

  Copy-Item (Join-Path $Source "bin\kaiwu-connector.exe") $InstallDir -Force
  Copy-Item (Join-Path $Source "bin\frpc.exe") $InstallDir -Force
  $InstalledByThisRun = $true
  Get-ChildItem (Join-Path $Source "docs\*.md") -ErrorAction SilentlyContinue |
    Copy-Item -Destination (Join-Path $InstallDir "docs") -Force
  Get-ChildItem (Join-Path $Source "compliance\*") -File -ErrorAction SilentlyContinue |
    Copy-Item -Destination (Join-Path $InstallDir "compliance") -Force
  foreach ($name in @("uninstall.ps1", "windows-smoke.ps1", "install.ps1", "版本信息.txt")) {
    $item = Join-Path $Source $name
    if (Test-Path -LiteralPath $item) {
      Copy-Item -LiteralPath $item -Destination $InstallDir -Force
    }
  }

  # 空白模板不应在升级时清掉客户已经填写的云端地址。
  $EnvFile = Join-Path $Source "config\connector.env.example"
  if (Test-Path $EnvFile) {
    $envText = [System.IO.File]::ReadAllText($EnvFile, [System.Text.Encoding]::UTF8)
    foreach ($line in ($envText -split "`r?`n")) {
      if ($line -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$') {
        $name = $matches[1]
        $value = $matches[2].Trim()
        if ([string]::IsNullOrWhiteSpace($value)) {
          # Keep a non-empty machine value even when the service is currently
          # absent. This is the normal case after a default uninstall, which
          # deliberately preserves configuration for a later reinstall.
          if (-not [string]::IsNullOrWhiteSpace($EnvironmentBefore[$name])) {
            Write-Host "保留已有环境变量 $name"
          } else {
            [Environment]::SetEnvironmentVariable($name, $null, "Machine")
            Remove-Item -Path ("Env:" + $name) -ErrorAction SilentlyContinue
            Write-Host "已清空未配置的环境变量 $name"
          }
        } else {
          [Environment]::SetEnvironmentVariable($name, $value, "Machine")
          Set-Item -Path ("Env:" + $name) -Value $value
          $check = [Environment]::GetEnvironmentVariable($name, "Machine")
          if ($check -ne $value) { throw "写入机器级环境变量失败: $name" }
          Write-Host "已设置环境变量 $name"
        }
      }
    }
  }

  $Binary = Join-Path $InstallDir "kaiwu-connector.exe"
  $BinPath = "`"$Binary`" serve --data-dir `"$DataDir`" --listen $Listen"

  # 使用 New-Service 避免 sc.exe 在 Windows PowerShell 5.1 下拆分带空格的 binPath。
  try {
    New-Service -Name $ServiceName -BinaryPathName $BinPath -StartupType Automatic -DisplayName "Kaiwu Intranet Connector" -ErrorAction Stop | Out-Null
  } catch {
    throw "注册服务失败: $($_.Exception.Message)"
  }

  sc.exe sidtype $ServiceName unrestricted | Out-Null
  sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/15000/restart/60000 | Out-Null

  try {
    Start-Service -Name $ServiceName -ErrorAction Stop
  } catch {
    $events = Get-WinEvent -FilterHashtable @{LogName='System'; StartTime=(Get-Date).AddMinutes(-5)} -ErrorAction SilentlyContinue |
      Where-Object { $_.Message -match 'Kaiwu|1053|7000|7009' } |
      Select-Object -First 5 |
      Format-List | Out-String
    throw "启动服务失败: $($_.Exception.Message)`n$events"
  }

  $service = Wait-ServiceRunning
  $HttpDeadline = (Get-Date).AddSeconds(30)
  do {
    try {
      $response = Invoke-WebRequest -Uri "http://$Listen/" -UseBasicParsing -TimeoutSec 2
      $httpReady = $response.StatusCode -eq 200
    } catch {
      $httpReady = $false
    }
    if (-not $httpReady) { Start-Sleep -Milliseconds 500 }
  } while (-not $httpReady -and (Get-Date) -lt $HttpDeadline)
  if (-not $httpReady) {
    throw "服务已经启动，但本机配置页面 http://$Listen/ 未在 30 秒内返回 200"
  }

  $VersionLine = "unknown"
  $VersionFile = Join-Path $Source "版本信息.txt"
  if (Test-Path -LiteralPath $VersionFile) {
    $VersionLine = (Get-Content -LiteralPath $VersionFile -Encoding UTF8 | Select-Object -First 1)
  }
  [ordered]@{
    version = $VersionLine
    installed_at = (Get-Date).ToUniversalTime().ToString("o")
    install_dir = $InstallDir
    data_dir = $DataDir
    previous_backup = $BackupDir
    source = "windows-installer"
  } | ConvertTo-Json -Depth 4 | Set-Content -Path (Join-Path $DataDir "install-state.json") -Encoding UTF8

  Write-Host ""
  Write-Host "安装完成。"
  Write-Host "服务名称：$ServiceName（$($service.Status)）"
  Write-Host "本机配置界面：http://$Listen/"
  Write-Host "数据目录：$DataDir"
  Write-Host "注意：界面只监听本机回环地址，不要通过防火墙对外开放。"
} catch {
  $OriginalError = $_.Exception.Message
  $RollbackErrors = @()
  try {
    Stop-AndDeleteService
  } catch {
    $RollbackErrors += "删除失败的新服务: $($_.Exception.Message)"
  }

  if ($BackupDir) {
    try {
      if (Test-Path -LiteralPath (Join-Path $BackupDir "program")) {
        New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
        Get-ChildItem -LiteralPath (Join-Path $BackupDir "program") -File -ErrorAction SilentlyContinue |
          Copy-Item -Destination $InstallDir -Force
      }
      foreach ($sourceFile in $ExistingSensitive) {
        $backupFile = Join-Path $BackupDir (Split-Path -Leaf $sourceFile)
        if (Test-Path -LiteralPath $backupFile) {
          Copy-Item -LiteralPath $backupFile -Destination $DataDir -Force
        }
      }
      foreach ($name in $ManagedEnvNames) {
        [Environment]::SetEnvironmentVariable($name, $EnvironmentBefore[$name], "Machine")
      }
    } catch {
      $RollbackErrors += "恢复旧文件和配置: $($_.Exception.Message)"
    }
  } elseif ($InstalledByThisRun -and -not $InstallDirExisted) {
    try {
      Remove-Item -LiteralPath $InstallDir -Recurse -Force -Confirm:$false
    } catch {
      $RollbackErrors += "清理失败安装目录: $($_.Exception.Message)"
    }
  }

  if ($HadExistingService) {
    try {
      $snapshot = Get-Content -LiteralPath (Join-Path $BackupDir "service.json") -Encoding UTF8 | ConvertFrom-Json
      $startupType = Get-ServiceStartupType $snapshot.start_mode
      $displayName = $snapshot.display_name
      if ([string]::IsNullOrWhiteSpace($displayName)) { $displayName = "Kaiwu Intranet Connector" }
      New-Service -Name $ServiceName -BinaryPathName $snapshot.binary_path -StartupType $startupType -DisplayName $displayName -ErrorAction Stop | Out-Null
      if ($snapshot.delayed_auto_start -and $startupType -eq "Automatic") {
        sc.exe config $ServiceName start= delayed-auto | Out-Null
      }
      if ($snapshot.was_running) { Start-Service -Name $ServiceName; Wait-ServiceRunning | Out-Null }
      Write-Host "安装失败，已恢复安装前服务：$ServiceName"
    } catch {
      $RollbackErrors += "恢复旧服务: $($_.Exception.Message)"
    }
  }

  $RollbackDetail = if ($RollbackErrors.Count -eq 0) { "已自动恢复安装前状态。" } else { "自动恢复存在错误：" + ($RollbackErrors -join "; ") }
  throw "安装失败：$OriginalError`n$RollbackDetail`n备份目录：$BackupDir"
}
