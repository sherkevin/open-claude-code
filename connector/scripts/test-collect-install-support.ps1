# Offline fixture tests for collect-install-support.ps1. No live probes or installation.
$ErrorActionPreference = 'Stop'
$collector = Join-Path $PSScriptRoot 'collect-install-support.ps1'
. $collector

function Assert-Equal {
  param($Actual, $Expected, [string]$Case)
  if ($Actual -cne $Expected) { throw "$Case expected '$Expected', got '$Actual'" }
  $script:assertions++
}

function Assert-Private {
  param([string]$Report, [string]$Case)
  if ($Report -match 'synthetic-private|C:\\|\\\\internal|198\.51\.100\.17|example\.invalid') {
    throw "$Case disclosed private sample data"
  }
  $script:assertions++
}

# Replace the live query inputs while exercising the real status mapping.
function Get-Service {
  param($Name, $ErrorAction)
  if ($Name -cne 'KaiwuConnector') { throw 'Unexpected service query' }
  if ($script:serviceStatus -ceq 'query_failed') { throw 'synthetic-private-service' }
  if ($script:serviceStatus -ceq 'not_found') {
    $exception = [InvalidOperationException]::new('synthetic-private-service')
    $errorRecord = [System.Management.Automation.ErrorRecord]::new($exception,
      'NoServiceFoundForGivenName,Microsoft.PowerShell.Commands.GetServiceCommand',
      [System.Management.Automation.ErrorCategory]::ObjectNotFound, $Name)
    throw $errorRecord
  }
  [pscustomobject]@{ Status = $script:serviceStatus; DisplayName = 'synthetic-private-display-name' }
}

function Get-KaiwuSupportTcpListeners {
  if ($script:portStatus -ceq 'query_failed') { throw 'synthetic-private-network' }
  [pscustomobject]@{ Port = 17871; Address = '198.51.100.17'; OwningProcess = 4444 }
  if ($script:portStatus -ceq 'listening') {
    [pscustomobject]@{ Port = 17872; Address = '198.51.100.17'; OwningProcess = 4444 }
  }
}

function Write-Checkpoints {
  param([string]$Id, [string[]]$Rows, [string]$Ending = $CRLF)
  $text = "package_check_attempt_id=$Id" + $CRLF + ($Rows -join $CRLF) + $Ending
  [IO.File]::WriteAllText($checkpointsPath, $text, [Text.Encoding]::Unicode)
}

function Write-Events {
  param([string]$Text)
  [IO.File]::WriteAllText($journalPath, $Text, [Text.Encoding]::ASCII)
}

function Read-Report {
  (ConvertTo-KaiwuSupportJson (Get-KaiwuSupportReport $requestedId $fixture)) | ConvertFrom-Json
}

$assertions = 0
$serviceStatus = 'Running'
$portStatus = 'listening'
$requestedId = '0123456789abcdef0123456789abcdef'
$oldId = 'abcdef0123456789abcdef0123456789'
$fixture = Join-Path $PSScriptRoot ('.install-support-fixture-' + [guid]::NewGuid().ToString('N'))
$journalDirectory = Join-Path $fixture 'install-attempts'
$checkpointsPath = Join-Path $fixture 'install-package-checkpoints.txt'
$journalPath = Join-Path $journalDirectory "$requestedId.jsonl"
$oldJournalPath = Join-Path $journalDirectory "$oldId.jsonl"
$CRLF = [string][char]13 + [char]10
$LF = [string][char]10
$start = '{"v":1,"seq":1,"stage":"started","outcome":"started","code":"ok"}'
$precheck = '{"v":1,"seq":2,"stage":"precheck","outcome":"ok","code":"ok"}'
$failure = '{"v":1,"seq":3,"stage":"precheck","outcome":"failed","code":"checksum_mismatch"}'
$finish = '{"v":1,"seq":4,"stage":"finish","outcome":"failed","code":"unknown"}'
$checkpoint = 'package_check=source_precheck|bin/kaiwu-connector.exe|present|ok'
$completeJournal = (@($start, $precheck, $failure, $finish, '') -join $LF)

try {
  $missingRoot = Get-KaiwuSupportReport $requestedId $fixture
  Assert-Equal $missingRoot.install_events.status 'missing' 'absent installation log directory'
  $null = [IO.Directory]::CreateDirectory($journalDirectory)
  $report = Read-Report
  Assert-Equal $report.package_checkpoints.status 'missing' 'absent checkpoints'
  Assert-Equal $report.install_events.status 'missing' 'absent journal'
  Assert-Equal $report.install_events.rows.Count 0 'absent journal rows'

  $checkpointRows = @(
    $checkpoint
    'package_check=source_precheck|bin/frpc.exe|present|ok'
    'package_check=source_precheck|service-handoff.ps1|present|ok'
    'package_check=pre_change_verification|bin/kaiwu-connector.exe|present|checksum_mismatch'
  )
  Write-Checkpoints $requestedId $checkpointRows
  Write-Events $completeJournal
  $before = [Convert]::ToBase64String([IO.File]::ReadAllBytes($journalPath))
  $json = ConvertTo-KaiwuSupportJson (Get-KaiwuSupportReport $requestedId $fixture)
  $report = $json | ConvertFrom-Json
  Assert-Equal $report.install_attempt_id $requestedId 'requested installation ID'
  Assert-Equal $report.package_checkpoints.status 'available' 'valid checkpoints'
  Assert-Equal $report.package_checkpoints.rows.Count 4 'checkpoints include both verification stages'
  Assert-Equal $report.package_checkpoints.rows[3].code 'checksum_mismatch' 'validated checkpoint code'
  Assert-Equal $report.install_events.status 'complete' 'finished failed attempt'
  Assert-Equal $report.install_events.rows.Count 4 'repeated precheck stage kept'
  Assert-Equal $report.install_events.rows[2].code 'checksum_mismatch' 'worker failure code kept'
  Assert-Equal $report.install_events.rows[3].code 'unknown' 'launcher outcome kept without overriding worker'
  Assert-Equal ($json.Length -lt 10000) $true 'small report'
  Assert-Equal ([Convert]::ToBase64String([IO.File]::ReadAllBytes($journalPath))) $before 'journal not modified'
  Assert-Private $json 'normal fixture'

  Write-Events ($start + $LF + '{"v":1,"seq":2,"stage":"finish","outcome":"ok","code":"ok"}' + $LF)
  Assert-Equal (Read-Report).install_events.status 'complete' 'successful attempt'

  $longJournal = @($start)
  foreach ($seq in 2..47) {
    $longJournal += '{"v":1,"seq":' + $seq + ',"stage":"verify","outcome":"started","code":"ok"}'
  }
  $longJournal += '{"v":1,"seq":48,"stage":"finish","outcome":"failed","code":"recovery_unconfirmed"}'
  Write-Events (($longJournal -join $LF) + $LF)
  $json = ConvertTo-KaiwuSupportJson (Get-KaiwuSupportReport $requestedId $fixture)
  $report = $json | ConvertFrom-Json
  Assert-Equal $report.install_events.status 'complete' 'journal at event count limit'
  Assert-Equal $report.install_events.rows.Count 48 'all allowed events retained'
  Assert-Equal ($json.Length -lt 10000) $true 'report with 48 events is small'
  Assert-Equal ($json -notmatch '[\r\n]') $true 'shareable output is one line'

  Write-Checkpoints $oldId @($checkpoint)
  [IO.File]::WriteAllText($oldJournalPath, $completeJournal, [Text.Encoding]::ASCII)
  $report = Read-Report
  Assert-Equal $report.package_checkpoints.status 'different_attempt' 'checkpoint belongs to older attempt'
  Assert-Equal $report.package_checkpoints.rows.Count 0 'old checkpoint rows omitted'
  Assert-Equal $report.install_events.status 'complete' 'requested journal unaffected by old checkpoint'
  [IO.File]::Delete($journalPath)
  $report = Read-Report
  Assert-Equal $report.install_events.status 'missing' 'no fallback to older journal'
  Assert-Equal ((ConvertTo-KaiwuSupportJson $report).Contains($oldId)) $false 'no old ID disclosed'
  $oldReport = Get-KaiwuSupportReport $oldId $fixture
  Assert-Equal $oldReport.install_events.status 'complete' 'old journal readable when specifically requested'
  Assert-Equal $oldReport.package_checkpoints.rows.Count 1 'single genuine row is valid for matching ID'

  Write-Checkpoints $requestedId @($checkpoint) ''
  $report = Read-Report
  Assert-Equal $report.package_checkpoints.status 'incomplete' 'checkpoint lacks terminating CRLF'
  Assert-Equal $report.package_checkpoints.rows.Count 0 'truncated checkpoint rows omitted'
  Write-Checkpoints $requestedId @($checkpoint)
  $bytes = [IO.File]::ReadAllBytes($checkpointsPath)
  [IO.File]::WriteAllBytes($checkpointsPath, $bytes[0..($bytes.Length - 2)])
  Assert-Equal (Read-Report).package_checkpoints.status 'incomplete' 'cut Unicode code unit'

  Write-Events ($completeJournal.TrimEnd([char]10))
  $report = Read-Report
  Assert-Equal $report.install_events.status 'incomplete' 'finish without newline'
  Assert-Equal $report.install_events.rows.Count 3 'unterminated finish omitted'
  Write-Events ($start + $LF + '{"v":1,"seq":2')
  $report = Read-Report
  Assert-Equal $report.install_events.status 'incomplete' 'journal truncated within final record'
  Assert-Equal $report.install_events.rows.Count 1 'prefix of truncated journal kept'
  Write-Events ($start + $LF)
  Assert-Equal (Read-Report).install_events.status 'incomplete' 'journal without any finish'
  Write-Events ''
  Assert-Equal (Read-Report).install_events.status 'incomplete' 'empty journal'

  foreach ($badCheckpoint in @(
      'package_check=source_precheck|C:\synthetic-private\config.json|present|ok'
      'package_check=source_precheck|bin/frpc.exe|present|synthetic-private'
      'package_check=source_precheck|bin/frpc.exe|present|file_missing'
      $checkpoint
  )) {
    Write-Checkpoints $requestedId @($checkpoint, $badCheckpoint)
    $report = Read-Report
    Assert-Equal $report.package_checkpoints.status 'invalid' 'invalid, incompatible or duplicate checkpoint'
    Assert-Equal $report.package_checkpoints.rows.Count 0 'no rows from invalid checkpoint'
    Assert-Private (ConvertTo-KaiwuSupportJson $report) 'invalid checkpoint'
  }

  foreach ($badEvent in @(
      '{"v":1,"seq":2,"stage":"precheck","outcome":"ok","code":"ok","note":"synthetic-private"}'
      '{"v":1,"seq":2,"stage":"precheck","outcome":"ok","code":"https://example.invalid/synthetic-private"}'
      '{"v":1,"seq":2,"stage":"C:\\synthetic-private\\config.json","outcome":"ok","code":"ok"}'
      '{"v":1,"seq":2,"stage":"precheck","outcome":"ok","code":"ok","code":"unknown"}'
      '{"v":1,"seq":1,"stage":"precheck","outcome":"ok","code":"ok"}'
      '{"v":1,"seq":3,"stage":"precheck","outcome":"ok","code":"ok"}'
      '{"v":1,"seq":2,"stage":"precheck","outcome":"OK","code":"ok"}'
  )) {
    Write-Events ($start + $LF + $badEvent + $LF)
    $report = Read-Report
    Assert-Equal $report.install_events.status 'invalid' 'malformed, duplicated, gapped or injected event'
    Assert-Equal $report.install_events.rows.Count 1 'only validated journal prefix'
    Assert-Private (ConvertTo-KaiwuSupportJson $report) 'malformed journal'
  }
  Write-Events ($completeJournal + 'synthetic-private')
  Assert-Equal (Read-Report).install_events.status 'invalid' 'data after finish'
  Write-Events ('x' * 4097)
  Assert-Equal (Read-Report).install_events.status 'size_limit' 'oversize journal'
  [IO.File]::WriteAllBytes($checkpointsPath, [byte[]]::new(4097))
  Assert-Equal (Read-Report).package_checkpoints.status 'size_limit' 'oversize checkpoint'

  [IO.File]::Delete($journalPath)
  $null = [IO.Directory]::CreateDirectory($journalPath)
  Assert-Equal (Read-Report).install_events.status 'unsafe_path' 'directory substituted for journal'
  [IO.Directory]::Delete($journalPath)

  Write-Events $completeJournal
  $locked = [IO.FileStream]::new($journalPath, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
  try {
    Assert-Equal (Read-Report).install_events.status 'unavailable' 'locked journal is not missing'
  } finally {
    $locked.Dispose()
  }

  # Both paths resolve to a synthetic config if followed. Some Windows hosts
  # disallow symlink creation even in a writable fixture directory.
  $privatePath = Join-Path $fixture 'config.json'
  [IO.File]::WriteAllText($privatePath, 'synthetic-private')
  foreach ($linkKind in @('SymbolicLink', 'HardLink')) {
    [IO.File]::Delete($journalPath)
    try {
      $null = New-Item -ItemType $linkKind -Path $journalPath -Target $privatePath -ErrorAction Stop
    } catch {
      Write-Output "Skipped $linkKind sample because this host cannot create it."
      continue
    }
    Assert-Equal (Read-Report).install_events.status 'unsafe_path' "$linkKind substituted for journal"
  }

  $linkedDirectory = Join-Path $fixture 'linked'
  try {
    $null = New-Item -ItemType SymbolicLink -Path $linkedDirectory -Target $journalDirectory -ErrorAction Stop
  } catch {
    Write-Output 'Skipped linked directory sample because this host cannot create it.'
    $linkedDirectory = $null
  }
  if ($linkedDirectory) {
    try {
      $redirectedJournal = Join-Path $linkedDirectory "$requestedId.jsonl"
      Assert-Equal (Read-KaiwuSupportFile $redirectedJournal).Status 'unsafe_path' 'linked journal parent'
    } finally {
      [IO.Directory]::Delete($linkedDirectory)
    }
  }

  Assert-Equal (Get-KaiwuSupportServiceStatus) 'running' 'service running'
  $serviceStatus = 'Stopped'
  Assert-Equal (Get-KaiwuSupportServiceStatus) 'stopped' 'service stopped'
  $serviceStatus = 'not_found'
  Assert-Equal (Get-KaiwuSupportServiceStatus) 'not_found' 'named service absent'
  $serviceStatus = 'query_failed'
  Assert-Equal (Get-KaiwuSupportServiceStatus) 'unavailable' 'service query error'
  $serviceStatus = 'C:\synthetic-private\service'
  Assert-Equal (Get-KaiwuSupportServiceStatus) 'unavailable' 'unknown service status is withheld'
  Assert-Equal (Get-KaiwuSupportPortStatus) 'listening' 'fixed port listening'
  $portStatus = 'no_listener'
  Assert-Equal (Get-KaiwuSupportPortStatus) 'not_listening' 'other local port ignored'
  $portStatus = 'query_failed'
  Assert-Equal (Get-KaiwuSupportPortStatus) 'unavailable' 'port query error'

  foreach ($badId in @('..\synthetic-private\config.json', '\\internal\synthetic-private', $requestedId.ToUpperInvariant(),
                        ($requestedId + $LF), '')) {
    $invalid = & $collector -AttemptId $badId 2>&1
    Assert-Equal $LASTEXITCODE 2 'invalid ID exit code'
    Assert-Equal $invalid '{"schema_version":1,"error":"expected_32_lowercase_hex_attempt_id"}' 'invalid ID response'
    Assert-Private $invalid 'invalid ID'
  }
} finally {
  Remove-Item -LiteralPath $fixture -Recurse -Force
}

Write-Output "Passed $assertions assertions."
