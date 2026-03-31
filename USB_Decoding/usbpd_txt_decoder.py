from __future__ import annotations

import argparse

from usb_pd_decoder.cli import cmd_decode_txt


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="USB PD TXT decoder (raw or Twinkie USBlyzer)")
    p.add_argument("--input", required=True, help="Input .txt path")
    p.add_argument("--format", choices=["auto", "raw", "usblyzer"], default="auto", help="Input format")
    p.add_argument("--out-prefix", help="Output prefix path (default: input file stem)")
    p.add_argument("--tick-ns", type=float, default=None, help="Capture timer tick period in ns for USBlyzer mode")
    p.add_argument("--json", action="store_true", help="Also write JSON output")
    p.add_argument("--print", action="store_true", help="Print decoded messages")
    p.add_argument("--plot", action="store_true", help="Show matplotlib timeline")
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return cmd_decode_txt(args)


if __name__ == "__main__":
    raise SystemExit(main())
