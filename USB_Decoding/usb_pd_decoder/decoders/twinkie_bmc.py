from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import List

from ..models import RawFrame
from ..inputs.twinkie_usblyzer import TwinkieRecord


@dataclass
class TwinkieAnalysis:
    total_records: int
    total_edges: int
    sequence_gaps: int
    skipped_records: int
    skipped_zero_payload: int
    skipped_non_cc: int
    half_ui_ticks: float
    estimated_kbps: float | None
    candidate_frames: int


class TwinkieBMCDecoder:
    """Heuristic Twinkie edge-stream to candidate PD byte frames.

    This is intentionally compatible with libsigrok-like staged decode flow:
    edge timestamps -> BMC bits -> candidate bytes -> PD message parser.
    """

    def _unwrap_edges(self, records: List[TwinkieRecord]) -> List[int]:
        edges8: List[int] = []
        for rec in records:
            edges8.extend(rec.edge_times)

        if not edges8:
            return []

        unwrapped = [0]
        last = edges8[0]
        acc = 0
        for cur in edges8[1:]:
            delta = (cur - last) & 0xFF
            acc += delta
            unwrapped.append(acc)
            last = cur
        return unwrapped

    @staticmethod
    def _estimate_half_ui(deltas: List[int]) -> float:
        if not deltas:
            return 1.0

        positives = sorted(d for d in deltas if d > 0)
        if not positives:
            return 1.0

        window = positives[: max(8, len(positives) // 4)]
        return float(median(window))

    @staticmethod
    def _interval_to_half_units(delta: int, half_ui: float) -> int:
        if half_ui <= 0:
            return 0
        return max(1, int(round(delta / half_ui)))

    def _bits_from_deltas(self, deltas: List[int], half_ui: float) -> List[List[int]]:
        frames_bits: List[List[int]] = []
        current: List[int] = []
        pending_half = False

        gap_units = 8
        for delta in deltas:
            units = self._interval_to_half_units(delta, half_ui)

            if units >= gap_units:
                if current:
                    frames_bits.append(current)
                    current = []
                pending_half = False
                continue

            if units == 1:
                if pending_half:
                    current.append(1)
                    pending_half = False
                else:
                    pending_half = True
                continue

            if units == 2:
                current.append(0)
                pending_half = False
                continue

            # Coarse fallback for unusual intervals.
            if units == 3:
                current.append(0)
                pending_half = True
                continue

            current.extend([0] * (units // 2))
            pending_half = (units % 2) == 1

        if current:
            frames_bits.append(current)

        return frames_bits

    @staticmethod
    def _bits_to_bytes_lsb_first(bits: List[int]) -> bytes:
        out = bytearray()
        for i in range(0, len(bits), 8):
            chunk = bits[i : i + 8]
            if len(chunk) < 8:
                break
            value = 0
            for bit_idx, bit in enumerate(chunk):
                value |= (bit & 1) << bit_idx
            out.append(value)
        return bytes(out)

    def decode(self, records: List[TwinkieRecord], sequence_gaps: int, tick_ns: float | None = None) -> tuple[TwinkieAnalysis, List[RawFrame]]:
        usable_records: List[TwinkieRecord] = []
        skipped_non_cc = 0
        skipped_zero_payload = 0
        for rec in records:
            if rec.packet_type != 0x00:
                skipped_non_cc += 1
                continue
            if rec.is_zero_payload:
                skipped_zero_payload += 1
                continue
            usable_records.append(rec)

        unwrapped = self._unwrap_edges(usable_records)
        if len(unwrapped) < 2:
            analysis = TwinkieAnalysis(
                total_records=len(records),
                total_edges=sum(len(r.edge_times) for r in records),
                sequence_gaps=sequence_gaps,
                skipped_records=skipped_non_cc + skipped_zero_payload,
                skipped_non_cc=skipped_non_cc,
                skipped_zero_payload=skipped_zero_payload,
                half_ui_ticks=0.0,
                estimated_kbps=None,
                candidate_frames=0,
            )
            return analysis, []

        deltas = [b - a for a, b in zip(unwrapped, unwrapped[1:])]
        half_ui = self._estimate_half_ui(deltas)

        raw_frames: List[RawFrame] = []
        for cc_line in ("CC1", "CC2"):
            cc_records = [rec for rec in usable_records if rec.cc_line == cc_line]
            if len(cc_records) < 2:
                continue

            cc_unwrapped = self._unwrap_edges(cc_records)
            if len(cc_unwrapped) < 2:
                continue

            cc_deltas = [b - a for a, b in zip(cc_unwrapped, cc_unwrapped[1:])]
            bits_frames = self._bits_from_deltas(cc_deltas, half_ui)
            ts_us = cc_records[0].timestamp_ms * 1000
            for bits in bits_frames:
                payload = self._bits_to_bytes_lsb_first(bits)
                if len(payload) >= 2:
                    raw_frames.append(
                        RawFrame(
                            timestamp_us=ts_us,
                            payload=payload,
                            source=f"twinkie-edge-{cc_line.lower()}",
                        )
                    )
                ts_us += 100

        estimated_kbps = None
        if tick_ns and half_ui > 0:
            bit_time_s = (half_ui * 2.0) * (tick_ns * 1e-9)
            if bit_time_s > 0:
                estimated_kbps = (1.0 / bit_time_s) / 1000.0

        analysis = TwinkieAnalysis(
            total_records=len(records),
            total_edges=sum(len(r.edge_times) for r in records),
            sequence_gaps=sequence_gaps,
            skipped_records=skipped_non_cc + skipped_zero_payload,
            skipped_non_cc=skipped_non_cc,
            skipped_zero_payload=skipped_zero_payload,
            half_ui_ticks=half_ui,
            estimated_kbps=estimated_kbps,
            candidate_frames=len(raw_frames),
        )
        return analysis, raw_frames
