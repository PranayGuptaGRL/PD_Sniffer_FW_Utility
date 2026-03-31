Bundled GRL WinUSB Driver Package
=================================

Files
- grl_sniffer_winusb.inf : WinUSB INF for VID_227F&PID_0005
- grl_sniffer_winusb.cat : signed catalog required for automatic installation

Important
- Windows will not install this package cleanly on normal x64 systems without a
  signed catalog file.
- Generate the catalog from the INF with WDK tooling and sign it before
  building the installer for distribution.
- The application checks for the .cat file before attempting automatic driver
  installation and will fail fast if it is missing.
- For an app-independent install path, use `scripts/install_grl_winusb_driver.ps1`
  or build the standalone driver-only package/installer from `scripts/`.
- For production distribution, replace `grl_sniffer_winusb.cat` with the
  Microsoft-signed catalog returned by Partner Center.
- The test-signing helper script is for development only and should not be used
  for customer-facing releases.
