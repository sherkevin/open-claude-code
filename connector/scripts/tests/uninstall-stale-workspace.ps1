param([string]$Source = (Join-Path $PSScriptRoot '../../packaging/windows/uninstall.ps1'))
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$errors)
if ($errors) { throw ($errors | Out-String) }
$definition = $ast.FindAll({
  param($node)
  $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Remove-KaiwuStaleWorkspaces'
}, $true)
if ($definition.Count -ne 1) { throw 'Missing uninstall function Remove-KaiwuStaleWorkspaces' }
. ([scriptblock]::Create($definition[0].Extent.Text))

$Work = Join-Path ([IO.Path]::GetTempPath()) ('kaiwu-stale-workspace-' + [guid]::NewGuid().ToString('N'))
$env:ProgramData = $Work
$Kaiwu = Join-Path $Work 'Kaiwu'
[void][IO.Directory]::CreateDirectory($Kaiwu)
$stale = Join-Path $Kaiwu ('kw-' + ('a' * 32))
$unrelated = Join-Path $Kaiwu 'kw-not-hex'
$data = Join-Path $Kaiwu 'Connector'
foreach ($path in @($stale, $unrelated, $data)) { [void][IO.Directory]::CreateDirectory($path) }
[IO.File]::WriteAllText((Join-Path $stale 'payload.bin'), 'synthetic fixture')
try {
  Remove-KaiwuStaleWorkspaces
  if (Test-Path -LiteralPath $stale) { throw 'Stale setup workspace was not removed' }
  if (-not (Test-Path -LiteralPath $data)) { throw 'Connector data directory must be preserved' }
  if (-not (Test-Path -LiteralPath $unrelated)) { throw 'Directories outside kw-<32 hex> must be preserved' }
  Write-Output 'PASS stale workspace removed, data and unrelated directories preserved'
} finally {
  if (Test-Path -LiteralPath $Work) { Remove-Item -LiteralPath $Work -Recurse -Force }
}
