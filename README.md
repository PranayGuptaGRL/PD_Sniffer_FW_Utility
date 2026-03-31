# USB PD Decoder (Python)

This project provides a Sigrok/libsigrok-style starter for USB Power Delivery (PD) decoding with three input paths:

1. Live capture from any USB device selected by VID/PID (not fixed to Twinkie IDs).
2. Offline decode from Twinkie-like raw data files (`timestamp_us + payload`).
3. Offline decode from Twinkie USBlyzer edge-capture logs (`4-byte header + 60 edge timestamps`).

## Features

- Enumerate USB devices and filter by VID/PID.
- Pluggable capture backends for custom USB interfaces.
- Decode raw PD bytes into high-level packet summaries.
- Decode Twinkie edge timestamp records into candidate PD frames (sigrok-like staged path).
- Print decoded output to console, export JSON, and plot timeline.

## Install

Run the commands below from the repository root.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .\USB_Decoding
```

## Quick Start

List USB devices:

```powershell
usbpd list-usb
```

Decode the bundled raw sample:

```powershell
usbpd decode-file --input .\USB_Decoding\samples\quick_raw.txt --print
```

Decode a USBlyzer Twinkie edge-capture log:

```powershell
usbpd decode-usblyzer --input <path-to-usblyzer-log.txt> --print --dump-normalized .\normalized.txt
```

One-shot `.txt` decoder (auto detect raw vs USBlyzer):

```powershell
usbpd decode-txt --input .\USB_Decoding\samples\quick_raw.txt --json --print
```

Run desktop UI:

```powershell
usbpd-gui
```

GUI behavior:

- On startup it asks for mode: `Offline (.txt)` or `Online (Live USB)`.
- Offline mode prompts for `.txt`, decodes, and writes `.decoded.txt` (+ optional `.decoded.json`).
- Online mode enumerates USB devices, captures/decode live traffic, and logs decoded messages to a text file.

## Build

Use the PowerShell scripts below from the repository root to generate Windows artifacts.

Add `-InstallPyInstaller` to `build_exe.ps1`, `build_gui_exe.ps1`, or
`build_installer.ps1` if PyInstaller is not already installed.

Build the CLI txt decoder EXE:

```powershell
.\USB_Decoding\scripts\build_exe.ps1
```

Build the desktop GUI EXE:

```powershell
.\USB_Decoding\scripts\build_gui_exe.ps1
```

Build the full Windows installer:

```powershell
.\USB_Decoding\scripts\build_installer.ps1
```

`build_installer.ps1` requires `iscc.exe` on `PATH` and a signed
`grl_sniffer_winusb.cat` in the driver package.

## Production Driver Signing

Use the repo scripts below for production driver packaging.

Generate/sign the local catalog:

```powershell
.\USB_Decoding\scripts\build_and_sign_grl_driver_catalog.ps1 -PfxPath .\ev_or_release_cert.pfx -PfxPassword <password>
```

Build the Partner Center submission CAB:

```powershell
.\USB_Decoding\scripts\build_partner_center_submission.ps1 -EvCertSubject "Your EV Certificate Subject" -TimestampUrl http://timestamp.digicert.com
```

If PowerShell execution is blocked:

```cmd
.\USB_Decoding\scripts\build_partner_center_submission.cmd -EvCertSubject "Your EV Certificate Subject" -TimestampUrl http://timestamp.digicert.com
```

Import a signed package back into the repo:

```powershell
.\USB_Decoding\scripts\import_partner_center_signed_driver.ps1 -SourceDir .\downloaded_signed_package
```

If PowerShell execution is blocked:

```cmd
.\USB_Decoding\scripts\import_partner_center_signed_driver.cmd -SourceDir .\downloaded_signed_package
```

The production packaging scripts use `USB_Decoding\usb_pd_decoder\drivers\grl_sniffer_winusb.cat`
as the release catalog.

## Standalone Driver Install

Install only the GRL WinUSB driver, without installing the main application:

```powershell
powershell -ExecutionPolicy Bypass -File .\USB_Decoding\scripts\install_grl_winusb_driver.ps1
```

Build a standalone driver package/zip:

```powershell
.\USB_Decoding\scripts\build_driver_package.ps1
```

If PowerShell script execution is blocked on your machine, use:

```cmd
.\USB_Decoding\scripts\build_driver_package.cmd
```

Build a standalone driver-only installer EXE:

```powershell
.\USB_Decoding\scripts\build_driver_installer.ps1
```

If PowerShell script execution is blocked on your machine, use:

```cmd
.\USB_Decoding\scripts\build_driver_installer.cmd
```

Build a single-file portable driver installer you can copy to a new Windows PC
and run directly:

```powershell
.\USB_Decoding\scripts\build_portable_grl_driver_installer.ps1
```

If PowerShell script execution is blocked:

```cmd
.\USB_Decoding\scripts\build_portable_grl_driver_installer.cmd
```

Capture from a custom USB device (example VID/PID):

```powershell
usbpd capture --vid 0x1234 --pid 0x5678 --endpoint 0x81 --seconds 3 --print
```

## Input Formats

Twinkie-like raw file (`decode-file`):

```text
100 12 34 A1 0F
220 9B55AA01
340 01 02 03 04 05
```

USBlyzer edge-capture (`decode-usblyzer`):

- Any whitespace-separated hex dump.
- Parser extracts bytes and consumes 64-byte records.
- Record layout: `[4-byte header][60 edge timestamp bytes]`.

## Unit Tests

The test suite uses Python's built-in `unittest`.

Run all unit tests from the repository root:

```powershell
Set-Location .\USB_Decoding
python -m unittest discover -s tests -p "test_*.py"
```

## Notes

- `decode-usblyzer` currently uses heuristic BMC-to-byte reconstruction suitable for iterative tuning.
- For strict PD compliance, extend the Twinkie BMC decoder with full SOP, 4b5b, and CRC verification stages.
- Automatic WinUSB installation requires a signed driver catalog (`grl_sniffer_winusb.cat`).
