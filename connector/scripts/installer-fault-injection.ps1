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
powershell.exe -NoProfile -NonInteractive -File .\installer-fault-injection.ps1
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
  $global:DeleteExitCode = 0
  $global:NewServiceCalls = 0
  $global:ServiceMetadataEvents = @()
  $global:MockServiceMetadata = $null
  $global:ServiceMetadataFault = ""
  $global:HttpCalls = 0
  $global:HttpReady = $true
  $global:MutateStateOnStart = $false
  $global:RemoveTargetOnStart = $false
  $global:AddDependentOnStart = $false
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
  $global:MockServiceMetadata = [pscustomobject]@{
    sid_type = 1
    reset_seconds = 86400
    on_non_crash_failures = $true
    reboot_message = ""
    command_line = ""
    actions = @(
      [pscustomobject]@{ type = 1; delay_ms = 5000 },
      [pscustomobject]@{ type = 1; delay_ms = 15000 },
      [pscustomobject]@{ type = 1; delay_ms = 60000 }
    )
  }
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
  $fields = @{}
  if (Test-Path -LiteralPath $resultPath -PathType Leaf) {
    foreach ($line in (Get-Content -LiteralPath $resultPath -Encoding UTF8)) {
      if ($line -match '^([a-z_]+)=(.*)$') { $fields[$matches[1]] = $matches[2] }
    }
  }
  return [pscustomobject]@{
    ResultCode = $fields["result"]
    Stage = $fields["stage"]
    FailureStage = $fields["failure_stage"]
    OldInstallChanged = $fields["old_install_changed"]
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

function global:Get-KaiwuServiceMetadata {
  param([string]$Name)
  if ($global:MockService.Name -ne $Name) { throw "test mock: metadata service not found" }
  $global:ServiceMetadataEvents += "read"
  if ($global:ServiceMetadataFault -eq "capture" -or
      ($global:ServiceMetadataFault -eq "readback" -and "write:Disabled" -in $global:ServiceMetadataEvents)) {
    throw "synthetic service metadata read failure"
  }
  return ($global:MockServiceMetadata | ConvertTo-Json -Depth 6 | ConvertFrom-Json)
}

function global:Set-KaiwuServiceMetadata {
  param([string]$Name, $Metadata)
  if ($global:MockService.Name -ne $Name) { throw "test mock: metadata service not found" }
  $global:ServiceMetadataEvents += "write:$($global:MockService.StartMode)"
  if ($global:ServiceMetadataFault -eq "write") {
    $global:MockServiceMetadata.sid_type = $Metadata.sid_type
    throw "synthetic service metadata write failure"
  }
  $global:MockServiceMetadata = $Metadata | ConvertTo-Json -Depth 6 | ConvertFrom-Json
  if ($global:ServiceMetadataFault -eq "mismatch") {
    $global:MockServiceMetadata.actions[1].delay_ms++
  }
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
  $global:NewServiceCalls++
  $global:MockServiceMetadata = [pscustomobject]@{
    sid_type = 0
    reset_seconds = 0
    on_non_crash_failures = $false
    reboot_message = ""
    command_line = ""
    actions = @()
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

function global:Set-Service {
  [CmdletBinding()]
  param([string]$Name, [string]$StartupType)
  if ($global:MockService.Name -ne $Name) { throw "test mock: Set-Service service not found" }
  $global:ServiceMetadataEvents += "mode:$StartupType"
  $global:MockService.StartMode = if ($StartupType -eq "Automatic") { "Auto" } else { $StartupType }
  $global:MockService.DelayedAutoStart = $false
}

function global:Start-Service {
  [CmdletBinding()]
  param([string]$Name)
  if ($null -eq $global:MockService -or $global:MockService.Name -ne $Name) {
    throw "test mock: service not found: $Name"
  }
  if ($global:MockService.StartMode -eq "Disabled") { throw "test mock: attempted to start disabled service" }
  $global:StartServiceCalls++
  $global:ServiceMetadataEvents += "start"
  if ($global:AddDependentOnStart) {
    $global:MockService.DependentServices = @([pscustomobject]@{ Name = "KaiwuDependent" })
  }
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
      if ($global:DeleteExitCode -ne 0) {
        $global:LASTEXITCODE = $global:DeleteExitCode
        return
      }
      $global:MockService = $null
      $global:MockServiceMetadata = $null
    }
    "config" {
      if ($null -ne $global:MockService) {
        $global:ServiceMetadataEvents += "mode:Delayed"
        $global:MockService.StartMode = "Auto"
        $global:MockService.DelayedAutoStart = $true
      }
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
    "Set-Service",
    "Get-KaiwuServiceMetadata",
    "Set-KaiwuServiceMetadata",
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
  Assert-RestoredServiceMetadata
  $events = $global:ServiceMetadataEvents -join ","
  Assert-True ($events -eq "read,write:Disabled,read,mode:Automatic,start,read") "B: wrong metadata/activation order: $events"
  Add-CaseResult "B" "passed" "source frpc disappeared during copy after backup" ([ordered]@{
    result = $run.ResultCode
    service_status = $global:MockService.Status
    metadata_events = $events
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
  Assert-True ($run.ResultCode -eq "FAILED_NO_PRIOR_CLEANED") "C: expected confirmed first-install cleanup"
  Assert-True ($run.FailureStage -eq "final_verification") "C: wrong failure stage: $($run.FailureStage)"
  Assert-True (($run.Error -split "`r?`n")[0] -ceq "安装失败：关键安装文件缺失: bin/frpc.exe") "C: wrong dependency failure: $($run.Error)"
  Assert-True (-not (Test-Path -LiteralPath (Join-Path $case.InstallDir "frpc.exe"))) "C: target frpc still exists"
  Assert-True ($global:StartServiceCalls -eq 1 -and $global:HttpCalls -eq 1) "C: service start and HTTP were not both exercised exactly once"
  Assert-True ($null -eq $global:MockService) "C: failed replacement service still exists"
  Add-CaseResult "C" "passed" "target frpc disappeared after service/HTTP doubles reported success" ([ordered]@{
    result = $run.ResultCode
    failure_stage = $run.FailureStage
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

function New-GeneratedRestoreFixture {
  param([string]$Id)
  $case = New-TestCase $Id -Existing
  Set-CaseEnvironment $case
  Reset-MockState
  $global:Case = $case
  $binary = Join-Path $case.InstallDir "kaiwu-connector.exe"
  $oldCommand = "`"$binary`" serve"
  Set-MockService $oldCommand "Running"
  $global:MockService.DelayedAutoStart = $true
  $sourceKaiwu = Join-Path $case.Source "bin\kaiwu-connector.exe"
  $oldConfigHash = Get-Sha256 (Join-Path $case.DataDir "config.json")
  $global:MockMachineEnvironment["KAIWU_CLOUD_BASE"] = "https://old.example.invalid"
  $global:CopyBeforeHook = {
    param($source, $destination)
    if ((Test-SamePath $source $sourceKaiwu) -and (Test-SamePath $destination $binary)) {
      throw "synthetic installer failure after a valid backup"
    }
  }.GetNewClosure()
  $firstRun = Invoke-Installer $case
  Assert-True ($firstRun.ResultCode -eq "FAILED_RESTORED") "${Id}: fixture did not create a verified backup: $($firstRun.Error)"
  $backup = Find-BackupDirectory $case
  Assert-True ($null -ne $backup) "${Id}: backup directory missing"
  $restorePath = Join-Path $backup.FullName "restore.ps1"
  Assert-True (Test-Path -LiteralPath $restorePath -PathType Leaf) "${Id}: generated restore.ps1 missing"
  $parseTokens = $null
  $parseErrors = $null
  $null = [System.Management.Automation.Language.Parser]::ParseFile($restorePath, [ref]$parseTokens, [ref]$parseErrors)
  Assert-True ($parseErrors.Count -eq 0) "${Id}: generated restore.ps1 has syntax errors: $parseErrors"
  $global:StopCalls = @()
  $global:DeleteCalls = 0
  $global:StartServiceCalls = 0
  $global:NewServiceCalls = 0
  $global:ServiceMetadataEvents = @()
  $global:CopyBeforeHook = $null
  return [pscustomobject]@{
    Case = $case
    OldCommand = $oldCommand
    OldConfigHash = $oldConfigHash
    Backup = $backup.FullName
    RestorePath = $restorePath
    ManifestPath = Join-Path $backup.FullName "manifest\backup-manifest.json"
  }
}

function Assert-RestoredServiceMetadata {
  $metadata = $global:MockServiceMetadata
  Assert-True ($metadata.sid_type -eq 1 -and $metadata.reset_seconds -eq 86400 -and
    $metadata.on_non_crash_failures -and $metadata.reboot_message -ceq "" -and $metadata.command_line -ceq "") "restored service metadata fields differ"
  Assert-True (($metadata.actions.type -join ",") -eq "1,1,1" -and
    ($metadata.actions.delay_ms -join ",") -eq "5000,15000,60000") "restored action types, delays or order differ"
}

function Run-CaseD2 {
  $fixture = New-GeneratedRestoreFixture "D2"
  # 修改合法 JSON 的空白。必须因摘要不匹配拒绝，而不能靠 JSON 或脚本解析失败通过。
  [System.IO.File]::AppendAllText($fixture.ManifestPath, "`r`n ")
  $null = Get-Content -LiteralPath $fixture.ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
  $restore = Invoke-GeneratedRestore $fixture.RestorePath
  Assert-True ($restore.Failed -and $restore.Error -ceq "恢复失败：备份清单摘要不匹配") "D2: wrong rejection: $($restore.Error)"
  Assert-True ($global:StopCalls.Count -eq 0 -and $global:DeleteCalls -eq 0 -and
    $global:StartServiceCalls -eq 0 -and $global:NewServiceCalls -eq 0) "D2: service was touched before hash rejection"
  Add-CaseResult "D2" "passed" "valid JSON with mismatched manifest hash rejected" ([ordered]@{
    error = $restore.Error
    stop_calls = $global:StopCalls.Count
    delete_calls = $global:DeleteCalls
    start_calls = $global:StartServiceCalls
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
  Assert-True ($run.OldInstallChanged -eq "false" -and $global:StartServiceCalls -eq 0 -and
    $global:NewServiceCalls -eq 0 -and ($global:ServiceMetadataEvents -join ",") -eq "read") "G: untouched original service was changed"
  Add-CaseResult "G" "passed" "dependent service blocks target stop and delete" ([ordered]@{
    result = $run.ResultCode
    stop_calls = $global:StopCalls.Count
    delete_calls = $global:DeleteCalls
  })
}

function Run-CaseH {
  $fixture = New-GeneratedRestoreFixture "H"
  $snapshot = Get-Content -LiteralPath (Join-Path $fixture.Backup "service.json") -Raw -Encoding UTF8 | ConvertFrom-Json
  Assert-True ($snapshot.scm_metadata.sid_type -eq 1 -and $snapshot.scm_metadata.reset_seconds -eq 86400 -and
    ($snapshot.scm_metadata.actions.delay_ms -join ",") -eq "5000,15000,60000") "H: service metadata not saved in backup"
  Set-MockService "replacement-command" "Running"
  $global:MockServiceMetadata.sid_type = 3
  $global:MockServiceMetadata.actions = @()
  $global:MockMachineEnvironment["KAIWU_CLOUD_BASE"] = "https://new.example.invalid"
  $config = Join-Path $fixture.Case.DataDir "config.json"
  [System.IO.File]::WriteAllText($config, '{"business":"replacement"}')
  $restore = Invoke-GeneratedRestore $fixture.RestorePath
  Assert-True (-not $restore.Failed) "H: generated restoration failed: $($restore.Error)"
  Assert-True ($global:NewServiceCalls -eq 1 -and $global:DeleteCalls -eq 1 -and $global:StartServiceCalls -eq 1) "H: expected one service rebuild and start"
  Assert-True ($global:MockService.Status -eq "Running" -and $global:MockService.PathName -ceq $fixture.OldCommand -and
    $global:MockService.StartMode -eq "Auto" -and $global:MockService.DelayedAutoStart) "H: restored registration or state differs"
  Assert-RestoredServiceMetadata
  $events = $global:ServiceMetadataEvents -join ","
  Assert-True ($events -eq "write:Disabled,read,mode:Delayed,start,read") "H: wrong metadata/activation order: $events"
  Assert-True ((Get-Sha256 $config) -eq $fixture.OldConfigHash -and
    $global:MockMachineEnvironment["KAIWU_CLOUD_BASE"] -eq "https://old.example.invalid") "H: prior business configuration was not restored"
  Add-CaseResult "H" "passed" "generated restoration from an intact backup" ([ordered]@{
    service_status = $global:MockService.Status
    metadata_events = $events
    sid_type = $global:MockServiceMetadata.sid_type
    failure_reset_seconds = $global:MockServiceMetadata.reset_seconds
    failure_action_delays_ms = $global:MockServiceMetadata.actions.delay_ms
  })
}

function Run-CaseI {
  $case = New-TestCase "I" -Existing
  Set-CaseEnvironment $case
  Reset-MockState
  Set-MockService (Join-Path $case.InstallDir "kaiwu-connector.exe") "Running"
  $config = Join-Path $case.DataDir "config.json"
  $hash = Get-Sha256 $config
  $global:ServiceMetadataFault = "capture"
  $run = Invoke-Installer $case
  Assert-True ($run.ResultCode -eq "PRECHECK_FAILED" -and $run.Stage -eq "backup_prepare" -and
    $run.Error -ceq "synthetic service metadata read failure") "I: wrong precheck failure: $($run.Error)"
  Assert-True ($run.OldInstallChanged -eq "false" -and $global:MockService.Status -eq "Running" -and
    $global:StopCalls.Count -eq 0 -and $global:DeleteCalls -eq 0 -and $global:StartServiceCalls -eq 0 -and
    $global:NewServiceCalls -eq 0 -and (Get-Sha256 $config) -eq $hash) "I: old service or files changed after failed capture"
  Add-CaseResult "I" "passed" "failed capture blocks mutation" @{ result = $run.ResultCode; stage = $run.Stage }
}

function Run-CaseJ {
  $case = New-TestCase "J" -Existing
  Set-CaseEnvironment $case
  Reset-MockState
  Set-MockService (Join-Path $case.InstallDir "kaiwu-connector.exe") "Running"
  $original = $global:MockService
  $global:DeleteExitCode = 5
  $run = Invoke-Installer $case
  $events = $global:ServiceMetadataEvents -join ","
  Assert-True ($run.ResultCode -eq "FAILED_RESTORED" -and $run.FailureStage -eq "replace_install") "J: old registration recovery failed: $($run.Error)"
  Assert-True ([object]::ReferenceEquals($original, $global:MockService)) "J: original registration was replaced"
  Assert-True ($global:MockService.Status -eq "Running" -and $global:StopCalls.Count -eq 1 -and
    $global:DeleteCalls -eq 1 -and $global:NewServiceCalls -eq 0 -and $global:StartServiceCalls -eq 1 -and
    $events -eq "read,read,start,read") "J: original service was retried or reconfigured: $events"
  $stoppedCase = New-TestCase "J-stopped" -Existing
  Set-CaseEnvironment $stoppedCase
  Reset-MockState
  Set-MockService (Join-Path $stoppedCase.InstallDir "kaiwu-connector.exe") "Stopped"
  $stoppedOriginal = $global:MockService
  $global:DeleteExitCode = 5
  $stoppedRun = Invoke-Installer $stoppedCase
  Assert-True ($stoppedRun.ResultCode -eq "FAILED_RECOVERY_UNCONFIRMED" -and $stoppedRun.OldInstallChanged -eq "false" -and
    [object]::ReferenceEquals($stoppedOriginal, $global:MockService) -and $global:MockService.Status -eq "Stopped" -and
    $global:DeleteCalls -eq 1 -and $global:StopCalls.Count -eq 0 -and $global:StartServiceCalls -eq 0 -and
    $global:NewServiceCalls -eq 0 -and ($global:ServiceMetadataEvents -join ",") -eq "read") "J: unchanged stopped service was touched"
  Add-CaseResult "J" "passed" "delete rejected with original service running or already stopped" @{
    resumed_result = $run.ResultCode
    resumed_metadata_events = $events
    untouched_result = $stoppedRun.ResultCode
    untouched_old_install_changed = $stoppedRun.OldInstallChanged
  }
}

function Run-CaseK {
  $fixture = New-GeneratedRestoreFixture "K"
  $snapshotPath = Join-Path $fixture.Backup "service.json"
  $snapshot = Get-Content -LiteralPath $snapshotPath -Raw -Encoding UTF8 | ConvertFrom-Json
  $snapshot.PSObject.Properties.Remove("scm_metadata")
  $snapshot | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $snapshotPath -Encoding UTF8
  # 仅在合成旧格式夹具中更新摘要，使检查确实到达服务快照校验。
  $oldHash = Get-Sha256 $fixture.ManifestPath
  $manifest = Get-Content -LiteralPath $fixture.ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
  $entry = $manifest.entries | Where-Object { $_.category -eq "metadata" -and $_.relative_path -eq "service.json" }
  $entry.sha256 = Get-Sha256 $snapshotPath
  $manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $fixture.ManifestPath -Encoding UTF8
  $body = Get-Content -LiteralPath $fixture.RestorePath -Raw -Encoding UTF8
  $body.Replace($oldHash, (Get-Sha256 $fixture.ManifestPath)) | Set-Content -LiteralPath $fixture.RestorePath -Encoding UTF8
  $restore = Invoke-GeneratedRestore $fixture.RestorePath
  Assert-True ($restore.Failed -and $restore.Error -ceq "恢复未确认：旧服务快照缺少安全标识符和故障动作元数据") "K: wrong rejection: $($restore.Error)"
  Assert-True ($global:StopCalls.Count -eq 0 -and $global:DeleteCalls -eq 0 -and $global:NewServiceCalls -eq 0 -and
    $global:StartServiceCalls -eq 0 -and $global:ServiceMetadataEvents.Count -eq 0) "K: old-format snapshot touched the current service"
  Add-CaseResult "K" "passed" "old-format snapshot blocked before changing current service" @{ error = $restore.Error }
}

function Run-RecoveryFaults {
  param([string]$Id, [switch]$Generated)
  $faults = [ordered]@{
    write = "synthetic service metadata write failure"
    readback = "synthetic service metadata read failure"
    mismatch = "恢复未确认：服务故障动作读回不匹配"
    stable = "恢复后稳定目标缺失: frpc.exe"
    dependent = "恢复后稳定目标缺失: frpc.exe"
  }
  $checks = [ordered]@{}
  foreach ($fault in $faults.Keys) {
    if ($Generated) {
      $fixture = New-GeneratedRestoreFixture "$Id-$fault"
    } else {
      $case = New-TestCase "$Id-$fault" -Existing
      Set-CaseEnvironment $case
      Reset-MockState
      $global:Case = $case
      $oldBinary = Join-Path $case.InstallDir "kaiwu-connector.exe"
      $sourceBinary = Join-Path $case.Source "bin\kaiwu-connector.exe"
      Set-MockService $oldBinary "Running"
      $global:CopyBeforeHook = {
        param($source, $destination)
        if ((Test-SamePath $source $sourceBinary) -and (Test-SamePath $destination $oldBinary)) {
          throw "synthetic replacement failure"
        }
      }.GetNewClosure()
    }
    $afterStart = $fault -in @("stable", "dependent")
    $global:ServiceMetadataFault = $fault
    $global:RemoveTargetOnStart = $afterStart
    $global:AddDependentOnStart = $fault -eq "dependent"

    if ($Generated) {
      $restore = Invoke-GeneratedRestore $fixture.RestorePath
      Assert-True $restore.Failed "${Id}/${fault}: generated restore reported success"
      $errorText = $restore.Error
    } else {
      $run = Invoke-Installer $case
      Assert-True ($run.ResultCode -eq "FAILED_RECOVERY_UNCONFIRMED") "${Id}/${fault}: wrong result: $($run.ResultCode)"
      $errorText = $run.Error
    }
    Assert-True ($errorText.Contains($faults[$fault])) "${Id}/${fault}: wrong failure: $errorText"
    Assert-True ($global:DeleteCalls -eq 1 -and $global:NewServiceCalls -eq 1) "${Id}/${fault}: expected one service rebuild"
    Assert-True ($global:StartServiceCalls -eq [int]$afterStart) "${Id}/${fault}: incorrect start count"
    if ($fault -eq "dependent") {
      Assert-True ($global:MockService.Status -eq "Running" -and $global:StopCalls.Count -eq 1 -and
        $errorText.Contains("未能安全收尾") -and $errorText.Contains("KaiwuDependent")) "${Id}/${fault}: dependent-services guard did not block cleanup"
    } else {
      Assert-True ($global:MockService.StartMode -eq "Disabled" -and $global:MockService.Status -eq "Stopped" -and
        $global:StopCalls.Count -eq (1 + [int]$afterStart)) "${Id}/${fault}: target was not safely contained"
    }
    Assert-True (@($global:StopCalls | Where-Object { $_ -ne "KaiwuConnector" }).Count -eq 0) "${Id}/${fault}: another service was stopped"
    $checks[$fault] = @{
      error = $faults[$fault]
      service_status = $global:MockService.Status
      start_calls = $global:StartServiceCalls
      stop_calls = $global:StopCalls.Count
    }
  }
  Add-CaseResult $Id "passed" "failed metadata write/readback and post-start verification" $checks
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
  Invoke-Case "H" { Run-CaseH }
  Invoke-Case "I" { Run-CaseI }
  Invoke-Case "J" { Run-CaseJ }
  Invoke-Case "K" { Run-CaseK }
  Invoke-Case "L" { Run-RecoveryFaults "L" }
  Invoke-Case "M" { Run-RecoveryFaults "M" -Generated }
  if (($script:Results.cases.id -join ",") -ceq "A,B,C,D1,D2,E,F,G,H,I,J,K,L,M" -and
      @($script:Results.cases | Where-Object { $_.status -ne "passed" }).Count -eq 0) {
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
