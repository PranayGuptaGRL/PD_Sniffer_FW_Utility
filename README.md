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

```bash
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
pip install -e .
```

## Quick Start

List USB devices:

```bash
usbpd list-usb
```

Decode Twinkie-like raw text file:

```bash
usbpd decode-file --input samples/twinkie_like_raw.txt --print --plot
```

Decode USBlyzer Twinkie edge-capture log:

```bash
usbpd decode-usblyzer --input samples/usblyzer_twinkie_sample.txt --print --dump-normalized samples/normalized.txt
```

One-shot `.txt` decoder (auto detect raw vs USBlyzer):

```bash
usbpd decode-txt --input samples/usblyzer_twinkie_sample.txt --json --print
```

Run desktop UI:

```bash
usbpd-gui
```

GUI behavior:

- On startup it asks for mode: `Offline (.txt)` or `Online (Live USB)`.
- Offline mode prompts for `.txt`, decodes, and writes `.decoded.txt` (+ optional `.decoded.json`).
- Online mode enumerates USB devices, captures/decode live traffic, and logs decoded messages to a text file.
- For live capture on Windows, bind a WinUSB/libusb-compatible driver to the device separately before starting the app.

## Production Driver Signing

For a production-ready driver package, use Microsoft's production signing flow.
The practical sequence is:

1. Generate the local catalog (`grl_sniffer_winusb.cat`) on the build machine.
2. Create an EV-signed Partner Center submission CAB.
3. Upload that CAB to Partner Center Hardware Dashboard.
4. Download the Microsoft-signed driver package.
5. Import that signed package back into this repo.
6. Build the installer or the single-file portable driver installer.

Generate/sign the local catalog on the build machine:

```powershell
.\scripts\build_and_sign_grl_driver_catalog.ps1 -PfxPath .\ev_or_release_cert.pfx -PfxPassword <password>
```

Create the Partner Center submission CAB:

```powershell
.\scripts\build_partner_center_submission.ps1 -EvCertSubject "Your EV Certificate Subject" -TimestampUrl http://timestamp.digicert.com
```

If PowerShell execution is blocked:

```cmd
.\scripts\build_partner_center_submission.cmd -EvCertSubject "Your EV Certificate Subject" -TimestampUrl http://timestamp.digicert.com
```

After Microsoft signs the package and you download/extract it, import it:

```powershell
.\scripts\import_partner_center_signed_driver.ps1 -SourceDir .\downloaded_signed_package
```

If PowerShell execution is blocked:

```cmd
.\scripts\import_partner_center_signed_driver.cmd -SourceDir .\downloaded_signed_package
```

The production packaging scripts now assume `usb_pd_decoder\drivers\grl_sniffer_winusb.cat`
is the Microsoft-signed catalog returned by Partner Center.

## Standalone Driver Install

Install only the GRL WinUSB driver, without installing the main application:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_grl_winusb_driver.ps1
```

Build a standalone driver package/zip:

```powershell
.\scripts\build_driver_package.ps1
```

If PowerShell script execution is blocked on your machine, use:

```cmd
.\scripts\build_driver_package.cmd
```

Build a standalone driver-only installer EXE:

```powershell
.\scripts\build_driver_installer.ps1
```

If PowerShell script execution is blocked on your machine, use:

```cmd
.\scripts\build_driver_installer.cmd
```

Build a single-file portable driver installer you can copy to a new Windows PC
and run directly:

```powershell
.\scripts\build_portable_grl_driver_installer.ps1
```

If PowerShell script execution is blocked:

```cmd
.\scripts\build_portable_grl_driver_installer.cmd
```

This produces:

```text
dist\install_grl_winusb_driver_portable.cmd
```

That generated single file embeds the production-signed INF and CAT. On the
target PC it self-elevates and installs the WinUSB driver with `pnputil`.
After that, the sniffer can be detected by the app.

Capture from a custom USB device (example VID/PID):

```bash
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

## Notes

- USB transport framing differs by device; parsing modules are under `usb_pd_decoder/inputs/`.
- `decode-usblyzer` currently uses heuristic BMC-to-byte reconstruction suitable for iterative tuning.
- For strict PD compliance, extend `twinkie_bmc.py` with full SOP, 4b5b, and CRC verification stages.
- Automatic WinUSB installation requires a signed driver catalog (`grl_sniffer_winusb.cat`). For production releases, use the Microsoft-signed catalog returned by Partner Center.
- To build executables on Windows:
  - CLI txt decoder: `scripts/build_exe.ps1`
  - GUI app: `scripts/build_gui_exe.ps1`
  - Full Windows installer with WinUSB preinstall: `scripts/build_installer.ps1`
  - Generate/sign the local submission catalog: `scripts/build_and_sign_grl_driver_catalog.ps1`
  - Build EV-signed Partner Center submission CAB: `scripts/build_partner_center_submission.ps1`
  - Import the Microsoft-signed package: `scripts/import_partner_center_signed_driver.ps1`
  - Standalone driver package/zip: `scripts/build_driver_package.ps1`
  - Standalone driver-only installer: `scripts/build_driver_installer.ps1`
  - Portable single-file driver installer: `scripts/build_portable_grl_driver_installer.ps1`
