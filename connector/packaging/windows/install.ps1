# 开物内网连接器 Windows 安装脚本。
# 用法：以管理员身份运行 PowerShell：.\install.ps1
#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"

$Source = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallDir = Join-Path $env:ProgramFiles "Kaiwu\Connector"
$DataDir = Join-Path $env:ProgramData "Kaiwu\Connector"
$ServiceName = "KaiwuConnector"
$Listen = "127.0.0.1:17872"

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir "docs") | Out-Null
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

Copy-Item (Join-Path $Source "bin\kaiwu-connector.exe") $InstallDir -Force
Copy-Item (Join-Path $Source "bin\frpc.exe") $InstallDir -Force
Get-ChildItem (Join-Path $Source "docs\*.md") -ErrorAction SilentlyContinue | Copy-Item -Destination (Join-Path $InstallDir "docs") -Force

$EnvFile = Join-Path $Source "config\connector.env.example"
if (Test-Path $EnvFile) {
  foreach ($line in Get-Content $EnvFile) {
    if ($line -match '^\s*([A-Z_]+)\s*=\s*(.+)$') {
      [Environment]::SetEnvironmentVariable($matches[1], $matches[2].Trim(), "Machine")
      Write-Host "已设置环境变量 $($matches[1])"
    }
  }
}

$Binary = Join-Path $InstallDir "kaiwu-connector.exe"
$Arguments = "serve --data-dir `"$DataDir`" --listen $Listen"

if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
  Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
  sc.exe delete $ServiceName | Out-Null
  Start-Sleep -Seconds 2
}

$result = sc.exe create $ServiceName binPath= "`"$Binary`" $Arguments" start= auto DisplayName= "Kaiwu Intranet Connector"
if ($LASTEXITCODE -ne 0) { throw "注册服务失败：$result" }

# 允许服务在无交互登录时访问网络和本机秘密目录。
sc.exe sidtype $ServiceName unrestricted | Out-Null
sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/15000/restart/60000 | Out-Null

Start-Service -Name $ServiceName
Write-Host ""
Write-Host "安装完成。"
Write-Host "本机配置界面：http://$Listen/"
Write-Host "查看状态：Get-Service $ServiceName"
Write-Host "注意：界面只监听本机回环地址，不要通过防火墙对外开放。"
