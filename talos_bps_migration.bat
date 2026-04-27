@echo off
REM ============================================================
REM  Launch Talos from the feat/bps-fp100-migration branch.
REM  Use this instead of talos.bat when testing the migration.
REM
REM  Prints the branch identity + commit count + last commit so
REM  you can confirm you're running the right build before it
REM  connects to Kalshi.
REM ============================================================

setlocal

pushd "%~dp0"

REM --- Branch sanity check (warn, don't block) --------------------
for /f "tokens=*" %%B in ('git rev-parse --abbrev-ref HEAD 2^>nul') do set BRANCH=%%B
if /i not "%BRANCH%"=="feat/bps-fp100-migration" (
    echo.
    echo [WARN] Current branch is "%BRANCH%", not "feat/bps-fp100-migration".
    echo        If you intended to test the migration, run: git checkout feat/bps-fp100-migration
    echo        Continuing anyway in 3 seconds — Ctrl-C to abort...
    timeout /t 3 /nobreak >nul
)

REM --- Build identity ---------------------------------------------
echo ============================================================
echo  Talos  (branch: %BRANCH%)
for /f "tokens=*" %%C in ('git log --oneline main..HEAD ^| find /c /v ""') do echo   migration commits ahead of main: %%C
for /f "tokens=*" %%H in ('git log -1 --oneline HEAD') do echo   HEAD: %%H
echo ============================================================
echo.

REM --- Logging ----------------------------------------------------
REM  Per CLAUDE.md: structlog emits key-value pairs; watching the
REM  log tail is the main diagnostic during migration testing.
set TALOS_LOG_FILE=%~dp0talos_bps_migration.log
echo  Log file: %TALOS_LOG_FILE%
echo  Tail hint (run in another terminal):
echo     tail -f "%TALOS_LOG_FILE%" ^| grep -E "raw_edge_bps^|reconcile^|cancel_order_with_verify^|legacy_migration^|stale_fills^|create_order"
echo.

REM --- Launch -----------------------------------------------------
"%~dp0.venv\Scripts\python.exe" -m talos %*
set EXITCODE=%ERRORLEVEL%

popd
endlocal & exit /b %EXITCODE%
