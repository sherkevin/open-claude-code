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
!ifndef JOURNAL_PLUGIN_DIR
  !error "JOURNAL_PLUGIN_DIR is required"
!endif
!addplugindir /x86-unicode "${JOURNAL_PLUGIN_DIR}"
!ifndef OUTPUT_FILE
  !error "OUTPUT_FILE is required"
!endif

; NSIS is a 32-bit process, so $SYSDIR points at SysWOW64. Sysnative is the
; supported way to start 64-bit Windows PowerShell from the 32-bit installer;
; this keeps $env:ProgramFiles and the service path on the native x64 tree.
!define POWERSHELL64 "$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
!define POWERSHELL_SYSTEM32 "$WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"

Var SetupMutex
Var AttemptDone
Var AttemptStage
Var AttemptCode
Var FailureStage
Var FailureReason
Var FailureFile
Var FailureRecovery
Var WorkerExit
Var WorkDir

!macro Attempt stage outcome code
  KaiwuJournal::Record /NOUNLOAD "${stage}" "${outcome}" "${code}"
!macroend

Name "开物内网连接器 ${VERSION}"
Caption "开物内网连接器 ${VERSION}"
BrandingText "Kaiwu Intranet Connector"
OutFile "${OUTPUT_FILE}"
InstallDir "$PROGRAMFILES64\Kaiwu\Connector"
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
!define MUI_CUSTOMFUNCTION_ABORT OnSetupCancel
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

Function ShowAttemptFailure
  ${If} $FailureStage == ""
    StrCpy $FailureStage "$AttemptStage"
  ${EndIf}
  KaiwuJournal::ExplainFailure /NOUNLOAD "$FailureStage" "$FailureReason" "$FailureFile" "$FailureRecovery"
  Pop $6
  KaiwuJournal::Close
  ${If} $WorkerExit != ""
    ; The worker launcher returns a numeric exit code or the literal "error".
    StrCpy $6 "$6$\r$\n状态：$FailureRecovery$\r$\n退出码：$WorkerExit"
  ${EndIf}
  MessageBox MB_ICONSTOP "$6" /SD IDOK
FunctionEnd

Function CompleteAttempt
  ${If} $AttemptDone == 1
    Return
  ${EndIf}
  StrCpy $AttemptDone 1
  ${If} $WorkDir != ""
    SetOutPath "$WINDIR"
    KaiwuJournal::ReleaseWorkspace /NOUNLOAD
    ClearErrors
    RMDir /r "$WorkDir"
    ${If} ${Errors}
      !insertmacro Attempt "extract" "failed" "write_failed"
    ${EndIf}
  ${EndIf}
  KaiwuJournal::Finish /NOUNLOAD "$AttemptCode"
  ${If} $AttemptCode != "ok"
    SetErrorLevel 1
  ${EndIf}
FunctionEnd

Function .onInstFailed
  ${If} $AttemptDone != 1
    StrCpy $AttemptCode "unknown"
    ${If} $AttemptStage == "extract"
      StrCpy $AttemptCode "write_failed"
    ${EndIf}
    !insertmacro Attempt "$AttemptStage" "failed" "$AttemptCode"
    Call CompleteAttempt
    ${If} $SetupMutex != 0
      Call ReleaseSetupTransaction
    ${EndIf}
    StrCpy $FailureReason "interrupted"
    Call ShowAttemptFailure
  ${EndIf}
FunctionEnd

Function OnSetupCancel
  ${If} $AttemptDone != 1
    StrCpy $AttemptCode "unknown"
    Call CompleteAttempt
    KaiwuJournal::Close
  ${EndIf}
FunctionEnd

Function .onInit
  KaiwuJournal::Begin /NOUNLOAD
  !insertmacro Attempt "precheck" "started" "ok"
  StrCpy $AttemptStage "precheck"
  StrCpy $AttemptCode "unknown"
  SetRegView 64
  StrCpy $INSTDIR "$PROGRAMFILES64\Kaiwu\Connector"
  ${IfNot} ${RunningX64}
    StrCpy $FailureReason "host_x64"
    Goto unsupported_host
  ${EndIf}
  ${IfNot} ${AtLeastWin10}
    StrCpy $FailureReason "host_version"
    Goto unsupported_host
  ${EndIf}
  ${IfNot} ${FileExists} "${POWERSHELL64}"
    StrCpy $FailureReason "host_powershell_missing"
    Goto unsupported_host
  ${EndIf}
  ; Check the actual native PowerShell process before touching the product.
  nsExec::Exec '"${POWERSHELL64}" -NoProfile -NonInteractive -Command "if ((Get-Host).Version.Major -gt 5 -or ((Get-Host).Version.Major -eq 5 -and (Get-Host).Version.Minor -ge 1)) { exit 0 }; exit 1"'
  Pop $0
  ${If} "$0" != "0"
    StrCpy $FailureReason "host_powershell_version"
    Goto unsupported_host
  ${EndIf}
  !insertmacro Attempt "precheck" "ok" "ok"
  StrCpy $AttemptStage "lock"
  SetShellVarContext all
  Return

  unsupported_host:
    StrCpy $AttemptCode "unsupported_host"
    StrCpy $FailureRecovery "not_started"
    !insertmacro Attempt "precheck" "failed" "unsupported_host"
    Call CompleteAttempt
    Call ShowAttemptFailure
    Abort
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
  StrCpy $SetupMutex 0
FunctionEnd

Section "安装" SecInstall
  ; uninstall.ps1 acquires this gate before the shared service transaction.
  ; Keep it through the NSIS-only maintenance writes.
  StrCpy $FailureRecovery "not_started"
  !insertmacro Attempt "lock" "started" "ok"
  System::Call 'kernel32::CreateMutexW(p 0, i 0, w "Global\KaiwuConnector.SetupTransaction") p .r0 ?e'
  Pop $1
  StrCpy $SetupMutex $0
  ${If} $SetupMutex == 0
    StrCpy $AttemptCode "unknown"
    ${If} $1 == 5
      StrCpy $AttemptCode "access_denied"
    ${EndIf}
    !insertmacro Attempt "lock" "failed" "$AttemptCode"
    StrCpy $FailureReason "lock_unavailable"
    Goto attempt_failed
  ${EndIf}
  System::Call 'kernel32::WaitForSingleObject(p $SetupMutex, i 0) i .r0'
  ${If} $0 != 0
  ${AndIf} $0 != 128
    System::Call 'kernel32::CloseHandle(p $SetupMutex)'
    StrCpy $SetupMutex 0
    StrCpy $AttemptCode "unknown"
    StrCpy $FailureReason "lock_unavailable"
    ${If} $0 == 258
      StrCpy $AttemptCode "busy"
      StrCpy $FailureReason "lock_busy"
    ${EndIf}
    !insertmacro Attempt "lock" "failed" "$AttemptCode"
    Goto attempt_failed
  ${EndIf}
  !insertmacro Attempt "lock" "ok" "ok"

  StrCpy $AttemptStage "extract"
  !insertmacro Attempt "extract" "started" "ok"
  KaiwuJournal::Workspace /NOUNLOAD
  Pop $WorkDir
  ${If} $WorkDir == ""
    ; NSIS creates the elevated plugin directory with restricted permissions.
    InitPluginsDir
    ${If} $PLUGINSDIR != ""
      StrLen $1 "$PLUGINSDIR"
      ${If} $1 < 164
        StrCpy $WorkDir "$PLUGINSDIR\p"
      ${EndIf}
    ${EndIf}
  ${EndIf}
  ${If} $WorkDir == ""
    StrCpy $AttemptCode "access_denied"
    !insertmacro Attempt "extract" "failed" "access_denied"
    StrCpy $FailureReason "workspace_unavailable"
    Goto attempt_failed
  ${EndIf}
  ClearErrors
  SetOutPath "$WorkDir"
  File /r "${PAYLOAD_DIR}/*"
  ${If} ${Errors}
    StrCpy $AttemptCode "write_failed"
    !insertmacro Attempt "extract" "failed" "write_failed"
    StrCpy $FailureReason "extract_write_failed"
    Goto attempt_failed
  ${EndIf}
  ${IfNot} ${FileExists} "$WorkDir\install.ps1"
    StrCpy $AttemptCode "file_missing"
    !insertmacro Attempt "extract" "failed" "file_missing"
    StrCpy $FailureReason "install_script_missing"
    Goto attempt_failed
  ${EndIf}
  !insertmacro Attempt "extract" "ok" "ok"

  DetailPrint "校验并安装连接器、官方 frpc 和 Windows 服务..."
  StrCpy $AttemptStage "precheck"
  StrCpy $FailureRecovery ""
  SetOutPath "$WorkDir"
  ; The launcher passes only an append-only journal handle to the worker.
  KaiwuJournal::RunInstaller /NOUNLOAD "${POWERSHELL64}" "$WorkDir"
  Pop $0

  StrCpy $3 "result=UNKNOWN"
  ClearErrors
  FileOpen $4 "$WorkDir\result.txt" r
  ${IfNot} ${Errors}
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

  ${If} "$0" != "0"
  ${OrIf} "$3" != "result=INSTALLED"
    StrCpy $AttemptCode "unknown"
    ${If} "$3" == "result=UNKNOWN"
      ${If} "$0" == "0"
        StrCpy $AttemptCode "write_failed"
        !insertmacro Attempt "verify" "failed" "write_failed"
      ${Else}
        !insertmacro Attempt "precheck" "failed" "unknown"
      ${EndIf}
    ${EndIf}
    StrCpy $FailureRecovery "$3"
    StrCpy $WorkerExit "$0"
    StrCpy $FailureStage "unknown"
    StrCpy $FailureReason "result_unavailable"
    ; The temporary worker output contains codes only. The native module
    ; accepts exact known values and renders the summary and popup together.
    ClearErrors
    FileOpen $2 "$WorkDir\failure.txt" r
    ${IfNot} ${Errors}
      FileReadUTF16LE $2 $FailureStage
      FileReadUTF16LE $2 $FailureReason
      FileReadUTF16LE $2 $FailureFile
      FileClose $2
      ${TrimNewLines} "$FailureStage" "$FailureStage"
      ${TrimNewLines} "$FailureReason" "$FailureReason"
      ${TrimNewLines} "$FailureFile" "$FailureFile"
    ${EndIf}

    Goto attempt_failed
  ${EndIf}

  StrCpy $AttemptStage "verify"
  !insertmacro Attempt "verify" "started" "ok"
  KaiwuJournal::GuardProgramWrites /NOUNLOAD "$INSTDIR"
  Pop $1
  ${If} $1 != 1
    Goto maintenance_failed
  ${EndIf}
  ClearErrors
  FileOpen $2 "$INSTDIR\open-config.cmd" w
  IfErrors maintenance_failed
  FileWrite $2 "@echo off$\r$\nstart $\"Kaiwu Connector$\" $\"http://127.0.0.1:17872/$\"$\r$\n"
  FileClose $2
  IfErrors maintenance_failed

  CreateDirectory "$SMPROGRAMS\开物内网连接器"
  CreateShortCut "$SMPROGRAMS\开物内网连接器\打开本机配置页面.lnk" "$INSTDIR\open-config.cmd"
  CreateShortCut "$SMPROGRAMS\开物内网连接器\交付与运维说明.lnk" "$INSTDIR\docs\交付与运维说明.md"
  CreateShortCut "$DESKTOP\开物内网连接器配置.lnk" "$INSTDIR\open-config.cmd"
  WriteUninstaller "$INSTDIR\uninstall.exe"
  CreateShortCut "$SMPROGRAMS\开物内网连接器\卸载开物内网连接器.lnk" "$INSTDIR\uninstall.exe"

  ; Remove a stale 32-bit uninstall record left by older candidate installers,
  ; then register the current x64 entry in the native registry view.
  IfErrors maintenance_failed
  SetRegView 32
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\KaiwuConnector"
  SetRegView 64
  ClearErrors ; The legacy registry entry need not exist.
  ; Early x64 probes were accidentally installed through 32-bit PowerShell.
  ; Remove only that known stale product directory after the native install
  ; succeeds; customer configuration remains under ProgramData.
  Call RemoveLegacyX86Install
  ClearErrors ; An absent historical x86 directory is expected on a clean host.
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
  IfErrors maintenance_failed
  !insertmacro Attempt "verify" "ok" "ok"
  StrCpy $AttemptCode "ok"
  Call CompleteAttempt
  KaiwuJournal::Close
  Call ReleaseSetupTransaction
  Goto attempt_end

  maintenance_failed:
    StrCpy $AttemptCode "write_failed"
    !insertmacro Attempt "verify" "failed" "write_failed"
    StrCpy $FailureReason "maintenance_write_failed"
    StrCpy $FailureRecovery "maintenance_failed"

  attempt_failed:
    Call CompleteAttempt
    ${If} $SetupMutex != 0
      Call ReleaseSetupTransaction
    ${EndIf}
    Call ShowAttemptFailure
    Abort

  attempt_end:
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
