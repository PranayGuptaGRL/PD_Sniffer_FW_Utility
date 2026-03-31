"""Edge-timestamp BMC decoder for GRL USB C PD Sniffer.

Sigrok-style approach: fixed classification thresholds, no dynamic estimation.

The GRL device streams 8-bit edge timestamps of CC line transitions.
Each payload byte is a wrapping counter value; the delta between consecutive
bytes (mod 256) gives the inter-edge interval in timer ticks.

Pipeline
--------
1. Edge bytes  -> deltas (mod 256)
2. Deltas      -> classify: 3/4/5 -> half-UI (normalize to 4),
                            7/8/9 -> full-UI (normalize to 8)
3. BMC decode  -> one full-UI (8) = bit 0  (no mid-bit transition),
                  two consecutive half-UIs (4+4) = bit 1  (mid-bit transition)
4. BMC bits    -> 4b5b symbols (5 bits each, LSB-first per USB PD spec)
5. Symbols     -> preamble / SOP detection -> data bytes -> CRC32 verify
6. Verified    -> RawFrame (payload = raw PD header + data objects, no CRC)

Framing: once a preamble is detected, everything until EOP is treated as
a single PD packet.  Packets can span multiple 64-byte USB chunks -- a
rolling buffer accumulates edges until a complete preamble->EOP frame
can be decoded.

Usage
-----
    decoder = GRLBMCDecoder()
    # inside live capture loop, for each GRL packet with edge data:
    frames = decoder.feed(edge_bytes, timestamp_us)
    # frames -> list[RawFrame] ready for PDDecoder.decode()
    decoder.reset()  # on session start / stop
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..models import RawFrame

# ── USB PD 4b5b / K-code constants ────────────────────────────────────────────

_4B5B_DEC: dict[int, int] = {
    0b11110: 0x0,  0b01001: 0x1,  0b10100: 0x2,  0b10101: 0x3,
    0b01010: 0x4,  0b01011: 0x5,  0b01110: 0x6,  0b01111: 0x7,
    0b10010: 0x8,  0b10011: 0x9,  0b10110: 0xA,  0b10111: 0xB,
    0b11010: 0xC,  0b11011: 0xD,  0b11100: 0xE,  0b11101: 0xF,
}

_K_SYNC1 = 0b11000   # 24
_K_SYNC2 = 0b10001   # 17
_K_SYNC3 = 0b00110   # 6
_K_RST1  = 0b00111   # 7
_K_RST2  = 0b11001   # 25
_K_EOP   = 0b01101   # 13

_KCODE_NAMES: dict[int, str] = {
    _K_SYNC1: "SYNC-1",
    _K_SYNC2: "SYNC-2",
    _K_SYNC3: "SYNC-3",
    _K_RST1:  "RST-1",
    _K_RST2:  "RST-2",
    _K_EOP:   "EOP",
}

_SOP_TABLE: dict[tuple, str] = {
    (_K_SYNC1, _K_SYNC1, _K_SYNC1, _K_SYNC2): "SOP",
    (_K_SYNC1, _K_SYNC1, _K_SYNC3, _K_SYNC2): "SOP'",
    (_K_SYNC1, _K_SYNC3, _K_SYNC1, _K_SYNC3): "SOP''",
    (_K_RST1,  _K_RST1,  _K_RST1,  _K_RST2):  "HARD_RST",
    (_K_RST1,  _K_SYNC1, _K_RST1,  _K_SYNC3): "CABLE_RST",
}

# Minimum alternating preamble bits before SOP (real spec = 64, relaxed for
# truncated captures / chunk-boundary starts).
_MIN_PRE_BITS = 6

# Max 4b5b data nibbles to read after SOP before giving up.
_MAX_NIBBLES = (34 + 36) * 2

# Gap threshold: delta >= this means inter-message silence.
_GAP_THRESHOLD = 24

# Minimum edges before attempting decode.
_MIN_EDGE_BYTES = 50


@dataclass
class _FrameCandidate:
    sop_name: str
    payload: bytes
    start_bit_pos: int
    end_bit_pos: int
    preamble_bits: int
    crc_mode: str
    nibble_count: int
    found_eop: bool


@dataclass
class _EdgeChunk:
    device_timestamp_us: int
    wall_timestamp_us: int
    start_edge_abs: int
    end_edge_abs: int


# ── CRC32 ──────────────────────────────────────────────────────────────────────

def _crc32_pd(data: bytes) -> int:
    """USB PD CRC32 (polynomial 0x04C11DB7, reflected = 0xEDB88320)."""
    crc = 0xFFFF_FFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xEDB8_8320 if (crc & 1) else (crc >> 1)
    return crc ^ 0xFFFF_FFFF


# ── Edge timestamps -> BMC bits (sigrok-style fixed classification) ────────────

def _classify_delta(delta: int) -> tuple[str, int]:
    """Classify an inter-edge delta using fixed thresholds.

    - 3/4/5  -> half-UI, normalize to 4
    - 7/8/9  -> full-UI, normalize to 8
    - >= 24  -> gap  (inter-message silence)
    - 0      -> zero (skip)
    - others -> noise (skip)
    """
    if delta <= 0:
        return ("zero", 0)
    if delta >= _GAP_THRESHOLD:
        return ("gap", delta)
    if 3 <= delta <= 5:
        return ("half", 4)
    if 7 <= delta <= 9:
        return ("full", 8)
    return ("noise", delta)


def _edges_to_bmc_bits(
    edges: bytes,
    debug: Optional[io.StringIO] = None,
) -> List[List[int]]:
    """Convert edge-timestamp bytes into BMC bit segments.

    Each segment is a continuous run of bits between gaps (inter-message
    silence).  Within a segment, the full preamble->SOP->data->EOP of one or
    more PD packets lives.

    BMC decode rules (standard USB PD biphase mark coding):
      - full-UI  (delta 7/8/9, normalized 8)  -> bit 0  (no mid-bit transition)
      - half-UI  (delta 3/4/5, normalized 4)  -> pending;
        two consecutive half-UIs              -> bit 1  (mid-bit transition)
    """
    if len(edges) < 4:
        return []

    deltas = [(edges[i + 1] - edges[i]) & 0xFF for i in range(len(edges) - 1)]

    segments: List[List[int]] = []
    current_bits: List[int] = []
    pending_half = False

    if debug:
        debug.write("\n" + "=" * 72 + "\n")
        debug.write("STEP 2: COMPUTE DELTAS  (next_byte - prev_byte) mod 256\n")
        debug.write("        Then classify each delta:\n")
        debug.write("          delta 3,4,5  -> half-UI  (normalize to 4)\n")
        debug.write("          delta 7,8,9  -> full-UI  (normalize to 8)\n")
        debug.write("          delta >= 24  -> GAP      (silence between messages)\n")
        debug.write("          other        -> noise    (skipped, no state reset)\n")
        debug.write("=" * 72 + "\n")

    for idx, delta in enumerate(deltas):
        if delta == 0:
            continue
        kind, normalized = _classify_delta(delta)

        if debug:
            edge_a = edges[idx]
            edge_b = edges[idx + 1]
            if kind == "half":
                bit_info = "(pending...)" if not pending_half else "-> bit 1 (mid-bit transition)"
                debug.write(
                    f"  edge[{idx:3d}]=0x{edge_a:02X}  edge[{idx+1:3d}]=0x{edge_b:02X}"
                    f"  delta={delta:3d}  -> half-UI (norm 4)  {bit_info}\n"
                )
            elif kind == "full":
                debug.write(
                    f"  edge[{idx:3d}]=0x{edge_a:02X}  edge[{idx+1:3d}]=0x{edge_b:02X}"
                    f"  delta={delta:3d}  -> full-UI (norm 8)  -> bit 0 (no mid-bit transition)\n"
                )
            elif kind == "gap":
                debug.write(
                    f"  edge[{idx:3d}]=0x{edge_a:02X}  edge[{idx+1:3d}]=0x{edge_b:02X}"
                    f"  delta={delta:3d}  -> GAP (message boundary)\n"
                )
            elif kind == "noise":
                debug.write(
                    f"  edge[{idx:3d}]=0x{edge_a:02X}  edge[{idx+1:3d}]=0x{edge_b:02X}"
                    f"  delta={delta:3d}  -> noise (skipped)\n"
                )

        if kind == "gap":
            if current_bits:
                segments.append(current_bits)
                current_bits = []
            pending_half = False
            continue

        if kind == "half":
            if pending_half:
                # Two consecutive half-UIs = mid-bit transition = bit 1
                current_bits.append(1)
                pending_half = False
            else:
                pending_half = True
            continue

        if kind == "full":
            # One full-UI = no mid-bit transition = bit 0
            current_bits.append(0)
            pending_half = False
            continue

        # noise -- skip without resetting pending (glitch edges shouldn't
        # break BMC state, same as sigrok behavior)
        continue

    if current_bits:
        segments.append(current_bits)

    if debug:
        debug.write("\n" + "=" * 72 + "\n")
        debug.write("STEP 3: BMC BIT SEGMENTS  (separated by gaps)\n")
        debug.write("=" * 72 + "\n")
        if not segments:
            debug.write("  (no bit segments found)\n")
        for i, bits in enumerate(segments):
            # Format bits in groups of 5 for readability (matches 4b5b symbol size)
            bit_str = ""
            for j, b in enumerate(bits):
                if j > 0 and j % 5 == 0:
                    bit_str += " "
                bit_str += str(b)
            debug.write(f"  Segment {i}: {len(bits)} bits\n")
            # Show in rows of 50 bits (10 groups of 5)
            for row_start in range(0, len(bits), 50):
                row_bits = bits[row_start:row_start + 50]
                row_str = ""
                for j, b in enumerate(row_bits):
                    if j > 0 and j % 5 == 0:
                        row_str += " "
                    row_str += str(b)
                debug.write(f"    bit[{row_start:4d}]: {row_str}\n")

    return segments


def _read5(bits: List[int], pos: int) -> Optional[int]:
    """Read a 5-bit symbol at pos (LSB first). Returns None if out of range."""
    if pos + 5 > len(bits):
        return None
    v = 0
    for i in range(5):
        v |= bits[pos + i] << i
    return v


def _symbol_name(code: int) -> str:
    """Human-readable name for a 5-bit symbol."""
    kname = _KCODE_NAMES.get(code)
    if kname:
        return kname
    nib = _4B5B_DEC.get(code)
    if nib is not None:
        return f"0x{nib:X}"
    return f"?({code:05b})"


def _extract_crc_payload_from_nibbles(nibbles: List[int]) -> Optional[Tuple[bytes, int]]:
    """Try CRC-checked payloads by trimming trailing nibbles."""
    if len(nibbles) < 8:
        return None
    effective = nibbles if len(nibbles) % 2 == 0 else nibbles[:-1]
    for cut in range(len(effective), 7, -2):
        frame_bytes = bytearray()
        for i in range(0, cut, 2):
            frame_bytes.append(effective[i] | (effective[i + 1] << 4))
        if len(frame_bytes) < 6:
            continue
        payload  = bytes(frame_bytes[:-4])
        recv_crc = int.from_bytes(frame_bytes[-4:], "little")
        calc_crc = _crc32_pd(payload)
        if calc_crc == recv_crc:
            return payload, cut
    return None


def _payload_matches_header_length(payload: bytes) -> bool:
    """Reject CRC-valid frames whose byte count disagrees with the PD header."""
    if len(payload) < 2:
        return False
    header = int.from_bytes(payload[:2], "little")
    num_data_objs = (header >> 12) & 0x07
    extended = (header >> 15) & 0x01
    if extended:
        return False
    expected_len = 2 + (num_data_objs * 4)
    return len(payload) == expected_len


# ── Frame finder ──────────────────────────────────────────────────────────────

def find_pd_frames(
    bits: List[int],
    debug: Optional[io.StringIO] = None,
) -> List[_FrameCandidate]:
    """Scan a BMC bit list for valid USB PD frames.

    Logic: scan for preamble (alternating 1010...) followed by SOP k-codes.
    Once SOP is found, read all 4b5b symbols until EOP -- that entire span is
    one PD packet.  Then CRC32-verify the assembled bytes.
    """
    results: List[_FrameCandidate] = []
    n = len(bits)
    pos = 0

    while pos <= n - 22:
        # ── 1. Look for SOP (4 consecutive k-codes) ──────────────────────
        k: List[int] = []
        p = pos
        for _ in range(4):
            code = _read5(bits, p)
            if code is None:
                k = []
                break
            k.append(code)
            p += 5

        if len(k) < 4:
            break

        sop_name = _SOP_TABLE.get(tuple(k))
        if sop_name is None:
            pos += 1
            continue

        # ── 2. Verify preamble before SOP ────────────────────────────────
        pre = 0
        q = pos - 1
        while q > 0 and bits[q] != bits[q - 1]:
            pre += 1
            q -= 1
        if pre < _MIN_PRE_BITS:
            pos += 1
            continue

        if debug:
            debug.write("\n" + "=" * 72 + "\n")
            debug.write("STEP 4: FOUND PREAMBLE + SOP\n")
            debug.write("=" * 72 + "\n")
            pre_start = pos - pre
            pre_bits_str = " ".join(str(bits[i]) for i in range(max(0, pre_start), pos))
            debug.write(f"  Preamble: {pre} alternating bits at bit[{max(0,pre_start)}..{pos-1}]\n")
            debug.write(f"    {pre_bits_str}\n")
            sop_codes_str = "  ".join(
                f"{_symbol_name(c)} ({c:05b})" for c in k
            )
            debug.write(f"  SOP = \"{sop_name}\" at bit[{pos}..{p-1}]\n")
            debug.write(f"    K-codes: {sop_codes_str}\n")
            debug.write(
                "  Now reading data symbols until EOP...\n"
                "  (Everything from preamble to EOP = one PD packet)\n"
            )

        # ── 3. Read 4b5b data symbols until EOP ─────────────────────────
        #    This is the core "preamble to EOP = one packet" rule.
        dp = p
        nibbles: List[int] = []
        invalid_sym = False
        found_eop = False

        if debug:
            debug.write("\n" + "=" * 72 + "\n")
            debug.write("STEP 5: 4b5b SYMBOL DECODE  (5 bits -> 1 nibble, LSB-first)\n")
            debug.write("=" * 72 + "\n")

        while dp + 5 <= n and len(nibbles) < _MAX_NIBBLES:
            code = _read5(bits, dp)
            if code is None:
                break

            if code == _K_EOP:
                if debug:
                    sym_bits = " ".join(str(bits[dp + i]) for i in range(5))
                    debug.write(
                        f"  bit[{dp:4d}..{dp+4}]: {sym_bits}"
                        f"  -> code {code:05b} -> *** EOP (End Of Packet) ***\n"
                    )
                dp += 5
                found_eop = True
                break

            nib = _4B5B_DEC.get(code)
            if nib is None:
                kname = _KCODE_NAMES.get(code)
                if debug:
                    sym_bits = " ".join(str(bits[dp + i]) for i in range(5))
                    label = kname if kname else "INVALID"
                    debug.write(
                        f"  bit[{dp:4d}..{dp+4}]: {sym_bits}"
                        f"  -> code {code:05b} -> {label} (not a data nibble)\n"
                    )
                invalid_sym = True
                dp += 5
                break

            if debug:
                sym_bits = " ".join(str(bits[dp + i]) for i in range(5))
                debug.write(
                    f"  bit[{dp:4d}..{dp+4}]: {sym_bits}"
                    f"  -> code {code:05b} -> nibble 0x{nib:X}\n"
                )

            nibbles.append(nib)
            dp += 5

        if not found_eop and not invalid_sym:
            # We ran out of bits before finding EOP -- the packet likely
            # spans more USB chunks that haven't arrived yet.  Skip for now;
            # we'll retry when more edges are buffered.
            if debug:
                debug.write(
                    f"  *** No EOP found yet (ran out of bits at {dp}).\n"
                    f"      Packet may span more USB chunks -- waiting for more data.\n"
                )
            pos += 1
            continue

        if invalid_sym:
            pos += 1
            continue

        if len(nibbles) < 8:   # min 4 bytes (2B header + 2B padding or CRC fragment)
            pos += 1
            continue

        # ── 4. Nibbles -> bytes (low nibble first per USB PD spec) ────────
        if debug:
            debug.write("\n" + "=" * 72 + "\n")
            debug.write("STEP 6: ASSEMBLE BYTES  (low nibble first, per USB PD spec)\n")
            debug.write("        Each pair of nibbles -> one byte:\n")
            debug.write("        byte = low_nibble | (high_nibble << 4)\n")
            debug.write("=" * 72 + "\n")

        frame_bytes = bytearray()
        for i in range(0, len(nibbles) - 1, 2):
            lo = nibbles[i]
            hi = nibbles[i + 1]
            byte_val = lo | (hi << 4)
            frame_bytes.append(byte_val)
            if debug:
                debug.write(
                    f"  nibble[{i}]=0x{lo:X}  nibble[{i+1}]=0x{hi:X}"
                    f"  -> byte 0x{byte_val:02X}\n"
                )

        if len(frame_bytes) < 6:   # 2B header + 4B CRC minimum
            pos += 1
            continue

        # ── 5. CRC32 check ───────────────────────────────────────────────
        payload  = bytes(frame_bytes[:-4])
        recv_crc = int.from_bytes(frame_bytes[-4:], "little")
        calc_crc = _crc32_pd(payload)
        crc_ok = calc_crc == recv_crc

        if debug:
            debug.write("\n" + "=" * 72 + "\n")
            debug.write("STEP 7: CRC32 VERIFICATION\n")
            debug.write("=" * 72 + "\n")
            debug.write(f"  All bytes (hex): {frame_bytes.hex(' ')}\n")
            debug.write(f"  Payload  ({len(payload)} bytes): {payload.hex(' ')}\n")
            debug.write(f"  CRC bytes (last 4):  {frame_bytes[-4:].hex(' ')}\n")
            debug.write(f"  CRC received:   0x{recv_crc:08X}\n")
            debug.write(f"  CRC calculated: 0x{calc_crc:08X}\n")
            if crc_ok:
                debug.write("  >>> CRC PASS -- Valid PD packet! <<<\n")
            else:
                debug.write("  >>> CRC FAIL <<<\n")

        if crc_ok:
            if not _payload_matches_header_length(payload):
                if debug:
                    header = int.from_bytes(payload[:2], "little")
                    n_obj = (header >> 12) & 0x07
                    expected = 2 + n_obj * 4
                    debug.write(
                        f"  Rejecting: payload {len(payload)} bytes but header says"
                        f" {n_obj} data objects -> expected {expected} bytes\n"
                    )
                pos = dp
                continue
            results.append(_FrameCandidate(
                sop_name=sop_name,
                payload=payload,
                start_bit_pos=pos,
                end_bit_pos=dp,
                preamble_bits=pre,
                crc_mode="direct",
                nibble_count=len(nibbles),
                found_eop=found_eop,
            ))
            if debug:
                header = int.from_bytes(payload[:2], "little")
                n_obj = (header >> 12) & 0x07
                msg_type = header & 0x1F
                debug.write(f"\n  DECODED PD HEADER:\n")
                debug.write(f"    Raw header word: 0x{header:04X}\n")
                debug.write(f"    Message type:    {msg_type}\n")
                debug.write(f"    Data objects:    {n_obj}\n")
                if n_obj > 0:
                    for obj_i in range(n_obj):
                        off = 2 + obj_i * 4
                        obj_word = int.from_bytes(payload[off:off+4], "little")
                        debug.write(f"    Object[{obj_i}]:       0x{obj_word:08X}\n")
            pos = dp
        else:
            # Try trimming trailing nibbles to find a CRC match
            salvage = _extract_crc_payload_from_nibbles(nibbles)
            if salvage is not None:
                salvage_payload, salvage_cut = salvage
                if not _payload_matches_header_length(salvage_payload):
                    pos += 1
                    continue
                salvage_end = p + (salvage_cut * 5)
                results.append(_FrameCandidate(
                    sop_name=sop_name,
                    payload=salvage_payload,
                    start_bit_pos=pos,
                    end_bit_pos=salvage_end,
                    preamble_bits=pre,
                    crc_mode="trimmed",
                    nibble_count=salvage_cut,
                    found_eop=found_eop,
                ))
                if debug:
                    debug.write(
                        f"  (CRC passed after trimming to {salvage_cut} nibbles,"
                        f" payload={salvage_payload.hex(' ')})\n"
                    )
                pos = salvage_end
            else:
                pos += 1

    return results


# ── Streaming decoder ──────────────────────────────────────────────────────────

class GRLBMCDecoder:
    """Streaming edge-timestamp BMC decoder for the GRL USB C PD Sniffer.

    Sigrok-style: fixed classification (3/4/5->half, 7/8/9->full), no dynamic
    estimation.  Packets can arrive across multiple 64-byte USB chunks -- a
    rolling buffer accumulates edges until a complete preamble->EOP frame can
    be decoded.

    Call feed() for each GRL packet that contains edge data.
    Frames that pass CRC32 are returned as RawFrame objects ready for
    PDDecoder.decode().
    """

    # Rolling buffer size in edge bytes.
    WINDOW = 2000

    # Stale timeout: if no data for 500ms, clear buffer (new PD session).
    _STALE_TIMEOUT_US = 500_000

    def __init__(self, debug_file: Optional[io.TextIOBase] = None) -> None:
        self._buf: bytearray = bytearray()
        self._debug_file = debug_file
        self._chunks: List[_EdgeChunk] = []
        self._next_edge_abs: int = 0
        self._seen: set[bytes] = set()
        self._feed_count: int = 0
        self._last_wall_ts: int = 0

    def _trim_buffer(self) -> None:
        if len(self._buf) <= self.WINDOW:
            return
        drop = len(self._buf) - self.WINDOW
        self._buf = self._buf[-self.WINDOW:]
        cutoff = self._next_edge_abs - self.WINDOW
        self._chunks = [c for c in self._chunks if c.end_edge_abs > cutoff]

    def feed(
        self,
        edge_bytes: bytes,
        timestamp_us: int,
        wall_timestamp_us: Optional[int] = None,
        debug_context: Optional[str] = None,
    ) -> List[RawFrame]:
        """Feed one packet of edge-timestamp bytes. Returns newly decoded PD frames."""
        if not edge_bytes:
            return []

        # Skip idle packets (all zeros)
        if not any(edge_bytes):
            return []

        ref_ts = wall_timestamp_us if wall_timestamp_us is not None else timestamp_us

        # Clear stale buffer
        if self._last_wall_ts and (ref_ts - self._last_wall_ts) > self._STALE_TIMEOUT_US:
            self.reset()
        self._last_wall_ts = ref_ts

        # Track chunk for timestamp attribution
        chunk = _EdgeChunk(
            device_timestamp_us=timestamp_us,
            wall_timestamp_us=ref_ts,
            start_edge_abs=self._next_edge_abs,
            end_edge_abs=self._next_edge_abs + len(edge_bytes),
        )
        self._next_edge_abs = chunk.end_edge_abs
        self._chunks.append(chunk)

        # Append to rolling buffer
        self._buf.extend(edge_bytes)
        self._trim_buffer()

        # ── Debug: STEP 1 -- show the raw bytes arriving from USB endpoint ─
        dbg = None
        if self._debug_file:
            dbg = io.StringIO()
            dbg.write("\n" + "#" * 72 + "\n")
            dbg.write(f"# NEW USB CHUNK RECEIVED  (feed #{self._feed_count + 1})\n")
            dbg.write(f"#   timestamp = {timestamp_us} us\n")
            dbg.write(f"#   buffer now holds {len(self._buf)} edge bytes total\n")
            if debug_context:
                dbg.write(f"#   context: {debug_context.rstrip()}\n")
            dbg.write("#" * 72 + "\n")

            dbg.write("\n" + "=" * 72 + "\n")
            dbg.write("STEP 1: RAW EDGE BYTES FROM USB ENDPOINT\n")
            dbg.write("        Each byte is an 8-bit wrapping counter value.\n")
            dbg.write("        The difference between consecutive bytes gives the\n")
            dbg.write("        time between CC line transitions (edges).\n")
            dbg.write("=" * 72 + "\n")
            # Show hex bytes in rows of 16
            for row in range(0, len(edge_bytes), 16):
                chunk_slice = edge_bytes[row:row + 16]
                hex_str = " ".join(f"{b:02X}" for b in chunk_slice)
                dbg.write(f"  byte[{row:3d}..{row + len(chunk_slice) - 1:3d}]: {hex_str}\n")

        if len(self._buf) < _MIN_EDGE_BYTES:
            if dbg and self._debug_file:
                dbg.write(
                    f"\n  Buffer has {len(self._buf)} edges, need at least"
                    f" {_MIN_EDGE_BYTES}. Waiting for more USB chunks...\n"
                )
                self._debug_file.write(dbg.getvalue())
            return []

        # Single-pass decode on entire rolling buffer
        bit_segments = _edges_to_bmc_bits(bytes(self._buf), debug=dbg)

        frames: List[RawFrame] = []
        for seg_idx, bits in enumerate(bit_segments):
            if len(bits) < 24:
                continue
            candidates = find_pd_frames(bits, debug=dbg)
            for candidate in candidates:
                if candidate.payload in self._seen:
                    if dbg:
                        dbg.write(
                            f"\n  (Duplicate frame skipped:"
                            f" {candidate.payload.hex(' ')})\n"
                        )
                    continue
                self._seen.add(candidate.payload)
                frame = RawFrame(
                    timestamp_us=timestamp_us,
                    payload=candidate.payload,
                    source=f"grl_bmc_{candidate.sop_name}",
                    metadata={
                        "decoder": "grl_bmc",
                        "crc_mode": candidate.crc_mode,
                        "preamble_bits": candidate.preamble_bits,
                    },
                )
                frames.append(frame)

        if dbg and self._debug_file:
            if frames:
                dbg.write("\n" + "=" * 72 + "\n")
                dbg.write("RESULT: DECODED PD FRAMES FROM THIS FEED\n")
                dbg.write("=" * 72 + "\n")
                for i, f in enumerate(frames):
                    dbg.write(
                        f"  Frame {i}: {f.source}"
                        f"  payload={f.payload.hex(' ')}"
                        f"  crc={f.metadata['crc_mode']}\n"
                    )
            else:
                dbg.write("\n  (No complete PD frames decoded in this feed)\n")
            self._debug_file.write(dbg.getvalue())

        # Periodically clear dedup cache
        self._feed_count += 1
        if self._feed_count > 200:
            self._seen.clear()
            self._feed_count = 0

        return frames

    def reset(self) -> None:
        """Clear all state. Call at capture session start / stop."""
        self._buf = bytearray()
        self._chunks = []
        self._next_edge_abs = 0
        self._seen.clear()
        self._feed_count = 0
        self._last_wall_ts = 0

    def reset_stream(self) -> None:
        """Clear only the rolling edge stream; keep dedup state."""
        self._buf = bytearray()
        self._chunks = []
        self._next_edge_abs = 0
        self._last_wall_ts = 0
