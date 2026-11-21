# Offline support collection for the Kaiwu Connector graphical installer.
param([string]$AttemptId)

function Get-KaiwuSupportLogsDirectory {
  try {
    $root = [Environment]::GetFolderPath('CommonApplicationData', 'DoNotVerify')
    if ($root -cmatch '\A[A-Za-z]:\\' -and [IO.DriveInfo]::new([IO.Path]::GetPathRoot($root)).DriveType -eq 'Fixed') {
      return [IO.Path]::Combine($root, 'Kaiwu', 'Connector', 'logs')
    }
  } catch {
    # The caller reports unavailable without exposing the path or system error.
  }
}

function Read-KaiwuSupportFile {
  param([string]$Path)

  if (-not $Path) { return @{ Status = 'unavailable' } }

  try {
    $file = [IO.FileInfo]::new($Path)
    $directories = [Collections.Generic.Stack[string]]::new()
    for ($parent = $file.Directory; $null -ne $parent; $parent = $parent.Parent) {
      $directories.Push($parent.FullName)
    }
    $unsafe = [IO.FileAttributes]'ReparsePoint, Offline, Device'
    foreach ($directory in $directories) {
      $attributes = [IO.File]::GetAttributes($directory)
      if (($attributes -band $unsafe) -or -not ($attributes -band [IO.FileAttributes]::Directory)) {
        return @{ Status = 'unsafe_path' }
      }
    }

    $attributes = [IO.File]::GetAttributes($file.FullName)
    if ($attributes -band ($unsafe -bor [IO.FileAttributes]::Directory)) {
      return @{ Status = 'unsafe_path' }
    }
    $item = Get-Item -LiteralPath $file.FullName -Force -ErrorAction Stop
    $linkType = $item.PSObject.Properties['LinkType']
    if ($null -eq $linkType) { return @{ Status = 'unavailable' } }
    if ($linkType.Value) { return @{ Status = 'unsafe_path' } }

    # Read at most one byte beyond the writer's 4096-byte limit. Sharing
    # permits an installer to append or publish a replacement during collection.
    $stream = [IO.FileStream]::new($file.FullName, [IO.FileMode]::Open, [IO.FileAccess]::Read,
                                  [IO.FileShare]'ReadWrite, Delete', 1)
    try {
      $reader = [IO.BinaryReader]::new($stream)
      $bytes = $reader.ReadBytes(4097)
      if ($bytes.Length -gt 4096) { return @{ Status = 'size_limit' } }
      return @{ Status = 'read'; Bytes = $bytes }
    } finally {
      $stream.Dispose()
    }
  } catch {
    $cause = $_.Exception.GetBaseException()
    if ($cause -is [IO.FileNotFoundException] -or $cause -is [IO.DirectoryNotFoundException]) {
      return @{ Status = 'missing' }
    }
    return @{ Status = 'unavailable' }
  }
}

function Get-KaiwuSupportCheckpoints {
  param([string]$Path, [string]$RequestedId)

  $file = Read-KaiwuSupportFile $Path
  $result = [ordered]@{ status = $file.Status; rows = @() }
  if ($file.Status -cne 'read') { return $result }

  # attempt-journal.c publishes UTF-16LE with a BOM and CRLF, without the
  # private worker's counted footer. A valid file can contain just one row.
  $result.status = 'invalid'
  $bytes = $file.Bytes
  if ($bytes.Length -lt 2 -or $bytes[0] -ne 255 -or $bytes[1] -ne 254) { return $result }
  if ($bytes.Length % 2 -ne 0) {
    $result.status = 'incomplete'
    return $result
  }
  try {
    $text = [Text.UnicodeEncoding]::new($false, $true, $true).GetString($bytes, 2, $bytes.Length - 2)
  } catch {
    return $result
  }
  $lines = $text -split '\r\n'
  if ($lines[0] -cnotmatch '\Apackage_check_attempt_id=([0-9a-f]{32})\z') { return $result }
  if ($Matches[1] -cne $RequestedId) {
    $result.status = 'different_attempt'
    return $result
  }
  if ($lines[-1] -cne '') {
    $result.status = 'incomplete'
    return $result
  }
  if ($lines.Length -lt 3 -or $lines.Length -gt 8) { return $result }

  $rowPattern = '\Apackage_check=(source_precheck|pre_change_verification)\|(bin/kaiwu-connector\.exe|bin/frpc\.exe|service-handoff\.ps1)\|(present\|ok|present\|checksum_mismatch|missing\|file_missing|access_denied\|access_denied|unknown\|unknown)\z'
  $rows = [Collections.Generic.List[object]]::new()
  $seen = @{}
  for ($index = 1; $index -lt $lines.Length - 1; $index++) {
    if ($lines[$index] -cnotmatch $rowPattern) { return $result }
    $checkpoint = $Matches[1]
    $productFile = $Matches[2]
    $state, $code = $Matches[3].Split('|')
    $key = "$checkpoint|$productFile"
    if ($seen.ContainsKey($key)) { return $result }
    $seen[$key] = $true
    $rows.Add([ordered]@{ checkpoint = $checkpoint; product_file = $productFile; state = $state; code = $code })
  }
  $result.status = 'available'
  $result.rows = $rows.ToArray()
  return $result
}

function Get-KaiwuSupportEvents {
  param([string]$Path)

  $file = Read-KaiwuSupportFile $Path
  $result = [ordered]@{ status = $file.Status; rows = @() }
  if ($file.Status -cne 'read') { return $result }

  # Both Windows writers emit these five fields in this order. A valid
  # prefix remains useful even if the writer never emitted a complete finish.
  $lines = [Text.Encoding]::ASCII.GetString($file.Bytes).Split([char]10)
  $eventPattern = '\A\{"v":1,"seq":([1-9][0-9]?),"stage":"([a-z_]+)","outcome":"([a-z_]+)","code":"([a-z_]+)"\}\z'
  $stages = @('started', 'precheck', 'extract', 'lock', 'backup', 'replace', 'service', 'verify', 'rollback', 'finish')
  $outcomes = @('started', 'ok', 'failed')
  $codes = @('ok', 'unknown', 'unsupported_host', 'busy', 'file_missing', 'checksum_mismatch', 'access_denied',
             'service_registration', 'service_start', 'local_page_unready', 'write_failed', 'restored', 'recovery_unconfirmed')
  $rows = [Collections.Generic.List[object]]::new()
  $result.status = 'incomplete'
  for ($index = 0; $index -lt $lines.Length - 1; $index++) {
    $line = $lines[$index]
    $result.status = 'invalid'
    if ($line.Length -gt 256 -or $line -cnotmatch $eventPattern) { break }
    $seq = [int]$Matches[1]
    $stage = $Matches[2]
    $outcome = $Matches[3]
    $code = $Matches[4]
    if ($seq -ne $index + 1 -or $seq -gt 48 -or $stage -cnotin $stages -or
        $outcome -cnotin $outcomes -or $code -cnotin $codes) { break }

    if ($index -eq 0) {
      if ($stage -cne 'started' -or $outcome -cne 'started' -or $code -cne 'ok') { break }
    } elseif ($stage -ceq 'started') {
      break
    }

    if ($stage -ceq 'finish') {
      if ($index -ne $lines.Length - 2 -or $lines[-1] -cne '' -or
          -not (($outcome -ceq 'ok' -and $code -ceq 'ok') -or
                ($outcome -ceq 'failed' -and $code -cne 'ok'))) { break }
      $result.status = 'complete'
    } else {
      $result.status = 'incomplete'
    }
    $rows.Add([ordered]@{ seq = $seq; stage = $stage; outcome = $outcome; code = $code })
  }
  $result.rows = $rows.ToArray()
  return $result
}

function Get-KaiwuSupportServiceStatus {
  try {
    $service = Get-Service -Name 'KaiwuConnector' -ErrorAction Stop
    switch -CaseSensitive ([string]$service.Status) {
      'Running'         { return 'running' }
      'Stopped'         { return 'stopped' }
      'Paused'          { return 'paused' }
      'StartPending'    { return 'start_pending' }
      'StopPending'     { return 'stop_pending' }
      'PausePending'    { return 'pause_pending' }
      'ContinuePending' { return 'continue_pending' }
    }
  } catch {
    if ($_.FullyQualifiedErrorId -ceq 'NoServiceFoundForGivenName,Microsoft.PowerShell.Commands.GetServiceCommand') {
      return 'not_found'
    }
  }
  return 'unavailable'
}

function Get-KaiwuSupportTcpListeners {
  [Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
}

function Get-KaiwuSupportPortStatus {
  try {
    foreach ($listener in (Get-KaiwuSupportTcpListeners)) {
      if ($listener.Port -eq 17872) { return 'listening' }
    }
    return 'not_listening'
  } catch {
    return 'unavailable'
  }
}

function Get-KaiwuSupportReport {
  param([string]$RequestedId, [string]$LogsDirectory)

  $checkpointsPath = $null
  $journalPath = $null
  if ($LogsDirectory) {
    $checkpointsPath = [IO.Path]::Combine($LogsDirectory, 'install-package-checkpoints.txt')
    $journalPath = [IO.Path]::Combine($LogsDirectory, 'install-attempts', "$RequestedId.jsonl")
  }

  $serviceStatus = 'unavailable'
  $portStatus = 'unavailable'
  if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
    $serviceStatus = Get-KaiwuSupportServiceStatus
    $portStatus = Get-KaiwuSupportPortStatus
  }
  return [ordered]@{
    schema_version = 1
    install_attempt_id = $RequestedId
    package_checkpoints = Get-KaiwuSupportCheckpoints $checkpointsPath $RequestedId
    install_events = Get-KaiwuSupportEvents $journalPath
    current_local_status = [ordered]@{
      service = $serviceStatus
      tcp_port_17872 = $portStatus
    }
  }
}

function ConvertTo-KaiwuSupportJson {
  param($Report)
  # PowerShell 5.1 indents nested objects more than newer versions by default.
  # Keep the transport size predictable while preserving every validated row.
  ConvertTo-Json -InputObject $Report -Depth 6 -Compress
}

# Dot-sourcing loads functions for the offline fixture test. Normal execution
# exposes only an attempt ID, never a path to arbitrary files.
if ($MyInvocation.InvocationName -eq '.') { return }
if ($AttemptId -cnotmatch '\A[0-9a-f]{32}\z' -or $args.Count -ne 0) {
  '{"schema_version":1,"error":"expected_32_lowercase_hex_attempt_id"}'
  exit 2
}

$report = Get-KaiwuSupportReport $AttemptId (Get-KaiwuSupportLogsDirectory)
ConvertTo-KaiwuSupportJson $report
