@echo off
set TALOS_TREE_MODE=1
set TALOS_LOG_FILE=%~dp0talos_tree_debug.log
"%~dp0Talos.exe" %*
