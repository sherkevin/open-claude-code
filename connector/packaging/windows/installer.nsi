Unicode true

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "TextFunc.nsh"
!include "WinVer.nsh"
!include "x64.nsh"

!ifndef VERSION
  !error "VERSION is required"
!endif
!ifndef VERSION_QUAD
  !error "VERSION_QUAD is required"
!endif
!ifndef COMMIT
  !define COMMIT "unknown"
!endif
!ifndef PAYLOAD_DIR
  !error "PAYLOAD_DIR is required"
!endif
!ifndef OUTPUT_FILE
  !error "OUTPUT_FILE is required"
!endif

; NSIS is a 32-bit process, so $SYSDIR points at SysWOW64. Sysnative is the
; supported way to start 64-bit Windows PowerShell from the 32-bit installer;
; this keeps $env:ProgramFiles and the service path on the native x64 tree.
!define POWERSHELL64 "$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
!define POWERSHELL_SYSTEM32 "$WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"

Var SetupMutex

Name "开物内网连接器 ${VERSION}"
Caption "开物内网连接器 ${VERSION}"
BrandingText "Kaiwu Intranet Connector"
OutFile "${OUTPUT_FILE}"
InstallDir "$PROGRAMFILES64\Kaiwu\Connector"
InstallDirRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector" "InstallLocation"
RequestExecutionLevel admin
CRCCheck on
XPStyle on
SetCompressor /SOLID lzma
SetCompressorDictSize 32
ShowInstDetails show
ShowUninstDetails show
VIProductVersion "${VERSION_QUAD}"
VIAddVersionKey /LANG=2052 "ProductName" "开物内网连接器"
VIAddVersionKey /LANG=2052 "ProductVersion" "${VERSION}"
VIAddVersionKey /LANG=2052 "CompanyName" "Kaiwu"
VIAddVersionKey /LANG=2052 "FileDescription" "开物内网连接器 Windows x64 安装程序"
VIAddVersionKey /LANG=2052 "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=2052 "LegalCopyright" "Kaiwu"
VIAddVersionKey /LANG=2052 "Comments" "source commit ${COMMIT}"

!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\open-config.cmd"
!define MUI_FINISHPAGE_RUN_TEXT "打开本机配置页面"
!define MUI_FINISHPAGE_LINK "查看交付说明"
!define MUI_FINISHPAGE_LINK_LOCATION "$INSTDIR\docs\交付与运维说明.md"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH

!insertmacro MUI_LANGUAGE "SimpChinese"

Function .onInit
  SetRegView 64
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "本安装包仅支持 64 位 Windows。"
    Abort
  ${EndIf}
  ${IfNot} ${AtLeastWin10}
    MessageBox MB_ICONSTOP "本安装包要求 Windows 10 或 Windows Server 2016 及以上版本。"
    Abort
  ${EndIf}
  ${IfNot} ${FileExists} "${POWERSHELL64}"
    MessageBox MB_ICONSTOP "系统缺少 64 位 Windows PowerShell，无法完成安装。"
    Abort
  ${EndIf}
  ; Windows Server 2019 ships Windows PowerShell 5.1. Fail before changing
  ; the machine if the native 64-bit PowerShell is older than 5.1.
  nsExec::ExecToStack '"${POWERSHELL64}" -NoProfile -NonInteractive -Command "if ((Get-Host).Version.Major -gt 5 -or ((Get-Host).Version.Major -eq 5 -and (Get-Host).Version.Minor -ge 1)) { exit 0 }; exit 1"'
  Pop $0
  Pop $1
  ${If} "$0" != "0"
    MessageBox MB_ICONSTOP "64 位 Windows PowerShell 版本过低或无法启动。本安装包要求 Windows PowerShell 5.1 及以上；Windows Server 2019 默认提供 5.1。检测退出码：$0"
    Abort
  ${EndIf}
  InitPluginsDir
FunctionEnd

Function RemoveLegacyX86Install
  ; Older candidate installers could be launched through 32-bit PowerShell and
  ; leave a second copy under Program Files (x86). Delete that copy only when
  ; it contains a known Kaiwu product file; never broad-delete an unrelated
  ; directory that happens to use the same path. ProgramData is preserved.
  ${If} ${FileExists} "$PROGRAMFILES32\Kaiwu\Connector\kaiwu-connector.exe"
  ${OrIf} ${FileExists} "$PROGRAMFILES32\Kaiwu\Connector\uninstall.exe"
  ${OrIf} ${FileExists} "$PROGRAMFILES32\Kaiwu\Connector\install.ps1"
  ${OrIf} ${FileExists} "$PROGRAMFILES32\Kaiwu\Connector\版本信息.txt"
    DetailPrint "清理旧候选版误装的 32 位程序目录..."
    RMDir /r "$PROGRAMFILES32\Kaiwu\Connector"
  ${EndIf}
  ; This removes the parent only when it is empty.
  RMDir "$PROGRAMFILES32\Kaiwu"
FunctionEnd

Function ReleaseSetupTransaction
  System::Call 'kernel32::ReleaseMutex(p $SetupMutex)'
  System::Call 'kernel32::CloseHandle(p $SetupMutex)'
FunctionEnd

Section "安装" SecInstall
  ; uninstall.ps1 acquires this gate before the shared service transaction.
  ; Keep the gate through the NSIS-only writes after install.ps1 has returned.
  System::Call 'kernel32::CreateMutexW(p 0, i 0, w "Global\KaiwuConnector.SetupTransaction") p .r0'
  StrCpy $SetupMutex $0
  ${If} $SetupMutex == 0
    MessageBox MB_ICONSTOP "无法取得安装事务锁。" /SD IDOK
    Abort
  ${EndIf}
  System::Call 'kernel32::WaitForSingleObject(p $SetupMutex, i 0) i .r0'
  ${If} $0 != 0
  ${AndIf} $0 != 128
    System::Call 'kernel32::CloseHandle(p $SetupMutex)'
    MessageBox MB_ICONSTOP "已有开物安装或卸载任务正在运行，或无法确认事务锁。" /SD IDOK
    Abort
  ${EndIf}

  SetShellVarContext all
  SetOutPath "$PLUGINSDIR\payload"
  File /r "${PAYLOAD_DIR}/*"

  CreateDirectory "$APPDATA\Kaiwu\Connector\logs"

  DetailPrint "校验并安装连接器、官方 frpc 和 Windows 服务..."
  Delete "$PLUGINSDIR\install-result.txt"
  Delete "$PLUGINSDIR\install-failure-details.txt"
  Delete "$APPDATA\Kaiwu\Connector\logs\install-failure-summary.txt"
  nsExec::ExecToStack '"${POWERSHELL64}" -NoProfile -ExecutionPolicy Bypass -File "$PLUGINSDIR\payload\install.ps1" -ResultPath "$PLUGINSDIR\install-result.txt" -FailureDetailsPath "$PLUGINSDIR\install-failure-details.txt" 2>&1'
  Pop $0
  Pop $1

  StrCpy $3 "result=UNKNOWN"
  ${If} ${FileExists} "$PLUGINSDIR\install-result.txt"
    FileOpen $4 "$PLUGINSDIR\install-result.txt" r
    FileRead $4 $3
    FileClose $4
    ${TrimNewLines} "$3" "$3"
  ${EndIf}
  StrCpy $5 "result=UNKNOWN"
  ${If} "$3" == "result=INSTALLED"
  ${OrIf} "$3" == "result=PRECHECK_FAILED"
  ${OrIf} "$3" == "result=FAILED_RESTORED"
  ${OrIf} "$3" == "result=FAILED_RECOVERY_UNCONFIRMED"
  ${OrIf} "$3" == "result=FAILED_NO_PRIOR_CLEANED"
  ${OrIf} "$3" == "result=FAILED_NO_PRIOR_CLEANUP_UNCONFIRMED"
    StrCpy $5 "$3"
  ${EndIf}
  StrCpy $3 "$5"

  SetOutPath "$PLUGINSDIR"
  FileOpen $2 "$APPDATA\Kaiwu\Connector\logs\install.log" w
  FileWrite $2 "$3$\r$\n"
  FileWrite $2 "---- install.ps1 output ----$\r$\n"
  FileWrite $2 "$1"
  FileClose $2

  ${If} "$0" == "0"
  ${AndIf} "$3" == "result=INSTALLED"
    CopyFiles /SILENT "$APPDATA\Kaiwu\Connector\logs\install.log" "$INSTDIR\install.log"
  ${Else}
    StrCpy $4 "恢复或清理状态无法确认。"
    ${If} "$3" == "result=PRECHECK_FAILED"
      StrCpy $4 "未开始更改已有安装。"
    ${ElseIf} "$3" == "result=FAILED_RESTORED"
      StrCpy $4 "已核验恢复安装前的程序和配置。业务连通性待确认。"
    ${ElseIf} "$3" == "result=FAILED_RECOVERY_UNCONFIRMED"
      StrCpy $4 "未能确认已恢复安装前的状态。"
    ${ElseIf} "$3" == "result=FAILED_NO_PRIOR_CLEANED"
      StrCpy $4 "已清理本次新增的受管文件。"
    ${ElseIf} "$3" == "result=FAILED_NO_PRIOR_CLEANUP_UNCONFIRMED"
      StrCpy $4 "未能确认已清理本次新增的受管文件。"
    ${EndIf}

    ; Read the two short UTF-16LE lines written by PowerShell, independently of
    ; nsExec stdout. The fallback also covers failure to start PowerShell.
    StrCpy $6 "失败阶段：无法确认"
    StrCpy $7 "原因：无法确认具体原因"
    ClearErrors
    FileOpen $2 "$PLUGINSDIR\install-failure-details.txt" r
    ${IfNot} ${Errors}
      FileReadUTF16LE $2 $8
      FileReadUTF16LE $2 $9
      ${TrimNewLines} "$8" "$8"
      ${TrimNewLines} "$9" "$9"
      ${If} "$8" != ""
      ${AndIf} "$9" != ""
        StrCpy $6 "$8"
        StrCpy $7 "$9"
      ${EndIf}
      FileClose $2
    ${EndIf}
    StrCpy $6 "安装失败$\r$\n$6$\r$\n$7$\r$\n恢复结果：$4$\r$\n状态：$3$\r$\n退出码：$0"

    StrCpy $7 "请将上述摘要转发给技术支持。"
    ClearErrors
    FileOpen $2 "$APPDATA\Kaiwu\Connector\logs\install-failure-summary.txt" w
    ${IfNot} ${Errors}
      FileWriteUTF16LE /BOM $2 "$6$\r$\n"
      ${IfNot} ${Errors}
        StrCpy $7 "可将此文件转发给技术支持：$\r$\n%ProgramData%\Kaiwu\Connector\logs\install-failure-summary.txt"
      ${EndIf}
      FileClose $2
    ${EndIf}
    Call ReleaseSetupTransaction
    MessageBox MB_ICONSTOP "$6$\r$\n$\r$\n$7" /SD IDOK
    Abort
  ${EndIf}

  FileOpen $2 "$INSTDIR\open-config.cmd" w
  FileWrite $2 "@echo off$\r$\nstart $\"Kaiwu Connector$\" $\"http://127.0.0.1:17872/$\"$\r$\n"
  FileClose $2

  CreateDirectory "$SMPROGRAMS\开物内网连接器"
  CreateShortCut "$SMPROGRAMS\开物内网连接器\打开本机配置页面.lnk" "$INSTDIR\open-config.cmd"
  CreateShortCut "$SMPROGRAMS\开物内网连接器\交付与运维说明.lnk" "$INSTDIR\docs\交付与运维说明.md"
  CreateShortCut "$DESKTOP\开物内网连接器配置.lnk" "$INSTDIR\open-config.cmd"
  WriteUninstaller "$INSTDIR\uninstall.exe"
  CreateShortCut "$SMPROGRAMS\开物内网连接器\卸载开物内网连接器.lnk" "$INSTDIR\uninstall.exe"

  ; Remove a stale 32-bit uninstall record left by older candidate installers,
  ; then register the current x64 entry in the native registry view.
  SetRegView 32
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector"
  SetRegView 64
  ; Early x64 probes were accidentally installed through 32-bit PowerShell.
  ; Remove only that known stale product directory after the native install
  ; succeeds; customer configuration remains under ProgramData.
  Call RemoveLegacyX86Install
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector" "DisplayName" "开物内网连接器"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector" "DisplayVersion" "${VERSION}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector" "Publisher" "Kaiwu"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector" "InstallLocation" "$INSTDIR"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector" "DisplayIcon" "$INSTDIR\kaiwu-connector.exe,0"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector" "UninstallString" '"$INSTDIR\uninstall.exe"'
  ; The normal NSIS launcher returns before its temporary uninstaller exits.
  ; Start the same PowerShell transaction directly for callers that need its exit code.
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector" "QuietUninstallString" '"${POWERSHELL_SYSTEM32}" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$INSTDIR\uninstall.ps1"'
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector" "Comments" "source commit ${COMMIT}"
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector" "NoModify" 1
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector" "NoRepair" 1
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector" "EstimatedSize" 26000
  Call ReleaseSetupTransaction
SectionEnd

Section "Uninstall"
  ; NSIS normally runs a temporary copy so the installed launcher can be removed.
  StrCpy $0 "$PROGRAMFILES64\Kaiwu\Connector"
  ${If} "$INSTDIR" == "$0\"
    StrCpy $INSTDIR "$0"
  ${EndIf}
  ${If} "$INSTDIR" != "$0"
  ${OrIf} "$EXEPATH" == "$INSTDIR\uninstall.exe"
    MessageBox MB_ICONSTOP "卸载程序的位置不受支持，请从 Windows 设置或开始菜单启动卸载。" /SD IDOK
    SetErrorLevel 1
    Abort
  ${EndIf}

  StrCpy $0 ""
  ${un.GetParameters} $1
  ${un.GetOptions} "$1" "/PURGE" $2
  ${IfNot} ${Errors}
  ${AndIf} "$2" == ""
    StrCpy $0 "-Purge"
  ${EndIf}
  ${IfNot} ${FileExists} "$INSTDIR\uninstall.ps1"
    MessageBox MB_ICONSTOP "找不到卸载脚本：$INSTDIR\uninstall.ps1" /SD IDOK
    SetErrorLevel 1
    Abort
  ${EndIf}

  SetOutPath "$TEMP"
  DetailPrint "停止服务并卸载连接器..."
  nsExec::ExecToStack '"${POWERSHELL64}" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$INSTDIR\uninstall.ps1" $0'
  Pop $1
  Pop $2
  ClearErrors
  FileOpen $3 "$TEMP\KaiwuConnector-uninstall.log" w
  ${IfNot} ${Errors}
    FileWrite $3 "$2"
    FileClose $3
  ${EndIf}

  ${If} "$1" != "0"
    MessageBox MB_ICONSTOP "卸载失败。$\r$\n$\r$\n退出码：$1$\r$\n日志：$TEMP\KaiwuConnector-uninstall.log" /SD IDOK
    SetErrorLevel 1
    Abort
  ${EndIf}
SectionEnd
