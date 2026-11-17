# 开物内网连接器 Windows 卸载脚本。
# 用法：以管理员身份运行 PowerShell：.\uninstall.ps1 [-Purge]
#   默认保留本机身份与配置；-Purge 彻底删除。
param([switch]$Purge)
#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"

$InstallDir = Join-Path $env:ProgramFiles "Kaiwu\Connector"
$DataDir = Join-Path $env:ProgramData "Kaiwu\Connector"
$ServiceName = "KaiwuConnector"

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($service) {
  Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
  sc.exe delete $ServiceName | Out-Null
  $deadline = (Get-Date).AddSeconds(15)
  do {
    Start-Sleep -Milliseconds 500
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
  } while ($null -ne $service -and (Get-Date) -lt $deadline)
  if ($null -ne $service) { throw "服务 $ServiceName 未能及时删除" }
}

if (Test-Path $InstallDir) {
  Remove-Item -LiteralPath $InstallDir -Recurse -Force -Confirm:$false
}

foreach ($name in @("KAIWU_CLOUD_BASE", "KAIWU_EDGE_BASE", "KAIWU_CONNECTOR_DATA")) {
  [Environment]::SetEnvironmentVariable($name, $null, "Machine")
  Remove-Item -Path ("Env:" + $name) -ErrorAction SilentlyContinue
}

if ($Purge) {
  if (Test-Path $DataDir) {
    Write-Host "删除数据目录：$DataDir"
    Remove-Item -LiteralPath $DataDir -Recurse -Force -Confirm:$false
  }
  if (Test-Path $DataDir) {
    throw "数据目录仍然存在: $DataDir"
  }
  Write-Host "已删除程序、配置与本机身份。"
} else {
  Write-Host "已删除程序与服务；配置和本机身份保留在 $DataDir。"
  Write-Host "如需彻底清除，请运行：.\uninstall.ps1 -Purge"
}
