GRL WinUSB Driver - Standalone Install
======================================

This package installs the WinUSB driver for the GRL sniffer device
(VID_227F&PID_0005) without installing the USB PD Decoder application.

How to install
1. Right-click PowerShell and run as Administrator, or allow the UAC prompt.
2. Run:
   powershell -ExecutionPolicy Bypass -File .\install_grl_winusb_driver.ps1

Notes
- The package must contain both grl_sniffer_winusb.inf and grl_sniffer_winusb.cat.
- For production deployment, the .cat file should be the Microsoft-signed
  catalog returned by Partner Center.
- If the sniffer is already connected, unplug and replug it after installation.
