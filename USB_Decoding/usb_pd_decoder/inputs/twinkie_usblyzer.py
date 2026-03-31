from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List


@dataclass
class TwinkieRecord:
    seq: int | None
    packet_type: int
    flags: int
    timestamp_ms: int
    header: bytes
    payload: bytes
    edge_times: bytes
    is_zero_payload: bool
    cc_line: str = ""


class TwinkieUSBlyzerParser:
    """Parse USBlyzer text dumps containing 64-byte Twinkie records."""

    RECORD_SIZE = 64
    HEADER_SIZE = 4
    PACKET_TYPE_CC_TIMING = 0x00
    PACKET_TYPE_ANALOG_EPR = 0x01

    @staticmethod
    def _hex_tokens(text: str) -> List[int]:
        tokens = re.findall(r"\b[0-9a-fA-F]{2}\b", text)
        return [int(tok, 16) for tok in tokens]

    def parse_text(self, text: str) -> List[TwinkieRecord]:
        raw = self._hex_tokens(text)
        if len(raw) < self.RECORD_SIZE:
            raise ValueError("Input does not contain a full 64-byte Twinkie record")

        usable = (len(raw) // self.RECORD_SIZE) * self.RECORD_SIZE
        raw = raw[:usable]

        records: List[TwinkieRecord] = []
        for i in range(0, len(raw), self.RECORD_SIZE):
            rec = bytes(raw[i : i + self.RECORD_SIZE])
            header = rec[:4]
            packet_type = header[0]
            flags = header[1]
            timestamp_ms = int.from_bytes(header[2:4], "little")
            payload = rec[self.HEADER_SIZE:]
            # Sequence is carried in header bytes 1..2 (little-endian), but overlaps
            # the timestamp bytes. Keep the full word for correlation/debug; only
            # the 9-bit sequence number (bits 3..11) is currently used.
            seq = int.from_bytes(header[1:3], "little")
            edges = payload
            ch = (seq >> 12) & 0x1
            records.append(
                TwinkieRecord(
                    seq=seq,
                    packet_type=packet_type,
                    flags=flags,
                    timestamp_ms=timestamp_ms,
                    header=header,
                    payload=payload,
                    edge_times=edges,
                    is_zero_payload=packet_type == self.PACKET_TYPE_CC_TIMING and not any(edges),
                    cc_line=f"CC{ch + 1}",
                )
            )
        return records

    @staticmethod
    def sequence_gaps(records: List[TwinkieRecord]) -> int:
        if len(records) < 2:
            return 0
        gaps = 0
        for prev, curr in zip(records, records[1:]):
            if prev.seq is None or curr.seq is None:
                continue
            prev_seq = prev.seq & 0x1FF
            curr_seq = curr.seq & 0x1FF
            expected = (prev_seq + 1) & 0x1FF
            if curr_seq != expected:
                gaps += 1
        return gaps

    @staticmethod
    def describe_flags(record: TwinkieRecord) -> str:
        seq = record.seq or 0
        buf_idx = seq & 0x7
        seq_num = (seq >> 3) & 0x1FF
        ch = (seq >> 12) & 0x1
        of = bool((seq >> 15) & 0x1)
        return (
            f"type=0x{record.packet_type:02X} flags=0x{record.flags:02X} "
            f"ts={record.timestamp_ms}ms seq={seq_num} buf={buf_idx} ch={ch} of={int(of)}"
        )
