from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from ..models import RawFrame

# GRL USB C PD Sniffer - 64-byte bulk packets.
#
# 4-byte header + 60-byte payload:
#   Bytes 0-1   : Sequence field (16-bit, little-endian)
#                   Bits  0-2   : Buffer Index (0-7, sub-buffer within message)
#                   Bits  3-11  : Sequence Number (0-511, wraps)
#                   Bit   12    : Channel (0=CC1, 1=CC2)
#                   Bits 13-14  : Reserved
#                   Bit   15    : Overflow flag used by the device timestamp scheme
#   Bytes 2-3   : 16-bit timestamp in milliseconds (little-endian)
#   Bytes 4-63  : 60 bytes of edge timestamps for BMC decode

GRL_PACKET_SIZE = 64


@dataclass
class GRLPacket:
    channel: int
    seq_num: int
    buf_idx: int
    overflow: bool
    timestamp_ms: int
    payload: bytes
    is_idle: bool
    raw: bytes

    @property
    def cc_line(self) -> str:
        return f"CC{self.channel + 1}"


@dataclass
class GRLVbusSample:
    voltage_count: int
    temp_count: int
    current_count: int
    power_count: int
    voltage_v: float
    temp_c: float
    current_a: float
    power_w: float
    raw: bytes


_VBUS_TELEMETRY_PREFIX = b"\xAA\xAA\xAA\xAA"


def parse_grl_packet(data: bytes) -> Optional[GRLPacket]:
    """Parse one 64-byte GRL sniffer packet. Returns None if too short."""
    if len(data) < 4:
        return None
    if len(data) < GRL_PACKET_SIZE:
        data = data + b"\x00" * (GRL_PACKET_SIZE - len(data))

    seq_field = data[0] | (data[1] << 8)
    buf_idx = seq_field & 0x07
    seq_num = (seq_field >> 3) & 0x1FF
    channel = (seq_field >> 12) & 0x1
    overflow = bool(seq_field & 0x8000)
    timestamp_ms = data[2] | (data[3] << 8)

    payload = bytes(data[4:GRL_PACKET_SIZE])
    is_idle = not any(payload)

    return GRLPacket(
        channel=channel,
        seq_num=seq_num,
        buf_idx=buf_idx,
        overflow=overflow,
        timestamp_ms=timestamp_ms,
        payload=payload,
        is_idle=is_idle,
        raw=bytes(data[:GRL_PACKET_SIZE]),
    )


def parse_grl_vbus_telemetry(data: bytes) -> Optional[GRLVbusSample]:
    """Parse a 64-byte GRL-side telemetry packet prefixed by 0xAA x4."""
    if len(data) < 4:
        return None
    if len(data) < GRL_PACKET_SIZE:
        data = data + b"\x00" * (GRL_PACKET_SIZE - len(data))
    if bytes(data[:4]) != _VBUS_TELEMETRY_PREFIX:
        return None

    payload = bytes(data[4:GRL_PACKET_SIZE])
    if len(payload) < 9:
        return None

    voltage_count = (payload[0] << 8) | payload[1]
    temp_count = ((payload[2] << 8) | payload[3]) >> 4
    current_raw = (payload[4] << 8) | payload[5]
    current_count = current_raw - 0x1_0000 if (current_raw & 0x8000) else current_raw
    power_count = (payload[6] << 16) | (payload[7] << 8) | payload[8]

    voltage_v = (3.125 * voltage_count) / 1000.0
    temp_c = 0.125 * temp_count
    current_a = 0.000244140625 * current_count
    power_w = 0.2 * 0.000244140625 * power_count

    return GRLVbusSample(
        voltage_count=voltage_count,
        temp_count=temp_count,
        current_count=current_count,
        power_count=power_count,
        voltage_v=voltage_v,
        temp_c=temp_c,
        current_a=current_a,
        power_w=power_w,
        raw=bytes(data[:GRL_PACKET_SIZE]),
    )


def update_grl_timestamp_state(state: Dict[str, Optional[int]], pkt: GRLPacket) -> int:
    """Expand the observed 16-bit millisecond timestamp into a monotonic value.

    Observed GRL captures use the overflow flag to mark that the packet timestamp
    should be interpreted relative to the last packet when the raw 16-bit timer
    wraps. This matches captures where e.g. 178 ms is followed by 6 ms with
    overflow set, meaning 184 ms on the wire.
    """
    ts16 = pkt.timestamp_ms & 0xFFFF
    last_expanded = state.get("last_expanded_ms")
    last_raw = state.get("last_raw_ms")
    offset_ms = state.get("offset_ms")

    if offset_ms is None:
        offset_ms = state.get("base_ms")
    if offset_ms is None:
        offset_ms = state.get("epoch_ms") or 0

    if last_expanded is None or last_raw is None:
        expanded_ms = ts16
        offset_ms = 0
    elif pkt.overflow:
        # Overflow means the raw value is a delta/carry indicator, not a new
        # absolute 16-bit timestamp. Preserve repeated sub-buffers at the same
        # timestamp and add only the wrapped delta when the value drops.
        if ts16 < last_raw:
            delta = ts16
        elif ts16 == last_raw:
            delta = 0
        else:
            delta = ts16 - last_raw
        expanded_ms = last_expanded + delta
        offset_ms = expanded_ms - ts16
    else:
        # The non-overflow value behaves as an absolute 16-bit reading. If it
        # moves backward without an overflow flag, re-anchor instead of adding a
        # synthetic 65.536 s wrap that breaks ordering.
        expanded_ms = offset_ms + ts16
        if expanded_ms < last_expanded:
            offset_ms = last_expanded - ts16
            expanded_ms = offset_ms + ts16

    state["epoch_ms"] = offset_ms
    state["base_ms"] = offset_ms
    state["offset_ms"] = offset_ms
    state["last_raw_ms"] = ts16
    state["last_expanded_ms"] = expanded_ms
    return expanded_ms * 1000


def format_grl_packet(pkt: GRLPacket, timestamp_us: int) -> str:
    """Return a human-readable summary line for one GRL packet."""
    of = " OVF" if pkt.overflow else ""
    idle = " IDLE" if pkt.is_idle else ""
    hex_part = " ".join(f"{b:02x}" for b in pkt.payload[:20])
    if not pkt.is_idle and len(pkt.payload) > 20:
        hex_part += " ..."
    non_zero = sum(1 for b in pkt.payload if b)
    return (
        f"[{timestamp_us} us] {pkt.cc_line} seq={pkt.seq_num} "
        f"buf={pkt.buf_idx}{of} ts={pkt.timestamp_ms}ms{idle}  "
        f"({non_zero}B non-zero): {hex_part}"
    )


def format_grl_vbus_sample(sample: GRLVbusSample, timestamp_us: int) -> str:
    return (
        f"[{timestamp_us} us] VBUS V={sample.voltage_v:.3f}V "
        f"I={sample.current_a:.6f}A P={sample.power_w:.3f}W T={sample.temp_c:.3f}C "
        f"counts=0x{sample.voltage_count:04X}/0x{sample.temp_count:03X}/"
        f"0x{sample.current_count & 0xFFFF:04X}/0x{sample.power_count:06X}"
    )


def extract_grl_direct_pd_payload(data: bytes) -> Optional[bytes]:
    """Extract a directly embedded PD payload from a 64-byte GRL packet.

    Some GRL captures store the PD header/data/CRC directly in bytes 4..63 and
    pad the remainder with zeros instead of sending CC edge timings.
    """
    if len(data) < 8:
        return None
    if len(data) < GRL_PACKET_SIZE:
        data = data + b"\x00" * (GRL_PACKET_SIZE - len(data))

    payload = bytes(data[4:GRL_PACKET_SIZE]).rstrip(b"\x00")
    if len(payload) < 6:
        return None

    header = int.from_bytes(payload[:2], "little")
    msg_type = header & 0x1F
    num_data_objs = (header >> 12) & 0x07
    spec_rev = (header >> 6) & 0x03
    if spec_rev == 3 or msg_type == 0:
        return None

    expected_len = 2 + (num_data_objs * 4) + 4
    if len(payload) != expected_len:
        return None
    return payload[:-4]


def grl_packets_to_raw_frames(raw_frames: List[RawFrame]) -> List[RawFrame]:
    """Strip the GRL 4-byte header and keep the 60-byte payload."""
    out: List[RawFrame] = []
    for fr in raw_frames:
        pkt = parse_grl_packet(fr.payload)
        if pkt is None or pkt.is_idle:
            continue
        out.append(RawFrame(
            timestamp_us=fr.timestamp_us,
            payload=pkt.payload,
            source=f"grl_{pkt.cc_line.lower()}",
        ))
    return out
