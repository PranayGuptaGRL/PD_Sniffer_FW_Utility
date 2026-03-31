from __future__ import annotations

import unittest

from usb_pd_decoder.decoders.grl_sniffer import GRL_PACKET_SIZE, parse_grl_vbus_telemetry


class GRLVbusTelemetryTest(unittest.TestCase):
    def test_parse_vbus_telemetry_example(self) -> None:
        packet = bytes.fromhex(
            "AA AA AA AA 06 26 0B 40 04 6A 00 6D 4E"
            + " 00" * (GRL_PACKET_SIZE - 13)
        )

        sample = parse_grl_vbus_telemetry(packet)

        self.assertIsNotNone(sample)
        assert sample is not None
        self.assertEqual(sample.voltage_count, 0x0626)
        self.assertEqual(sample.temp_count, 0x0B4)
        self.assertEqual(sample.current_count, 0x046A)
        self.assertEqual(sample.power_count, 0x006D4E)
        self.assertAlmostEqual(sample.voltage_v, 4.91875)
        self.assertAlmostEqual(sample.temp_c, 22.5)
        self.assertAlmostEqual(sample.current_a, 0.27587890625)
        self.assertAlmostEqual(sample.power_w, 1.36630859375)

    def test_parse_vbus_telemetry_negative_current(self) -> None:
        packet = bytes.fromhex(
            "AA AA AA AA 00 64 01 00 FF 00 00 00 10"
            + " 00" * (GRL_PACKET_SIZE - 13)
        )

        sample = parse_grl_vbus_telemetry(packet)

        self.assertIsNotNone(sample)
        assert sample is not None
        self.assertEqual(sample.current_count, -256)
        self.assertAlmostEqual(sample.current_a, -0.0625)


if __name__ == "__main__":
    unittest.main()
