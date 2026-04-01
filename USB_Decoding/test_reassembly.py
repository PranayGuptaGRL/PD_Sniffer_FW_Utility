#!/usr/bin/env python3
"""Test script for packet reassembly logic.

This script simulates packet arrival scenarios to verify the reassembly logic works correctly.
"""

import sys
from pathlib import Path

# Add the usb_pd_decoder directory to the path
sys.path.insert(0, str(Path(__file__).parent / "usb_pd_decoder"))

from packet_reassembly import PacketReassembler, PacketFragment, ReassembledPacket
from decoders.grl_sniffer import GRLPacket


def create_test_packet(channel, seq_num, buf_idx, payload=None):
    """Create a test GRLPacket for testing."""
    if payload is None:
        payload = bytes([buf_idx] * 60)  # Unique payload based on buf_idx

    return GRLPacket(
        channel=channel,
        seq_num=seq_num,
        buf_idx=buf_idx,
        overflow=False,
        timestamp_ms=1000 + buf_idx,
        payload=payload,
        is_idle=False,
        raw=b"\x00" * 64
    )


def test_in_order_reassembly():
    """Test: Packets arrive in order (buf_idx 0-7)."""
    print("\n" + "="*60)
    print("TEST 1: In-Order Reassembly")
    print("="*60)

    reassembler = PacketReassembler()
    channel = 0
    seq_num = 10

    # Send packets in order
    for buf_idx in range(8):
        pkt = create_test_packet(channel, seq_num, buf_idx)
        result = reassembler.add_packet(pkt, 1000 + buf_idx, 1000 + buf_idx)

        if buf_idx < 7:
            assert result is None, f"Should return None for buf_idx={buf_idx}"
            print(f"  ✓ Fragment {buf_idx}/7 buffered")
        else:
            assert result is not None, "Should return complete packet for buf_idx=7"
            assert result.complete, "Packet should be marked complete"
            assert result.fragment_count == 8, "Should have 8 fragments"
            print(f"  ✓ Fragment {buf_idx}/7 buffered")
            print(f"  ✅ COMPLETE: Reassembled seq={result.seq_num} with {result.fragment_count} fragments")

    # Verify payload concatenation
    payload = result.get_concatenated_payload()
    assert len(payload) == 60 * 8, "Concatenated payload should be 480 bytes"

    # Verify order (each fragment has unique payload)
    for i in range(8):
        fragment_payload = payload[i*60:(i+1)*60]
        assert fragment_payload[0] == i, f"Fragment {i} in wrong position"

    print("  ✓ Payload concatenation correct")
    print("  ✅ TEST PASSED\n")


def test_out_of_order_reassembly():
    """Test: Packets arrive out of order."""
    print("\n" + "="*60)
    print("TEST 2: Out-of-Order Reassembly")
    print("="*60)

    reassembler = PacketReassembler()
    channel = 0
    seq_num = 20

    # Send packets in scrambled order
    order = [3, 0, 7, 2, 5, 1, 6, 4]
    print(f"  Sending order: {order}")

    for i, buf_idx in enumerate(order):
        pkt = create_test_packet(channel, seq_num, buf_idx)
        result = reassembler.add_packet(pkt, 2000 + i, 2000 + i)

        if i < 7:
            assert result is None, f"Should return None for fragment {i}"
            print(f"  ✓ Fragment buf_idx={buf_idx} buffered ({i+1}/8)")
        else:
            assert result is not None, "Should return complete packet"
            assert result.complete, "Packet should be marked complete"
            print(f"  ✓ Fragment buf_idx={buf_idx} buffered ({i+1}/8)")
            print(f"  ✅ COMPLETE: Reassembled seq={result.seq_num}")

    # Verify fragments are sorted by buf_idx
    for i, frag in enumerate(result.fragments):
        assert frag.grl_packet.buf_idx == i, f"Fragment {i} not in correct position"

    print("  ✓ Fragments sorted correctly (0-7)")
    print("  ✅ TEST PASSED\n")


def test_duplicate_handling():
    """Test: Duplicate buf_idx packets are rejected."""
    print("\n" + "="*60)
    print("TEST 3: Duplicate Packet Handling")
    print("="*60)

    reassembler = PacketReassembler()
    channel = 0
    seq_num = 30

    # Send buf_idx=0 twice
    pkt1 = create_test_packet(channel, seq_num, 0, payload=b"A" * 60)
    pkt2 = create_test_packet(channel, seq_num, 0, payload=b"B" * 60)  # Different payload

    result1 = reassembler.add_packet(pkt1, 3000, 3000)
    assert result1 is None, "First packet should be buffered"
    print("  ✓ First buf_idx=0 accepted")

    result2 = reassembler.add_packet(pkt2, 3001, 3001)
    assert result2 is None, "Duplicate should be rejected silently"
    print("  ✓ Duplicate buf_idx=0 rejected")

    stats = reassembler.get_stats()
    assert stats['duplicate_dropped'] == 1, "Should have 1 duplicate"
    print(f"  ✓ Stats show {stats['duplicate_dropped']} duplicate dropped")
    print("  ✅ TEST PASSED\n")


def test_timeout_flush():
    """Test: Incomplete sequences are flushed on timeout."""
    print("\n" + "="*60)
    print("TEST 4: Timeout-Based Flush")
    print("="*60)

    reassembler = PacketReassembler()
    reassembler.INCOMPLETE_TIMEOUT_S = 0.1  # Short timeout for testing
    channel = 0
    seq_num = 40

    # Send only 3 fragments
    for buf_idx in [0, 1, 2]:
        pkt = create_test_packet(channel, seq_num, buf_idx)
        result = reassembler.add_packet(pkt, 4000 + buf_idx, 4000 + buf_idx)
        assert result is None, "Should buffer incomplete packet"
        print(f"  ✓ Fragment {buf_idx} buffered")

    print("  ⏳ Waiting for timeout...")
    import time
    time.sleep(0.15)  # Wait for timeout

    # Flush incomplete
    incomplete = reassembler.flush_incomplete()
    assert len(incomplete) == 1, "Should have 1 incomplete packet"
    assert incomplete[0].fragment_count == 3, "Should have 3 fragments"
    assert not incomplete[0].complete, "Should be marked incomplete"
    print(f"  ⚠️  INCOMPLETE: Flushed seq={incomplete[0].seq_num} with {incomplete[0].fragment_count}/8 fragments")

    stats = reassembler.get_stats()
    assert stats['incomplete_flushed'] == 1, "Should show 1 incomplete flush"
    print(f"  ✓ Stats show {stats['incomplete_flushed']} incomplete flush")
    print("  ✅ TEST PASSED\n")


def test_multiple_sequences():
    """Test: Multiple sequences can be tracked simultaneously."""
    print("\n" + "="*60)
    print("TEST 5: Multiple Concurrent Sequences")
    print("="*60)

    reassembler = PacketReassembler()
    channel = 0

    # Interleave fragments from 3 different sequences
    print("  Interleaving fragments from seq=50, 51, 52...")

    # seq=50: buf_idx 0,1,2
    for buf_idx in [0, 1, 2]:
        pkt = create_test_packet(channel, 50, buf_idx)
        reassembler.add_packet(pkt, 5000, 5000)

    # seq=51: buf_idx 0,1
    for buf_idx in [0, 1]:
        pkt = create_test_packet(channel, 51, buf_idx)
        reassembler.add_packet(pkt, 5100, 5100)

    # seq=52: buf_idx 0,1,2,3
    for buf_idx in [0, 1, 2, 3]:
        pkt = create_test_packet(channel, 52, buf_idx)
        reassembler.add_packet(pkt, 5200, 5200)

    pending = reassembler.get_pending_info()
    assert len(pending) == 3, "Should have 3 pending sequences"
    print(f"  ✓ Tracking {len(pending)} sequences simultaneously")

    # Complete seq=50
    for buf_idx in [3, 4, 5, 6, 7]:
        pkt = create_test_packet(channel, 50, buf_idx)
        result = reassembler.add_packet(pkt, 5000 + buf_idx, 5000 + buf_idx)
        if buf_idx == 7:
            assert result is not None, "seq=50 should complete"
            print(f"  ✅ seq=50 completed")

    pending = reassembler.get_pending_info()
    assert len(pending) == 2, "Should have 2 pending sequences now"
    print(f"  ✓ {len(pending)} sequences still pending")
    print("  ✅ TEST PASSED\n")


def test_statistics():
    """Test: Statistics tracking works correctly."""
    print("\n" + "="*60)
    print("TEST 6: Statistics Tracking")
    print("="*60)

    reassembler = PacketReassembler()
    channel = 0

    # Complete 2 sequences
    for seq in [60, 61]:
        for buf_idx in range(8):
            pkt = create_test_packet(channel, seq, buf_idx)
            reassembler.add_packet(pkt, 6000 + seq * 100 + buf_idx, 6000)

    # Add duplicate
    pkt = create_test_packet(channel, 62, 0)
    reassembler.add_packet(pkt, 6200, 6200)
    reassembler.add_packet(pkt, 6201, 6201)  # Duplicate

    stats = reassembler.get_stats()
    print(f"  Stats: {stats}")

    assert stats['total_received'] == 17, f"Expected 17 received, got {stats['total_received']}"
    assert stats['reassembled'] == 2, f"Expected 2 reassembled, got {stats['reassembled']}"
    assert stats['duplicate_dropped'] == 1, f"Expected 1 duplicate, got {stats['duplicate_dropped']}"
    assert stats['pending_sequences'] == 1, f"Expected 1 pending, got {stats['pending_sequences']}"

    print("  ✓ total_received: 17 (16 + 1 dup)")
    print("  ✓ reassembled: 2")
    print("  ✓ duplicate_dropped: 1")
    print("  ✓ pending_sequences: 1")
    print("  ✅ TEST PASSED\n")


def run_all_tests():
    """Run all reassembly tests."""
    print("\n" + "#"*60)
    print("# PACKET REASSEMBLY UNIT TESTS")
    print("#"*60)

    tests = [
        test_in_order_reassembly,
        test_out_of_order_reassembly,
        test_duplicate_handling,
        test_timeout_flush,
        test_multiple_sequences,
        test_statistics,
    ]

    passed = 0
    failed = 0

    for test_func in tests:
        try:
            test_func()
            passed += 1
        except AssertionError as e:
            print(f"  ❌ TEST FAILED: {e}")
            failed += 1
        except Exception as e:
            print(f"  ❌ TEST ERROR: {e}")
            failed += 1

    print("="*60)
    print(f"SUMMARY: {passed} passed, {failed} failed")
    print("="*60)

    if failed == 0:
        print("✅ ALL TESTS PASSED!")
    else:
        print("❌ SOME TESTS FAILED")

    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
