from __future__ import annotations

import io
import unittest
from pathlib import Path

from usb_pd_decoder.decoders.grl_bmc import (
    GRLBMCDecoder,
    _classify_delta,
    _edges_to_bmc_bits,
)
from usb_pd_decoder.decoders.grl_sniffer import (
    extract_grl_direct_pd_payload,
    parse_grl_packet,
    parse_grl_vbus_telemetry,
    update_grl_timestamp_state,
)


def _load_grl_rows(raw_path: Path) -> list[tuple]:
    ts_state = {
        0: {"epoch_ms": 0, "last_expanded_ms": None},
        1: {"epoch_ms": 0, "last_expanded_ms": None},
    }
    parsed_packets: list[tuple] = []

    for line in raw_path.read_text(encoding="utf-8-sig").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        parts = text.split()
        raw_ts = int(parts[0])
        payload = bytes.fromhex("".join(parts[2:]))
        if parse_grl_vbus_telemetry(payload) is not None:
            parsed_packets.append(("vbus", raw_ts, raw_ts))
            continue
        if extract_grl_direct_pd_payload(payload) is not None:
            parsed_packets.append(("direct", raw_ts, raw_ts))
            continue
        pkt = parse_grl_packet(payload)
        if pkt is None:
            continue
        ts_us = update_grl_timestamp_state(ts_state[pkt.channel], pkt)
        parsed_packets.append((pkt, ts_us, raw_ts))

    return parsed_packets


class GRLBMCDecoderTest(unittest.TestCase):
    def test_classify_delta_fixed_thresholds(self) -> None:
        """3/4/5 → half(4), 7/8/9 → full(8)."""
        for delta in (3, 4, 5):
            self.assertEqual(_classify_delta(delta), ("half", 4))
        for delta in (7, 8, 9):
            self.assertEqual(_classify_delta(delta), ("full", 8))
        # Noise values
        for delta in (1, 2, 6, 10, 11, 12, 15, 20, 23):
            self.assertEqual(_classify_delta(delta)[0], "noise")
        # Gap
        for delta in (24, 30, 50, 128):
            self.assertEqual(_classify_delta(delta)[0], "gap")
        # Zero
        self.assertEqual(_classify_delta(0)[0], "zero")

    def test_bmc_polarity(self) -> None:
        # Edges: 0x00, 0x04, 0x08, 0x10
        # Deltas: 4, 4, 8 → half, half, full
        # BMC: two halfs = bit 1 (mid-bit transition), full = bit 0 (no transition)
        segments = _edges_to_bmc_bits(bytes([0x00, 0x04, 0x08, 0x10]))
        self.assertEqual(segments, [[1, 0]])

    def test_feed_logs_debug_output(self) -> None:
        debug_out = io.StringIO()
        decoder = GRLBMCDecoder(debug_file=debug_out)
        edge_bytes = bytes.fromhex(
            "f901050911191d20282c3034383c4044484b4f575b5f63676b6f767a7e868a8e"
            "92969ea1a5adb1b5bdc1c5ccd0d4d8dce4ecfb737c80848c90949b9f"
        )
        decoder.feed(edge_bytes, 3_595_708_000, wall_timestamp_us=5_545_453)
        debug_text = debug_out.getvalue()
        # Verify step-by-step debug output is present
        self.assertIn("STEP 1: RAW EDGE BYTES", debug_text)
        self.assertIn("STEP 2: COMPUTE DELTAS", debug_text)
        self.assertIn("STEP 3: BMC BIT SEGMENTS", debug_text)

    def test_overlapping_recoveries_collapse_to_single_request(self) -> None:
        logs_dir = Path(__file__).resolve().parents[1] / "live_logs"
        raw_paths = sorted(logs_dir.glob("*_raw.txt"), key=lambda path: path.stat().st_mtime, reverse=True)
        if not raw_paths:
            raise unittest.SkipTest("No live log capture available for GRL BMC regression")

        frames = []
        for raw_path in raw_paths:
            decoder = GRLBMCDecoder()
            candidate_frames = []
            for item, ts_us, raw_ts in _load_grl_rows(raw_path):
                if item == "vbus" or item == "direct":
                    decoder.reset_stream()
                    continue
                if item.is_idle:
                    decoder.reset_stream()
                    continue
                candidate_frames.extend(decoder.feed(item.payload, ts_us, wall_timestamp_us=raw_ts))
            if any(frame.payload.hex() == "82102cb10413" for frame in candidate_frames):
                frames = candidate_frames
                break

        if not frames:
            raise unittest.SkipTest("No capture with a recovered Request frame is available")

        payloads = [frame.payload.hex() for frame in frames]
        self.assertTrue(payloads)
        self.assertIn("82102cb10413", payloads)
        self.assertEqual(payloads.count("82102cb10413"), 1)

        request_frame = next(frame for frame in frames if frame.payload.hex() == "82102cb10413")
        self.assertEqual(request_frame.metadata["crc_mode"], "direct")


if __name__ == "__main__":
    unittest.main()
