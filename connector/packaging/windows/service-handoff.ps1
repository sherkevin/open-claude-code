# 安装、恢复和卸载共用。调用方设置 InstallDir、DataDir、ServiceName 和 Listen。
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
  } elseif ($null -eq $current -or $current.PathName -ne $PreviousRecord.PathName -or $current.StartName -ne $PreviousRecord.StartName -or
      $null -eq $current.ProcessId -or $current.State -ne "Stopped" -or [uint32]$current.ProcessId -ne 0 -or $current.StartMode -ne "Disabled") {
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
  if ($Record.PathName -ne (Get-KaiwuProductCommand) -or $Record.StartName -ne "LocalSystem" -or
      $null -eq $Record.ProcessId -or $Record.State -notin @("Running", "Stopped") -or
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
    if ($null -eq $current -or $current.PathName -ne $Record.PathName -or $current.StartName -ne $Record.StartName -or
        $current.State -ne $Record.State -or
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
  param($Record, $Handoff, [switch]$RequireQuiescentRestart)
  if (-not ($Handoff.startup_changed -or $Handoff.stop_attempted)) { return }
  $current = Get-KaiwuServiceRecord
  if ($null -eq $current -or $current.PathName -ne $Record.PathName -or
      $current.StartName -ne $Record.StartName -or $current.DisplayName -ne $Record.DisplayName) {
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
    if ($RequireQuiescentRestart) { Assert-KaiwuQuiescent $Record }
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
  if ($null -eq $current -or $current.PathName -ne $Record.PathName -or $current.StartName -ne $Record.StartName -or
        $current.State -ne $Record.State -or
      $current.StartMode -ne $Record.StartMode -or
      ($Record.StartMode -eq "Auto" -and [bool]$current.DelayedAutoStart -ne [bool]$Record.DelayedAutoStart)) {
    throw "交接中断后未能读回原服务运行状态和启动方式"
  }
}

function Remove-KaiwuStoppedService {
  param($Record, $Handoff, [switch]$DeferPidMarker)
  Assert-KaiwuQuiescent $Record
  # Even when the replacement remains stopped, a later start must not reuse the old PID.
  if (-not $DeferPidMarker) { Move-KaiwuRuntimePidMarker }
  if ($null -eq $Record) { return }
  & sc.exe delete $ServiceName | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "删除服务 $ServiceName 失败，退出码 $LASTEXITCODE" }
  $Handoff.delete_accepted = $true
  Wait-ServiceAbsent
  Assert-KaiwuQuiescent $null
}

function Enter-KaiwuInstallTransaction {
  param([string]$Name = 'Global\KaiwuConnector.InstallTransaction')
  $mutex = [System.Threading.Mutex]::new($false, $Name)
  try {
    try {
      if (-not $mutex.WaitOne(0)) { throw [TimeoutException]::new("已有开物安装、恢复或卸载任务正在运行") }
    } catch [System.Threading.AbandonedMutexException] {
      # 内核已把锁交给本次执行。服务与文件仍须通过正常交接检查。
    }
    return $mutex
  } catch {
    $mutex.Dispose()
    throw
  }
}
