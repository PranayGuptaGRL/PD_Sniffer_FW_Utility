from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import List

from .decoders.pd import PDDecoder
from .decoders.twinkie_bmc import TwinkieBMCDecoder
from .inputs.raw_file import RawFrameParser
from .inputs.twinkie_usblyzer import TwinkieUSBlyzerParser
from .models import DecodedMessage, RawFrame
from .plot.timeline import plot_messages


def _parse_int(x: str) -> int:
    return int(x, 0)


def _message_line(m: DecodedMessage) -> str:
    return (
        f"{m.timestamp_us:>9} us | {m.source:<12} | {m.message_type:<16} "
        f"header=0x{m.header:04X} objs={len(m.payload_words)} crc_hint={m.valid_crc_hint}"
    )


def _print_messages(messages: List[DecodedMessage]) -> None:
    for m in messages:
        print(_message_line(m))


def _export_json(messages: List[DecodedMessage], out_path: Path) -> None:
    out_path.write_text(json.dumps([m.to_dict() for m in messages], indent=2), encoding="utf-8")
    print(f"Wrote JSON: {out_path}")


def _export_txt(messages: List[DecodedMessage], out_path: Path) -> None:
    lines = [_message_line(m) for m in messages]
    out_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    print(f"Wrote text decode: {out_path}")


def _list_usb_rows() -> List[str]:
    try:
        from .inputs.usb_capture import USBDeviceCapture
    except ModuleNotFoundError as exc:
        if exc.name == "usb":
            raise RuntimeError("PyUSB is not installed. Run: pip install pyusb") from exc
        raise

    return USBDeviceCapture.list_devices()


def cmd_list_usb(_args: argparse.Namespace) -> int:
    try:
        rows = _list_usb_rows()
    except RuntimeError as exc:
        print(str(exc))
        return 2
    if not rows:
        print("No USB devices detected by PyUSB backend.")
        return 1
    print("Detected USB devices:")
    for row in rows:
        print(f"- {row}")
    return 0


def decode_frames(frames: List[RawFrame], args: argparse.Namespace) -> int:
    decoder = PDDecoder()
    messages = decoder.decode(frames)

    if args.print:
        _print_messages(messages)

    if getattr(args, "txt_out", None):
        _export_txt(messages, Path(args.txt_out))

    if getattr(args, "json_out", None):
        _export_json(messages, Path(args.json_out))

    if args.plot:
        plot_messages(messages)

    print(f"Decoded {len(messages)} message(s) from {len(frames)} frame(s).")
    return 0


def cmd_decode_file(args: argparse.Namespace) -> int:
    parser = RawFrameParser()
    lines = Path(args.input).read_text(encoding="utf-8-sig").splitlines()
    frames = parser.parse_lines(lines, source="file")
    return decode_frames(frames, args)


def _decode_usblyzer_to_frames(args: argparse.Namespace) -> List[RawFrame]:
    text = Path(args.input).read_text(encoding="utf-8-sig")

    parser = TwinkieUSBlyzerParser()
    records = parser.parse_text(text)
    gaps = parser.sequence_gaps(records)

    decoder = TwinkieBMCDecoder()
    analysis, frames = decoder.decode(records, sequence_gaps=gaps, tick_ns=getattr(args, "tick_ns", None))

    print("Twinkie USBlyzer analysis:")
    print(f"- records: {analysis.total_records}")
    print(f"- edge samples: {analysis.total_edges}")
    print(f"- sequence gaps: {analysis.sequence_gaps}")
    print(f"- skipped records: {analysis.skipped_records} "
          f"(CC filtered: {analysis.skipped_non_cc}, zero payload: {analysis.skipped_zero_payload})")
    print(f"- estimated half UI (ticks): {analysis.half_ui_ticks:.2f}")
    if analysis.estimated_kbps is not None:
        print(f"- estimated bitrate: {analysis.estimated_kbps:.1f} kbps")
    print(f"- candidate frames: {analysis.candidate_frames}")

    if getattr(args, "dump_normalized", None):
        out = Path(args.dump_normalized)
        lines = []
        for fr in frames:
            hex_blob = " ".join(f"{b:02x}" for b in fr.payload)
            lines.append(f"{fr.timestamp_us} {hex_blob}")
        out.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        print(f"Wrote normalized candidate frames: {out}")

    return frames


def cmd_decode_usblyzer(args: argparse.Namespace) -> int:
    frames = _decode_usblyzer_to_frames(args)
    return decode_frames(frames, args)


def cmd_decode_txt(args: argparse.Namespace) -> int:
    input_path = Path(args.input)
    out_prefix = Path(args.out_prefix) if args.out_prefix else input_path.with_suffix("")

    fmt = args.format
    if fmt == "auto":
        hex_pairs = len(__import__("re").findall(r"\b[0-9a-fA-F]{2}\b", input_path.read_text(encoding="utf-8-sig")))
        fmt = "usblyzer" if hex_pairs >= 64 and (hex_pairs % 64 == 0) else "raw"

    args.txt_out = str(out_prefix.with_suffix(".decoded.txt"))
    if args.json:
        args.json_out = str(out_prefix.with_suffix(".decoded.json"))

    if fmt == "usblyzer":
        args.dump_normalized = str(out_prefix.with_suffix(".normalized.txt"))
        frames = _decode_usblyzer_to_frames(args)
    else:
        parser = RawFrameParser()
        lines = input_path.read_text(encoding="utf-8-sig").splitlines()
        frames = parser.parse_lines(lines, source="file")

    return decode_frames(frames, args)


def cmd_capture(args: argparse.Namespace) -> int:
    try:
        from .inputs.usb_capture import USBDeviceCapture
    except ModuleNotFoundError as exc:
        if exc.name == "usb":
            print("PyUSB is not installed. Run: pip install pyusb")
            return 2
        raise

    capturer = USBDeviceCapture(
        vid=args.vid,
        pid=args.pid,
        endpoint=args.endpoint,
        interface=args.interface,
        timeout_ms=args.timeout_ms,
    )
    frames = capturer.capture(seconds=args.seconds, max_frames=args.max_frames)
    return decode_frames(frames, args)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="usbpd", description="USB PD data capture + decode")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list-usb", help="List visible USB devices")
    p_list.set_defaults(func=cmd_list_usb)

    p_file = sub.add_parser("decode-file", help="Decode Twinkie-like raw file")
    p_file.add_argument("--input", required=True, help="Raw input text file path")
    p_file.add_argument("--print", action="store_true", help="Print decoded messages")
    p_file.add_argument("--plot", action="store_true", help="Show matplotlib timeline")
    p_file.add_argument("--txt-out", help="Optional decoded text output path")
    p_file.add_argument("--json-out", help="Optional JSON export path")
    p_file.set_defaults(func=cmd_decode_file)

    p_lyzer = sub.add_parser("decode-usblyzer", help="Decode Twinkie USBlyzer edge-capture logs")
    p_lyzer.add_argument("--input", required=True, help="USBlyzer text file path")
    p_lyzer.add_argument("--tick-ns", type=float, default=None, help="Capture timer tick period in ns (optional)")
    p_lyzer.add_argument("--dump-normalized", help="Optional output file for candidate '<ts_us> <hex>' frames")
    p_lyzer.add_argument("--print", action="store_true", help="Print decoded messages")
    p_lyzer.add_argument("--plot", action="store_true", help="Show matplotlib timeline")
    p_lyzer.add_argument("--txt-out", help="Optional decoded text output path")
    p_lyzer.add_argument("--json-out", help="Optional JSON export path")
    p_lyzer.set_defaults(func=cmd_decode_usblyzer)

    p_txt = sub.add_parser("decode-txt", help="One-shot decode for .txt input with output files")
    p_txt.add_argument("--input", required=True, help="Input .txt path (raw or USBlyzer Twinkie)")
    p_txt.add_argument("--format", choices=["auto", "raw", "usblyzer"], default="auto", help="Input format")
    p_txt.add_argument("--out-prefix", help="Output prefix path (default: input file stem)")
    p_txt.add_argument("--tick-ns", type=float, default=None, help="Capture timer tick period in ns for USBlyzer mode")
    p_txt.add_argument("--json", action="store_true", help="Also write JSON output")
    p_txt.add_argument("--print", action="store_true", help="Print decoded messages")
    p_txt.add_argument("--plot", action="store_true", help="Show matplotlib timeline")
    p_txt.set_defaults(func=cmd_decode_txt)

    p_cap = sub.add_parser("capture", help="Capture from custom USB VID/PID and decode")
    p_cap.add_argument("--vid", type=_parse_int, required=True, help="USB VID, e.g. 0x18D1")
    p_cap.add_argument("--pid", type=_parse_int, required=True, help="USB PID, e.g. 0x501A")
    p_cap.add_argument("--endpoint", type=_parse_int, default=0x81, help="IN endpoint address")
    p_cap.add_argument("--interface", type=int, default=0, help="USB interface number")
    p_cap.add_argument("--timeout-ms", type=int, default=200, help="Read timeout in ms")
    p_cap.add_argument("--seconds", type=float, default=3.0, help="Capture duration")
    p_cap.add_argument("--max-frames", type=int, default=500, help="Safety frame cap")
    p_cap.add_argument("--print", action="store_true", help="Print decoded messages")
    p_cap.add_argument("--plot", action="store_true", help="Show matplotlib timeline")
    p_cap.add_argument("--txt-out", help="Optional decoded text output path")
    p_cap.add_argument("--json-out", help="Optional JSON export path")
    p_cap.set_defaults(func=cmd_capture)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
