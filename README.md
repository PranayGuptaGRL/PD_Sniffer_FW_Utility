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
.\.venv\Scripts\python -m pip install -e .\USB_Decoding
```

## Quick Start

List USB devices:

```powershell
.\.venv\Scripts\usbpd.exe list-usb
```

Decode the bundled raw sample:

```powershell
.\.venv\Scripts\usbpd.exe decode-file --input .\USB_Decoding\samples\quick_raw.txt --print
```

Decode a USBlyzer Twinkie edge-capture log:

```powershell
.\.venv\Scripts\usbpd.exe decode-usblyzer --input <path-to-usblyzer-log.txt> --print --dump-normalized .\normalized.txt
```

One-shot `.txt` decoder (auto detect raw vs USBlyzer):

```powershell
.\.venv\Scripts\usbpd.exe decode-txt --input .\USB_Decoding\samples\quick_raw.txt --json --print
```

Run desktop UI:

```powershell
.\.venv\Scripts\usbpd-gui.exe
```

GUI behavior:

- On startup it asks for mode: `Offline (.txt)` or `Online (Live USB)`.
- Offline mode prompts for `.txt`, decodes, and writes `.decoded.txt` (+ optional `.decoded.json`).
- Online mode enumerates USB devices, captures/decode live traffic, and logs decoded messages to a text file.

## Build

Build from the `USB_Decoding` folder with PyInstaller.

Install PyInstaller if needed:

```powershell
.\.venv\Scripts\python -m pip install pyinstaller
```

Build the desktop GUI EXE:

```powershell
Set-Location .\USB_Decoding
python -m PyInstaller --noconfirm --clean --onefile --windowed --name "usbpd_gui" --workpath "$env:TEMP\usbpd_gui_build" --exclude-module usb_pd_decoder.windows_driver usbpd_gui.py
```

This generates `USB_Decoding\dist\usbpd_gui.exe`.

Build the CLI txt decoder EXE:

```powershell
Set-Location .\USB_Decoding
python -m PyInstaller --noconfirm --clean --onefile --name "usbpd_txt_decoder" --workpath "$env:TEMP\usbpd_txt_decoder_build" usbpd_txt_decoder.py
```

This generates `USB_Decoding\dist\usbpd_txt_decoder.exe`.

Capture from a custom USB device (example VID/PID):

```powershell
.\.venv\Scripts\usbpd.exe capture --vid 0x1234 --pid 0x5678 --endpoint 0x81 --seconds 3 --print
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
..\.venv\Scripts\python -m unittest discover -s tests -p "test_*.py"
```

## Notes

- `decode-usblyzer` currently uses heuristic BMC-to-byte reconstruction suitable for iterative tuning.
- For strict PD compliance, extend the Twinkie BMC decoder with full SOP, 4b5b, and CRC verification stages.
