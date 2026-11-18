# 开物内网连接器 Windows 安装脚本。
# 用法：以管理员身份运行 PowerShell：.\install.ps1
#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"

$Source = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallDir = Join-Path $env:ProgramFiles "Kaiwu\Connector"
$DataDir = Join-Path $env:ProgramData "Kaiwu\Connector"
$ServiceName = "KaiwuConnector"
$Listen = "127.0.0.1:17872"

$ChecksumFile = Join-Path $Source "SHA256SUMS"
if (-not (Test-Path $ChecksumFile)) {
  throw "安装包缺少 SHA256SUMS，拒绝安装"
}
foreach ($line in [System.IO.File]::ReadAllLines($ChecksumFile, [System.Text.Encoding]::UTF8)) {
  if ($line -notmatch '^([0-9a-fA-F]{64})\s+(.+)$') { continue }
  $expected = $matches[1].ToLowerInvariant()
  $relative = $matches[2].TrimStart('.', '\', '/')
  $target = Join-Path $Source $relative
  if (-not (Test-Path $target)) { throw "安装包缺少文件: $relative" }
  $actual = (Get-FileHash -Path $target -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($actual -ne $expected) { throw "安装包文件校验失败: $relative" }
}

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

function Set-PrivateDataAcl {
  param([Parameter(Mandatory = $true)][string]$Path)

  & icacls.exe $Path /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" /T /C | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "设置数据目录 ACL 失败，icacls 退出码为 $LASTEXITCODE: $Path"
  }

  $systemSid = [System.Security.Principal.SecurityIdentifier]::new("S-1-5-18")
  $adminSid = [System.Security.Principal.SecurityIdentifier]::new("S-1-5-32-544")
  $items = @(Get-Item -LiteralPath $Path -Force) + @(Get-ChildItem -LiteralPath $Path -Force -Recurse -ErrorAction Stop)
  foreach ($item in $items) {
    $acl = Get-Acl -LiteralPath $item.FullName
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) {
      $acl.RemoveAccessRuleSpecific($rule) | Out-Null
    }
    if ($item.PSIsContainer) {
      $inheritance = (
        [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
      )
    } else {
      $inheritance = [System.Security.AccessControl.InheritanceFlags]::None
    }
    foreach ($sid in @($systemSid, $adminSid)) {
      $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
        $sid,
        [System.Security.AccessControl.FileSystemRights]::FullControl,
        $inheritance,
        [System.Security.AccessControl.PropagationFlags]::None,
        [System.Security.AccessControl.AccessControlType]::Allow
      )
      $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $item.FullName -AclObject $acl
  }

  $verified = Get-Acl -LiteralPath $Path
  if (-not $verified.AreAccessRulesProtected) {
    throw "数据目录 ACL 仍会继承上级权限: $Path"
  }
  foreach ($rule in @($verified.Access)) {
    $sid = $rule.IdentityReference.Translate(
      [System.Security.Principal.SecurityIdentifier]
    ).Value
    if ($sid -notin @("S-1-5-18", "S-1-5-32-544")) {
      throw "数据目录 ACL 含有未授权主体 $sid: $Path"
    }
  }
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
New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir "compliance") | Out-Null
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

Set-PrivateDataAcl -Path $DataDir

# 升级前保留身份、通道材料和认证配置。备份仍在受 ACL 保护的数据目录内。
$SensitiveFiles = @(
  "identity.json",
  "device_key.json",
  "secrets.json",
  "auth_profiles.json",
  "config.json",
  "frpc.toml",
  "state.json",
  "revoked.json"
)
$ExistingSensitive = @($SensitiveFiles | ForEach-Object { Join-Path $DataDir $_ } | Where-Object { Test-Path $_ })
if ($ExistingSensitive.Count -gt 0) {
  $BackupDir = Join-Path $DataDir ("backups\" + (Get-Date -Format "yyyyMMdd-HHmmss"))
  New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
  foreach ($sourceFile in $ExistingSensitive) {
    Copy-Item -LiteralPath $sourceFile -Destination $BackupDir -Force
  }
  [System.IO.File]::WriteAllText(
    (Join-Path $BackupDir "backup-info.txt"),
    "created_at=$((Get-Date).ToUniversalTime().ToString("o"))`r`nprevious_install=$InstallDir`r`n",
    [System.Text.Encoding]::UTF8
  )
  Set-PrivateDataAcl -Path $BackupDir
  Write-Host "升级前本机秘密与配置已备份到 $BackupDir"
}

Copy-Item (Join-Path $Source "bin\kaiwu-connector.exe") $InstallDir -Force
Copy-Item (Join-Path $Source "bin\frpc.exe") $InstallDir -Force
Get-ChildItem (Join-Path $Source "docs\*.md") -ErrorAction SilentlyContinue |
  Copy-Item -Destination (Join-Path $InstallDir "docs") -Force
Get-ChildItem (Join-Path $Source "compliance\*") -File -ErrorAction SilentlyContinue |
  Copy-Item -Destination (Join-Path $InstallDir "compliance") -Force

# 写入云端地址模板。失败时立即停止，避免安装出一个导出不可用的连接器。
$EnvFile = Join-Path $Source "config\connector.env.example"
if (Test-Path $EnvFile) {
  $envText = [System.IO.File]::ReadAllText($EnvFile, [System.Text.Encoding]::UTF8)
  foreach ($line in ($envText -split "`r?`n")) {
    if ($line -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$') {
      $name = $matches[1]
      $value = $matches[2].Trim()
      if ([string]::IsNullOrWhiteSpace($value)) {
        [Environment]::SetEnvironmentVariable($name, $null, "Machine")
        Remove-Item -Path ("Env:" + $name) -ErrorAction SilentlyContinue
        Write-Host "已清空未配置的环境变量 $name"
      } else {
        [Environment]::SetEnvironmentVariable($name, $value, "Machine")
        Set-Item -Path ("Env:" + $name) -Value $value
        $check = [Environment]::GetEnvironmentVariable($name, "Machine")
        if ($check -ne $value) { throw "写入机器级环境变量失败: $name" }
        Write-Host "已设置环境变量 $name"
      }
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
