; Hooks for the Tauri NSIS installer (bundle.windows.nsis.installerHooks in tauri.conf.json).
;
; Before installing, remove the earlier Inno Setup build of Wednesday (the pywebview app, up
; to 0.3.0) so only one Wednesday is installed. Its uninstaller keeps %LOCALAPPDATA%\Wednesday
; and the secrets in Credential Manager, which the Tauri app goes on using.

!define WED_INNO_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\{A6AF6DF9-A869-4069-B4AF-1F9F3978D315}_is1"

!macro NSIS_HOOK_PREINSTALL
  Push $R0
  Push $R1
  ReadRegStr $R0 HKCU "${WED_INNO_KEY}" "UninstallString"
  ${If} $R0 != ""
    ReadRegStr $R1 HKCU "${WED_INNO_KEY}" "InstallLocation"
    DetailPrint "Removing the earlier Wednesday in $R1 (your data and settings stay)"
    ; Close it first: files in use can't be removed.
    nsExec::Exec `powershell -NoProfile -NonInteractive -Command "Get-Process Wednesday -ErrorAction SilentlyContinue | Where-Object { $$_.Path -like '$R1*' } | Stop-Process -Force"`
    Pop $R1 ; nsExec's exit code, not needed
    ExecWait '$R0 /VERYSILENT /SUPPRESSMSGBOXES /NORESTART'
  ${EndIf}
  Pop $R1
  Pop $R0
!macroend

; A fresh start: Wednesday's data folder (%LOCALAPPDATA%\Wednesday: settings, database, journals,
; logs) and the secrets it saved in Credential Manager go. `Wednesday.exe --forget-secrets` removes
; the secrets (generic credentials `Wednesday`, `<name>@Wednesday`, and the old `Wednesday MT5`)
; and exits without opening a window.
!macro WED_FRESH_START
  DetailPrint "Deleting Wednesday's data and saved secrets"
  ExecWait '"$INSTDIR\${MAINBINARYNAME}.exe" --forget-secrets'
  RMDir /r "$LOCALAPPDATA\Wednesday"
!macroend

; Installer: `WednesdaySetup.exe /FRESH=1` (silent installs too) starts from scratch.
!macro NSIS_HOOK_POSTINSTALL
  Push $R0
  ${GetOptions} $CMDLINE "/FRESH=" $R0
  ${If} $R0 == "1"
    !insertmacro WED_FRESH_START
  ${EndIf}
  Pop $R0
!macroend

; Uninstaller: "Delete the application data" also deletes Wednesday's data and secrets (not on
; an update, which runs the old uninstaller with /UPDATE).
!macro NSIS_HOOK_PREUNINSTALL
  ${If} $DeleteAppDataCheckboxState = 1
  ${AndIf} $UpdateMode <> 1
    !insertmacro WED_FRESH_START
  ${EndIf}
!macroend
