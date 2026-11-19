#Requires -Version 5.1
<#
.SYNOPSIS
在隔离临时目录中执行安装器故障注入。

.DESCRIPTION
本脚本调用真实的 install.ps1，通过隔离的 ProgramFiles、ProgramData 和 TEMP
目录以及平台命令替身覆盖安装事务。脚本不会访问真实服务、注册表或生产目录。

必须在已提升权限的 Windows PowerShell 5.1 中运行，原因是被测 install.ps1
保留了 #Requires -RunAsAdministrator。平台替身只改变服务、ACL、HTTP、
文件和环境变量调用，不改变安装器的校验和恢复算法。

.EXAMPLE
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\installer-fault-injection.ps1
#>
[CmdletBinding()]
param(
  [string]$InstallerPath = (Join-Path (Split-Path -Parent $PSScriptRoot) "packaging\windows\install.ps1"),
  [string]$OutputPath = (Join-Path ([System.IO.Path]::GetTempPath()) "installer-fault-injection-result.json")
)

$ErrorActionPreference = "Stop"
$script:Results = [ordered]@{
  task = "huawi installer fault injection"
  status = "not_run"
  started_at = (Get-Date).ToUniversalTime().ToString("o")
  completed_at = $null
  runtime = [ordered]@{
    os = [string]$env:OS
    powershell = [string]$PSVersionTable.PSVersion
    elevated = $false
    platform_doubles_verified = $false
  }
  cases = [System.Collections.ArrayList]::new()
  error = $null
}
$script:OriginalEnvironment = @{
  ProgramFiles = [Environment]::GetEnvironmentVariable("ProgramFiles", "Process")
  ProgramData = [Environment]::GetEnvironmentVariable("ProgramData", "Process")
  TEMP = [Environment]::GetEnvironmentVariable("TEMP", "Process")
  KAIWU_CLOUD_BASE = [Environment]::GetEnvironmentVariable("KAIWU_CLOUD_BASE", "Process")
  KAIWU_EDGE_BASE = [Environment]::GetEnvironmentVariable("KAIWU_EDGE_BASE", "Process")
}
$script:InstallerSource = $null
$script:Root = $null
$script:Case = $null

function Write-TestResult {
  $json = $script:Results | ConvertTo-Json -Depth 12
  $parent = Split-Path -Parent $OutputPath
  if ([string]::IsNullOrWhiteSpace($parent)) { $parent = (Get-Location).Path }
  New-Item -ItemType Directory -Force -Path $parent | Out-Null
  [System.IO.File]::WriteAllText(
    $OutputPath,
    $json,
    [System.Text.UTF8Encoding]::new($false)
  )
}

function Finish-Test {
  param([int]$ExitCode)
  $script:Results.completed_at = (Get-Date).ToUniversalTime().ToString("o")
  Write-TestResult
  Restore-ProcessEnvironment
  if ($null -ne $script:Root -and (Test-Path -LiteralPath $script:Root -PathType Container)) {
    Remove-Item -LiteralPath $script:Root -Recurse -Force -ErrorAction SilentlyContinue
  }
  exit $ExitCode
}

function Add-CaseResult {
  param(
    [string]$Id,
    [string]$Status,
    [string]$Detail,
    $Checks
  )
  [void]$script:Results.cases.Add([ordered]@{
    id = $Id
    status = $Status
    detail = $Detail
    checks = $Checks
  })
}

function Assert-True {
  param(
    [bool]$Condition,
    [string]$Message
  )
  if (-not $Condition) { throw $Message }
}

function Get-Sha256 {
  param([string]$Path)
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return "<missing>" }
  return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Set-CaseEnvironment {
  param($Case)
  $env:ProgramFiles = $Case.ProgramFiles
  $env:ProgramData = $Case.ProgramData
  $env:TEMP = $Case.Temp
}

function Restore-ProcessEnvironment {
  foreach ($name in @("ProgramFiles", "ProgramData", "TEMP", "KAIWU_CLOUD_BASE", "KAIWU_EDGE_BASE")) {
    $value = $script:OriginalEnvironment[$name]
    if ($null -eq $value) {
      Remove-Item -Path ("Env:" + $name) -ErrorAction SilentlyContinue
    } else {
      Set-Item -Path ("Env:" + $name) -Value $value
    }
  }
}

function New-TestCase {
  param(
    [string]$Name,
    [switch]$Existing,
    [switch]$UnrelatedFile
  )
  $caseRoot = Join-Path $script:Root $Name
  $source = Join-Path $caseRoot "source"
  $programFiles = Join-Path $caseRoot "ProgramFiles"
  $programData = Join-Path $caseRoot "ProgramData"
  $temp = Join-Path $caseRoot "Temp"
  $installDir = Join-Path $programFiles "Kaiwu\Connector"
  $dataDir = Join-Path $programData "Kaiwu\Connector"
  $installer = Join-Path $source "install.ps1"
  New-Item -ItemType Directory -Force -Path $source, $programFiles, $programData, $temp | Out-Null
  [System.IO.File]::Copy($script:InstallerSource, $installer, $true)

  $payload = [ordered]@{
    "bin/kaiwu-connector.exe" = "fault-test-kaiwu-binary"
    "bin/frpc.exe" = "fault-test-frpc-binary"
    "docs/readme.md" = "fault-test documentation"
    "compliance/LICENSE" = "fault-test license"
    "config/connector.env.example" = "KAIWU_CLOUD_BASE=`r`nKAIWU_EDGE_BASE=`r`n"
    "版本信息.txt" = "fault-test-version"
  }
  foreach ($relative in $payload.Keys) {
    $path = Join-Path $source ($relative -replace '/', '\')
    $parent = Split-Path -Parent $path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    [System.IO.File]::WriteAllText(
      $path,
      [string]$payload[$relative],
      [System.Text.UTF8Encoding]::new($false)
    )
  }
  $checksumLines = @()
  foreach ($relative in $payload.Keys) {
    $path = Join-Path $source ($relative -replace '/', '\')
    $checksumLines += ((Get-Sha256 $path) + "  " + $relative)
  }
  [System.IO.File]::WriteAllText(
    (Join-Path $source "SHA256SUMS"),
    (($checksumLines -join "`r`n") + "`r`n"),
    [System.Text.UTF8Encoding]::new($false)
  )

  if ($Existing) {
    New-Item -ItemType Directory -Force -Path $installDir, $dataDir | Out-Null
    [System.IO.File]::WriteAllText(
      (Join-Path $installDir "kaiwu-connector.exe"),
      "old-kaiwu-binary",
      [System.Text.UTF8Encoding]::new($false)
    )
    [System.IO.File]::WriteAllText(
      (Join-Path $installDir "frpc.exe"),
      "old-frpc-binary",
      [System.Text.UTF8Encoding]::new($false)
    )
    [System.IO.File]::WriteAllText(
      (Join-Path $installDir "版本信息.txt"),
      "old-version",
      [System.Text.UTF8Encoding]::new($false)
    )
    [System.IO.File]::WriteAllText(
      (Join-Path $dataDir "config.json"),
      '{"business":"old"}',
      [System.Text.UTF8Encoding]::new($false)
    )
    [System.IO.File]::WriteAllText(
      (Join-Path $dataDir "state.json"),
      '{"phase":"READY"}',
      [System.Text.UTF8Encoding]::new($false)
    )
  } elseif ($UnrelatedFile) {
    New-Item -ItemType Directory -Force -Path $installDir | Out-Null
    [System.IO.File]::WriteAllText(
      (Join-Path $installDir "unrelated.txt"),
      "must survive failed first install",
      [System.Text.UTF8Encoding]::new($false)
    )
  }

  return [pscustomobject]@{
    Name = $Name
    Root = $caseRoot
    Source = $source
    Installer = $installer
    ProgramFiles = $programFiles
    ProgramData = $programData
    Temp = $temp
    InstallDir = $installDir
    DataDir = $dataDir
  }
}

function Reset-MockState {
  $global:MockService = $null
  $global:StopCalls = @()
  $global:StartServiceCalls = 0
  $global:DeleteCalls = 0
  $global:HttpCalls = 0
  $global:HttpReady = $true
  $global:MutateStateOnStart = $false
  $global:RemoveTargetOnStart = $false
  $global:CopyBeforeHook = $null
  $global:CopyAfterHook = $null
  $global:MockMachineEnvironment = @{}
  $global:Case = $null
}

function Set-MockService {
  param(
    [string]$BinaryPath = "",
    [string]$State = "Running",
    [string[]]$Dependents = @()
  )
  $global:MockService = [pscustomobject]@{
    Name = "KaiwuConnector"
    DisplayName = "Kaiwu Intranet Connector"
    PathName = $BinaryPath
    StartMode = "Auto"
    DelayedAutoStart = $false
    State = $State
    Status = $State
    DependentServices = @($Dependents | ForEach-Object { [pscustomobject]@{ Name = $_ } })
  }
}

function global:Test-SamePath {
  param([string]$Left, [string]$Right)
  if ([string]::IsNullOrWhiteSpace($Left) -or [string]::IsNullOrWhiteSpace($Right)) { return $false }
  return ([System.IO.Path]::GetFullPath($Left).ToLowerInvariant() -eq
    [System.IO.Path]::GetFullPath($Right).ToLowerInvariant())
}

function Invoke-Installer {
  param($Case)
  $resultPath = Join-Path $Case.Root ("install-result-" + [guid]::NewGuid().ToString("N") + ".txt")
  $errorText = $null
  try {
    & $Case.Installer -ResultPath $resultPath | Out-Null
  } catch {
    $errorText = $_.Exception.Message
  }
  $resultCode = "<missing>"
  if (Test-Path -LiteralPath $resultPath -PathType Leaf) {
    $firstLine = [string](Get-Content -LiteralPath $resultPath -Encoding UTF8 -TotalCount 1)
    if ($firstLine -match '^result=([A-Z_]+)$') { $resultCode = $matches[1] }
  }
  return [pscustomobject]@{
    ResultPath = $resultPath
    ResultCode = $resultCode
    Error = $errorText
  }
}

function global:Find-BackupDirectory {
  param($Case)
  $backupRoot = Join-Path $Case.DataDir "backups"
  if (-not (Test-Path -LiteralPath $backupRoot -PathType Container)) { return $null }
  return @(
    Get-ChildItem -LiteralPath $backupRoot -Directory -ErrorAction SilentlyContinue |
      Sort-Object LastWriteTime -Descending |
      Select-Object -First 1
  )[0]
}

function Invoke-GeneratedRestore {
  param([string]$RestorePath)
  $errorText = $null
  try {
    & $RestorePath | Out-Null
  } catch {
    $errorText = $_.Exception.Message
  }
  return [pscustomobject]@{
    Failed = $null -ne $errorText
    Error = $errorText
  }
}

function Invoke-Case {
  param([string]$Id, [scriptblock]$Body)
  try {
    & $Body
  } catch {
    Add-CaseResult $Id "failed" $_.Exception.Message @{}
  }
}

function global:Get-KaiwuMachineEnvironmentVariable {
  param([string]$Name)
  if ($global:MockMachineEnvironment.ContainsKey($Name)) {
    return [string]$global:MockMachineEnvironment[$Name]
  }
  return $null
}

function global:Set-KaiwuMachineEnvironmentVariable {
  param(
    [string]$Name,
    [AllowNull()][AllowEmptyString()][string]$Value
  )
  if ([string]::IsNullOrEmpty($Value)) {
    $global:MockMachineEnvironment.Remove($Name)
  } else {
    $global:MockMachineEnvironment[$Name] = $Value
  }
}

function global:Get-Service {
  [CmdletBinding()]
  param([string]$Name)
  if ($null -eq $global:MockService) { return }
  if ($Name -and $global:MockService.Name -ne $Name) { return }
  return $global:MockService
}

function global:Get-CimInstance {
  [CmdletBinding()]
  param([string]$ClassName, [string]$Filter)
  return $global:MockService
}

function global:New-Service {
  [CmdletBinding()]
  param(
    [string]$Name,
    [string]$BinaryPathName,
    [string]$StartupType,
    [string]$DisplayName
  )
  if ($null -ne $global:MockService) {
    throw "test mock: service already exists: $Name"
  }
  $global:MockService = [pscustomobject]@{
    Name = $Name
    DisplayName = $DisplayName
    PathName = $BinaryPathName
    StartMode = $(if ($StartupType -eq "Automatic") { "Auto" } else { $StartupType })
    DelayedAutoStart = $false
    State = "Stopped"
    Status = "Stopped"
    DependentServices = @()
  }
}

function global:Start-Service {
  [CmdletBinding()]
  param([string]$Name)
  if ($null -eq $global:MockService -or $global:MockService.Name -ne $Name) {
    throw "test mock: service not found: $Name"
  }
  $global:StartServiceCalls++
  $global:MockService.State = "Running"
  $global:MockService.Status = "Running"
  if ($global:RemoveTargetOnStart) {
    $target = Join-Path $global:Case.InstallDir "frpc.exe"
    if (Test-Path -LiteralPath $target -PathType Leaf) {
      Remove-Item -LiteralPath $target -Force
    }
    $global:RemoveTargetOnStart = $false
  }
  if ($global:MutateStateOnStart) {
    $statePath = Join-Path $global:Case.DataDir "state.json"
    [System.IO.File]::WriteAllText(
      $statePath,
      '{"phase":"CONFIGURING"}',
      [System.Text.UTF8Encoding]::new($false)
    )
  }
}

function global:Stop-Service {
  [CmdletBinding()]
  param([string]$Name, [switch]$Force)
  if ($Force) { throw "test mock: Stop-Service -Force is forbidden" }
  $global:StopCalls += $Name
  if ($null -eq $global:MockService -or $global:MockService.Name -ne $Name) {
    throw "test mock: service not found: $Name"
  }
  $global:MockService.State = "Stopped"
  $global:MockService.Status = "Stopped"
}

function global:Invoke-WebRequest {
  [CmdletBinding()]
  param([string]$Uri, [switch]$UseBasicParsing, [int]$TimeoutSec)
  $global:HttpCalls++
  if (-not $global:HttpReady) { throw "test mock: HTTP endpoint unavailable" }
  return [pscustomobject]@{ StatusCode = 200 }
}

function global:Get-WinEvent {
  [CmdletBinding()]
  param([hashtable]$FilterHashtable)
}

function global:sc.exe {
  [CmdletBinding()]
  param([Parameter(ValueFromRemainingArguments = $true)][object[]]$Arguments)
  $action = if ($Arguments.Count -gt 0) { [string]$Arguments[0] } else { "" }
  switch ($action) {
    "delete" {
      $global:DeleteCalls++
      if ($null -ne $global:MockService) { $global:MockService = $null }
    }
    "config" {
      if ($null -ne $global:MockService) { $global:MockService.DelayedAutoStart = $true }
    }
  }
  $global:LASTEXITCODE = 0
}

function global:icacls.exe {
  [CmdletBinding()]
  param([Parameter(ValueFromRemainingArguments = $true)][object[]]$Arguments)
  $global:LASTEXITCODE = 0
}

function global:Get-Acl {
  [CmdletBinding()]
  param([string]$LiteralPath)
  $acl = [pscustomobject]@{
    AreAccessRulesProtected = $true
    Access = @()
  }
  $acl | Add-Member -MemberType ScriptMethod -Name SetAccessRuleProtection -Force -Value {
    param($protect, $inherit)
    $this.AreAccessRulesProtected = $true
  }
  $acl | Add-Member -MemberType ScriptMethod -Name RemoveAccessRuleSpecific -Force -Value {
    param($rule)
  }
  $acl | Add-Member -MemberType ScriptMethod -Name AddAccessRule -Force -Value {
    param($rule)
  }
  return $acl
}

function global:Set-Acl {
  [CmdletBinding()]
  param([string]$LiteralPath, $AclObject)
}

function global:Copy-Item {
  [CmdletBinding()]
  param(
    [string]$LiteralPath,
    [string]$Path,
    [string]$Destination,
    [switch]$Force
  )
  $source = if ($LiteralPath) { $LiteralPath } else { $Path }
  if ($null -ne $global:CopyBeforeHook) {
    & $global:CopyBeforeHook $source $Destination
  }
  Microsoft.PowerShell.Management\Copy-Item -LiteralPath $source -Destination $Destination -Force:$Force
  if ($null -ne $global:CopyAfterHook) {
    & $global:CopyAfterHook $source $Destination
  }
}

function Assert-PlatformDoubles {
  $names = @(
    "Get-Service",
    "New-Service",
    "Start-Service",
    "Stop-Service",
    "Get-CimInstance",
    "sc.exe",
    "icacls.exe",
    "Invoke-WebRequest",
    "Get-WinEvent",
    "Get-Acl",
    "Set-Acl",
    "Copy-Item",
    "Get-KaiwuMachineEnvironmentVariable",
    "Set-KaiwuMachineEnvironmentVariable"
  )
  foreach ($name in $names) {
    $command = Get-Command $name -ErrorAction Stop
    if ($command.CommandType -ne "Function") {
      throw "platform double is not active: $name ($($command.CommandType))"
    }
  }
}

function Run-CaseA {
  $case = New-TestCase "A" -Existing
  Set-CaseEnvironment $case
  Reset-MockState
  Set-MockService (Join-Path $case.InstallDir "kaiwu-connector.exe") "Running"
  $case | Add-Member -NotePropertyName ConfigPath -NotePropertyValue (Join-Path $case.DataDir "config.json")
  [System.IO.File]::WriteAllText($case.ConfigPath, '{"business":"unchanged"}', [System.Text.UTF8Encoding]::new($false))
  $before = Get-Sha256 $case.ConfigPath
  $oldFrpc = Get-Sha256 (Join-Path $case.InstallDir "frpc.exe")
  Remove-Item -LiteralPath (Join-Path $case.Source "bin\frpc.exe") -Force
  $run = Invoke-Installer $case
  Assert-True ($run.ResultCode -eq "PRECHECK_FAILED") "A: expected PRECHECK_FAILED, got $($run.ResultCode)"
  Assert-True ($global:StopCalls.Count -eq 0) "A: Stop-Service was called"
  Assert-True ($global:DeleteCalls -eq 0) "A: sc delete was called"
  Assert-True ((Get-Sha256 $case.ConfigPath) -eq $before) "A: business config changed"
  Assert-True ((Get-Sha256 (Join-Path $case.InstallDir "frpc.exe")) -eq $oldFrpc) "A: existing business program changed"
  Assert-True ($global:MockService.Status -eq "Running") "A: existing business service was touched"
  Add-CaseResult "A" "passed" "source frpc missing before first validation" ([ordered]@{
    result = $run.ResultCode
    stop_calls = $global:StopCalls.Count
    delete_calls = $global:DeleteCalls
    config_sha256 = $before
    frpc_sha256 = $oldFrpc
    service_status = $global:MockService.Status
  })
}

function Run-CaseB {
  $case = New-TestCase "B" -Existing
  Set-CaseEnvironment $case
  Reset-MockState
  $global:Case = $case
  Set-MockService (Join-Path $case.InstallDir "kaiwu-connector.exe") "Running"
  $oldConfig = Get-Sha256 (Join-Path $case.DataDir "config.json")
  $oldFrpc = Get-Sha256 (Join-Path $case.InstallDir "frpc.exe")
  $sourceFrpc = Join-Path $case.Source "bin\frpc.exe"
  $targetFrpc = Join-Path $case.InstallDir "frpc.exe"
  $global:CopyBeforeHook = {
    param($source, $destination)
    if ((Test-SamePath $source $sourceFrpc) -and (Test-SamePath $destination $targetFrpc)) {
      Remove-Item -LiteralPath $source -Force
      throw "synthetic source frpc disappearance"
    }
  }.GetNewClosure()
  $run = Invoke-Installer $case
  Assert-True ($run.ResultCode -eq "FAILED_RESTORED") "B: expected FAILED_RESTORED, got $($run.ResultCode)"
  Assert-True ($global:MockService.Status -eq "Running") "B: old service was not restored to Running"
  Assert-True ((Get-Sha256 (Join-Path $case.DataDir "config.json")) -eq $oldConfig) "B: old config was not restored"
  Assert-True ((Get-Sha256 (Join-Path $case.InstallDir "frpc.exe")) -eq $oldFrpc) "B: old frpc was not restored"
  Add-CaseResult "B" "passed" "source frpc disappeared during copy after backup" ([ordered]@{
    result = $run.ResultCode
    service_status = $global:MockService.Status
    config_sha256 = $oldConfig
    frpc_sha256 = $oldFrpc
  })
}

function Run-CaseC {
  $case = New-TestCase "C"
  Set-CaseEnvironment $case
  Reset-MockState
  $global:Case = $case
  $global:RemoveTargetOnStart = $true
  $run = Invoke-Installer $case
  Assert-True ($run.ResultCode -ne "INSTALLED") "C: installer reported success"
  Assert-True (-not (Test-Path -LiteralPath (Join-Path $case.InstallDir "frpc.exe"))) "C: target frpc still exists"
  Assert-True ($global:HttpCalls -gt 0) "C: HTTP double was not exercised"
  Add-CaseResult "C" "passed" "target frpc disappeared after service/HTTP doubles reported success" ([ordered]@{
    result = $run.ResultCode
    start_calls = $global:StartServiceCalls
    http_calls = $global:HttpCalls
  })
}

function Run-CaseD1 {
  $case = New-TestCase "D1" -Existing
  Set-CaseEnvironment $case
  Reset-MockState
  $global:Case = $case
  Set-MockService (Join-Path $case.InstallDir "kaiwu-connector.exe") "Running"
  $sourceKaiwu = Join-Path $case.Source "bin\kaiwu-connector.exe"
  $targetKaiwu = Join-Path $case.InstallDir "kaiwu-connector.exe"
  $global:CopyBeforeHook = {
    param($source, $destination)
    if ((Test-SamePath $source $sourceKaiwu) -and (Test-SamePath $destination $targetKaiwu)) {
      $backup = Find-BackupDirectory $global:Case
      if ($null -eq $backup) { throw "synthetic backup missing" }
      $manifest = Join-Path $backup.FullName "manifest\backup-manifest.json"
      Add-Content -LiteralPath $manifest -Value "corrupt" -Encoding UTF8
      throw "synthetic failure with corrupted backup"
    }
  }.GetNewClosure()
  $run = Invoke-Installer $case
  Assert-True ($run.ResultCode -eq "FAILED_RECOVERY_UNCONFIRMED") "D1: expected FAILED_RECOVERY_UNCONFIRMED, got $($run.ResultCode)"
  Assert-True ($global:StartServiceCalls -eq 0) "D1: service was started from mixed content"
  Add-CaseResult "D1" "passed" "corrupt backup rejected by automatic recovery" ([ordered]@{
    result = $run.ResultCode
    start_calls = $global:StartServiceCalls
    delete_calls = $global:DeleteCalls
  })
}

function Run-CaseD2 {
  $case = New-TestCase "D2" -Existing
  Set-CaseEnvironment $case
  Reset-MockState
  $global:Case = $case
  Set-MockService (Join-Path $case.InstallDir "kaiwu-connector.exe") "Running"
  $sourceKaiwu = Join-Path $case.Source "bin\kaiwu-connector.exe"
  $targetKaiwu = Join-Path $case.InstallDir "kaiwu-connector.exe"
  $global:CopyBeforeHook = {
    param($source, $destination)
    if ((Test-SamePath $source $sourceKaiwu) -and (Test-SamePath $destination $targetKaiwu)) {
      throw "synthetic failure to create a real backup"
    }
  }.GetNewClosure()
  $firstRun = Invoke-Installer $case
  Assert-True ($firstRun.ResultCode -eq "FAILED_RESTORED") "D2: setup did not create a verified backup"
  $backup = Find-BackupDirectory $case
  Assert-True ($null -ne $backup) "D2: backup directory missing"
  $restorePath = Join-Path $backup.FullName "restore.ps1"
  $manifestPath = Join-Path $backup.FullName "manifest\backup-manifest.json"
  Assert-True (Test-Path -LiteralPath $restorePath -PathType Leaf) "D2: generated restore.ps1 missing"
  Add-Content -LiteralPath $manifestPath -Value "corrupt" -Encoding UTF8
  $global:StopCalls = @()
  $global:DeleteCalls = 0
  $global:StartServiceCalls = 0
  $global:CopyBeforeHook = $null
  $restore = Invoke-GeneratedRestore $restorePath
  Assert-True $restore.Failed "D2: generated restore.ps1 accepted a corrupt manifest"
  Assert-True ($global:StopCalls.Count -eq 0) "D2: generated restore stopped a service before verification"
  Assert-True ($global:DeleteCalls -eq 0) "D2: generated restore deleted a service before verification"
  Assert-True ($global:StartServiceCalls -eq 0) "D2: generated restore started a service"
  Add-CaseResult "D2" "passed" "corrupt backup rejected by generated restore.ps1" ([ordered]@{
    auto_result = $firstRun.ResultCode
    restore_failed = $restore.Failed
    stop_calls = $global:StopCalls.Count
    delete_calls = $global:DeleteCalls
  })
}

function Run-CaseE {
  $case = New-TestCase "E" -UnrelatedFile
  Set-CaseEnvironment $case
  Reset-MockState
  $global:Case = $case
  $sourceFrpc = Join-Path $case.Source "bin\frpc.exe"
  $targetFrpc = Join-Path $case.InstallDir "frpc.exe"
  $global:CopyBeforeHook = {
    param($source, $destination)
    if ((Test-SamePath $source $sourceFrpc) -and (Test-SamePath $destination $targetFrpc)) {
      throw "synthetic first-install failure"
    }
  }.GetNewClosure()
  $run = Invoke-Installer $case
  Assert-True ($run.ResultCode -eq "FAILED_NO_PRIOR_CLEANED") "E: expected FAILED_NO_PRIOR_CLEANED, got $($run.ResultCode)"
  Assert-True (-not (Test-Path -LiteralPath (Join-Path $case.InstallDir "kaiwu-connector.exe"))) "E: newly copied kaiwu was not removed"
  Assert-True (Test-Path -LiteralPath (Join-Path $case.InstallDir "unrelated.txt") -PathType Leaf) "E: unrelated pre-existing file was removed"
  Add-CaseResult "E" "passed" "first-install cleanup preserved unrelated files" ([ordered]@{
    result = $run.ResultCode
    unrelated_file_present = $true
  })
}

function Run-CaseF {
  $case = New-TestCase "F" -Existing
  Set-CaseEnvironment $case
  Reset-MockState
  $global:Case = $case
  Set-MockService (Join-Path $case.InstallDir "kaiwu-connector.exe") "Running"
  $sourceKaiwu = Join-Path $case.Source "bin\kaiwu-connector.exe"
  $targetKaiwu = Join-Path $case.InstallDir "kaiwu-connector.exe"
  $global:CopyBeforeHook = {
    param($source, $destination)
    if ((Test-SamePath $source $sourceKaiwu) -and (Test-SamePath $destination $targetKaiwu)) {
      throw "synthetic restore failure"
    }
  }.GetNewClosure()
  $global:MutateStateOnStart = $true
  $run = Invoke-Installer $case
  Assert-True ($run.ResultCode -eq "FAILED_RESTORED") "F: expected FAILED_RESTORED, got $($run.ResultCode)"
  Assert-True ($global:MockService.Status -eq "Running") "F: restored service was stopped after state.json changed"
  Assert-True ($global:StopCalls.Count -eq 1) "F: unexpected extra Stop-Service call"
  $state = Get-Content -LiteralPath (Join-Path $case.DataDir "state.json") -Raw -Encoding UTF8
  Assert-True ($state -match "CONFIGURING") "F: Start-Service double did not change state.json"
  Add-CaseResult "F" "passed" "runtime state.json change did not stop a verified restored service" ([ordered]@{
    result = $run.ResultCode
    service_status = $global:MockService.Status
    stop_calls = $global:StopCalls.Count
    state = $state.Trim()
  })
}

function Run-CaseG {
  $case = New-TestCase "G" -Existing
  Set-CaseEnvironment $case
  Reset-MockState
  $global:Case = $case
  Set-MockService (Join-Path $case.InstallDir "kaiwu-connector.exe") "Running" @("KaiwuDependent")
  $run = Invoke-Installer $case
  Assert-True ($run.ResultCode -eq "FAILED_RECOVERY_UNCONFIRMED") "G: expected FAILED_RECOVERY_UNCONFIRMED, got $($run.ResultCode)"
  Assert-True ($global:StopCalls.Count -eq 0) "G: Stop-Service was called despite a dependent service"
  Assert-True ($global:DeleteCalls -eq 0) "G: sc delete was called despite a dependent service"
  Add-CaseResult "G" "passed" "dependent service blocks target stop and delete" ([ordered]@{
    result = $run.ResultCode
    stop_calls = $global:StopCalls.Count
    delete_calls = $global:DeleteCalls
  })
}

if ($env:OS -ne "Windows_NT") {
  $script:Results.error = "not_run: this fault-injection test requires Windows PowerShell 5.1"
  Finish-Test 2
}

$principal = [Security.Principal.WindowsPrincipal]::new(
  [Security.Principal.WindowsIdentity]::GetCurrent()
)
$script:Results.runtime.elevated = $principal.IsInRole(
  [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $script:Results.runtime.elevated) {
  $script:Results.error = "not_run: elevated Windows PowerShell is required by install.ps1 #Requires"
  Finish-Test 2
}

try {
  $script:InstallerSource = (Resolve-Path -LiteralPath $InstallerPath).Path
  $script:Root = Join-Path ([System.IO.Path]::GetTempPath()) ("KaiwuInstallerFaultInjection-" + [guid]::NewGuid().ToString("N"))
  New-Item -ItemType Directory -Force -Path $script:Root | Out-Null

  Assert-PlatformDoubles
  $script:Results.runtime.platform_doubles_verified = $true

  Invoke-Case "A" { Run-CaseA }
  Invoke-Case "B" { Run-CaseB }
  Invoke-Case "C" { Run-CaseC }
  Invoke-Case "D1" { Run-CaseD1 }
  Invoke-Case "D2" { Run-CaseD2 }
  Invoke-Case "E" { Run-CaseE }
  Invoke-Case "F" { Run-CaseF }
  Invoke-Case "G" { Run-CaseG }
  if (@($script:Results.cases | Where-Object { $_.status -ne "passed" }).Count -eq 0) {
    $script:Results.status = "passed"
  } else {
    $script:Results.status = "failed"
  }
} catch {
  $script:Results.status = "failed"
  $script:Results.error = $_.Exception.Message
}

if ($script:Results.status -eq "passed") { Finish-Test 0 }
Finish-Test 1
