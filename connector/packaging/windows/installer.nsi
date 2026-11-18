Unicode true

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "TextFunc.nsh"
!include "WinVer.nsh"
!include "x64.nsh"
!include "StrFunc.nsh"

${Using:StrFunc} UnStrStr

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
  InitPluginsDir
FunctionEnd

Section "安装" SecInstall
  SetShellVarContext all
  SetOutPath "$PLUGINSDIR\payload"
  File /r "${PAYLOAD_DIR}/*"

  CreateDirectory "$INSTDIR"
  CreateDirectory "$APPDATA\Kaiwu\Connector\logs"

  DetailPrint "校验并安装连接器、官方 frpc 和 Windows 服务..."
  nsExec::ExecToStack '"${POWERSHELL64}" -NoProfile -ExecutionPolicy Bypass -File "$PLUGINSDIR\payload\install.ps1" 2>&1'
  Pop $0
  Pop $1

  SetOutPath "$INSTDIR"
  FileOpen $2 "$INSTDIR\install.log" w
  FileWrite $2 "$1"
  FileClose $2
  CopyFiles /SILENT "$INSTDIR\install.log" "$APPDATA\Kaiwu\Connector\logs\install.log"

  ${If} "$0" != "0"
    MessageBox MB_ICONSTOP "安装失败，安装脚本已尝试恢复安装前状态。$\r$\n$\r$\n退出码：$0$\r$\n日志：$APPDATA\Kaiwu\Connector\logs\install.log"
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
  RMDir /r "$PROGRAMFILES32\Kaiwu\Connector"
  RMDir "$PROGRAMFILES32\Kaiwu"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector" "DisplayName" "开物内网连接器"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector" "DisplayVersion" "${VERSION}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector" "Publisher" "Kaiwu"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector" "InstallLocation" "$INSTDIR"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector" "DisplayIcon" "$INSTDIR\kaiwu-connector.exe,0"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector" "Comments" "source commit ${COMMIT}"
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector" "NoModify" 1
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector" "NoRepair" 1
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector" "EstimatedSize" 26000
SectionEnd

Section "Uninstall"
  SetShellVarContext all
  SetRegView 64
  StrCpy $0 ""
  ${UnStrStr} $1 "$CMDLINE" "/PURGE"
  ${If} $1 != ""
    StrCpy $0 "-Purge -KeepProgramDirectory"
  ${Else}
    StrCpy $0 "-KeepProgramDirectory"
  ${EndIf}

  ${IfNot} ${FileExists} "$INSTDIR\uninstall.ps1"
    MessageBox MB_ICONSTOP "找不到卸载脚本：$INSTDIR\uninstall.ps1"
    Abort
  ${EndIf}

  DetailPrint "停止服务并卸载连接器..."
  nsExec::ExecToStack '"${POWERSHELL64}" -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\uninstall.ps1" $0'
  Pop $1
  Pop $2
  FileOpen $3 "$TEMP\KaiwuConnector-uninstall.log" w
  FileWrite $3 "$2"
  FileClose $3

  ${If} "$1" != "0"
    MessageBox MB_ICONSTOP "卸载失败。$\r$\n$\r$\n退出码：$1$\r$\n日志：$TEMP\KaiwuConnector-uninstall.log"
    Abort
  ${EndIf}

  Delete "$DESKTOP\开物内网连接器配置.lnk"
  RMDir /r "$SMPROGRAMS\开物内网连接器"
  RMDir /r "$INSTDIR"
  RMDir /r "$PROGRAMFILES32\Kaiwu\Connector"
  RMDir "$PROGRAMFILES32\Kaiwu"
  SetRegView 32
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector"
  SetRegView 64
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector"
SectionEnd
