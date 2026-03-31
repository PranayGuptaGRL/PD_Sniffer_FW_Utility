@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0import_partner_center_signed_driver.ps1" %*
exit /b %ERRORLEVEL%
