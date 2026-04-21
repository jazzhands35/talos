@echo off
set TALOS_LOG_FILE=%~dp0talos_tree_debug.log
"%~dp0.venv\Scripts\python.exe" -m talos %*
