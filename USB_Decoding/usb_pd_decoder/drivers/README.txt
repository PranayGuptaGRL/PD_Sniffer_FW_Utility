Bundled GRL WinUSB Driver Package
=================================

Files
- grl_sniffer_winusb.inf : WinUSB INF for VID_227F&PID_0005
- grl_sniffer_winusb.cat : signed catalog required for automatic installation

Important
- Windows will not install this package cleanly on normal x64 systems without a
  signed catalog file.
- The application checks for the .cat file before attempting automatic driver
  installation and will fail fast if it is missing.
