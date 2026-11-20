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
$MaintenanceRegistryNames = @(
  "DisplayName", "DisplayVersion", "Publisher", "InstallLocation", "DisplayIcon", "UninstallString",
  "QuietUninstallString", "Comments", "NoModify", "NoRepair", "EstimatedSize"
)
if ([string]::IsNullOrWhiteSpace($ResultPath)) {
  $ResultPath = Join-Path $env:TEMP "KaiwuConnector-install-result.txt"
}

$Stage = "startup"
$FailureStage = $null
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
  if (-not [Environment]::Is64BitProcess) { throw "请使用 64 位 Windows PowerShell" }
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

# 同一份服务恢复逻辑同时用于自动回滚和生成的独立恢复脚本。
$ServiceRecoveryFunctions = @'
function Get-KaiwuServiceRecord {
  Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction Stop
}

function Get-KaiwuProductCommand {
  $binary = Join-Path $InstallDir "kaiwu-connector.exe"
  return ('"{0}" serve --data-dir "{1}" --listen {2}' -f $binary, $DataDir, $Listen)
}

function Get-KaiwuProcessRows {
  Get-CimInstance Win32_Process -Filter "Name='kaiwu-connector.exe' OR Name='frpc.exe'" -ErrorAction Stop
}

function Get-KaiwuRelevantProcesses {
  param([uint32]$ServicePid = 0)
  $config = [regex]::Escape((Join-Path $DataDir "frpc.toml"))
  $configArgument = '(?:^|\s)-c\s+(?:"' + $config + '"|' + $config + ')(?=\s|$)'
  foreach ($row in @(Get-KaiwuProcessRows)) {
    $id = [uint32]$row.ProcessId
    if ($id -eq 0 -or [string]::IsNullOrWhiteSpace($row.ExecutablePath)) {
      throw "无法读取同名进程的映像或进程号 ($id)，拒绝交接"
    }
    if ($row.Name -eq "frpc.exe" -and [string]::IsNullOrWhiteSpace($row.CommandLine)) {
      throw "无法读取同名隧道进程的命令行 ($id)，拒绝交接"
    }
    $path = Join-Path $InstallDir $row.Name
    if ($row.ExecutablePath -ne $path) {
      if (($ServicePid -ne 0 -and ($id -eq $ServicePid -or $row.ParentProcessId -eq $ServicePid)) -or
          ($row.Name -eq "frpc.exe" -and $row.CommandLine -match $configArgument)) {
        throw "发现使用本产品服务或配置但映像路径不同的进程 ($id)"
      }
      continue
    }
    if ($row.CreationDate -isnot [datetime]) { throw "无法读取产品进程创建时间 ($id)" }
    if ($row.Name -eq "frpc.exe") {
      # 两代守护器均以 exec.CommandContext(binary, "-c", config) 启动，只有一个配置参数。
      $image = [regex]::Escape($path)
      $imageArg = if ($path -match '\s') { '"' + $image + '"' } else { '(?:"' + $image + '"|' + $image + ')' }
      $configArg = if ((Join-Path $DataDir "frpc.toml") -match '\s') {
        '"' + $config + '"'
      } else {
        '(?:"' + $config + '"|' + $config + ')'
      }
      $command = '^' + $imageArg + '\s+-c\s+' + $configArg + '\s*$'
      if ($row.CommandLine -notmatch $command) { throw "无法核验隧道进程的完整配置参数 ($id)" }
    }
    $row
  }
}

if (-not (Get-Command Open-KaiwuProcess -CommandType Function -ErrorAction SilentlyContinue)) {
function Open-KaiwuProcess {
  param($Row, [switch]$CanTerminate)
  if (-not ('Kaiwu.Installer.ProcessHandle' -as [type])) {
    Add-Type -ErrorAction Stop -TypeDefinition @"
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace Kaiwu.Installer {
    public sealed class ProcessHandle : IDisposable {
        private const uint QueryLimitedInformation = 0x1000;
        private const uint Synchronize = 0x00100000;
        private const uint TerminateAccess = 0x0001;
        private const uint WaitObject = 0;
        private const uint WaitTimeout = 258;
        private readonly SafeProcessHandle handle;

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern SafeProcessHandle OpenProcess(uint access, bool inherit, uint pid);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetProcessTimes(SafeProcessHandle handle, out long creation,
            out long exit, out long kernel, out long user);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
        private static extern bool QueryFullProcessImageNameW(SafeProcessHandle handle, int flags,
            StringBuilder path, ref int length);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint WaitForSingleObject(SafeProcessHandle handle, uint milliseconds);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool TerminateProcess(SafeProcessHandle handle, uint exitCode);

        public ProcessHandle(uint pid, bool canTerminate, string expectedPath, DateTime expectedCreationUtc) {
            uint access = QueryLimitedInformation | Synchronize;
            if (canTerminate) access |= TerminateAccess;
            handle = OpenProcess(access, false, pid);
            try {
                if (handle.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error());
                long creation, exit, kernel, user;
                if (!GetProcessTimes(handle, out creation, out exit, out kernel, out user))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                int length = 32768;
                var path = new StringBuilder(length);
                if (!QueryFullProcessImageNameW(handle, 0, path, ref length))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                // CIM exposes creation time at microsecond precision. The handle uses 100 ns ticks.
                long delta = DateTime.FromFileTimeUtc(creation).Ticks - expectedCreationUtc.Ticks;
                if (delta <= -10 || delta >= 10 ||
                    !String.Equals(path.ToString(), expectedPath, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidOperationException("Process identity changed before its handle was opened.");
            } catch {
                handle.Dispose();
                throw;
            }
        }

        public bool WaitForExit(uint milliseconds) {
            uint result = WaitForSingleObject(handle, milliseconds);
            if (result == WaitObject) return true;
            if (result == WaitTimeout) return false;
            throw new Win32Exception(Marshal.GetLastWin32Error());
        }

        public void Terminate() {
            if (!TerminateProcess(handle, 1)) {
                int error = Marshal.GetLastWin32Error();
                if (!WaitForExit(0)) throw new Win32Exception(error);
            }
        }

        public void Dispose() { handle.Dispose(); }
    }
}
"@
  }
  return [Kaiwu.Installer.ProcessHandle]::new(
    [uint32]$Row.ProcessId, [bool]$CanTerminate, [string]$Row.ExecutablePath, $Row.CreationDate.ToUniversalTime()
  )
}
}

function Assert-KaiwuQuiescent {
  param($PreviousRecord)
  $current = Get-KaiwuServiceRecord
  if ($null -eq $PreviousRecord) {
    if ($null -ne $current) { throw "交接时发现未接管的同名服务" }
  } elseif ($null -eq $current -or $current.PathName -ne $PreviousRecord.PathName -or
      $current.State -ne "Stopped" -or [uint32]$current.ProcessId -ne 0 -or $current.StartMode -ne "Disabled") {
    throw "服务注册或停止状态在交接时发生变化"
  }
  if (@(Get-KaiwuRelevantProcesses).Count -gt 0) {
    throw "仍有产品进程存活或出现未捕获的进程，拒绝备份或写入"
  }
}

function Assert-KaiwuPidMarker {
  param($Children)
  $marker = Join-Path $DataDir "frpc.pid"
  if (-not (Test-Path -LiteralPath $marker)) { return }
  $id = [uint32]0
  $raw = Get-Content -LiteralPath $marker -Raw -ErrorAction Stop
  if (-not [uint32]::TryParse(([string]$raw).Trim(), [ref]$id) -or $id -eq 0) { return }
  $sameId = @(Get-KaiwuProcessRows | Where-Object { $_.ProcessId -eq $id })
  if ($sameId.Count -eq 0) { return }
  if ($sameId.Count -ne 1 -or @($Children | Where-Object {
    $_.ProcessId -eq $id -and $_.CreationDate -eq $sameId[0].CreationDate -and
      $_.ExecutablePath -eq $sameId[0].ExecutablePath
  }).Count -ne 1) {
    throw "隧道进程号记录与已核验的子进程不符 ($id)，拒绝接管"
  }
}

function Move-KaiwuRuntimePidMarker {
  # 任一版本启动时都会按 PID 和名称尝试清除旧 frpc；旧 PID 不能跨这次进程交接使用。
  $marker = Join-Path $DataDir "frpc.pid"
  if (Test-Path -LiteralPath $marker) {
    $name = "frpc.pid.pre-handoff-" + [guid]::NewGuid().ToString("N")
    Rename-Item -LiteralPath $marker -NewName $name -ErrorAction Stop
  }
}

function Stop-KaiwuWriters {
  param($Record, $Handoff)
  if ($null -eq $Record) {
    Assert-KaiwuQuiescent $null
    Assert-KaiwuPidMarker @()
    return
  }
  if ($Record.PathName -ne (Get-KaiwuProductCommand) -or $Record.StartName -ne "LocalSystem" -or $Record.State -notin @("Running", "Stopped") -or
      $Record.StartMode -notin @("Auto", "Manual", "Disabled") -or
      ($Record.State -eq "Running") -ne ([uint32]$Record.ProcessId -ne 0)) {
    throw "无法确认是受支持的本产品服务注册或稳定运行状态"
  }
  Assert-ServiceStoppable (Get-Service -Name $ServiceName -ErrorAction Stop)
  $servicePid = [uint32]$Record.ProcessId
  $processes = @(Get-KaiwuRelevantProcesses $servicePid)
  $parents = @($processes | Where-Object { $_.Name -eq "kaiwu-connector.exe" })
  $children = @($processes | Where-Object { $_.Name -eq "frpc.exe" })
  if ($servicePid -eq 0) {
    if ($processes.Count -gt 0) { throw "服务已停止且仍有产品进程，无法核验进程归属" }
  } else {
    if ($parents.Count -ne 1 -or $parents[0].ProcessId -ne $servicePid) {
      throw "无法核验服务控制管理器报告的连接器进程 ($servicePid)"
    }
    foreach ($child in $children) {
      if ($child.ParentProcessId -ne $servicePid -or $child.CreationDate -lt $parents[0].CreationDate) {
        throw "无法核验隧道进程的父进程或创建顺序 ($($child.ProcessId))"
      }
    }
  }
  Assert-KaiwuPidMarker $children

  $serviceHandle = $null
  $tunnels = @()
  try {
    if ($servicePid -ne 0) {
      $serviceHandle = Open-KaiwuProcess $parents[0]
      foreach ($child in $children) {
        $tunnels += [pscustomobject]@{ Row = $child; Handle = (Open-KaiwuProcess $child -CanTerminate) }
      }
    }
    $current = Get-KaiwuServiceRecord
    if ($null -eq $current -or $current.PathName -ne $Record.PathName -or $current.State -ne $Record.State -or
        $current.ProcessId -ne $Record.ProcessId -or $current.StartMode -ne $Record.StartMode -or
        [bool]$current.DelayedAutoStart -ne [bool]$Record.DelayedAutoStart) {
      throw "服务注册在捕获进程句柄时发生变化"
    }
    if ($Record.StartMode -ne "Disabled") {
      $Handoff.startup_changed = $true
      Set-Service -Name $ServiceName -StartupType Disabled -ErrorAction Stop
    }
    if ($servicePid -ne 0) {
      $Handoff.stop_attempted = $true
      & sc.exe stop $ServiceName | Out-Null
      if ($LASTEXITCODE -ne 0) { throw "停止服务 $ServiceName 失败，退出码 $LASTEXITCODE" }
      Wait-ServiceStopped
      if (-not $serviceHandle.WaitForExit(10000)) { throw "服务进程仍未退出 ($servicePid)" }
    }
    $current = Get-KaiwuServiceRecord
    if ($null -eq $current -or $current.PathName -ne $Record.PathName -or
        $current.State -ne "Stopped" -or [uint32]$current.ProcessId -ne 0 -or $current.StartMode -ne "Disabled") {
      throw "未确认原服务已停止并阻止重新启动"
    }
    foreach ($tunnel in $tunnels) {
      if ($tunnel.Handle.WaitForExit(1000)) { continue }
      $old = $tunnel.Row
      $now = @(Get-KaiwuRelevantProcesses $servicePid | Where-Object { $_.ProcessId -eq $old.ProcessId })
      if ($tunnel.Handle.WaitForExit(0)) { continue }
      if ($now.Count -ne 1 -or $now[0].CreationDate -ne $old.CreationDate -or
          $now[0].ParentProcessId -ne $old.ParentProcessId -or
          $now[0].ExecutablePath -ne $old.ExecutablePath -or $now[0].CommandLine -cne $old.CommandLine) {
        throw "停止服务后，隧道进程身份无法再次核验 ($($old.ProcessId))"
      }
      $tunnel.Handle.Terminate()
      if (-not $tunnel.Handle.WaitForExit(10000)) { throw "隧道进程仍未退出 ($($old.ProcessId))" }
      Write-Host "已确认旧隧道进程退出 (PID $($old.ProcessId))"
    }
    Assert-KaiwuQuiescent $Record
  } finally {
    foreach ($tunnel in $tunnels) { $tunnel.Handle.Dispose() }
    if ($null -ne $serviceHandle) { $serviceHandle.Dispose() }
  }
}

function Set-KaiwuStartupMode {
  param([string]$Mode, [bool]$Delayed)
  if ($Mode -eq "Auto" -and $Delayed) {
    & sc.exe config $ServiceName start= delayed-auto | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "设置服务延迟启动失败" }
    return
  }
  $startupType = if ($Mode -eq "Auto") { "Automatic" } else { $Mode }
  Set-Service -Name $ServiceName -StartupType $startupType -ErrorAction Stop
}

function Start-KaiwuService {
  param([string]$Mode)
  Move-KaiwuRuntimePidMarker
  if ($Mode -eq "Disabled") {
    # SCM 允许正在运行的服务被改为禁用。重现这个组合需要临时允许手动启动。
    Set-KaiwuStartupMode "Manual" $false
  }
  try {
    & sc.exe start $ServiceName | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "启动服务 $ServiceName 失败，退出码 $LASTEXITCODE" }
    Wait-ServiceRunning | Out-Null
  } finally {
    if ($Mode -eq "Disabled") { Set-KaiwuStartupMode "Disabled" $false }
  }
}

function Restore-KaiwuHandoff {
  param($Record, $Handoff)
  if (-not ($Handoff.startup_changed -or $Handoff.stop_attempted)) { return }
  $current = Get-KaiwuServiceRecord
  if ($null -eq $current -or $current.PathName -ne $Record.PathName -or
      $current.DisplayName -ne $Record.DisplayName) {
    throw "无法核验原服务注册，未尝试启动"
  }
  if ($Handoff.stop_attempted -and $current.State -eq "Stop Pending") {
    Wait-ServiceStopped
    $current = Get-KaiwuServiceRecord
  }
  if ($null -eq $current -or $current.State -notin @("Running", "Stopped")) {
    throw "无法核验交接中断后的服务状态"
  }
  $needsRestart = $Record.State -eq "Running" -and $current.State -eq "Stopped"
  if ($needsRestart) {
    # Do not enable a restart while the old connector or its stale PID marker can still interfere.
    $connectorImage = Join-Path $InstallDir "kaiwu-connector.exe"
    $blocking = @(Get-KaiwuProcessRows | Where-Object {
      $_.Name -eq "kaiwu-connector.exe" -and
        ($_.ProcessId -eq $Record.ProcessId -or $_.ExecutablePath -eq $connectorImage -or
         [string]::IsNullOrWhiteSpace($_.ExecutablePath))
    })
    if ($blocking.Count -gt 0) { throw "原服务进程尚未确认退出，未重新启用服务" }
    Move-KaiwuRuntimePidMarker
  }
  if ($Handoff.startup_changed) {
    Set-KaiwuStartupMode $Record.StartMode ([bool]$Record.DelayedAutoStart)
  }
  if ($needsRestart) { Start-KaiwuService $Record.StartMode }
  $current = Get-KaiwuServiceRecord
  if ($null -eq $current -or $current.PathName -ne $Record.PathName -or $current.State -ne $Record.State -or
      $current.StartMode -ne $Record.StartMode -or
      ($Record.StartMode -eq "Auto" -and [bool]$current.DelayedAutoStart -ne [bool]$Record.DelayedAutoStart)) {
    throw "交接中断后未能读回原服务运行状态和启动方式"
  }
}

function Remove-KaiwuStoppedService {
  param($Record, $Handoff)
  Assert-KaiwuQuiescent $Record
  # Even when the replacement remains stopped, a later start must not reuse the old PID.
  Move-KaiwuRuntimePidMarker
  if ($null -eq $Record) { return }
  & sc.exe delete $ServiceName | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "删除服务 $ServiceName 失败，退出码 $LASTEXITCODE" }
  $Handoff.delete_accepted = $true
  Wait-ServiceAbsent
  Assert-KaiwuQuiescent $null
}

function Enter-KaiwuInstallTransaction {
  $mutex = [System.Threading.Mutex]::new($false, 'Global\KaiwuConnector.InstallTransaction')
  try {
    try {
      if (-not $mutex.WaitOne(0)) { throw "已有开物安装或恢复任务正在运行" }
    } catch [System.Threading.AbandonedMutexException] {
      # 内核已把锁交给本次执行。服务与文件仍须通过正常交接检查。
    }
    return $mutex
  } catch {
    $mutex.Dispose()
    throw
  }
}

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
  "windows-smoke.ps1", "open-config.cmd", "uninstall.exe"
)
$PayloadDocs = @(Get-ChildItem (Join-Path $Source "docs\*.md") -File -ErrorAction SilentlyContinue)
$PayloadCompliance = @(Get-ChildItem (Join-Path $Source "compliance\*") -File -ErrorAction SilentlyContinue)
$BackupProgramNames += @($PayloadDocs | ForEach-Object { "docs/" + $_.Name })
$BackupProgramNames += @($PayloadCompliance | ForEach-Object { "compliance/" + $_.Name })

try {
  $Transaction = Enter-KaiwuInstallTransaction
} catch {
  Write-InstallResult "PRECHECK_FAILED"
  throw
}

try {
$EnvironmentBefore = @{}
foreach ($name in $ManagedEnvNames) {
  $EnvironmentBefore[$name] = Get-KaiwuMachineEnvironmentVariable $name
}

$Stage = "backup_prepare"
try {
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
    Set-PrivateDataAcl -Path $DataDir
  } elseif (-not $NeedsBackup) {
    Set-PrivateDataAcl -Path $DataDir
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
  Assert-CriticalFiles
  $Stage = "replace_install"
  Remove-KaiwuStoppedService $ExistingService $OriginalHandoff
  $InstallMutationStarted = $true
  $OldInstallChanged = $NeedsBackup
  if (-not $NeedsBackup) {
    foreach ($name in $BackupDataNames) { $CreatedManagedFiles.Add((Join-Path $DataDir $name)) }
  }

  Ensure-ManagedDirectory $InstallDir
  Ensure-ManagedDirectory (Join-Path $InstallDir "docs")
  Ensure-ManagedDirectory (Join-Path $InstallDir "compliance")

  Copy-ManagedFile (Join-Path $Source "bin\kaiwu-connector.exe") (Join-Path $InstallDir "kaiwu-connector.exe")
  Copy-ManagedFile (Join-Path $Source "bin\frpc.exe") (Join-Path $InstallDir "frpc.exe")
  Assert-CriticalFiles -Target
  $PayloadDocs | ForEach-Object {
      Copy-ManagedFile $_.FullName (Join-Path (Join-Path $InstallDir "docs") $_.Name)
    }
  $PayloadCompliance | ForEach-Object {
      Copy-ManagedFile $_.FullName (Join-Path (Join-Path $InstallDir "compliance") $_.Name)
    }
  foreach ($name in @("uninstall.ps1", "windows-smoke.ps1", "install.ps1", "版本信息.txt")) {
    $item = Join-Path $Source $name
    if (Test-Path -LiteralPath $item -PathType Leaf) {
      Copy-ManagedFile $item (Join-Path $InstallDir $name)
    }
  }

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
      $events = Get-WinEvent -FilterHashtable @{LogName='System'; StartTime=(Get-Date).AddMinutes(-5)} -ErrorAction SilentlyContinue |
        Where-Object { $_.Message -match 'Kaiwu|1053|7000|7009' } |
        Select-Object -First 5 |
        Format-List | Out-String
      throw "启动服务失败: $($_.Exception.Message)`n$events"
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
      throw "服务已经启动，但本机配置页面 http://$Listen/ 未在 30 秒内返回 200"
    }
  }
  Assert-RestoredServiceRegistration $ReplacementSnapshot
  $service = Get-Service -Name $ServiceName -ErrorAction Stop
  $expectedStatus = if ($ReplacementSnapshot.was_running) { "Running" } else { "Stopped" }
  if ($service.Status -ne $expectedStatus) { throw "安装后的服务未保持预期状态 ($expectedStatus)" }

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
  Write-InstallResult "INSTALLED"
} catch {
  $OriginalError = $_.Exception.Message
  $FailureStage = $Stage
  $PriorHandoffChanged = $OriginalHandoff.startup_changed -or $OriginalHandoff.stop_attempted
  if (-not $InstallMutationStarted -and -not $OriginalHandoff.delete_accepted -and -not $PriorHandoffChanged) {
    Write-InstallResult "PRECHECK_FAILED"
    throw "安装前检查失败：$OriginalError"
  }
  $OldInstallChanged = $OldInstallChanged -or $PriorHandoffChanged
  $Stage = "rollback"
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
