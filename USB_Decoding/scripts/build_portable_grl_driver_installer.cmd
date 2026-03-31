@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_portable_grl_driver_installer.ps1" %*
exit /b %ERRORLEVEL%
