"""Packet reassembly module for GRL USB PD Sniffer.

Handles grouping and stitching of fragmented packets based on sequence number
and buffer index. Packets are streamed from the USB endpoint with:
- 9-bit sequence number (0-511)
- 3-bit buffer index (0-7)

A complete message consists of all 8 buffer indices (0-7) for a given sequence number.
This module handles out-of-order packet arrival and reassembles them correctly.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

try:
    from .decoders.grl_sniffer import GRLPacket
except ImportError:
    from decoders.grl_sniffer import GRLPacket


@dataclass
class PacketFragment:
    """Represents a single fragment of a complete message."""
    grl_packet: GRLPacket
    device_ts_us: int
    wall_ts_us: int
    received_time: float


@dataclass
class ReassembledPacket:
    """A complete message reassembled from all buffer indices."""
    channel: int
    seq_num: int
    fragments: List[PacketFragment]  # Sorted by buf_idx
    device_ts_us: int  # Timestamp from buf_idx=0
    wall_ts_us: int
    complete: bool
    fragment_count: int

    @property
    def cc_line(self) -> str:
        return f"CC{self.channel + 1}"

    def get_concatenated_payload(self) -> bytes:
        """Concatenate all fragment payloads in buf_idx order."""
        return b"".join(frag.grl_packet.payload for frag in self.fragments)


class PacketReassembler:
    """Reassembles fragmented GRL packets based on sequence number and buffer index.

    Features:
    - Handles out-of-order packet arrival
    - Groups packets by (channel, sequence_number)
    - Stitches all buf_idx (0-7) together
    - Timeout-based flushing of incomplete sequences
    - Automatic cleanup of old sequences
    """

    # Expected number of fragments per sequence (buf_idx 0-7)
    EXPECTED_FRAGMENTS = 8

    # Timeout for incomplete sequences (seconds)
    INCOMPLETE_TIMEOUT_S = 2.0

    # Maximum number of pending sequences to track
    MAX_PENDING_SEQUENCES = 100

    def __init__(self) -> None:
        # Key: (channel, seq_num), Value: dict {buf_idx: PacketFragment}
        self._pending: Dict[Tuple[int, int], Dict[int, PacketFragment]] = defaultdict(dict)
        self._stats_total_received = 0
        self._stats_reassembled = 0
        self._stats_incomplete_flushed = 0
        self._stats_duplicate_dropped = 0

    def add_packet(
        self,
        grl_packet: GRLPacket,
        device_ts_us: int,
        wall_ts_us: int,
    ) -> Optional[ReassembledPacket]:
        """Add a packet fragment and return a complete packet if ready.

        Args:
            grl_packet: Parsed GRL packet with seq_num and buf_idx
            device_ts_us: Device timestamp in microseconds
            wall_ts_us: Wall clock timestamp in microseconds

        Returns:
            ReassembledPacket if all fragments (buf_idx 0-7) are received, None otherwise
        """
        self._stats_total_received += 1

        channel = grl_packet.channel
        seq_num = grl_packet.seq_num
        buf_idx = grl_packet.buf_idx

        key = (channel, seq_num)
        fragments_dict = self._pending[key]

        # Check for duplicate buf_idx
        if buf_idx in fragments_dict:
            self._stats_duplicate_dropped += 1
            # Keep the first received fragment, discard duplicate
            return None

        # Add new fragment
        fragment = PacketFragment(
            grl_packet=grl_packet,
            device_ts_us=device_ts_us,
            wall_ts_us=wall_ts_us,
            received_time=time.monotonic(),
        )
        fragments_dict[buf_idx] = fragment

        # Check if we have all fragments (buf_idx 0-7)
        if len(fragments_dict) == self.EXPECTED_FRAGMENTS:
            # All fragments received - reassemble
            reassembled = self._reassemble(channel, seq_num, fragments_dict)
            del self._pending[key]
            self._stats_reassembled += 1
            return reassembled

        # Cleanup old sequences if too many pending
        if len(self._pending) > self.MAX_PENDING_SEQUENCES:
            self._cleanup_old_sequences()

        return None

    def flush_incomplete(self, force: bool = False) -> List[ReassembledPacket]:
        """Flush incomplete sequences that have timed out.

        Args:
            force: If True, flush all pending sequences regardless of timeout

        Returns:
            List of incomplete ReassembledPacket objects
        """
        now = time.monotonic()
        to_flush = []

        for key, fragments_dict in list(self._pending.items()):
            channel, seq_num = key

            # Check timeout
            oldest_time = min(frag.received_time for frag in fragments_dict.values())
            age = now - oldest_time

            if force or age > self.INCOMPLETE_TIMEOUT_S:
                reassembled = self._reassemble(channel, seq_num, fragments_dict)
                to_flush.append(reassembled)
                del self._pending[key]
                self._stats_incomplete_flushed += 1

        return to_flush

    def _reassemble(
        self,
        channel: int,
        seq_num: int,
        fragments_dict: Dict[int, PacketFragment],
    ) -> ReassembledPacket:
        """Reassemble fragments into a complete packet."""
        # Sort fragments by buf_idx
        sorted_fragments = [
            fragments_dict[buf_idx]
            for buf_idx in sorted(fragments_dict.keys())
        ]

        # Use timestamp from buf_idx=0 if available, otherwise use earliest
        if 0 in fragments_dict:
            device_ts_us = fragments_dict[0].device_ts_us
            wall_ts_us = fragments_dict[0].wall_ts_us
        else:
            # Fallback: use timestamp from lowest buf_idx
            first_frag = sorted_fragments[0]
            device_ts_us = first_frag.device_ts_us
            wall_ts_us = first_frag.wall_ts_us

        complete = len(fragments_dict) == self.EXPECTED_FRAGMENTS

        return ReassembledPacket(
            channel=channel,
            seq_num=seq_num,
            fragments=sorted_fragments,
            device_ts_us=device_ts_us,
            wall_ts_us=wall_ts_us,
            complete=complete,
            fragment_count=len(fragments_dict),
        )

    def _cleanup_old_sequences(self) -> None:
        """Remove oldest incomplete sequences when buffer is full."""
        if not self._pending:
            return

        # Find oldest sequence
        oldest_key = None
        oldest_time = float('inf')

        for key, fragments_dict in self._pending.items():
            min_time = min(frag.received_time for frag in fragments_dict.values())
            if min_time < oldest_time:
                oldest_time = min_time
                oldest_key = key

        if oldest_key:
            del self._pending[oldest_key]
            self._stats_incomplete_flushed += 1

    def get_stats(self) -> Dict[str, int]:
        """Return reassembly statistics."""
        return {
            "total_received": self._stats_total_received,
            "reassembled": self._stats_reassembled,
            "incomplete_flushed": self._stats_incomplete_flushed,
            "duplicate_dropped": self._stats_duplicate_dropped,
            "pending_sequences": len(self._pending),
        }

    def reset(self) -> None:
        """Clear all pending sequences and reset statistics."""
        self._pending.clear()
        self._stats_total_received = 0
        self._stats_reassembled = 0
        self._stats_incomplete_flushed = 0
        self._stats_duplicate_dropped = 0

    def get_pending_info(self) -> List[Dict]:
        """Get information about pending incomplete sequences."""
        now = time.monotonic()
        info = []

        for (channel, seq_num), fragments_dict in self._pending.items():
            buf_indices = sorted(fragments_dict.keys())
            oldest_time = min(frag.received_time for frag in fragments_dict.values())
            age = now - oldest_time
            missing = [i for i in range(self.EXPECTED_FRAGMENTS) if i not in buf_indices]

            info.append({
                "channel": channel,
                "cc_line": f"CC{channel + 1}",
                "seq_num": seq_num,
                "received_buf_idx": buf_indices,
                "missing_buf_idx": missing,
                "fragment_count": len(fragments_dict),
                "age_seconds": age,
            })

        return info
