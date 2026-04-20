@echo off
REM Build Talos.exe directly into the project root so it shares
REM games_full.json / settings.json / tree_metadata.json with
REM `python -m talos` (launched via talos.bat). Without --distpath
REM PyInstaller writes to dist\, which is a separate data dir and
REM causes the "exe shows different events than talos.bat" symptom.
"%~dp0.venv\Scripts\python.exe" -m PyInstaller --noconfirm --distpath "%~dp0" "%~dp0talos.spec"
