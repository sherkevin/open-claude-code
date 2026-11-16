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
  Start-Sleep -Seconds 2
}

if (Test-Path $InstallDir) {
  Remove-Item $InstallDir -Recurse -Force
}

foreach ($name in @("KAIWU_CLOUD_BASE", "KAIWU_EDGE_BASE", "KAIWU_CONNECTOR_DATA")) {
  [Environment]::SetEnvironmentVariable($name, $null, "Machine")
}

if ($Purge -and (Test-Path $DataDir)) {
  Remove-Item $DataDir -Recurse -Force
  Write-Host "已删除程序、配置与本机身份。"
} else {
  Write-Host "已删除程序与服务；配置和本机身份保留在 $DataDir。"
  Write-Host "如需彻底清除，请运行：.\uninstall.ps1 -Purge"
}
