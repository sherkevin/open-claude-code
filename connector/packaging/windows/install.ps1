# 开物内网连接器 Windows 安装脚本。
# 用法：以管理员身份运行 PowerShell：.\install.ps1
#Requires -RunAsAdministrator
param(
  [string]$ResultPath = "",
  [string]$FailureDetailsPath = "",
  [long]$AttemptHandle = 0,
  [switch]$UsePackageTemp,
  [switch]$FailureDetailsCodes
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
if ($UsePackageTemp) {
  # Keep PowerShell 5.1 SCM interop compilation in the launcher's protected short path.
  # The finish page may launch a browser after this directory has been removed.
  $env:TEMP = $Source
  $env:TMP = $Source
}
$InstallDir = Join-Path $env:ProgramFiles "Kaiwu\Connector"
$DataDir = Join-Path $env:ProgramData "Kaiwu\Connector"
$ServiceName = "KaiwuConnector"
$Listen = "127.0.0.1:17872"
$MaintenanceRegistryNames = @(
  "DisplayName", "DisplayVersion", "Publisher", "InstallLocation", "DisplayIcon", "UninstallString",
  "QuietUninstallString", "Comments", "NoModify", "NoRepair", "EstimatedSize"
)
if ([string]::IsNullOrWhiteSpace($ResultPath)) {
  $ResultPath = Join-Path $env:TEMP "KaiwuConnector-install-result.txt"
}

$Stage = "startup"
$FailureStage = $null
$FailureReason = "unknown"
$FailureFile = $null
$OriginalHandoff = @{ startup_changed = $false; stop_attempted = $false; delete_accepted = $false }
$ReplacementServiceCreated = $false
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
  [pscustomobject]@{ SourceRelative = "bin/frpc.exe"; TargetName = "frpc.exe" },
  [pscustomobject]@{ SourceRelative = "service-handoff.ps1"; TargetName = "service-handoff.ps1" }
)

# The launcher gives this process an append-only file handle. The script never
# opens a diagnostic path, and standalone installs can run without the handle.
$AttemptPhase = $null
$AttemptSequence = $null

function Write-InstallEvent {
  param([string]$Phase, [string]$Outcome, [string]$Code)
  if (-not $AttemptHandle) { return }
  if ($Phase -cnotin @('precheck', 'lock', 'backup', 'replace', 'service', 'verify', 'rollback') -or
      $Outcome -cnotin @('started', 'ok', 'failed') -or
      $Code -cnotin @('ok', 'unknown', 'unsupported_host', 'busy', 'file_missing', 'checksum_mismatch', 'access_denied',
                     'service_registration', 'service_start', 'local_page_unready', 'write_failed', 'restored',
                     'recovery_unconfirmed')) { return }
  $stream = $null
  try {
    $handle = [Microsoft.Win32.SafeHandles.SafeFileHandle]::new([IntPtr]::new($AttemptHandle), $false)
    $stream = [IO.FileStream]::new($handle, [IO.FileAccess]::ReadWrite)
    if ($stream.Length -lt 1 -or $stream.Length -ge 4096) { return }
    if ($null -eq $AttemptSequence) {
      $null = $stream.Seek(0, [IO.SeekOrigin]::Begin)
      $prefix = New-Object byte[] ([int]$stream.Length)
      if ($stream.Read($prefix, 0, $prefix.Length) -ne $prefix.Length -or $prefix[-1] -ne 10) { return }
      $script:AttemptSequence = 0
      foreach ($byte in $prefix) { if ($byte -eq 10) { $script:AttemptSequence++ } }
    }
    $next = $AttemptSequence + 1
    $line = '{"v":1,"seq":' + $next.ToString([Globalization.CultureInfo]::InvariantCulture) +
      ',"stage":"' + $Phase + '","outcome":"' + $Outcome + '","code":"' + $Code + '"}' + "`n"
    $bytes = [Text.Encoding]::ASCII.GetBytes($line)
    if ($next -gt 48 -or ($stream.Length + $bytes.Length) -gt 4096) { return }
    $null = $stream.Seek(0, [IO.SeekOrigin]::End)
    $stream.Write($bytes, 0, $bytes.Length)
    $script:AttemptSequence = $next
    $stream.Flush()
  } catch {
    $script:AttemptHandle = 0
  } finally {
    if ($stream) { try { $stream.Dispose() } catch {} }
  }
}

function Enter-InstallPhase {
  param([string]$Phase)
  if ($AttemptPhase) { Write-InstallEvent $AttemptPhase 'ok' 'ok' }
  $script:AttemptPhase = $Phase
  Write-InstallEvent $Phase 'started' 'ok'
}

function Fail-InstallPhase {
  param([System.Management.Automation.ErrorRecord]$ErrorRecord)
  $code = switch ($FailureReason) {
    { $_ -in @('manifest_missing', 'file_missing') } { 'file_missing' }
    { $_ -in @('manifest_invalid', 'file_unlisted', 'checksum_mismatch') } { 'checksum_mismatch' }
    { $_ -in @('unsupported_host', 'service_registration', 'service_start', 'local_page_unready') } { $_ }
    default {
      $errorCause = $ErrorRecord.Exception.GetBaseException()
      if ($errorCause -is [UnauthorizedAccessException] -or $ErrorRecord.CategoryInfo.Category -eq 'PermissionDenied') {
        'access_denied'
      } elseif ($AttemptPhase -eq 'lock' -and $errorCause -is [TimeoutException]) {
        'busy'
      } else {
        'unknown'
      }
    }
  }
  if ($AttemptPhase) { Write-InstallEvent $AttemptPhase 'failed' $code }
  $script:AttemptPhase = $null
}

function Set-InstallFailure {
  param([string]$Reason, [string]$SourceRelative)
  $script:FailureReason = $Reason
  # Only product filenames from code are eligible for the support summary.
  $script:FailureFile = $CriticalFiles | Where-Object SourceRelative -eq $SourceRelative |
    ForEach-Object { $_.TargetName }
}

function Write-InstallFailureDetails {
  if ([string]::IsNullOrWhiteSpace($FailureDetailsPath)) { return }
  if ($FailureDetailsCodes) {
    # The graphical launcher renders these identifiers with its own allowlist.
    try {
      [IO.File]::WriteAllText($FailureDetailsPath, "$FailureStage`r`n$FailureReason`r`n$FailureFile`r`n",
        [Text.UnicodeEncoding]::new($false, $false))
    } catch {
      Write-Host "无法写入安装失败摘要。"
    }
    return
  }
  $phase = switch ($FailureStage) {
    "source_precheck"          { "校验解包后的安装文件" }
    "transaction_lock"         { "取得安装事务锁" }
    "backup_prepare"           { "检查并备份已有安装" }
    "quiesce"                  { "停止原服务和相关进程" }
    "pre_change_verification"  { "替换程序前再次校验安装文件" }
    "replace_install"          { "替换程序或配置服务" }
    "final_verification"       { "最终校验并记录安装状态" }
    default                    { "无法确认" }
  }
  $reason = switch ($FailureReason) {
    "manifest_missing"     { "缺少校验清单 SHA256SUMS" }
    "manifest_invalid"     { "校验清单 SHA256SUMS 含非法路径" }
    "file_missing"         { if ($FailureFile) { "未找到文件 $FailureFile" } else { "未找到安装包清单中的文件" } }
    "file_unlisted"        { "校验清单 SHA256SUMS 缺少 $FailureFile 的记录" }
    "checksum_mismatch"    { if ($FailureFile) { "$FailureFile 未通过 SHA256 校验" } else { "安装包中的文件未通过 SHA256 校验" } }
    "unsupported_host"     { "安装脚本需要 64 位 Windows PowerShell" }
    "service_registration" { "无法注册连接器服务" }
    "service_start"        { "无法启动连接器服务" }
    "local_page_unready"   { "本机配置页面未在规定时间内就绪" }
    default               { "无法确认具体原因" }
  }
  try {
    [IO.File]::WriteAllText(
      $FailureDetailsPath, "失败阶段：$phase`r`n原因：$reason`r`n", [Text.UnicodeEncoding]::new($false, $false)
    )
  } catch {
    Write-Host "无法写入安装失败摘要。"
  }
}

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
  if ($ResultCode -ne "INSTALLED") {
    if (-not $FailureStage) { $FailureStage = $Stage }
    $lines += @("failure_stage=$FailureStage", "failure_reason=$FailureReason")
    if ($FailureFile) { $lines += "failure_file=$FailureFile" }
    Write-InstallFailureDetails
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
    Write-InstallEvent 'verify' 'failed' 'write_failed'
    Write-Host "无法写入安装结果状态。"
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
      Set-InstallFailure "file_missing" $item.SourceRelative
      throw "关键安装文件缺失: $($item.SourceRelative)"
    }
    $expected = $FrozenHashes[$item.SourceRelative]
    if (-not $expected) {
      Set-InstallFailure "file_unlisted" $item.SourceRelative
      throw "SHA256SUMS 缺少关键文件: $($item.SourceRelative)"
    }
    if ((Get-FileSha256 $path) -ne $expected) {
      Set-InstallFailure "checksum_mismatch" $item.SourceRelative
      throw "关键安装文件校验失败: $($item.SourceRelative)"
    }
  }
}

$Stage = "source_precheck"
Enter-InstallPhase 'precheck'
try {
  if (-not [Environment]::Is64BitProcess) {
    Set-InstallFailure "unsupported_host"
    throw "请使用 64 位 Windows PowerShell"
  }
  $ChecksumFile = Join-Path $Source "SHA256SUMS"
  if (-not (Test-Path -LiteralPath $ChecksumFile -PathType Leaf)) {
    Set-InstallFailure "manifest_missing"
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
      Set-InstallFailure "manifest_invalid"
      throw "SHA256SUMS 含非法路径"
    }
    $FrozenHashes[$relative] = $expected
    $target = Join-Path $Source ($relative -replace '/', '\')
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
      Set-InstallFailure "file_missing" $relative
      throw "安装包缺少文件: $relative"
    }
    if ((Get-FileSha256 $target) -ne $expected) {
      Set-InstallFailure "checksum_mismatch" $relative
      throw "安装包文件校验失败: $relative"
    }
  }
  Assert-CriticalFiles
  $ServiceHandoffFunctions = [IO.File]::ReadAllText((Join-Path $Source "service-handoff.ps1"), [Text.Encoding]::UTF8)
  . ([scriptblock]::Create($ServiceHandoffFunctions))
} catch {
  Fail-InstallPhase $_
  Write-InstallResult "PRECHECK_FAILED"
  throw
}

function Set-PrivateDataAcl {
  param([Parameter(Mandatory = $true)][string]$Path, [switch]$SkipAttemptJournal)

  $items = @(Get-Item -LiteralPath $Path -Force)
  if ($SkipAttemptJournal) {
    # The launcher owns this subtree and may be writing concurrently with an earlier attempt.
    # Protect the rest of DataDir even if the journal directory is unavailable.
    $pending = [System.Collections.Generic.Stack[object]]::new()
    $pending.Push($items[0])
    $launcherManaged = @(
      (Join-Path $Path 'logs\install-attempts'),
      (Join-Path $Path 'logs\install-failure-summary.txt'),
      (Join-Path $Path 'logs\install-failure-summary.pending')
    )
    $checked = [System.Collections.Generic.List[object]]::new()
    while ($pending.Count) {
      $item = $pending.Pop()
      $checked.Add($item)
      if ($item.PSIsContainer) {
        foreach ($child in Get-ChildItem -LiteralPath $item.FullName -Force) {
          if ($child.FullName -notin $launcherManaged -and -not ($child.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            $pending.Push($child)
          }
        }
      }
    }
    $items = $checked
  } else {
    & icacls.exe $Path /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" /T /C | Out-Null
    if ($LASTEXITCODE -ne 0) {
      throw "设置数据目录 ACL 失败，icacls 退出码为 ${LASTEXITCODE}: $Path"
    }
    $items += @(Get-ChildItem -LiteralPath $Path -Force -Recurse -ErrorAction Stop)
  }

  $systemSid = [System.Security.Principal.SecurityIdentifier]::new("S-1-5-18")
  $adminSid = [System.Security.Principal.SecurityIdentifier]::new("S-1-5-32-544")
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

  foreach ($verifiedItem in $items) {
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

# 同一份服务恢复逻辑同时用于自动回滚和生成的独立恢复脚本。
$ServiceRecoveryFunctions = @'
function Test-KaiwuBackupTarget {
  param([string]$Category, [string]$Path)
  if ($Category -eq "data") { return $Path -match '^[^/\\:]+$' -and $Path -notin @(".", "..") }
  if ($Category -ne "program") { return $false }
  return $Path -match '^(?:(?:docs|compliance)/)?[^/\\:]+$' -and $Path -notmatch '(?:^|/)\.{1,2}$'
}

function Assert-RestoredFiles {
  param($Manifest, [switch]$AfterStart)
  foreach ($entry in $Manifest.entries) {
    if ($entry.category -notin @("program", "data")) { continue }
    # The service can rewrite these at startup. All were checked byte-for-byte beforehand.
    if ($AfterStart -and $entry.category -eq "data" -and
        $entry.relative_path -in @("state.json", "frpc.toml", "diagnostics.json")) { continue }
    $path = Get-BackupTargetPath $entry
    if ($entry.absent -eq $true) {
      if (Test-Path -LiteralPath $path) { throw "恢复目标应不存在: $($entry.relative_path)" }
    } elseif (-not (Test-Path -LiteralPath $path -PathType Leaf) -or
        (Get-FileSha256 $path) -ne [string]$entry.sha256) {
      throw "恢复目标摘要不匹配: $($entry.relative_path)"
    }
  }
}

function Assert-KaiwuServiceBackupFiles {
  param($Manifest)
  foreach ($name in @("kaiwu-connector.exe", "frpc.exe")) {
    $entries = @($Manifest.entries | Where-Object {
      $_.category -eq "program" -and $_.relative_path -eq $name -and $_.absent -ne $true
    })
    if ($entries.Count -ne 1) { throw "恢复未确认：备份缺少旧服务程序 $name" }
  }
}

function Restore-KaiwuFiles {
  param($Manifest)
  foreach ($entry in $Manifest.entries) {
    if ($entry.category -notin @("program", "data")) { continue }
    $target = Get-BackupTargetPath $entry
    if ($entry.absent -eq $true) {
      if (Test-Path -LiteralPath $target -PathType Leaf) {
        Remove-Item -LiteralPath $target -Force -ErrorAction Stop
      }
      continue
    }
    # A late sharing failure may leave the old file untouched and still read-locked.
    if ((Test-Path -LiteralPath $target -PathType Leaf) -and (Get-FileSha256 $target) -eq $entry.sha256) {
      continue
    }
    $payload = Get-BackupPayloadPath ([string]$entry.backup_relative_path)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
    Copy-Item -LiteralPath $payload -Destination $target -Force
  }
}

if (-not (Get-Command Get-KaiwuMaintenanceRegistration -CommandType Function -ErrorAction SilentlyContinue)) {
  function Get-KaiwuMaintenanceRegistration {
    $values = [ordered]@{}
    $key = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey(
      'Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector'
    )
    if ($null -eq $key) { return $values }
    try {
      foreach ($name in $MaintenanceRegistryNames) {
        if ($name -in $key.GetValueNames()) {
          $values[$name] = [ordered]@{
            kind = [string]$key.GetValueKind($name)
            value = $key.GetValue($name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
          }
        }
      }
      return $values
    } finally {
      $key.Dispose()
    }
  }
}

if (-not (Get-Command Restore-KaiwuMaintenanceRegistration -CommandType Function -ErrorAction SilentlyContinue)) {
  function Restore-KaiwuMaintenanceRegistration {
    param($Values)
    $path = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector'
    $key = if (@($Values.PSObject.Properties).Count -gt 0) {
      [Microsoft.Win32.Registry]::LocalMachine.CreateSubKey($path)
    } else {
      [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($path, $true)
    }
    if ($null -eq $key) { return }
    try {
      foreach ($name in $MaintenanceRegistryNames) {
        $property = $Values.PSObject.Properties[$name]
        if ($null -eq $property) {
          $key.DeleteValue($name, $false)
          if ($name -in $key.GetValueNames()) { throw "恢复卸载记录后仍存在字段: $name" }
          continue
        }
        $entry = $property.Value
        $kind = [Microsoft.Win32.RegistryValueKind]$entry.kind
        $value = switch ($entry.kind) {
          "DWord" { [int]$entry.value }
          "QWord" { [long]$entry.value }
          "MultiString" { ,([string[]]$entry.value) }
          "Binary" { ,([byte[]]$entry.value) }
          default { [string]$entry.value }
        }
        $key.SetValue($name, $value, $kind)
        $readback = $key.GetValue($name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        if ($key.GetValueKind($name) -ne $kind -or
            (ConvertTo-Json -InputObject $readback -Compress) -cne (ConvertTo-Json -InputObject $value -Compress)) {
          throw "恢复卸载记录后读回不一致: $name"
        }
      }
    } finally {
      $key.Dispose()
    }
    # 保留该键中的任何非事务字段。
  }
}

function Initialize-KaiwuServiceConfigurationApi {
  if ('Kaiwu.Installer.ServiceConfiguration' -as [type]) { return }
  Add-Type -ErrorAction Stop -TypeDefinition @"
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

namespace Kaiwu.Installer {
    public static class ServiceConfiguration {
        private const uint SC_MANAGER_CONNECT = 0x0001;
        private const uint SERVICE_QUERY_CONFIG = 0x0001;
        private const uint SERVICE_CHANGE_CONFIG = 0x0002;
        private const uint SERVICE_START = 0x0010;
        private const uint SERVICE_CONFIG_FAILURE_ACTIONS = 2;
        private const uint SERVICE_CONFIG_FAILURE_ACTIONS_FLAG = 4;
        private const uint SERVICE_CONFIG_SERVICE_SID_INFO = 5;

        [StructLayout(LayoutKind.Sequential)]
        public struct Action {
            public uint type;
            public uint delay_ms;
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct FailureActions {
            public uint ResetSeconds;
            public IntPtr RebootMessage;
            public IntPtr CommandLine;
            public uint ActionCount;
            public IntPtr Actions;
        }

        public sealed class Metadata {
            public uint sid_type { get; set; }
            public uint reset_seconds { get; set; }
            public bool on_non_crash_failures { get; set; }
            public string reboot_message { get; set; }
            public string command_line { get; set; }
            public Action[] actions { get; set; }
        }

        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
        private static extern IntPtr OpenSCManagerW(string machine, string database, uint access);
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
        private static extern IntPtr OpenServiceW(IntPtr manager, string name, uint access);
        [DllImport("advapi32.dll")]
        private static extern bool CloseServiceHandle(IntPtr handle);
        [DllImport("advapi32.dll", ExactSpelling = true, SetLastError = true)]
        private static extern bool QueryServiceConfig2W(IntPtr service, uint level, IntPtr buffer, uint size, out uint needed);
        [DllImport("advapi32.dll", ExactSpelling = true, SetLastError = true)]
        private static extern bool ChangeServiceConfig2W(IntPtr service, uint level, IntPtr config);

        private static Win32Exception Error(string operation) {
            int code = Marshal.GetLastWin32Error();
            return new Win32Exception(code, operation + " (Win32 " + code + ")");
        }

        private static void WithService(string name, uint access, System.Action<IntPtr> operation) {
            IntPtr manager = OpenSCManagerW(null, null, SC_MANAGER_CONNECT);
            if (manager == IntPtr.Zero) throw Error("OpenSCManagerW");
            try {
                IntPtr service = OpenServiceW(manager, name, access);
                if (service == IntPtr.Zero) throw Error("OpenServiceW");
                try { operation(service); }
                finally { CloseServiceHandle(service); }
            } finally { CloseServiceHandle(manager); }
        }

        private static void Query(IntPtr service, uint level, IntPtr buffer) {
            uint needed;
            // QueryServiceConfig2 documents 8 KiB as the maximum buffer size.
            if (!QueryServiceConfig2W(service, level, buffer, 8192, out needed))
                throw Error("QueryServiceConfig2W(" + level + ")");
        }

        public static Metadata Read(string name) {
            Metadata metadata = new Metadata();
            WithService(name, SERVICE_QUERY_CONFIG, service => {
                IntPtr buffer = Marshal.AllocHGlobal(8192);
                try {
                    Query(service, SERVICE_CONFIG_SERVICE_SID_INFO, buffer);
                    metadata.sid_type = unchecked((uint)Marshal.ReadInt32(buffer));
                    Query(service, SERVICE_CONFIG_FAILURE_ACTIONS_FLAG, buffer);
                    metadata.on_non_crash_failures = Marshal.ReadInt32(buffer) != 0;
                    Query(service, SERVICE_CONFIG_FAILURE_ACTIONS, buffer);
                    FailureActions config = (FailureActions)Marshal.PtrToStructure(buffer, typeof(FailureActions));
                    metadata.reset_seconds = config.ResetSeconds;
                    metadata.reboot_message = Marshal.PtrToStringUni(config.RebootMessage) ?? String.Empty;
                    metadata.command_line = Marshal.PtrToStringUni(config.CommandLine) ?? String.Empty;
                    metadata.actions = new Action[checked((int)config.ActionCount)];
                    int actionSize = Marshal.SizeOf(typeof(Action));
                    for (int index = 0; index < metadata.actions.Length; index++) {
                        metadata.actions[index] = (Action)Marshal.PtrToStructure(
                            IntPtr.Add(config.Actions, index * actionSize), typeof(Action));
                    }
                } finally { Marshal.FreeHGlobal(buffer); }
            });
            return metadata;
        }

        private static void Change(IntPtr service, uint level, IntPtr config) {
            if (!ChangeServiceConfig2W(service, level, config))
                throw Error("ChangeServiceConfig2W(" + level + ")");
        }

        public static void Write(string name, uint sidType, uint resetSeconds, bool onNonCrashFailures,
                                 string rebootMessage, string commandLine, uint[] actionTypes, uint[] delays) {
            uint access = SERVICE_CHANGE_CONFIG;
            if (Array.IndexOf(actionTypes, 1u) >= 0) access |= SERVICE_START;
            WithService(name, access, service => {
                IntPtr buffer = IntPtr.Zero;
                FailureActions config = new FailureActions();
                try {
                    buffer = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(FailureActions)));
                    Marshal.WriteInt32(buffer, unchecked((int)sidType));
                    Change(service, SERVICE_CONFIG_SERVICE_SID_INFO, buffer);

                    config.ResetSeconds = resetSeconds;
                    config.RebootMessage = Marshal.StringToHGlobalUni(rebootMessage);
                    config.CommandLine = Marshal.StringToHGlobalUni(commandLine);
                    config.ActionCount = (uint)actionTypes.Length;
                    int actionSize = Marshal.SizeOf(typeof(Action));
                    // A non-null pointer with count zero explicitly clears actions.
                    config.Actions = Marshal.AllocHGlobal(checked(actionSize * Math.Max(1, actionTypes.Length)));
                    for (int index = 0; index < actionTypes.Length; index++) {
                        Action action = new Action { type = actionTypes[index], delay_ms = delays[index] };
                        Marshal.StructureToPtr(action, IntPtr.Add(config.Actions, index * actionSize), false);
                    }
                    Marshal.StructureToPtr(config, buffer, false);
                    Change(service, SERVICE_CONFIG_FAILURE_ACTIONS, buffer);

                    Marshal.WriteInt32(buffer, onNonCrashFailures ? 1 : 0);
                    Change(service, SERVICE_CONFIG_FAILURE_ACTIONS_FLAG, buffer);
                } finally {
                    Marshal.FreeHGlobal(config.Actions);
                    Marshal.FreeHGlobal(config.CommandLine);
                    Marshal.FreeHGlobal(config.RebootMessage);
                    Marshal.FreeHGlobal(buffer);
                }
            });
        }
    }
}
"@
}

$UseNativeServiceMetadata = -not (
  (Get-Command Get-KaiwuServiceMetadata -CommandType Function -ErrorAction SilentlyContinue) -and
  (Get-Command Set-KaiwuServiceMetadata -CommandType Function -ErrorAction SilentlyContinue)
)
if ($UseNativeServiceMetadata) {
  function Get-KaiwuServiceMetadata {
    param([string]$Name)
    Initialize-KaiwuServiceConfigurationApi
    return [Kaiwu.Installer.ServiceConfiguration]::Read($Name)
  }
  function Set-KaiwuServiceMetadata {
    param([string]$Name, $Metadata)
    $actionTypes = [uint32[]]@($Metadata.actions | ForEach-Object { $_.type })
    $delays = [uint32[]]@($Metadata.actions | ForEach-Object { $_.delay_ms })
    Initialize-KaiwuServiceConfigurationApi
    [Kaiwu.Installer.ServiceConfiguration]::Write(
      $Name, $Metadata.sid_type, $Metadata.reset_seconds, $Metadata.on_non_crash_failures,
      $Metadata.reboot_message, $Metadata.command_line, $actionTypes, $delays
    )
  }
}

function Test-ServiceDword {
  param($Value)
  return (($Value -is [int] -or $Value -is [long] -or $Value -is [uint32]) -and
    $Value -ge 0 -and $Value -le [uint32]::MaxValue)
}

function Assert-ServiceMetadataValid {
  param($Metadata)
  if ($null -eq $Metadata -or -not (Test-ServiceDword $Metadata.sid_type) -or
      $Metadata.sid_type -notin @(0, 1, 3) -or -not (Test-ServiceDword $Metadata.reset_seconds) -or
      $Metadata.on_non_crash_failures -isnot [bool] -or $Metadata.reboot_message -isnot [string] -or
      $Metadata.command_line -isnot [string] -or $Metadata.actions -isnot [array]) {
    throw "恢复未确认：服务安全标识符或故障动作元数据无效"
  }
  foreach ($action in $Metadata.actions) {
    if (-not (Test-ServiceDword $action.type) -or $action.type -gt 3 -or
        -not (Test-ServiceDword $action.delay_ms)) {
      throw "恢复未确认：服务故障动作类型或延时无效"
    }
  }
}

function Assert-ServiceSnapshotValid {
  param($Snapshot)
  if ($null -eq $Snapshot) { throw "恢复未确认：服务快照缺失" }
  if ([string]$Snapshot.name -ne $ServiceName -or
      [string]::IsNullOrWhiteSpace([string]$Snapshot.binary_path) -or
      [string]::IsNullOrWhiteSpace([string]$Snapshot.display_name) -or
      $Snapshot.start_mode -notin @("Auto", "Manual", "Disabled") -or
      $Snapshot.delayed_auto_start -isnot [bool] -or $Snapshot.was_running -isnot [bool]) {
    throw "恢复未确认：服务快照的名称、程序路径或启动状态无效"
  }
  if ($null -eq $Snapshot.PSObject.Properties["scm_metadata"]) {
    throw "恢复未确认：旧服务快照缺少安全标识符和故障动作元数据"
  }
  Assert-ServiceMetadataValid $Snapshot.scm_metadata
  if ($Snapshot.binary_path -ne (Get-KaiwuProductCommand)) { throw "恢复未确认：服务程序及参数不属于本产品" }
}

function Assert-ServiceMetadataReadback {
  param($Expected)
  $actual = Get-KaiwuServiceMetadata $ServiceName
  Assert-ServiceMetadataValid $actual
  if ($actual.sid_type -ne $Expected.sid_type) {
    throw "恢复未确认：服务安全标识符类型读回不匹配"
  }
  if ($actual.reset_seconds -ne $Expected.reset_seconds -or
      $actual.on_non_crash_failures -ne $Expected.on_non_crash_failures -or
      $actual.reboot_message -cne $Expected.reboot_message -or
      $actual.command_line -cne $Expected.command_line -or
      $actual.actions.Count -ne $Expected.actions.Count) {
    throw "恢复未确认：服务故障策略读回不匹配"
  }
  for ($index = 0; $index -lt $Expected.actions.Count; $index++) {
    if ($actual.actions[$index].type -ne $Expected.actions[$index].type -or
        $actual.actions[$index].delay_ms -ne $Expected.actions[$index].delay_ms) {
      throw "恢复未确认：服务故障动作读回不匹配"
    }
  }
}

function Assert-RestoredServiceRegistration {
  param($Snapshot)
  $record = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction Stop
  if ($null -eq $record -or [string]$record.PathName -ne [string]$Snapshot.binary_path -or
      [string]$record.DisplayName -ne [string]$Snapshot.display_name -or
      [string]$record.StartMode -ne [string]$Snapshot.start_mode -or
      ($Snapshot.start_mode -eq "Auto" -and [bool]$record.DelayedAutoStart -ne $Snapshot.delayed_auto_start)) {
    throw "恢复未确认：服务注册信息读回不匹配"
  }
}

function Restore-VerifiedService {
  param($Manifest, $Snapshot)
  $created = $false
  Assert-KaiwuServiceBackupFiles $Manifest
  Assert-RestoredFiles $Manifest
  try {
    New-Service -Name $ServiceName -BinaryPathName ([string]$Snapshot.binary_path) -StartupType Disabled -DisplayName ([string]$Snapshot.display_name) -ErrorAction Stop | Out-Null
    $created = $true
    Set-KaiwuServiceMetadata $ServiceName $Snapshot.scm_metadata
    Assert-ServiceMetadataReadback $Snapshot.scm_metadata
    if ($Snapshot.start_mode -ne "Disabled") {
      Set-KaiwuStartupMode $Snapshot.start_mode $Snapshot.delayed_auto_start
    }
    Assert-RestoredServiceRegistration $Snapshot
    $service = Get-Service -Name $ServiceName -ErrorAction Stop
    if ($Snapshot.was_running -and $service.Status -eq "Stopped") {
      Assert-RestoredFiles $Manifest
      Start-KaiwuService $Snapshot.start_mode
      Assert-RestoredFiles $Manifest -AfterStart
      Assert-RestoredServiceRegistration $Snapshot
      Assert-ServiceMetadataReadback $Snapshot.scm_metadata
    }
    $service = Get-Service -Name $ServiceName -ErrorAction Stop
    $expectedStatus = if ($Snapshot.was_running) { "Running" } else { "Stopped" }
    if ($null -eq $service -or $service.Status -ne $expectedStatus) {
      throw "恢复未确认：旧服务未保持预期运行状态 ($expectedStatus)"
    }
  } catch {
    $restoreError = $_
    if ($created) {
      try {
        $record = Get-KaiwuServiceRecord
        $handoff = @{}
        Stop-KaiwuWriters $record $handoff
      } catch {
        throw "$($restoreError.Exception.Message)`n未能安全收尾：$($_.Exception.Message)"
      }
    }
    throw $restoreError
  }
}
'@
. ([scriptblock]::Create($ServiceRecoveryFunctions))

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
    [Parameter(Mandatory = $true)][string]$DestinationPath,
    [string]$CriticalSource
  )
  if (-not (Test-Path -LiteralPath $DestinationPath -PathType Leaf)) {
    $script:CreatedManagedFiles.Add($DestinationPath)
  }
  try {
    Copy-Item -LiteralPath $SourcePath -Destination $DestinationPath -Force
  } catch {
    if ($CriticalSource) {
      # A critical source can disappear between verification and the copy.
      $probeErrors = @()
      $sourcePresent = Test-Path -LiteralPath $SourcePath -PathType Leaf -ErrorAction SilentlyContinue -ErrorVariable probeErrors
      if (-not $sourcePresent -and $probeErrors.Count -eq 0) {
        Set-InstallFailure "file_missing" $CriticalSource
      }
    }
    throw
  }
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
  param($Entry)
  $category = [string]$Entry.category
  $path = [string]$Entry.relative_path
  if (-not (Test-KaiwuBackupTarget $category $path)) { throw "备份清单包含非法恢复目标" }
  $root = if ($category -eq "program") { $InstallDir } else { $DataDir }
  return (Join-Path $root ($path -replace '/', '\'))
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
    if ($category -eq "metadata") {
      if ($relativePath -notmatch '^[^/\\:]+$' -or $relativePath -in @(".", "..")) {
        throw "恢复未确认：备份元数据目标无效"
      }
    } elseif (-not (Test-KaiwuBackupTarget $category $relativePath)) {
      throw "恢复未确认：备份清单包含非法目标"
    }
    $key = $category + ":" + $relativePath
    if ($seen.ContainsKey($key)) {
      $script:BackupAvailable = $false
      throw "恢复未确认：备份清单包含重复项"
    }
    $seen[$key] = $true
    if ($entry.absent -eq $true) {
      if ($category -eq "metadata" -or $backupRelativePath -or $expected) {
        throw "恢复未确认：备份清单的缺失项无效"
      }
      continue
    }
    $expectedPath = if ($category -eq "program") { "program/" + $relativePath } else { $relativePath }
    if ($backupRelativePath -cne $expectedPath -or $expected -notmatch '^[0-9a-fA-F]{64}$') {
      throw "恢复未确认：备份文件路径或摘要无效"
    }
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

$BackupDataNames = @(
  "identity.json", "device_key.json", "secrets.json", "auth_profiles.json", "config.json",
  "frpc.toml", "state.json", "revoked.json", "install-state.json", "builtin_demo.json", "diagnostics.json"
)
$ManagedEnvNames = @("KAIWU_CLOUD_BASE", "KAIWU_EDGE_BASE")
$BackupProgramNames = @(
  "kaiwu-connector.exe", "frpc.exe", "版本信息.txt", "install.ps1", "uninstall.ps1",
  "service-handoff.ps1", "windows-smoke.ps1", "open-config.cmd", "uninstall.exe"
)
$PayloadDocs = @(Get-ChildItem (Join-Path $Source "docs\*.md") -File -ErrorAction SilentlyContinue)
$PayloadCompliance = @(Get-ChildItem (Join-Path $Source "compliance\*") -File -ErrorAction SilentlyContinue)
$BackupProgramNames += @($PayloadDocs | ForEach-Object { "docs/" + $_.Name })
$BackupProgramNames += @($PayloadCompliance | ForEach-Object { "compliance/" + $_.Name })

$Stage = "transaction_lock"
Enter-InstallPhase 'lock'
try {
  $Transaction = Enter-KaiwuInstallTransaction
} catch {
  Fail-InstallPhase $_
  Write-InstallResult "PRECHECK_FAILED"
  throw
}

try {
$Stage = "backup_prepare"
Enter-InstallPhase 'backup'
try {
  $EnvironmentBefore = @{}
  foreach ($name in $ManagedEnvNames) {
    $EnvironmentBefore[$name] = Get-KaiwuMachineEnvironmentVariable $name
  }

  $ExistingService = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction Stop
  $HadExistingService = $null -ne $ExistingService
  $NeedsBackup = $HadExistingService -or
    @($BackupDataNames | Where-Object { Test-Path -LiteralPath (Join-Path $DataDir $_) }).Count -gt 0 -or
    @($BackupProgramNames | Where-Object { Test-Path -LiteralPath (Join-Path $InstallDir $_) }).Count -gt 0
  if ($HadExistingService) {
    if ($ExistingService.PathName -ne (Get-KaiwuProductCommand) -or
        $ExistingService.StartName -ne "LocalSystem") {
      throw "原服务路径、参数或运行账户与标准产品安装不符"
    }
    $VersionLine = $null
    $VersionFile = Join-Path $InstallDir "版本信息.txt"
    if (Test-Path -LiteralPath $VersionFile -PathType Leaf) {
      $VersionLine = [string](Get-Content -LiteralPath $VersionFile -Encoding UTF8 -TotalCount 1)
    }
    $ServiceSnapshot = [pscustomobject][ordered]@{
      name = $ServiceName
      display_name = $ExistingService.DisplayName
      binary_path = $ExistingService.PathName
      start_mode = $ExistingService.StartMode
      delayed_auto_start = [bool]$ExistingService.DelayedAutoStart
      was_running = $ExistingService.State -eq "Running"
      version_line = $VersionLine
      scm_metadata = Get-KaiwuServiceMetadata $ServiceName
    }
    Assert-ServiceSnapshotValid $ServiceSnapshot
  }
  $Stage = "quiesce"
  Stop-KaiwuWriters $ExistingService $OriginalHandoff
  $OldInstallChanged = $OriginalHandoff.startup_changed -or $OriginalHandoff.stop_attempted
  $Stage = "backup_prepare"
  if (-not (Test-Path -LiteralPath $DataDir -PathType Container)) {
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
    Set-PrivateDataAcl -Path $DataDir -SkipAttemptJournal
  } elseif (-not $NeedsBackup) {
    Set-PrivateDataAcl -Path $DataDir -SkipAttemptJournal
  }

  if ($NeedsBackup) {
    $BackupDir = Join-Path $DataDir ("backups\" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-" + [guid]::NewGuid().ToString("N") + "-pre-upgrade")
    $BackupManifestPath = Join-Path $BackupDir "manifest\backup-manifest.json"
    New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $BackupDir "program") | Out-Null
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $BackupManifestPath) | Out-Null

    # Protect the backup before copying any credentials. Do not rewrite the existing data tree ACL.
    Set-PrivateDataAcl -Path $BackupDir
    Assert-KaiwuQuiescent $ExistingService
    $BackupEntries = @()
    foreach ($category in @("data", "program")) {
      $names = if ($category -eq "data") { $BackupDataNames } else { $BackupProgramNames }
      foreach ($name in $names) {
        $sourceFile = Get-BackupTargetPath @{ category = $category; relative_path = $name }
        if (-not (Test-Path -LiteralPath $sourceFile)) {
          $BackupEntries += [ordered]@{ category = $category; relative_path = $name; absent = $true }
          continue
        }
        if (-not (Test-Path -LiteralPath $sourceFile -PathType Leaf)) { throw "备份目标不是文件: $name" }
        $relative = if ($category -eq "program") { "program/" + $name } else { $name }
        $destination = Get-BackupPayloadPath $relative
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
        Copy-Item -LiteralPath $sourceFile -Destination $destination -Force
        $entry = New-BackupEntry $category $name $relative
        if ((Get-FileSha256 $sourceFile) -ne $entry.sha256) { throw "复制备份时源文件变化: $name" }
        $BackupEntries += $entry
      }
    }
    $maintenancePath = Join-Path $BackupDir "maintenance.json"
    Write-Utf8Text $maintenancePath (Get-KaiwuMaintenanceRegistration | ConvertTo-Json -Depth 6)
    $BackupEntries += New-BackupEntry "metadata" "maintenance.json" "maintenance.json"
    if ($HadExistingService) {
      $servicePath = Join-Path $BackupDir "service.json"
      Write-Utf8Text $servicePath ($ServiceSnapshot | ConvertTo-Json -Depth 6)
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
#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"
if (-not [Environment]::Is64BitProcess) { throw "请使用 64 位 Windows PowerShell" }
$ExpectedManifestSha256 = "__BACKUP_MANIFEST_SHA256__"
$BackupDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ManifestPath = Join-Path (Join-Path $BackupDir "manifest") "backup-manifest.json"
$ServiceName = "KaiwuConnector"
$InstallDir = Join-Path $env:ProgramFiles "Kaiwu\Connector"
$DataDir = Join-Path $env:ProgramData "Kaiwu\Connector"
$ManagedEnvNames = @("KAIWU_CLOUD_BASE", "KAIWU_EDGE_BASE")
$Listen = "127.0.0.1:17872"
$MaintenanceRegistryNames = @(__MAINTENANCE_REGISTRY_NAMES__)

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

__SERVICE_HANDOFF_FUNCTIONS__

__SERVICE_RECOVERY_FUNCTIONS__

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
  $category = [string]$Entry.category
  $path = [string]$Entry.relative_path
  if (-not (Test-KaiwuBackupTarget $category $path)) { throw "备份清单包含非法恢复目标" }
  $root = if ($category -eq "program") { $InstallDir } else { $DataDir }
  return (Join-Path $root ($path -replace '/', '\'))
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
    if ($category -eq "metadata") {
      if ($relativePath -notmatch '^[^/\\:]+$' -or $relativePath -in @(".", "..")) {
        throw "恢复失败：备份元数据目标无效"
      }
    } elseif (-not (Test-KaiwuBackupTarget $category $relativePath)) {
      throw "恢复失败：备份清单包含非法目标"
    }
    $key = $category + ":" + $relativePath
    if ($seen.ContainsKey($key)) { throw "恢复失败：备份清单包含重复项" }
    $seen[$key] = $true
    if ($entry.absent -eq $true) {
      if ($category -eq "metadata" -or $backupRelativePath -or $expected) {
        throw "恢复失败：备份清单的缺失项无效"
      }
      continue
    }
    $expectedPath = if ($category -eq "program") { "program/" + $relativePath } else { $relativePath }
    if ($backupRelativePath -cne $expectedPath -or $expected -notmatch '^[0-9a-fA-F]{64}$') {
      throw "恢复失败：备份文件路径或摘要无效"
    }
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

function Wait-ServiceRunning {
  $deadline = (Get-Date).AddSeconds(45)
  do {
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($null -ne $service -and $service.Status -eq "Running") { return }
    Start-Sleep -Milliseconds 500
  } while ((Get-Date) -lt $deadline)
  throw "恢复失败：旧服务未进入 Running 状态"
}

$transaction = Enter-KaiwuInstallTransaction
try {
  $manifest = Assert-BackupIntact
  $snapshotEntry = Get-MetadataEntry $manifest "service.json"
  $snapshot = $null
  if ($null -ne $snapshotEntry) {
    $snapshotPath = Get-BackupPayloadPath ([string]$snapshotEntry.backup_relative_path)
    $snapshot = Get-Content -LiteralPath $snapshotPath -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert-ServiceSnapshotValid $snapshot
    Assert-KaiwuServiceBackupFiles $manifest
    if ($UseNativeServiceMetadata) { Initialize-KaiwuServiceConfigurationApi }
  }
  $maintenanceEntry = Get-MetadataEntry $manifest "maintenance.json"
  $maintenance = if ($null -ne $maintenanceEntry) {
    Get-Content -LiteralPath (Get-BackupPayloadPath $maintenanceEntry.backup_relative_path) -Raw -Encoding UTF8 | ConvertFrom-Json
  }

  $current = Get-KaiwuServiceRecord
  $handoff = @{}
  try {
    Stop-KaiwuWriters $current $handoff
    $manifest = Assert-BackupIntact
    Remove-KaiwuStoppedService $current $handoff
  } catch {
    if (-not $handoff.delete_accepted) { Restore-KaiwuHandoff $current $handoff }
    throw
  }
  Restore-KaiwuFiles $manifest
  Restore-Environment $manifest
  if ($null -ne $maintenance) { Restore-KaiwuMaintenanceRegistration $maintenance }
  Assert-RestoredFiles $manifest
  if ($null -ne $snapshot) { Restore-VerifiedService $manifest $snapshot }
} finally {
  $transaction.ReleaseMutex()
  $transaction.Dispose()
}
Write-Host "已核验恢复安装前的程序和配置。业务连通性待确认。"
'@
    $RestoreScript = $RestoreScript.Replace("__BACKUP_MANIFEST_SHA256__", $BackupManifestHash)
    $RestoreScript = $RestoreScript.Replace("__SERVICE_HANDOFF_FUNCTIONS__", $ServiceHandoffFunctions)
    $RestoreScript = $RestoreScript.Replace("__SERVICE_RECOVERY_FUNCTIONS__", $ServiceRecoveryFunctions)
    $RestoreScript = $RestoreScript.Replace("__MAINTENANCE_REGISTRY_NAMES__", (($MaintenanceRegistryNames | ForEach-Object { '"' + $_ + '"' }) -join ', '))
    $restorePath = Join-Path $BackupDir "restore.ps1"
    [System.IO.File]::WriteAllText(
      $restorePath,
      $RestoreScript,
      [System.Text.UTF8Encoding]::new($true)
    )
    Set-PrivateDataAcl -Path $BackupDir
    $verifiedBackup = Assert-BackupIntact
    Assert-KaiwuQuiescent $ExistingService
    try {
      if ($HadExistingService) { Assert-KaiwuServiceBackupFiles $verifiedBackup }
      Assert-RestoredFiles $verifiedBackup
    } catch {
      $BackupAvailable = $false
      throw
    }
    Write-Host "升级前程序、配置和本机秘密已备份并校验到 $BackupDir"
  }

  $Stage = "pre_change_verification"
  Enter-InstallPhase 'verify'
  Assert-CriticalFiles
  $Stage = "replace_install"
  Enter-InstallPhase 'replace'
  Remove-KaiwuStoppedService $ExistingService $OriginalHandoff
  $InstallMutationStarted = $true
  $OldInstallChanged = $NeedsBackup
  if (-not $NeedsBackup) {
    foreach ($name in $BackupDataNames) { $CreatedManagedFiles.Add((Join-Path $DataDir $name)) }
  }

  Ensure-ManagedDirectory $InstallDir
  Ensure-ManagedDirectory (Join-Path $InstallDir "docs")
  Ensure-ManagedDirectory (Join-Path $InstallDir "compliance")

  Copy-ManagedFile (Join-Path $Source "bin\kaiwu-connector.exe") (Join-Path $InstallDir "kaiwu-connector.exe") -CriticalSource "bin/kaiwu-connector.exe"
  Copy-ManagedFile (Join-Path $Source "bin\frpc.exe") (Join-Path $InstallDir "frpc.exe") -CriticalSource "bin/frpc.exe"
  $PayloadDocs | ForEach-Object {
      Copy-ManagedFile $_.FullName (Join-Path (Join-Path $InstallDir "docs") $_.Name)
    }
  $PayloadCompliance | ForEach-Object {
      Copy-ManagedFile $_.FullName (Join-Path (Join-Path $InstallDir "compliance") $_.Name)
    }
  foreach ($name in @("service-handoff.ps1", "uninstall.ps1", "windows-smoke.ps1", "install.ps1", "版本信息.txt")) {
    $item = Join-Path $Source $name
    if (Test-Path -LiteralPath $item -PathType Leaf) {
      Copy-ManagedFile $item (Join-Path $InstallDir $name)
    }
  }

  Assert-CriticalFiles -Target

  # 包内地址是缺省值。升级或重装保留已经设置的机器级端点。
  $EnvFile = Join-Path $Source "config\connector.env.example"
  if (Test-Path -LiteralPath $EnvFile -PathType Leaf) {
    foreach ($line in [IO.File]::ReadAllLines($EnvFile, [Text.Encoding]::UTF8)) {
      if ($line -notmatch '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$') { continue }
      $name = $matches[1]
      $value = $matches[2].Trim()
      if ($name -notin $ManagedEnvNames) { continue }
      $existing = $EnvironmentBefore[$name]
      if (-not [string]::IsNullOrWhiteSpace($existing)) {
        Set-Item -LiteralPath ("Env:" + $name) -Value $existing
        Write-Host "保留已有环境变量 $name"
        continue
      }
      if ([string]::IsNullOrWhiteSpace($value)) { continue }
      Set-KaiwuMachineEnvironmentVariable $name $value
      Set-Item -LiteralPath ("Env:" + $name) -Value $value
      if ((Get-KaiwuMachineEnvironmentVariable $name) -cne $value) { throw "写入机器级环境变量失败: $name" }
      Write-Host "已设置环境变量 $name"
    }
  }

  Enter-InstallPhase 'service'
  $BinPath = Get-KaiwuProductCommand
  # Register disabled until the program, startup settings and security metadata are in place.
  $ReplacementSnapshot = if ($HadExistingService) { $ServiceSnapshot } else {
    [pscustomobject]@{
      binary_path = $BinPath; display_name = "Kaiwu Intranet Connector"; start_mode = "Auto"
      delayed_auto_start = $false; was_running = $true
    }
  }
  try {
    New-Service -Name $ServiceName -BinaryPathName $BinPath -StartupType Disabled -DisplayName $ReplacementSnapshot.display_name -ErrorAction Stop | Out-Null
  } catch {
    Set-InstallFailure "service_registration"
    throw "注册服务失败: $($_.Exception.Message)"
  }
  $ReplacementServiceCreated = $true
  if ($HadExistingService) {
    Set-KaiwuServiceMetadata $ServiceName $ServiceSnapshot.scm_metadata
    Assert-ServiceMetadataReadback $ServiceSnapshot.scm_metadata
  } else {
    & sc.exe sidtype $ServiceName unrestricted | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "设置服务安全标识失败" }
    & sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/15000/restart/60000 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "设置服务故障动作失败" }
  }
  if ($ReplacementSnapshot.start_mode -ne "Disabled") {
    Set-KaiwuStartupMode $ReplacementSnapshot.start_mode $ReplacementSnapshot.delayed_auto_start
  }
  Assert-RestoredServiceRegistration $ReplacementSnapshot
  if ($ReplacementSnapshot.was_running) {
    try {
      Start-KaiwuService $ReplacementSnapshot.start_mode
    } catch {
      Set-InstallFailure "service_start"
      throw "启动服务失败: $($_.Exception.Message)"
    }
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
      Set-InstallFailure "local_page_unready"
      throw "服务已经启动，但本机配置页面 http://$Listen/ 未在 30 秒内返回 200"
    }
  }
  Assert-RestoredServiceRegistration $ReplacementSnapshot
  $service = Get-Service -Name $ServiceName -ErrorAction Stop
  $expectedStatus = if ($ReplacementSnapshot.was_running) { "Running" } else { "Stopped" }
  if ($service.Status -ne $expectedStatus) { throw "安装后的服务未保持预期状态 ($expectedStatus)" }

  $Stage = "final_verification"
  Enter-InstallPhase 'verify'
  Assert-CriticalFiles -Target

  $VersionLine = "unknown"
  $VersionFile = Join-Path $Source "版本信息.txt"
  if (Test-Path -LiteralPath $VersionFile) {
    # Get-Content -TotalCount returns the string itself. Piping through
    # Select-Object would persist a Selected.System.String wrapper and its
    # installer-temporary metadata into install-state.json on PowerShell 5.1.
    $VersionLine = [string](Get-Content -LiteralPath $VersionFile -Encoding UTF8 -TotalCount 1)
  }
  $InstallStatePath = Join-Path $DataDir "install-state.json"
  if (-not (Test-Path -LiteralPath $InstallStatePath -PathType Leaf)) {
    $CreatedManagedFiles.Add($InstallStatePath)
  }
  [ordered]@{
    version = $VersionLine
    installed_at = (Get-Date).ToUniversalTime().ToString("o")
    install_dir = $InstallDir
    data_dir = $DataDir
    previous_backup = $BackupDir
    source = "windows-installer"
  } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $InstallStatePath -Encoding UTF8

  Write-Host ""
  Write-Host "安装完成。"
  Write-Host "服务名称：$ServiceName（$($service.Status)）"
  Write-Host "本机配置界面：http://$Listen/"
  Write-Host "数据目录：$DataDir"
  Write-Host "注意：界面只监听本机回环地址，不要通过防火墙对外开放。"
  $Stage = "installed"
  $Cleanup = "not_needed"
  Write-InstallEvent $AttemptPhase 'ok' 'ok'
  Write-InstallResult "INSTALLED"
} catch {
  $OriginalError = $_.Exception.Message
  Fail-InstallPhase $_
  $FailureStage = $Stage
  $PriorHandoffChanged = $OriginalHandoff.startup_changed -or $OriginalHandoff.stop_attempted
  if (-not $InstallMutationStarted -and -not $OriginalHandoff.delete_accepted -and -not $PriorHandoffChanged) {
    Write-InstallResult "PRECHECK_FAILED"
    throw "安装前检查失败：$OriginalError"
  }
  $OldInstallChanged = $OldInstallChanged -or $PriorHandoffChanged
  $Stage = "rollback"
  Enter-InstallPhase 'rollback'
  $RollbackErrors = @()
  $CanRestoreFiles = $false
  try {
    if ($ReplacementServiceCreated) {
      $current = Get-KaiwuServiceRecord
      $replacementHandoff = @{}
      try {
        Stop-KaiwuWriters $current $replacementHandoff
        Remove-KaiwuStoppedService $current $replacementHandoff
      } catch {
        if (-not $replacementHandoff.delete_accepted) { Restore-KaiwuHandoff $current $replacementHandoff }
        throw
      }
    }
    if ($OriginalHandoff.delete_accepted -and -not $InstallMutationStarted) {
      Wait-ServiceAbsent
    }
    if ($InstallMutationStarted -or $OriginalHandoff.delete_accepted) {
      Assert-KaiwuQuiescent $null
      $CanRestoreFiles = $true
    } else {
      # The original registration and files are still in place, even if no backup was completed.
      Restore-KaiwuHandoff $ExistingService $OriginalHandoff
    }
  } catch {
    $RollbackErrors += "确认当前服务状态: $($_.Exception.Message)"
  }

  if ($CanRestoreFiles -and $NeedsBackup) {
    try {
      $manifest = Assert-BackupIntact
      $snapshot = $null
      if ($HadExistingService) {
        $snapshot = Get-BackupServiceSnapshot $manifest
        Assert-ServiceSnapshotValid $snapshot
      }
      Remove-ManagedFilesCreatedByRun
      Restore-KaiwuFiles $manifest
      Restore-EnvironmentSnapshot $manifest
      Assert-RestoredFiles $manifest
      if ($HadExistingService) { Restore-VerifiedService $manifest $snapshot }
      Write-Host "安装失败，已核验恢复安装前程序和配置。业务连通性待确认。"
    } catch {
      $RollbackErrors += "恢复旧安装: $($_.Exception.Message)"
    }
  } elseif ($CanRestoreFiles) {
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
    Write-InstallEvent 'rollback' 'ok' 'restored'
    if ($NeedsBackup) {
      $Stage = "restored"
      $Cleanup = "restored"
      Write-InstallResult "FAILED_RESTORED"
    } else {
      $Stage = "cleaned"
      $Cleanup = "cleaned"
      Write-InstallResult "FAILED_NO_PRIOR_CLEANED"
    }
  } else {
    Write-InstallEvent 'rollback' 'failed' 'recovery_unconfirmed'
    if ($NeedsBackup) {
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
    if ($NeedsBackup) { "已核验恢复安装前程序、配置和服务状态。业务连通性待确认。" } else { "已清理本次新增受管文件。" }
  } else {
    "恢复或清理未确认：" + ($RollbackErrors -join "; ")
  }
  throw "安装失败：$OriginalError`n$RollbackDetail`n备份目录：$BackupDir"
}
} finally {
  $Transaction.ReleaseMutex()
  $Transaction.Dispose()
}
