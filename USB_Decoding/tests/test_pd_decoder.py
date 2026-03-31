from __future__ import annotations

import unittest

from usb_pd_decoder.decoders.pd import PDDecoder
from usb_pd_decoder.models import RawFrame


class PDDecoderTest(unittest.TestCase):
    def test_decode_frame_matches_batch_decode(self) -> None:
        frame = RawFrame(
            timestamp_us=1234,
            payload=bytes.fromhex("82102cb10413"),
            source="cc1_capture",
        )

        decoder = PDDecoder()
        msg = decoder.decode_frame(frame)
        batch = decoder.decode([frame])

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(len(batch), 1)
        self.assertEqual(msg.to_dict(), batch[0].to_dict())
        self.assertEqual(msg.message_type, "Request")
        self.assertEqual(msg.direction, "SNK->SRC")
        self.assertEqual(msg.cc_line, "CC1")


if __name__ == "__main__":
    unittest.main()
