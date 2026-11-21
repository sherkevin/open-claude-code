param(
    [string]$Installer = (Join-Path $PSScriptRoot '../../packaging/windows/install.ps1')
)
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$sourceAst = [System.Management.Automation.Language.Parser]::ParseFile($Installer, [ref]$tokens, [ref]$errors)
if ($errors) { throw ($errors | Out-String) }
$functionNames = @('Get-FileSha256', 'Get-PackageCheckErrorCode', 'Assert-PackageSourceFile', 'Set-InstallFailure', 'Write-InstallFailureDetails')
$definitions = $sourceAst.FindAll({ param($ast) $ast -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $ast.Name -in $functionNames }, $false)
if ($definitions.Count -ne $functionNames.Count) { throw 'Missing installer function' }
foreach ($definition in $definitions) { . ([scriptblock]::Create($definition.Extent.Text)) }

$Work = Join-Path ([IO.Path]::GetTempPath()) ('kaiwu-summary-test-' + [guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($Work)
try {
    $FailureDetailsPath = Join-Path $Work 'summary.txt'
    $FailureDetailsCodes = $false
    $CriticalFiles = @([pscustomobject]@{SourceRelative = 'bin/frpc.exe'; TargetName = 'frpc.exe'})
    $file = $CriticalFiles[0]
    $inputPath = Join-Path $Work 'synthetic-payload.txt'
    [IO.File]::WriteAllText($inputPath, 'synthetic fixture, not executable')
    $expected = Get-FileSha256 $inputPath
    [IO.File]::Delete($inputPath)

    foreach ($case in @('first', 'later', 'checksum')) {
        $PackageChecks = [ordered]@{}
        $Stage = 'source_precheck'
        if ($case -eq 'later') {
            [IO.File]::WriteAllText($inputPath, 'synthetic fixture, not executable')
            Assert-PackageSourceFile $file $inputPath $expected
            [IO.File]::Delete($inputPath)
            $Stage = 'pre_change_verification'
        } elseif ($case -eq 'checksum') {
            [IO.File]::WriteAllText($inputPath, 'synthetic changed fixture')
        }
        $FailureStage = $Stage
        $FailureReason = 'unknown'
        $FailureFile = $null
        $rejected = $false
        try { Assert-PackageSourceFile $file $inputPath $expected } catch { $rejected = $true }
        if (-not $rejected) { throw "$case incorrectly accepted the fixture" }
        $expectedReason = switch ($case) { first { 'file_missing' } later { 'file_missing_after_check' } checksum { 'checksum_mismatch' } }
        $expectedCheck = if ($case -eq 'checksum') { "$Stage|bin/frpc.exe|present|checksum_mismatch" } else { "$Stage|bin/frpc.exe|missing|file_missing" }
        if ($FailureReason -cne $expectedReason -or $FailureFile -cne 'frpc.exe' -or
            $PackageChecks["$Stage|bin/frpc.exe"] -cne $expectedCheck) { throw "$case incorrect diagnostic" }
        if ($case -eq 'later' -and $PackageChecks['source_precheck|bin/frpc.exe'] -cne 'source_precheck|bin/frpc.exe|present|ok') {
            throw 'Later failure lost the first check'
        }
        Write-InstallFailureDetails
        $summary = [IO.File]::ReadAllText($FailureDetailsPath, [Text.Encoding]::Unicode)
        if (-not $summary.Contains('frpc.exe') -or ($summary.Contains('终端防护处置记录') -ne ($case -ne 'checksum'))) {
            throw "$case incorrect support text"
        }
        [IO.File]::Delete($inputPath)
        Write-Output "PASS $case $expectedReason"
    }
} finally {
    [IO.Directory]::Delete($Work, $true)
}
