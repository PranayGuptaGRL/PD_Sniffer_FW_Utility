@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_partner_center_submission.ps1" %*
exit /b %ERRORLEVEL%
