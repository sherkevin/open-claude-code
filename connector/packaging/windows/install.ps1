# 开物内网连接器 Windows 安装脚本。
# 用法：以管理员身份运行 PowerShell：.\install.ps1
#Requires -RunAsAdministrator
param(
  [string]$ResultPath = ""
)
$ErrorActionPreference = "Stop"

# 平台调用保留可替换接缝，故障注入测试可在内存中模拟机器级环境变量。
# 正常安装没有这些函数时，仍直接使用 .NET 的 Machine 作用域 API。
if (-not (Get-Command Get-KaiwuMachineEnvironmentVariable -CommandType Function -ErrorAction SilentlyContinue)) {
  function Get-KaiwuMachineEnvironmentVariable {
    param([Parameter(Mandatory = $true)][string]$Name)
    return [Environment]::GetEnvironmentVariable($Name, "Machine")
  }
}
if (-not (Get-Command Set-KaiwuMachineEnvironmentVariable -CommandType Function -ErrorAction SilentlyContinue)) {
  function Set-KaiwuMachineEnvironmentVariable {
    param(
      [Parameter(Mandatory = $true)][string]$Name,
      [AllowNull()][AllowEmptyString()][string]$Value
    )
    [Environment]::SetEnvironmentVariable($Name, $Value, "Machine")
  }
}

$Source = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallDir = Join-Path $env:ProgramFiles "Kaiwu\Connector"
$DataDir = Join-Path $env:ProgramData "Kaiwu\Connector"
$ServiceName = "KaiwuConnector"
$Listen = "127.0.0.1:17872"
if ([string]::IsNullOrWhiteSpace($ResultPath)) {
  $ResultPath = Join-Path $env:TEMP "KaiwuConnector-install-result.txt"
}

$Stage = "startup"
$OldInstallChanged = $false
$InstallMutationStarted = $false
$Cleanup = "not_needed"
$BackupDir = $null
$BackupManifestPath = $null
$BackupManifestHash = $null
$BackupAvailable = $false
$BackupEntries = @()
$FrozenHashes = @{}
$CreatedManagedFiles = [System.Collections.Generic.List[string]]::new()
$CreatedManagedDirectories = [System.Collections.Generic.List[string]]::new()
$CriticalFiles = @(
  [pscustomobject]@{ SourceRelative = "bin/kaiwu-connector.exe"; TargetName = "kaiwu-connector.exe" },
  [pscustomobject]@{ SourceRelative = "bin/frpc.exe"; TargetName = "frpc.exe" }
)

function Write-InstallResult {
  param([string]$ResultCode)
  $allowed = @(
    "INSTALLED",
    "PRECHECK_FAILED",
    "FAILED_RESTORED",
    "FAILED_RECOVERY_UNCONFIRMED",
    "FAILED_NO_PRIOR_CLEANED",
    "FAILED_NO_PRIOR_CLEANUP_UNCONFIRMED"
  )
  if ($ResultCode -notin $allowed) {
    $ResultCode = "FAILED_RECOVERY_UNCONFIRMED"
  }
  $lines = @(
    "result=$ResultCode",
    "stage=$Stage",
    "old_install_changed=$($OldInstallChanged.ToString().ToLowerInvariant())",
    "backup_available=$($BackupAvailable.ToString().ToLowerInvariant())",
    "cleanup=$Cleanup"
  )
  if ($BackupDir) {
    $lines += "backup_dir=$BackupDir"
  }
  try {
    $parent = Split-Path -Parent $ResultPath
    if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    [System.IO.File]::WriteAllText(
      $ResultPath,
      (($lines -join "`r`n") + "`r`n"),
      [System.Text.UTF8Encoding]::new($false)
    )
  } catch {
    Write-Host "无法写入安装结果状态: $($_.Exception.Message)"
  }
}

function Get-FileSha256 {
  param([string]$Path)
  return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-CriticalFiles {
  param([switch]$Target)
  foreach ($item in $CriticalFiles) {
    if ($Target) {
      $path = Join-Path $InstallDir $item.TargetName
    } else {
      $path = Join-Path $Source ($item.SourceRelative -replace '/', '\')
    }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
      throw "关键安装文件缺失: $($item.SourceRelative)"
    }
    $expected = $FrozenHashes[$item.SourceRelative]
    if (-not $expected) {
      throw "SHA256SUMS 缺少关键文件: $($item.SourceRelative)"
    }
    if ((Get-FileSha256 $path) -ne $expected) {
      throw "关键安装文件校验失败: $($item.SourceRelative)"
    }
  }
}

$Stage = "source_precheck"
try {
  $ChecksumFile = Join-Path $Source "SHA256SUMS"
  if (-not (Test-Path -LiteralPath $ChecksumFile -PathType Leaf)) {
    throw "安装包缺少 SHA256SUMS，拒绝安装"
  }
  foreach ($line in [System.IO.File]::ReadAllLines($ChecksumFile, [System.Text.Encoding]::UTF8)) {
    if ($line -notmatch '^([0-9a-fA-F]{64})\s+(.+)$') { continue }
    $expected = $matches[1].ToLowerInvariant()
    $relative = ($matches[2].Trim() -replace '\\', '/')
    if ($relative.StartsWith("./")) { $relative = $relative.Substring(2) }
    if (
      [string]::IsNullOrWhiteSpace($relative) -or
      [System.IO.Path]::IsPathRooted($relative) -or
      $relative -match '(^|/)\.\.?(/|$)'
    ) {
      throw "SHA256SUMS 含非法路径"
    }
    $FrozenHashes[$relative] = $expected
    $target = Join-Path $Source ($relative -replace '/', '\')
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { throw "安装包缺少文件: $relative" }
    if ((Get-FileSha256 $target) -ne $expected) { throw "安装包文件校验失败: $relative" }
  }
  Assert-CriticalFiles
} catch {
  Write-InstallResult "PRECHECK_FAILED"
  throw
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

function Wait-ServiceStopped {
  param(
    [string]$Name = $ServiceName,
    [int]$TimeoutSeconds = 15
  )
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  do {
    $service = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if ($null -eq $service) {
      throw "服务 $Name 在停止确认前消失"
    }
    if ($service.Status -eq "Stopped") { return }
    Start-Sleep -Milliseconds 500
  } while ((Get-Date) -lt $deadline)
  throw "服务 $Name 未在 $TimeoutSeconds 秒内进入 Stopped 状态"
}

function Assert-ServiceStoppable {
  param([Parameter(Mandatory = $true)]$Service)
  $dependents = @($Service.DependentServices)
  if ($dependents.Count -gt 0) {
    $names = @($dependents | ForEach-Object { [string]$_.Name }) -join ", "
    throw "服务 $ServiceName 存在依赖服务（$names），拒绝停止；请先处理依赖服务"
  }
}

function Stop-TargetService {
  param([string]$Name = $ServiceName)
  $service = Get-Service -Name $Name -ErrorAction SilentlyContinue
  if ($null -eq $service) { return }
  Assert-ServiceStoppable $service
  if ($service.Status -eq "Stopped") { return }
  Stop-Service -Name $Name -ErrorAction Stop
  Wait-ServiceStopped -Name $Name
}

function Stop-AndDeleteService {
  $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
  if (-not $service) { return }
  Assert-ServiceStoppable $service
  Stop-TargetService
  & sc.exe delete $ServiceName | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "删除服务 $ServiceName 失败：sc.exe 退出码为 $LASTEXITCODE"
  }
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

function Write-Utf8Text {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$Content
  )
  [System.IO.File]::WriteAllText(
    $Path,
    $Content,
    [System.Text.UTF8Encoding]::new($false)
  )
}

function Ensure-ManagedDirectory {
  param([Parameter(Mandatory = $true)][string]$Path)
  if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
    $script:CreatedManagedDirectories.Add($Path)
  }
}

function Copy-ManagedFile {
  param(
    [Parameter(Mandatory = $true)][string]$SourcePath,
    [Parameter(Mandatory = $true)][string]$DestinationPath
  )
  if (-not (Test-Path -LiteralPath $DestinationPath -PathType Leaf)) {
    $script:CreatedManagedFiles.Add($DestinationPath)
  }
  Copy-Item -LiteralPath $SourcePath -Destination $DestinationPath -Force
}

function Remove-ManagedFilesCreatedByRun {
  $failures = @()
  for ($index = $script:CreatedManagedFiles.Count - 1; $index -ge 0; $index--) {
    $path = $script:CreatedManagedFiles[$index]
    try {
      if (Test-Path -LiteralPath $path -PathType Leaf) {
        Remove-Item -LiteralPath $path -Force -Confirm:$false
      }
    } catch {
      $failures += "删除本次新增文件失败: $path"
    }
  }
  for ($index = $script:CreatedManagedDirectories.Count - 1; $index -ge 0; $index--) {
    $path = $script:CreatedManagedDirectories[$index]
    try {
      if (Test-Path -LiteralPath $path -PathType Container) {
        $children = @(Get-ChildItem -LiteralPath $path -Force -ErrorAction Stop)
        if ($children.Count -eq 0) {
          Remove-Item -LiteralPath $path -Force -Confirm:$false
        }
      }
    } catch {
      $failures += "清理本次新增目录失败: $path"
    }
  }
  foreach ($path in @($script:CreatedManagedFiles | Select-Object -Unique)) {
    if (Test-Path -LiteralPath $path -PathType Leaf) {
      $failures += "本次新增文件仍存在: $path"
    }
  }
  if ($failures.Count -gt 0) {
    throw ($failures -join "; ")
  }
}

function New-BackupEntry {
  param(
    [Parameter(Mandatory = $true)][string]$Category,
    [Parameter(Mandatory = $true)][string]$RelativePath,
    [Parameter(Mandatory = $true)][string]$BackupRelativePath
  )
  $payloadPath = Join-Path $script:BackupDir ($BackupRelativePath -replace '/', '\')
  return [ordered]@{
    category = $Category
    relative_path = $RelativePath
    backup_relative_path = $BackupRelativePath
    sha256 = (Get-FileSha256 $payloadPath)
  }
}

function Get-BackupPayloadPath {
  param([Parameter(Mandatory = $true)][string]$BackupRelativePath)
  if (
    [System.IO.Path]::IsPathRooted($BackupRelativePath) -or
    $BackupRelativePath -match '(^|[\\/])\.\.([\\/]|$)'
  ) {
    throw "备份清单包含非法路径"
  }
  return (Join-Path $script:BackupDir ($BackupRelativePath -replace '/', '\'))
}

function Get-BackupTargetPath {
  param([Parameter(Mandatory = $true)]$Entry)
  $relativePath = [string]$Entry.relative_path
  if (
    [string]::IsNullOrWhiteSpace($relativePath) -or
    [System.IO.Path]::IsPathRooted($relativePath) -or
    $relativePath -match '[/\\]' -or
    $relativePath -in @(".", "..")
  ) {
    throw "备份清单包含非法恢复目标"
  }
  switch ([string]$Entry.category) {
    "program" { return (Join-Path $script:InstallDir $relativePath) }
    "data" { return (Join-Path $script:DataDir $relativePath) }
    default { throw "备份清单包含未知目标类别" }
  }
}

function Assert-BackupIntact {
  $script:BackupAvailable = $false
  if (
    [string]::IsNullOrWhiteSpace([string]$script:BackupManifestPath) -or
    [string]::IsNullOrWhiteSpace([string]$script:BackupManifestHash) -or
    -not (Test-Path -LiteralPath $script:BackupManifestPath -PathType Leaf)
  ) {
    $script:BackupAvailable = $false
    throw "恢复未确认：备份清单缺失"
  }
  if ((Get-FileSha256 $script:BackupManifestPath) -ne $script:BackupManifestHash) {
    $script:BackupAvailable = $false
    throw "恢复未确认：备份清单摘要不匹配"
  }
  try {
    $manifest = Get-Content -LiteralPath $script:BackupManifestPath -Raw -Encoding UTF8 |
      ConvertFrom-Json
  } catch {
    $script:BackupAvailable = $false
    throw "恢复未确认：备份清单无法解析"
  }
  if ([int]$manifest.schema_version -ne 1) {
    $script:BackupAvailable = $false
    throw "恢复未确认：备份清单版本不受支持"
  }
  $entries = @($manifest.entries)
  if ($entries.Count -eq 0 -or [int]$manifest.entry_count -ne $entries.Count) {
    $script:BackupAvailable = $false
    throw "恢复未确认：备份清单内容不完整"
  }
  $seen = @{}
  foreach ($entry in $entries) {
    $category = [string]$entry.category
    $relativePath = [string]$entry.relative_path
    $backupRelativePath = [string]$entry.backup_relative_path
    $expected = [string]$entry.sha256
    if ($category -notin @("program", "data", "metadata")) {
      $script:BackupAvailable = $false
      throw "恢复未确认：备份清单包含未知目标类别"
    }
    if (
      [string]::IsNullOrWhiteSpace($relativePath) -or
      $relativePath -match '[/\\]' -or
      $relativePath -in @(".", "..")
    ) {
      $script:BackupAvailable = $false
      throw "恢复未确认：备份清单包含非法恢复目标"
    }
    if ($category -eq "program" -and $backupRelativePath -notmatch '^program/[^/]+$') {
      $script:BackupAvailable = $false
      throw "恢复未确认：程序备份路径不匹配"
    }
    if ($category -eq "data" -and $backupRelativePath -match '[/\\]') {
      $script:BackupAvailable = $false
      throw "恢复未确认：配置备份路径不匹配"
    }
    if ($category -eq "metadata" -and $backupRelativePath -match '[/\\]') {
      $script:BackupAvailable = $false
      throw "恢复未确认：备份元数据路径不匹配"
    }
    if ($expected -notmatch '^[0-9a-f]{64}$') {
      $script:BackupAvailable = $false
      throw "恢复未确认：备份文件摘要格式不正确"
    }
    $key = $category + ":" + $relativePath
    if ($seen.ContainsKey($key)) {
      $script:BackupAvailable = $false
      throw "恢复未确认：备份清单包含重复项"
    }
    $seen[$key] = $true
    $payloadPath = Get-BackupPayloadPath $backupRelativePath
    if (-not (Test-Path -LiteralPath $payloadPath -PathType Leaf)) {
      $script:BackupAvailable = $false
      throw "恢复未确认：备份文件缺失"
    }
    if ((Get-FileSha256 $payloadPath) -ne $expected) {
      $script:BackupAvailable = $false
      throw "恢复未确认：备份文件摘要不匹配"
    }
  }
  $script:BackupAvailable = $true
  return $manifest
}

function Assert-RestoredFileDigests {
  param([Parameter(Mandatory = $true)]$Manifest)
  foreach ($entry in @($Manifest.entries)) {
    if ($entry.category -notin @("program", "data")) { continue }
    $targetPath = Get-BackupTargetPath $entry
    if (-not (Test-Path -LiteralPath $targetPath -PathType Leaf)) {
      throw "恢复后关键目标缺失"
    }
    if ((Get-FileSha256 $targetPath) -ne [string]$entry.sha256) {
      throw "恢复后关键目标摘要不匹配"
    }
  }
}

function Get-BackupMetadataEntry {
  param(
    [Parameter(Mandatory = $true)]$Manifest,
    [Parameter(Mandatory = $true)][string]$RelativePath
  )
  $entries = @(
    @($Manifest.entries) |
      Where-Object { $_.category -eq "metadata" -and $_.relative_path -eq $RelativePath }
  )
  if ($entries.Count -gt 1) {
    throw "恢复未确认：备份元数据包含重复项 ($RelativePath)"
  }
  if ($entries.Count -eq 0) { return $null }
  return $entries[0]
}

function Get-BackupServiceSnapshot {
  param([Parameter(Mandatory = $true)]$Manifest)
  $entry = Get-BackupMetadataEntry $Manifest "service.json"
  if ($null -eq $entry) { return $null }
  $snapshotPath = Get-BackupPayloadPath ([string]$entry.backup_relative_path)
  try {
    return (Get-Content -LiteralPath $snapshotPath -Raw -Encoding UTF8 | ConvertFrom-Json)
  } catch {
    throw "恢复未确认：服务快照无法解析"
  }
}

function Assert-RestoredStableState {
  param([Parameter(Mandatory = $true)]$Manifest)

  # state.json 是服务启动后会合法更新的运行态文件。启动前已经做过全量摘要校验，
  # 启动后只复核稳定程序和配置，避免把正常状态写入误判为恢复破坏。
  foreach ($entry in @($Manifest.entries)) {
    if ($entry.category -notin @("program", "data")) { continue }
    if ($entry.category -eq "data" -and $entry.relative_path -eq "state.json") { continue }
    $targetPath = Get-BackupTargetPath $entry
    if (-not (Test-Path -LiteralPath $targetPath -PathType Leaf)) {
      throw "恢复后稳定目标缺失: $($entry.relative_path)"
    }
    if ((Get-FileSha256 $targetPath) -ne [string]$entry.sha256) {
      throw "恢复后稳定目标摘要不匹配: $($entry.relative_path)"
    }
  }

  if ($script:HadExistingService) {
    foreach ($name in @("kaiwu-connector.exe", "frpc.exe")) {
      if (-not (Test-Path -LiteralPath (Join-Path $script:InstallDir $name) -PathType Leaf)) {
        throw "恢复后关键程序缺失: $name"
      }
    }
  }

  if (-not $script:HadExistingService) { return }
  $snapshot = Get-BackupServiceSnapshot $Manifest
  if ($null -eq $snapshot) { throw "恢复未确认：服务快照缺失" }
  if ([string]$snapshot.name -ne $script:ServiceName) {
    throw "恢复未确认：服务快照名称不匹配"
  }
  if ([string]::IsNullOrWhiteSpace([string]$snapshot.binary_path)) {
    throw "恢复未确认：服务快照缺少程序路径"
  }
  $service = Get-Service -Name $script:ServiceName -ErrorAction Stop
  if ($null -eq $service) { throw "恢复未确认：旧服务不存在" }
  if ([bool]$snapshot.was_running) {
    if ($service.Status -ne "Running") { throw "恢复未确认：旧服务未运行" }
  } elseif ($service.Status -eq "Running") {
    throw "恢复未确认：旧服务安装前未运行，恢复后却处于运行状态"
  }

  $serviceRecord = Get-CimInstance Win32_Service -Filter "Name='$script:ServiceName'" -ErrorAction Stop
  if ($null -eq $serviceRecord) { throw "恢复未确认：服务记录不存在" }
  if ([string]$serviceRecord.PathName -ne [string]$snapshot.binary_path) {
    throw "恢复未确认：服务程序路径不匹配"
  }
  if (-not [string]::IsNullOrWhiteSpace([string]$snapshot.version_line)) {
    $versionPath = Join-Path $script:InstallDir "版本信息.txt"
    if (-not (Test-Path -LiteralPath $versionPath -PathType Leaf)) {
      throw "恢复未确认：旧版本信息缺失"
    }
    $actualVersion = [string](Get-Content -LiteralPath $versionPath -Encoding UTF8 -TotalCount 1)
    if ($actualVersion -ne [string]$snapshot.version_line) {
      throw "恢复未确认：旧版本信息不匹配"
    }
  }
}

function Restore-EnvironmentSnapshot {
  param([Parameter(Mandatory = $true)]$Manifest)
  $entry = @(
    @($Manifest.entries) |
      Where-Object { $_.category -eq "metadata" -and $_.relative_path -eq "environment.json" }
  )
  if ($entry.Count -ne 1) {
    throw "恢复未确认：环境变量快照缺失"
  }
  $snapshotPath = Get-BackupPayloadPath ([string]$entry[0].backup_relative_path)
  $snapshot = Get-Content -LiteralPath $snapshotPath -Raw -Encoding UTF8 | ConvertFrom-Json
  foreach ($name in $script:ManagedEnvNames) {
    $property = $snapshot.PSObject.Properties[$name]
    $expected = if ($null -eq $property) { $null } else { [string]$property.Value }
    Set-KaiwuMachineEnvironmentVariable $name $expected
    if ([string]::IsNullOrEmpty($expected)) {
      Remove-Item -Path ("Env:" + $name) -ErrorAction SilentlyContinue
    } else {
      Set-Item -Path ("Env:" + $name) -Value $expected
    }
    if ([string]::IsNullOrEmpty($expected)) {
      if (-not [string]::IsNullOrEmpty((Get-KaiwuMachineEnvironmentVariable $name))) {
        throw "恢复后环境变量摘要不匹配: $name"
      }
    } elseif ((Get-KaiwuMachineEnvironmentVariable $name) -ne $expected) {
      throw "恢复后环境变量摘要不匹配: $name"
    }
  }
}

function Restore-EnvironmentFromMemory {
  foreach ($name in $script:ManagedEnvNames) {
    $expected = $script:EnvironmentBefore[$name]
    Set-KaiwuMachineEnvironmentVariable $name $expected
    if ([string]::IsNullOrEmpty($expected)) {
      Remove-Item -Path ("Env:" + $name) -ErrorAction SilentlyContinue
    } else {
      Set-Item -Path ("Env:" + $name) -Value $expected
    }
    if ((Get-KaiwuMachineEnvironmentVariable $name) -ne $expected) {
      throw "恢复后环境变量与安装前不一致: $name"
    }
  }
}

function Restore-PreviousService {
  param([Parameter(Mandatory = $true)]$Manifest)
  if (-not $script:HadExistingService) { return }
  $snapshot = Get-BackupServiceSnapshot $Manifest
  if ($null -eq $snapshot) { throw "恢复未确认：服务快照缺失" }
  if ([string]$snapshot.name -ne $script:ServiceName) {
    throw "恢复未确认：服务快照名称不匹配"
  }
  if ([string]::IsNullOrWhiteSpace([string]$snapshot.binary_path)) {
    throw "恢复未确认：服务快照缺少程序路径"
  }
  if ($null -ne (Get-Service -Name $script:ServiceName -ErrorAction SilentlyContinue)) {
    throw "恢复未确认：恢复旧服务前仍存在同名服务"
  }

  $startupType = Get-ServiceStartupType ([string]$snapshot.start_mode)
  $displayName = [string]$snapshot.display_name
  if ([string]::IsNullOrWhiteSpace($displayName)) {
    $displayName = "Kaiwu Intranet Connector"
  }
  New-Service `
    -Name $script:ServiceName `
    -BinaryPathName ([string]$snapshot.binary_path) `
    -StartupType $startupType `
    -DisplayName $displayName `
    -ErrorAction Stop | Out-Null
  if ([bool]$snapshot.delayed_auto_start -and $startupType -eq "Automatic") {
    sc.exe config $script:ServiceName start= delayed-auto | Out-Null
    if ($LASTEXITCODE -ne 0) {
      throw "恢复未确认：设置旧服务延迟启动失败"
    }
  }
  if ([bool]$snapshot.was_running) {
    Start-Service -Name $script:ServiceName -ErrorAction Stop
    Wait-ServiceRunning | Out-Null
  } else {
    $service = Get-Service -Name $script:ServiceName -ErrorAction Stop
    if ($service.Status -eq "Running") {
      throw "恢复未确认：旧服务安装前未运行，恢复后却处于运行状态"
    }
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
$ExistingSensitive = @($SensitiveFiles | ForEach-Object { Join-Path $DataDir $_ } | Where-Object { Test-Path -LiteralPath $_ })
$ExistingProgramFiles = @(
  @("kaiwu-connector.exe", "frpc.exe", "版本信息.txt") |
    ForEach-Object { Join-Path $InstallDir $_ } |
    Where-Object { Test-Path -LiteralPath $_ }
)
$EnvironmentBefore = @{}
foreach ($name in $ManagedEnvNames) {
  $EnvironmentBefore[$name] = Get-KaiwuMachineEnvironmentVariable $name
}

$NeedsBackup = $HadExistingService -or ($ExistingSensitive.Count -gt 0) -or ($ExistingProgramFiles.Count -gt 0)
$Stage = "backup_prepare"
try {
  New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
  Set-PrivateDataAcl -Path $DataDir

  if ($NeedsBackup) {
    $BackupDir = Join-Path $DataDir ("backups\" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-pre-upgrade")
    $BackupManifestPath = Join-Path $BackupDir "manifest\backup-manifest.json"
    New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $BackupDir "program") | Out-Null
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $BackupManifestPath) | Out-Null

    $BackupEntries = @()
    foreach ($sourceFile in $ExistingSensitive) {
      $name = Split-Path -Leaf $sourceFile
      Copy-Item -LiteralPath $sourceFile -Destination $BackupDir -Force
      $BackupEntries += New-BackupEntry "data" $name $name
    }
    foreach ($programFile in $ExistingProgramFiles) {
      $name = Split-Path -Leaf $programFile
      Copy-Item -LiteralPath $programFile -Destination (Join-Path $BackupDir "program") -Force
      $BackupEntries += New-BackupEntry "program" $name ("program/" + $name)
    }
    if ($HadExistingService) {
      $VersionLine = $null
      $VersionFile = Join-Path $InstallDir "版本信息.txt"
      if (Test-Path -LiteralPath $VersionFile -PathType Leaf) {
        $VersionLine = [string](Get-Content -LiteralPath $VersionFile -Encoding UTF8 -TotalCount 1)
      }
      $ServiceSnapshot = [ordered]@{
        name = $ServiceName
        display_name = $ExistingService.DisplayName
        binary_path = $ExistingService.PathName
        start_mode = $ExistingService.StartMode
        delayed_auto_start = [bool]$ExistingService.DelayedAutoStart
        was_running = $ExistingService.State -eq "Running"
        version_line = $VersionLine
      }
      $servicePath = Join-Path $BackupDir "service.json"
      Write-Utf8Text $servicePath ($ServiceSnapshot | ConvertTo-Json -Depth 4)
      $BackupEntries += New-BackupEntry "metadata" "service.json" "service.json"
    }
    $environmentPath = Join-Path $BackupDir "environment.json"
    Write-Utf8Text $environmentPath ($EnvironmentBefore | ConvertTo-Json -Depth 4)
    $BackupEntries += New-BackupEntry "metadata" "environment.json" "environment.json"

    $infoPath = Join-Path $BackupDir "backup-info.txt"
    [System.IO.File]::WriteAllText(
      $infoPath,
      "created_at=$((Get-Date).ToUniversalTime().ToString("o"))`r`nprevious_install=$InstallDir`r`nservice=$ServiceName`r`nrestore_script=restore.ps1`r`n",
      [System.Text.Encoding]::UTF8
    )
    $BackupEntries += New-BackupEntry "metadata" "backup-info.txt" "backup-info.txt"
    $manifest = [ordered]@{
      schema_version = 1
      created_at = (Get-Date).ToUniversalTime().ToString("o")
      entry_count = $BackupEntries.Count
      entries = @($BackupEntries)
    }
    $manifestDirectory = Split-Path -Parent $BackupManifestPath
    New-Item -ItemType Directory -Force -Path $manifestDirectory | Out-Null
    Write-Utf8Text $BackupManifestPath ($manifest | ConvertTo-Json -Depth 6)
    $BackupManifestHash = Get-FileSha256 $BackupManifestPath
    $RestoreScript = @'
# 仅供本次安装前备份使用。由管理员在 PowerShell 中运行。
$ErrorActionPreference = "Stop"
$ExpectedManifestSha256 = "__BACKUP_MANIFEST_SHA256__"
$BackupDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ManifestPath = Join-Path (Join-Path $BackupDir "manifest") "backup-manifest.json"
$ServiceName = "KaiwuConnector"
$InstallDir = Join-Path $env:ProgramFiles "Kaiwu\Connector"
$DataDir = Join-Path $env:ProgramData "Kaiwu\Connector"
$ManagedEnvNames = @("KAIWU_CLOUD_BASE", "KAIWU_EDGE_BASE")

if (-not (Get-Command Get-KaiwuMachineEnvironmentVariable -CommandType Function -ErrorAction SilentlyContinue)) {
  function Get-KaiwuMachineEnvironmentVariable {
    param([Parameter(Mandatory = $true)][string]$Name)
    return [Environment]::GetEnvironmentVariable($Name, "Machine")
  }
}
if (-not (Get-Command Set-KaiwuMachineEnvironmentVariable -CommandType Function -ErrorAction SilentlyContinue)) {
  function Set-KaiwuMachineEnvironmentVariable {
    param(
      [Parameter(Mandatory = $true)][string]$Name,
      [AllowNull()][AllowEmptyString()][string]$Value
    )
    [Environment]::SetEnvironmentVariable($Name, $Value, "Machine")
  }
}

function Get-FileSha256 {
  param([string]$Path)
  return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-BackupPayloadPath {
  param([string]$RelativePath)
  if (
    [string]::IsNullOrWhiteSpace($RelativePath) -or
    [System.IO.Path]::IsPathRooted($RelativePath) -or
    $RelativePath -match '(^|[\\/])\.\.([\\/]|$)'
  ) { throw "备份清单包含非法路径" }
  return (Join-Path $BackupDir ($RelativePath -replace '/', '\'))
}

function Get-BackupTargetPath {
  param($Entry)
  $relativePath = [string]$Entry.relative_path
  if (
    [string]::IsNullOrWhiteSpace($relativePath) -or
    [System.IO.Path]::IsPathRooted($relativePath) -or
    $relativePath -match '[/\\]' -or
    $relativePath -in @(".", "..")
  ) { throw "备份清单包含非法恢复目标" }
  switch ([string]$Entry.category) {
    "program" { return (Join-Path $InstallDir $relativePath) }
    "data" { return (Join-Path $DataDir $relativePath) }
    default { throw "备份元数据不能写入业务目录" }
  }
}

function Assert-BackupIntact {
  if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "恢复失败：备份清单缺失"
  }
  if ((Get-FileSha256 $ManifestPath) -ne $ExpectedManifestSha256) {
    throw "恢复失败：备份清单摘要不匹配"
  }
  try {
    $manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 |
      ConvertFrom-Json
  } catch {
    throw "恢复失败：备份清单无法解析"
  }
  if ([int]$manifest.schema_version -ne 1) {
    throw "恢复失败：备份清单版本不受支持"
  }
  $entries = @($manifest.entries)
  if ($entries.Count -eq 0 -or [int]$manifest.entry_count -ne $entries.Count) {
    throw "恢复失败：备份清单内容不完整"
  }
  $seen = @{}
  foreach ($entry in $entries) {
    $category = [string]$entry.category
    $relativePath = [string]$entry.relative_path
    $backupRelativePath = [string]$entry.backup_relative_path
    $expected = [string]$entry.sha256
    if ($category -notin @("program", "data", "metadata")) {
      throw "恢复失败：备份清单包含未知类别"
    }
    if (
      [string]::IsNullOrWhiteSpace($relativePath) -or
      [System.IO.Path]::IsPathRooted($relativePath) -or
      $relativePath -match '[/\\]' -or
      $relativePath -in @(".", "..")
    ) { throw "恢复失败：备份清单包含非法目标" }
    if ($category -eq "program" -and $backupRelativePath -notmatch '^program/[^/\\]+$') {
      throw "恢复失败：程序备份路径不匹配"
    }
    if ($category -ne "program" -and $backupRelativePath -match '[/\\]') {
      throw "恢复失败：业务或元数据备份路径不匹配"
    }
    if ($expected -notmatch '^[0-9a-fA-F]{64}$') {
      throw "恢复失败：备份文件摘要格式不正确"
    }
    $key = $category + ":" + $relativePath
    if ($seen.ContainsKey($key)) { throw "恢复失败：备份清单包含重复项" }
    $seen[$key] = $true
    $payloadPath = Get-BackupPayloadPath $backupRelativePath
    if (-not (Test-Path -LiteralPath $payloadPath -PathType Leaf)) {
      throw "恢复失败：备份文件缺失"
    }
    if ((Get-FileSha256 $payloadPath) -ne $expected.ToLowerInvariant()) {
      throw "恢复失败：备份文件摘要不匹配"
    }
  }
  return $manifest
}

function Get-MetadataEntry {
  param($Manifest, [string]$RelativePath)
  $entries = @(
    @($Manifest.entries) |
      Where-Object { $_.category -eq "metadata" -and $_.relative_path -eq $RelativePath }
  )
  if ($entries.Count -gt 1) { throw "恢复失败：元数据重复 ($RelativePath)" }
  if ($entries.Count -eq 0) { return $null }
  return $entries[0]
}

function Assert-RestoredFileDigests {
  param($Manifest)
  foreach ($entry in @($Manifest.entries)) {
    if ($entry.category -notin @("program", "data")) { continue }
    $targetPath = Get-BackupTargetPath $entry
    if (-not (Test-Path -LiteralPath $targetPath -PathType Leaf)) {
      throw "恢复失败：恢复目标缺失"
    }
    if ((Get-FileSha256 $targetPath) -ne [string]$entry.sha256) {
      throw "恢复失败：恢复目标摘要不匹配"
    }
  }
}

function Assert-RestoredStableState {
  param($Manifest, $Snapshot)
  foreach ($entry in @($Manifest.entries)) {
    if ($entry.category -notin @("program", "data")) { continue }
    if ($entry.category -eq "data" -and $entry.relative_path -eq "state.json") { continue }
    $targetPath = Get-BackupTargetPath $entry
    if (-not (Test-Path -LiteralPath $targetPath -PathType Leaf)) {
      throw "恢复失败：稳定目标缺失"
    }
    if ((Get-FileSha256 $targetPath) -ne [string]$entry.sha256) {
      throw "恢复失败：稳定目标摘要不匹配"
    }
  }
  if ($null -ne $Snapshot) {
    foreach ($name in @("kaiwu-connector.exe", "frpc.exe")) {
      if (-not (Test-Path -LiteralPath (Join-Path $InstallDir $name) -PathType Leaf)) {
        throw "恢复失败：关键程序缺失"
      }
    }
  }
  if ($null -eq $Snapshot) { return }
  $service = Get-Service -Name $ServiceName -ErrorAction Stop
  if ($null -eq $service) { throw "恢复失败：旧服务不存在" }
  if ([bool]$Snapshot.was_running) {
    if ($service.Status -ne "Running") { throw "恢复失败：旧服务未运行" }
  } elseif ($service.Status -eq "Running") {
    throw "恢复失败：旧服务安装前未运行，恢复后却处于运行状态"
  }
  $record = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction Stop
  if ($null -eq $record) { throw "恢复失败：服务记录不存在" }
  if ([string]$record.PathName -ne [string]$Snapshot.binary_path) {
    throw "恢复失败：服务程序路径不匹配"
  }
  if (-not [string]::IsNullOrWhiteSpace([string]$Snapshot.version_line)) {
    $versionPath = Join-Path $InstallDir "版本信息.txt"
    if (-not (Test-Path -LiteralPath $versionPath -PathType Leaf)) {
      throw "恢复失败：旧版本信息缺失"
    }
    $actualVersion = [string](Get-Content -LiteralPath $versionPath -Encoding UTF8 -TotalCount 1)
    if ($actualVersion -ne [string]$Snapshot.version_line) {
      throw "恢复失败：旧版本信息不匹配"
    }
  }
}

function Wait-ServiceStopped {
  param([int]$TimeoutSeconds = 15)
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  do {
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($null -eq $service) { throw "恢复失败：目标服务在停止确认前消失" }
    if ($service.Status -eq "Stopped") { return }
    Start-Sleep -Milliseconds 500
  } while ((Get-Date) -lt $deadline)
  throw "恢复失败：目标服务未进入 Stopped 状态"
}

function Wait-ServiceAbsent {
  param([int]$TimeoutSeconds = 15)
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  do {
    if ($null -eq (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) { return }
    Start-Sleep -Milliseconds 500
  } while ((Get-Date) -lt $deadline)
  throw "恢复失败：目标服务未能删除"
}

function Assert-ServiceStoppable {
  param($Service)
  $dependents = @($Service.DependentServices)
  if ($dependents.Count -gt 0) {
    $names = @($dependents | ForEach-Object { [string]$_.Name }) -join ", "
    throw "恢复失败：服务存在依赖服务（$names），拒绝停止"
  }
}

function Stop-AndDeleteCurrentService {
  $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
  if ($null -eq $service) { return }
  Assert-ServiceStoppable $service
  if ($service.Status -ne "Stopped") {
    Stop-Service -Name $ServiceName -ErrorAction Stop
    Wait-ServiceStopped
  }
  & sc.exe delete $ServiceName | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "恢复失败：目标服务删除失败" }
  Wait-ServiceAbsent
}

function Restore-Environment {
  param($Manifest)
  $entry = Get-MetadataEntry $Manifest "environment.json"
  if ($null -eq $entry) { throw "恢复失败：环境变量快照缺失" }
  $snapshotPath = Get-BackupPayloadPath ([string]$entry.backup_relative_path)
  $snapshot = Get-Content -LiteralPath $snapshotPath -Raw -Encoding UTF8 | ConvertFrom-Json
  foreach ($name in $ManagedEnvNames) {
    $property = $snapshot.PSObject.Properties[$name]
    $expected = if ($null -eq $property) { $null } else { [string]$property.Value }
    Set-KaiwuMachineEnvironmentVariable $name $expected
    if ([string]::IsNullOrEmpty($expected)) {
      Remove-Item -Path ("Env:" + $name) -ErrorAction SilentlyContinue
    } else {
      Set-Item -Path ("Env:" + $name) -Value $expected
    }
    if ([string]::IsNullOrEmpty($expected)) {
      if (-not [string]::IsNullOrEmpty((Get-KaiwuMachineEnvironmentVariable $name))) {
        throw "恢复失败：环境变量恢复后仍非空 ($name)"
      }
    } elseif ((Get-KaiwuMachineEnvironmentVariable $name) -ne $expected) {
      throw "恢复失败：环境变量恢复后摘要不匹配 ($name)"
    }
  }
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

$manifest = Assert-BackupIntact
$snapshotEntry = Get-MetadataEntry $manifest "service.json"
$snapshot = $null
if ($null -ne $snapshotEntry) {
  $snapshotPath = Get-BackupPayloadPath ([string]$snapshotEntry.backup_relative_path)
  $snapshot = Get-Content -LiteralPath $snapshotPath -Raw -Encoding UTF8 | ConvertFrom-Json
  if ([string]$snapshot.name -ne $ServiceName) { throw "恢复失败：服务快照名称不匹配" }
  if ([string]::IsNullOrWhiteSpace([string]$snapshot.binary_path)) {
    throw "恢复失败：服务快照缺少程序路径"
  }
}

Stop-AndDeleteCurrentService
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
foreach ($entry in @($manifest.entries)) {
  if ($entry.category -notin @("program", "data")) { continue }
  $sourcePath = Get-BackupPayloadPath ([string]$entry.backup_relative_path)
  $targetPath = Get-BackupTargetPath $entry
  $targetDirectory = Split-Path -Parent $targetPath
  if ($targetDirectory) { New-Item -ItemType Directory -Force -Path $targetDirectory | Out-Null }
  Copy-Item -LiteralPath $sourcePath -Destination $targetPath -Force
}
Assert-RestoredFileDigests $manifest
Restore-Environment $manifest

if ($null -ne $snapshot) {
  $startupType = Get-ServiceStartupType ([string]$snapshot.start_mode)
  $displayName = [string]$snapshot.display_name
  if ([string]::IsNullOrWhiteSpace($displayName)) { $displayName = "Kaiwu Intranet Connector" }
  New-Service -Name $ServiceName -BinaryPathName ([string]$snapshot.binary_path) -StartupType $startupType -DisplayName $displayName -ErrorAction Stop | Out-Null
  if ([bool]$snapshot.delayed_auto_start -and $startupType -eq "Automatic") {
    & sc.exe config $ServiceName start= delayed-auto | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "恢复失败：设置旧服务延迟启动失败" }
  }
  if ([bool]$snapshot.was_running) {
    Start-Service -Name $ServiceName -ErrorAction Stop
    $deadline = (Get-Date).AddSeconds(45)
    do {
      $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
      if ($null -ne $service -and $service.Status -eq "Running") { break }
      Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    if ($null -eq $service -or $service.Status -ne "Running") {
      throw "恢复失败：旧服务未进入 Running 状态"
    }
  }
}

Assert-RestoredStableState $manifest $snapshot
Write-Host "已恢复安装前版本。"
'@
    $RestoreScript = $RestoreScript.Replace("__BACKUP_MANIFEST_SHA256__", $BackupManifestHash)
    $restorePath = Join-Path $BackupDir "restore.ps1"
    [System.IO.File]::WriteAllText(
      $restorePath,
      $RestoreScript,
      [System.Text.UTF8Encoding]::new($true)
    )
    Set-PrivateDataAcl -Path $BackupDir
    Assert-BackupIntact | Out-Null
    Write-Host "升级前程序、配置和本机秘密已备份并校验到 $BackupDir"
  }
} catch {
  Write-InstallResult "PRECHECK_FAILED"
  throw
}

$OriginalError = $null
try {
  $Stage = "pre_change_verification"
  Assert-CriticalFiles
  $Stage = "replace_install"
  $InstallMutationStarted = $true
  $OldInstallChanged = $NeedsBackup
  Stop-AndDeleteService

  Ensure-ManagedDirectory $InstallDir
  Ensure-ManagedDirectory (Join-Path $InstallDir "docs")
  Ensure-ManagedDirectory (Join-Path $InstallDir "compliance")

  Copy-ManagedFile (Join-Path $Source "bin\kaiwu-connector.exe") (Join-Path $InstallDir "kaiwu-connector.exe")
  Copy-ManagedFile (Join-Path $Source "bin\frpc.exe") (Join-Path $InstallDir "frpc.exe")
  Assert-CriticalFiles -Target
  Get-ChildItem (Join-Path $Source "docs\*.md") -ErrorAction SilentlyContinue |
    ForEach-Object {
      Copy-ManagedFile $_.FullName (Join-Path (Join-Path $InstallDir "docs") $_.Name)
    }
  Get-ChildItem (Join-Path $Source "compliance\*") -File -ErrorAction SilentlyContinue |
    ForEach-Object {
      Copy-ManagedFile $_.FullName (Join-Path (Join-Path $InstallDir "compliance") $_.Name)
    }
  foreach ($name in @("uninstall.ps1", "windows-smoke.ps1", "install.ps1", "版本信息.txt")) {
    $item = Join-Path $Source $name
    if (Test-Path -LiteralPath $item -PathType Leaf) {
      Copy-ManagedFile $item (Join-Path $InstallDir $name)
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
            Set-KaiwuMachineEnvironmentVariable $name $null
            Remove-Item -Path ("Env:" + $name) -ErrorAction SilentlyContinue
            Write-Host "已清空未配置的环境变量 $name"
          }
        } else {
          Set-KaiwuMachineEnvironmentVariable $name $value
          Set-Item -Path ("Env:" + $name) -Value $value
          $check = Get-KaiwuMachineEnvironmentVariable $name
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

  $Stage = "final_verification"
  Assert-CriticalFiles -Target

  $VersionLine = "unknown"
  $VersionFile = Join-Path $Source "版本信息.txt"
  if (Test-Path -LiteralPath $VersionFile) {
    # Get-Content -TotalCount returns the string itself. Piping through
    # Select-Object would persist a Selected.System.String wrapper and its
    # installer-temporary metadata into install-state.json on PowerShell 5.1.
    $VersionLine = [string](Get-Content -LiteralPath $VersionFile -Encoding UTF8 -TotalCount 1)
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
  $Stage = "installed"
  $Cleanup = "not_needed"
  Write-InstallResult "INSTALLED"
} catch {
  $OriginalError = $_.Exception.Message
  $Stage = "rollback"
  $RollbackErrors = @()
  $NewServiceRemoved = $false
  if ($InstallMutationStarted) {
    try {
      Stop-AndDeleteService
      $NewServiceRemoved = $true
    } catch {
      $RollbackErrors += "删除失败的新服务: $($_.Exception.Message)"
    }
  } else {
    $NewServiceRemoved = $true
  }

  if (-not $InstallMutationStarted) {
    Write-InstallResult "PRECHECK_FAILED"
    throw "安装前检查失败：$OriginalError"
  }

  if (-not $NewServiceRemoved) {
    $RollbackErrors += "当前服务未能移除，未执行旧安装恢复"
  } elseif ($BackupDir) {
    try {
      $manifest = Assert-BackupIntact
      Remove-ManagedFilesCreatedByRun
      foreach ($entry in @($manifest.entries)) {
        if ($entry.category -notin @("program", "data")) { continue }
        $sourcePath = Get-BackupPayloadPath ([string]$entry.backup_relative_path)
        $targetPath = Get-BackupTargetPath $entry
        $targetDirectory = Split-Path -Parent $targetPath
        if ($targetDirectory) {
          New-Item -ItemType Directory -Force -Path $targetDirectory | Out-Null
        }
        Copy-Item -LiteralPath $sourcePath -Destination $targetPath -Force
      }
      Assert-RestoredFileDigests $manifest
      Restore-EnvironmentSnapshot $manifest
      Assert-RestoredFileDigests $manifest
      Restore-PreviousService $manifest
      Assert-RestoredStableState $manifest
      Write-Host "安装失败，已核验恢复安装前程序和配置。"
    } catch {
      $RollbackErrors += "恢复旧安装: $($_.Exception.Message)"
      if ($HadExistingService) {
        $restoredService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
        if ($null -ne $restoredService -and $restoredService.Status -ne "Stopped") {
          try {
            Stop-TargetService
          } catch {
            $RollbackErrors += "停止未确认恢复的旧服务: $($_.Exception.Message)"
          }
        }
      }
    }
  } else {
    try {
      Restore-EnvironmentFromMemory
    } catch {
      $RollbackErrors += "恢复环境变量: $($_.Exception.Message)"
    }
    try {
      Remove-ManagedFilesCreatedByRun
      Write-Host "安装失败，已清理本次新增受管文件。"
    } catch {
      $RollbackErrors += "清理本次新增受管文件: $($_.Exception.Message)"
    }
  }

  if ($RollbackErrors.Count -eq 0) {
    if ($BackupDir) {
      $Stage = "restored"
      $Cleanup = "restored"
      Write-InstallResult "FAILED_RESTORED"
    } else {
      $Stage = "cleaned"
      $Cleanup = "cleaned"
      Write-InstallResult "FAILED_NO_PRIOR_CLEANED"
    }
  } else {
    if ($BackupDir) {
      $Stage = "recovery_unconfirmed"
      $Cleanup = "unconfirmed"
      Write-InstallResult "FAILED_RECOVERY_UNCONFIRMED"
    } else {
      $Stage = "cleanup_unconfirmed"
      $Cleanup = "unconfirmed"
      Write-InstallResult "FAILED_NO_PRIOR_CLEANUP_UNCONFIRMED"
    }
  }

  $RollbackDetail = if ($RollbackErrors.Count -eq 0) {
    if ($BackupDir) { "已核验恢复安装前状态。" } else { "已清理本次新增受管文件。" }
  } else {
    "恢复或清理未确认：" + ($RollbackErrors -join "; ")
  }
  throw "安装失败：$OriginalError`n$RollbackDetail`n备份目录：$BackupDir"
}
