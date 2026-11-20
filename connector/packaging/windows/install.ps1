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
$FailureStage = $null
$OriginalServiceStopAttempted = $false
$OriginalServiceDeleteAccepted = $false
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
  if ($FailureStage) { $lines += "failure_stage=$FailureStage" }
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
    $record = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction Stop
    if ($null -eq $record) { return }
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
  param([string]$Name = $ServiceName, [switch]$Original)
  $service = Get-Service -Name $Name -ErrorAction Stop
  Assert-ServiceStoppable $service
  if ($service.Status -eq "Stopped") { return }
  if ($Original) {
    $script:OriginalServiceStopAttempted = $true
    $script:OldInstallChanged = $true
  }
  Stop-Service -Name $Name -ErrorAction Stop
  Wait-ServiceStopped -Name $Name
}

function Stop-AndDeleteService {
  param([switch]$Original)
  $record = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction Stop
  if ($Original -and -not $script:HadExistingService) {
    if ($null -ne $record) { throw "替换前出现未备份的同名服务" }
    return
  }
  $expectedPath = if ($Original) { $script:ServiceSnapshot.binary_path } else { $script:BinPath }
  if ($null -eq $record -or [string]$record.PathName -ne [string]$expectedPath) {
    throw "无法确认待删除的同名服务"
  }
  Stop-TargetService -Original:$Original
  & sc.exe delete $ServiceName | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "删除服务 $ServiceName 失败：sc.exe 退出码为 $LASTEXITCODE"
  }
  if ($Original) {
    $script:OriginalServiceDeleteAccepted = $true
    $script:OldInstallChanged = $true
  }
  Wait-ServiceAbsent
}

# 同一份服务恢复逻辑同时用于自动回滚和生成的独立恢复脚本。
$ServiceRecoveryFunctions = @'
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

function Assert-RestoredStableFiles {
  param($Manifest, $Snapshot)
  # state.json 在服务启动后可合法变化。重建服务前已核验全量文件。
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
  if ($null -eq $Snapshot) { return }
  foreach ($name in @("kaiwu-connector.exe", "frpc.exe")) {
    if (-not (Test-Path -LiteralPath (Join-Path $InstallDir $name) -PathType Leaf)) {
      throw "恢复后关键程序缺失: $name"
    }
  }
  if (-not [string]::IsNullOrWhiteSpace([string]$Snapshot.version_line)) {
    $versionPath = Join-Path $InstallDir "版本信息.txt"
    $actualVersion = [string](Get-Content -LiteralPath $versionPath -Encoding UTF8 -TotalCount 1)
    if ($actualVersion -ne [string]$Snapshot.version_line) {
      throw "恢复未确认：旧版本信息不匹配"
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
  param($Manifest, $Snapshot, [switch]$ReuseExisting)
  $created = $false
  $startAttempted = $false
  Assert-RestoredStableFiles $Manifest $Snapshot
  try {
    if (-not $ReuseExisting) {
      # 保持禁用，直到服务控制管理器读回的配置与快照一致。
      New-Service -Name $ServiceName -BinaryPathName ([string]$Snapshot.binary_path) -StartupType Disabled -DisplayName ([string]$Snapshot.display_name) -ErrorAction Stop | Out-Null
      $created = $true
      Set-KaiwuServiceMetadata $ServiceName $Snapshot.scm_metadata
    }
    Assert-ServiceMetadataReadback $Snapshot.scm_metadata
    if ($created) {
      if ($Snapshot.start_mode -eq "Auto" -and $Snapshot.delayed_auto_start) {
        & sc.exe config $ServiceName start= delayed-auto | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "恢复未确认：设置旧服务延迟启动失败" }
      } elseif ($Snapshot.start_mode -ne "Disabled") {
        $startupType = if ($Snapshot.start_mode -eq "Auto") { "Automatic" } else { "Manual" }
        Set-Service -Name $ServiceName -StartupType $startupType -ErrorAction Stop
      }
    }
    Assert-RestoredServiceRegistration $Snapshot
    $service = Get-Service -Name $ServiceName -ErrorAction Stop
    if ($Snapshot.was_running -and $service.Status -eq "Stopped") {
      $startAttempted = $true
      Start-Service -Name $ServiceName -ErrorAction Stop
      Wait-ServiceRunning | Out-Null
      Assert-RestoredStableFiles $Manifest $Snapshot
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
    if ($created -or $startAttempted) {
      try {
        $service = Get-Service -Name $ServiceName -ErrorAction Stop
        Assert-ServiceStoppable $service
        $record = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction Stop
        if ($null -eq $record -or [string]$record.PathName -ne [string]$Snapshot.binary_path) {
          throw "无法确认同名服务属于本次恢复"
        }
        # 禁用新建服务以拦住已排队的自动重启。原有注册则保留其启动配置。
        if ($created -and $record.StartMode -ne "Disabled") {
          Set-Service -Name $ServiceName -StartupType Disabled -ErrorAction Stop
        }
        Stop-TargetService
        if ($created) {
          $record = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction Stop
          if ($null -eq $record -or $record.StartMode -ne "Disabled") {
            throw "无法核验未确认恢复的服务已禁用"
          }
        }
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

$Stage = "backup_prepare"
try {
  $ExistingService = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction Stop
  $HadExistingService = $null -ne $ExistingService
  $NeedsBackup = $HadExistingService -or ($ExistingSensitive.Count -gt 0) -or ($ExistingProgramFiles.Count -gt 0)
  if ($HadExistingService) {
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
    $record = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction Stop
    if ($null -eq $record) { return }
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

function Stop-TargetService {
  $service = Get-Service -Name $ServiceName -ErrorAction Stop
  Assert-ServiceStoppable $service
  if ($service.Status -eq "Stopped") { return }
  Stop-Service -Name $ServiceName -ErrorAction Stop
  Wait-ServiceStopped
}

function Stop-AndDeleteCurrentService {
  $record = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction Stop
  if ($null -eq $record) { return }
  Stop-TargetService
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

function Wait-ServiceRunning {
  $deadline = (Get-Date).AddSeconds(45)
  do {
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($null -ne $service -and $service.Status -eq "Running") { return }
    Start-Sleep -Milliseconds 500
  } while ((Get-Date) -lt $deadline)
  throw "恢复失败：旧服务未进入 Running 状态"
}

$manifest = Assert-BackupIntact
$snapshotEntry = Get-MetadataEntry $manifest "service.json"
$snapshot = $null
if ($null -ne $snapshotEntry) {
  $snapshotPath = Get-BackupPayloadPath ([string]$snapshotEntry.backup_relative_path)
  $snapshot = Get-Content -LiteralPath $snapshotPath -Raw -Encoding UTF8 | ConvertFrom-Json
  Assert-ServiceSnapshotValid $snapshot
  if ($UseNativeServiceMetadata) { Initialize-KaiwuServiceConfigurationApi }
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
  Restore-VerifiedService $manifest $snapshot
} else {
  Assert-RestoredStableFiles $manifest $null
}
Write-Host "已恢复安装前版本。"
'@
    $RestoreScript = $RestoreScript.Replace("__BACKUP_MANIFEST_SHA256__", $BackupManifestHash)
    $RestoreScript = $RestoreScript.Replace("__SERVICE_RECOVERY_FUNCTIONS__", $ServiceRecoveryFunctions)
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
  Stop-AndDeleteService -Original
  $InstallMutationStarted = $true
  $OldInstallChanged = $NeedsBackup

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

  $ReplacementServiceCreated = $true
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
  $FailureStage = $Stage
  if ($FailureStage -eq "pre_change_verification") {
    Write-InstallResult "PRECHECK_FAILED"
    throw "安装前检查失败：$OriginalError"
  }
  $Stage = "rollback"
  $RollbackErrors = @()
  $CanRestoreFiles = $false
  try {
    if ($ReplacementServiceCreated) { Stop-AndDeleteService }
    if ($InstallMutationStarted) {
      $CanRestoreFiles = $true
    } elseif ($OriginalServiceDeleteAccepted) {
      # 删除已受理但尚未确认；等待删除完成，不再按名称删除。
      Wait-ServiceAbsent
      $CanRestoreFiles = $true
    } elseif ($OriginalServiceStopAttempted) {
      # 文件替换尚未开始。若原注册仍可核对，只恢复其运行状态。
      $manifest = Assert-BackupIntact
      $snapshot = Get-BackupServiceSnapshot $manifest
      Assert-ServiceSnapshotValid $snapshot
      Restore-VerifiedService $manifest $snapshot -ReuseExisting
    } else {
      throw "原服务未获准替换，保留现有服务及文件"
    }
  } catch {
    $RollbackErrors += "确认当前服务状态: $($_.Exception.Message)"
  }

  if ($CanRestoreFiles -and $BackupDir) {
    try {
      $manifest = Assert-BackupIntact
      $snapshot = $null
      if ($HadExistingService) {
        $snapshot = Get-BackupServiceSnapshot $manifest
        Assert-ServiceSnapshotValid $snapshot
      }
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
      if ($HadExistingService) {
        Restore-VerifiedService $manifest $snapshot
      } else {
        Assert-RestoredStableFiles $manifest $null
      }
      Write-Host "安装失败，已核验恢复安装前程序和配置。"
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
