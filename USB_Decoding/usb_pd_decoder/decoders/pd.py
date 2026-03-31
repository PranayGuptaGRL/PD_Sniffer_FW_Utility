from __future__ import annotations

from typing import List

from ..models import DecodedMessage, RawFrame


class PDDecoder:
    """Decode PD headers conservatively and drop unsupported header patterns."""

    CONTROL_TYPES = {
        0x01: "GoodCRC",
        0x02: "GotoMin",
        0x03: "Accept",
        0x04: "Reject",
        0x05: "Ping",
        0x06: "PS_RDY",
        0x07: "Get_Source_Cap",
        0x08: "Get_Sink_Cap",
        0x09: "DR_Swap",
        0x0A: "PR_Swap",
        0x0B: "VCONN_Swap",
        0x0C: "Wait",
        0x0D: "Soft_Reset",
        0x0E: "Data_Reset",
        0x0F: "Data_Reset_Complete",
        0x10: "Not_Supported",
        0x11: "Get_Source_Cap_Extended",
        0x12: "Get_Status",
        0x13: "FR_Swap",
        0x14: "Get_PPS_Status",
        0x15: "Get_Country_Codes",
        0x16: "Get_Sink_Cap_Extended",
        0x17: "Get_Source_Info",
        0x18: "Get_Revision",
    }

    DATA_TYPES = {
        0x01: "Source_Caps",
        0x02: "Request",
        0x03: "BIST",
        0x04: "Sink_Caps",
        0x05: "Battery_Status",
        0x06: "Alert",
        0x07: "Get_Country_Info",
        0x0F: "Vendor_Defined",
    }

    @staticmethod
    def _cc_line_from_source(source: str) -> str:
        src = source.lower()
        if "cc1" in src:
            return "CC1"
        if "cc2" in src:
            return "CC2"
        return ""

    @staticmethod
    def _direction_from_header(header: int, direction_bit: int | None = None) -> str:
        bit = ((header >> 8) & 0x1) if direction_bit is None else direction_bit
        return "SRC->SNK" if bit else "SNK->SRC"

    def decode_frame(self, fr: RawFrame) -> DecodedMessage | None:
        if len(fr.payload) < 2:
            return None

        header = int.from_bytes(fr.payload[:2], "little")
        msg_type = header & 0x1F
        num_data_objs = (header >> 12) & 0x07
        spec_rev = (header >> 6) & 0x03
        direction_bit = (header >> 8) & 0x1

        if spec_rev == 3 or msg_type == 0:
            return None

        expected_len = 2 + (num_data_objs * 4)
        if len(fr.payload) < expected_len:
            return None

        is_control = num_data_objs == 0
        if is_control:
            msg_name = self.CONTROL_TYPES.get(msg_type)
        else:
            msg_name = self.DATA_TYPES.get(msg_type)
        if msg_name is None:
            return None

        body = fr.payload[2:expected_len]
        payload_words = []
        for i in range(0, len(body), 4):
            chunk = body[i : i + 4]
            if len(chunk) == 4:
                payload_words.append(int.from_bytes(chunk, "little"))

        return DecodedMessage(
            timestamp_us=fr.timestamp_us,
            message_type=msg_name,
            header=header,
            payload_words=payload_words,
            valid_crc_hint=True,
            source=fr.source,
            direction=self._direction_from_header(header, direction_bit=direction_bit),
            cc_line=self._cc_line_from_source(fr.source),
        )

    def decode(self, frames: List[RawFrame]) -> List[DecodedMessage]:
        messages: List[DecodedMessage] = []
        for fr in frames:
            msg = self.decode_frame(fr)
            if msg is not None:
                messages.append(msg)
        return messages
