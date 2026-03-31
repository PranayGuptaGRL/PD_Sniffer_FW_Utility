@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_and_sign_grl_driver_catalog.ps1" %*
exit /b %ERRORLEVEL%
