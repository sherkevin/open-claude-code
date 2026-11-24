# 开物内网连接器 Windows 卸载脚本。
# 用法：以管理员身份运行 PowerShell：.\uninstall.ps1 [-Purge] [-KeepProgramDirectory]
# 默认保留本机身份、配置和云端环境变量；-Purge 彻底删除。
#Requires -RunAsAdministrator
param(
  [switch]$Purge,
  [switch]$KeepProgramDirectory
)
$ErrorActionPreference = "Stop"

# QuietUninstallString may be invoked by a 32-bit management process.
if (-not [Environment]::Is64BitProcess) {
  $nativePowerShell = Join-Path $env:WINDIR "Sysnative\WindowsPowerShell\v1.0\powershell.exe"
  $options = @()
  if ($Purge) { $options += "-Purge" }
  if ($KeepProgramDirectory) { $options += "-KeepProgramDirectory" }
  & $nativePowerShell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $PSCommandPath @options
  if ($LASTEXITCODE -ne 0) { throw "64 位卸载进程失败，退出码 $LASTEXITCODE" }
  return
}

$InstallDir = Join-Path $env:ProgramFiles "Kaiwu\Connector"
$DataDir = Join-Path $env:ProgramData "Kaiwu\Connector"
$ServiceName = "KaiwuConnector"
$Listen = "127.0.0.1:17872"
. (Join-Path $PSScriptRoot "service-handoff.ps1")

if (-not (Get-Command Clear-KaiwuMachineEnvironmentVariable -CommandType Function -ErrorAction SilentlyContinue)) {
  function Clear-KaiwuMachineEnvironmentVariable {
    param([string]$Name)
    [Environment]::SetEnvironmentVariable($Name, $null, "Machine")
    if (-not [string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($Name, "Machine"))) {
      throw "未确认机器级环境变量已清除: $Name"
    }
  }
}

if (-not (Get-Command Remove-KaiwuSetupRegistration -CommandType Function -ErrorAction SilentlyContinue)) {
  function Remove-KaiwuSetupRegistration {
    $key = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector'
    foreach ($view in @([Microsoft.Win32.RegistryView]::Registry32, [Microsoft.Win32.RegistryView]::Registry64)) {
      $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine, $view)
      try {
        $base.DeleteSubKeyTree($key, $false)
        $remaining = $base.OpenSubKey($key)
        if ($null -ne $remaining) {
          $remaining.Dispose()
          throw "卸载登记仍然存在 ($view)"
        }
      } finally {
        $base.Dispose()
      }
    }
  }
}

function Remove-KaiwuUninstallPath {
  param([string]$Path)
  if (-not (Test-Path -LiteralPath $Path)) { return }
  Remove-Item -LiteralPath $Path -Recurse -Force -Confirm:$false
  if (Test-Path -LiteralPath $Path) { throw "卸载后路径仍然存在: $Path" }
}

function Remove-KaiwuStaleWorkspaces {
  # Setup staging directories are kw-<32 hex> under %ProgramData%\Kaiwu. Normal
  # installs delete them on completion; a crashed or cancelled install leaves
  # them behind. Only that exact name shape is removed, so the Connector data
  # directory, logs and journal evidence stay untouched.
  $workspaceRoot = Join-Path $env:ProgramData "Kaiwu"
  if (-not (Test-Path -LiteralPath $workspaceRoot -PathType Container)) { return }
  foreach ($item in @(Get-ChildItem -LiteralPath $workspaceRoot -Directory -Force -ErrorAction SilentlyContinue)) {
    if ($item.Name -notmatch '^kw-[0-9a-f]{32}$') { continue }
    try {
      Remove-Item -LiteralPath $item.FullName -Recurse -Force -Confirm:$false -ErrorAction Stop
    } catch {
      Write-Warning "无法删除遗留解包目录：$($item.FullName)（$($_.Exception.Message)）"
    }
  }
}

function Remove-KaiwuSetupShortcuts {
  $desktop = [Environment]::GetFolderPath("CommonDesktopDirectory")
  $programs = [Environment]::GetFolderPath("CommonPrograms")
  if ($desktop) { Remove-KaiwuUninstallPath (Join-Path $desktop "开物内网连接器配置.lnk") }
  if ($programs) {
    $menu = Join-Path $programs "开物内网连接器"
    foreach ($name in @("打开本机配置页面.lnk", "交付与运维说明.lnk", "卸载开物内网连接器.lnk")) {
      Remove-KaiwuUninstallPath (Join-Path $menu $name)
    }
    if ((Test-Path -LiteralPath $menu -PathType Container) -and
        -not (Get-ChildItem -LiteralPath $menu -Force | Select-Object -First 1)) {
      Remove-Item -LiteralPath $menu -Force -Confirm:$false
    }
  }
}

function Assert-KaiwuLegacyIdle {
  param([string]$Directory)
  if (-not $Directory) { return }
  foreach ($row in @(Get-KaiwuProcessRows)) {
    if ([string]::IsNullOrWhiteSpace($row.ExecutablePath) -or
        $row.ExecutablePath -eq (Join-Path $Directory $row.Name)) {
      throw "无法确认旧 32 位程序目录没有运行中的产品进程 ($($row.ProcessId))"
    }
  }
}

# The setup executable holds this mutex through its own shortcuts and registry writes.
$setupTransaction = Enter-KaiwuInstallTransaction 'Global\KaiwuConnector.SetupTransaction'
$transaction = $null
try {
  $transaction = Enter-KaiwuInstallTransaction
  $legacyDir = $null
  if (-not $KeepProgramDirectory -and ${env:ProgramFiles(x86)}) {
    $candidate = Join-Path ${env:ProgramFiles(x86)} "Kaiwu\Connector"
    if ($candidate -ne $InstallDir -and
        @("kaiwu-connector.exe", "uninstall.exe", "install.ps1", "版本信息.txt" |
          Where-Object { Test-Path -LiteralPath (Join-Path $candidate $_) -PathType Leaf }).Count -gt 0) {
      $legacyDir = $candidate
    }
  }

  Assert-KaiwuLegacyIdle $legacyDir
  $service = Get-KaiwuServiceRecord
  $handoff = @{}
  try {
    Stop-KaiwuWriters $service $handoff
    Remove-KaiwuStoppedService $service $handoff -DeferPidMarker
  } catch {
    $failure = $_
    if (-not $handoff.delete_accepted) {
      try {
        Restore-KaiwuHandoff $service $handoff -RequireQuiescentRestart
      } catch {
        throw "$($failure.Exception.Message)`n原服务状态未能安全恢复：$($_.Exception.Message)"
      }
    }
    throw $failure
  }

  Assert-KaiwuLegacyIdle $legacyDir
  Assert-KaiwuQuiescent $null
  Remove-KaiwuStaleWorkspaces
  if ($Purge) {
    foreach ($name in @("KAIWU_CLOUD_BASE", "KAIWU_EDGE_BASE", "KAIWU_SUPPORT_PORTAL_URL", "KAIWU_CONNECTOR_DATA")) {
      Clear-KaiwuMachineEnvironmentVariable $name
      Remove-Item -Path ("Env:" + $name) -ErrorAction SilentlyContinue
    }
    Remove-KaiwuUninstallPath $DataDir
  } else {
    # The service is gone. Keep configuration and identity, and retire the transient PID.
    Move-KaiwuRuntimePidMarker
  }

  if (-not $KeepProgramDirectory) {
    # Leave the launcher, script and helper available if an ordinary file cannot be removed.
    if (Test-Path -LiteralPath $InstallDir) {
      Get-ChildItem -LiteralPath $InstallDir -Force |
        Where-Object { $_.Name -notin @("uninstall.exe", "uninstall.ps1", "service-handoff.ps1") } |
        ForEach-Object { Remove-KaiwuUninstallPath $_.FullName }
    }
    if ($legacyDir) {
      Remove-KaiwuUninstallPath $legacyDir
      $legacyParent = Split-Path -Parent $legacyDir
      if (-not (Get-ChildItem -LiteralPath $legacyParent -Force | Select-Object -First 1)) {
        Remove-Item -LiteralPath $legacyParent -Force -Confirm:$false
      }
    }
    Remove-KaiwuSetupShortcuts
    Remove-KaiwuUninstallPath $InstallDir
    Remove-KaiwuSetupRegistration
  }
} finally {
  if ($null -ne $transaction) {
    $transaction.ReleaseMutex()
    $transaction.Dispose()
  }
  $setupTransaction.ReleaseMutex()
  $setupTransaction.Dispose()
}
if ($KeepProgramDirectory) {
  Write-Host "已删除服务，程序目录仍保留在 $InstallDir。"
} else {
  Write-Host "已删除程序与服务。"
}
if ($Purge) {
  Write-Host "已删除配置和本机身份。"
} else {
  Write-Host "配置和本机身份保留在 $DataDir。"
}
