@echo off
rem ============================================================
rem  test-data-platform . one-click entry for Windows
rem
rem  Why this wrapper exists:
rem  Windows defaults to ExecutionPolicy=Restricted, so running
rem  dev.ps1 directly fails with "running scripts is disabled".
rem  -ExecutionPolicy Bypass applies to this process only and does
rem  NOT change the machine-wide security setting.
rem
rem  Usage is identical to dev.ps1:
rem      dev.cmd                 check + init + test
rem      dev.cmd check           environment health check only
rem      dev.cmd init            create db/tables + demo data
rem      dev.cmd test            run pytest
rem      dev.cmd bench           run the 1000-concurrency bench
rem      dev.cmd api -Reload     start API with auto reload
rem      dev.cmd docs            print URLs and sample requests
rem
rem  This file is intentionally ASCII-only: cmd.exe reads .cmd
rem  files in the OEM codepage, and mixing UTF-8 with non-ASCII
rem  comments gets lines executed as commands.
rem ============================================================
setlocal

chcp 65001 >nul

where pwsh >nul 2>nul
if %ERRORLEVEL%==0 (
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0dev.ps1" %*
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0dev.ps1" %*
)

exit /b %ERRORLEVEL%
