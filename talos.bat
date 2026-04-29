@echo off
set TALOS_LOG_FILE=%~dp0talos_tree_debug.log
REM Capture stderr too — Textual prints unhandled tracebacks to stderr after
REM TUI cleanup, and without a redirect they vanish when the window closes.
"%~dp0.venv\Scripts\python.exe" -m talos %* 2>>"%~dp0talos_stderr.log"
