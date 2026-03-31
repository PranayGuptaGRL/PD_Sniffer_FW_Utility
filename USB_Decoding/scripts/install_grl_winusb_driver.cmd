@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_grl_winusb_driver.ps1" %*
exit /b %ERRORLEVEL%
