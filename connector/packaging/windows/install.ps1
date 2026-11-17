# 开物内网连接器 Windows 安装脚本。
# 用法：以管理员身份运行 PowerShell：.\install.ps1
#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"

$Source = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallDir = Join-Path $env:ProgramFiles "Kaiwu\Connector"
$DataDir = Join-Path $env:ProgramData "Kaiwu\Connector"
$ServiceName = "KaiwuConnector"
$Listen = "127.0.0.1:17872"

function Wait-ServiceAbsent {
  param([int]$TimeoutSeconds = 15)
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  do {
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($null -eq $service) { return }
    Start-Sleep -Milliseconds 500
  } while ((Get-Date) -lt $deadline)
  throw "服务 $ServiceName 未能及时删除"
}

# 先停止旧服务，避免覆盖正在运行的二进制。
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
  Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
  sc.exe delete $ServiceName | Out-Null
  Wait-ServiceAbsent
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir "docs") | Out-Null
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

Copy-Item (Join-Path $Source "bin\kaiwu-connector.exe") $InstallDir -Force
Copy-Item (Join-Path $Source "bin\frpc.exe") $InstallDir -Force
Get-ChildItem (Join-Path $Source "docs\*.md") -ErrorAction SilentlyContinue |
  Copy-Item -Destination (Join-Path $InstallDir "docs") -Force

# 数据目录只允许 SYSTEM 和 Administrators 访问。服务以 LocalSystem 运行。
icacls $DataDir /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" /t | Out-Null

# 写入云端地址模板。失败时立即停止，避免安装出一个导出不可用的连接器。
$EnvFile = Join-Path $Source "config\connector.env.example"
if (Test-Path $EnvFile) {
  foreach ($line in Get-Content $EnvFile) {
    if ($line -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.+?)\s*$') {
      $name = $matches[1]
      $value = $matches[2]
      [Environment]::SetEnvironmentVariable($name, $value, "Machine")
      Set-Item -Path ("Env:" + $name) -Value $value
      $check = [Environment]::GetEnvironmentVariable($name, "Machine")
      if ($check -ne $value) { throw "写入机器级环境变量失败: $name" }
      Write-Host "已设置环境变量 $name"
    }
  }
}

$Binary = Join-Path $InstallDir "kaiwu-connector.exe"
$BinPath = "`"$Binary`" serve --data-dir `"$DataDir`" --listen $Listen"

# 使用 New-Service 避免 sc.exe 在 Windows PowerShell 5.1 下拆分带空格的 binPath。
try {
  New-Service -Name $ServiceName -BinaryPathName $BinPath -StartupType Automatic -DisplayName "Kaiwu Intranet Connector" -ErrorAction Stop | Out-Null
} catch {
  throw "注册服务失败: $($_.Exception.Message)"
}

sc.exe sidtype $ServiceName unrestricted | Out-Null
sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/15000/restart/60000 | Out-Null

try {
  Start-Service -Name $ServiceName -ErrorAction Stop
} catch {
  $events = Get-WinEvent -FilterHashtable @{LogName='System'; StartTime=(Get-Date).AddMinutes(-5)} -ErrorAction SilentlyContinue |
    Where-Object { $_.Message -match 'Kaiwu|1053|7000|7009' } |
    Select-Object -First 5 |
    Format-List | Out-String
  throw "启动服务失败: $($_.Exception.Message)`n$events"
}

$deadline = (Get-Date).AddSeconds(30)
do {
  Start-Sleep -Milliseconds 500
  $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
} while (($null -eq $service -or $service.Status -ne "Running") -and (Get-Date) -lt $deadline)

if ($null -eq $service -or $service.Status -ne "Running") {
  throw "服务 $ServiceName 未在 30 秒内进入 Running 状态"
}

Write-Host ""
Write-Host "安装完成。"
Write-Host "服务名称：$ServiceName（$($service.Status)）"
Write-Host "本机配置界面：http://$Listen/"
Write-Host "数据目录：$DataDir"
Write-Host "注意：界面只监听本机回环地址，不要通过防火墙对外开放。"
