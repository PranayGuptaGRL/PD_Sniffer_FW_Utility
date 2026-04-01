# Packet Reassembly for GRL USB PD Sniffer

## Overview

The GRL USB PD Sniffer streams data from USB endpoints using a fragmentation scheme based on **sequence numbers** and **buffer indices**. This document explains how packet reassembly works and how to use it.

## Packet Structure

Each 64-byte USB packet from the GRL sniffer contains:

### Header (4 bytes):
- **Bytes 0-1**: Sequence field (16-bit, little-endian)
  - **Bits 0-2**: Buffer Index (3-bit, 0-7)
  - **Bits 3-11**: Sequence Number (9-bit, 0-511, wraps around)
  - **Bit 12**: Channel (0=CC1, 1=CC2)
  - **Bits 13-14**: Reserved
  - **Bit 15**: Overflow flag
- **Bytes 2-3**: Timestamp (16-bit, milliseconds)
- **Bytes 4-63**: Payload (60 bytes of edge timestamps for BMC decoding)

### Fragmentation Scheme

A complete PD message may span multiple USB packets:
- **Sequence Number**: Identifies a logical message (0-511, wraps)
- **Buffer Index**: Identifies which fragment within that message (0-7)
- **Complete Message**: All 8 fragments (buf_idx 0-7) for a given sequence number

## Problem Statement

**Before Reassembly:**
- Each 64-byte packet was processed independently
- Fragmented messages were never stitched together
- Out-of-order packets caused decode failures
- Only single-packet messages could be decoded

**Example Issue:**
```
Received:
  seq=10, buf_idx=2  ← Fragment received out of order
  seq=10, buf_idx=0  ← First fragment arrives late
  seq=10, buf_idx=1  ← Second fragment
  ...

Without reassembly: Decoding fails - incomplete data
```

## Solution: Packet Reassembly

The `PacketReassembler` class handles:

1. **Grouping by (channel, sequence_number)**
2. **Collecting all buf_idx fragments (0-7)**
3. **Sorting fragments by buf_idx**
4. **Concatenating payloads in correct order**
5. **Detecting complete vs incomplete sequences**
6. **Timeout-based flushing of incomplete sequences**

### Architecture

```
┌─────────────────┐
│ USB Endpoint    │
│ 64-byte packets │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Parse GRL Header│
│ Extract seq_num │
│ Extract buf_idx │
└────────┬────────┘
         │
         ▼
┌─────────────────────┐
│ PacketReassembler   │
│ Group by:           │
│  (channel, seq_num) │
│ Collect buf_idx 0-7 │
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│ Complete?           │
│ Yes: Sort & Concat  │
│ No: Wait for more   │
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│ BMC Decoder         │
│ Decode edges→frames │
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│ PD Decoder          │
│ Decode PD messages  │
└─────────────────────┘
```

## Usage

### In GUI Application

The GUI (`gui.py`) automatically uses packet reassembly in live capture mode:

```python
# Reassemblers are created per channel
packet_reassemblers = {
    0: PacketReassembler(),  # CC1
    1: PacketReassembler(),  # CC2
}

# For each incoming packet
reassembled = packet_reassemblers[channel].add_packet(grl, device_ts_us, wall_ts_us)

# If complete packet is ready
if reassembled is not None:
    # All fragments received - decode
    cc_line, ts, frames = decode_reassembled_packet(reassembled, bmc_decoder)
    # Process decoded frames...

# Periodically flush incomplete packets
incomplete = packet_reassemblers[channel].flush_incomplete()
for packet in incomplete:
    # Timeout occurred - decode partial data
    ...
```

### In Command-Line Script

See `usb_read.py` for a standalone example:

```python
from packet_reassembly import PacketReassembler
from decoders.grl_sniffer import parse_grl_packet
from decoders.grl_bmc import GRLBMCDecoder

# Initialize
reassembler = PacketReassembler()
bmc_decoder = GRLBMCDecoder()

while True:
    # Read from USB endpoint
    data = dev.read(endpoint, 64)

    # Parse GRL packet
    grl = parse_grl_packet(data)

    # Add to reassembler
    reassembled = reassembler.add_packet(grl, timestamp_us, timestamp_us)

    # If complete
    if reassembled:
        # Concatenate all fragments in buf_idx order
        edge_bytes = reassembled.get_concatenated_payload()

        # Decode
        frames = bmc_decoder.feed(edge_bytes, reassembled.device_ts_us)
        # Process frames...
```

## Key Features

### 1. Out-of-Order Handling
Packets arriving in any order are automatically sorted:
```
Receive: buf_idx 3, 0, 5, 2, 7, 1, 4, 6
Process: buf_idx 0, 1, 2, 3, 4, 5, 6, 7  ✓
```

### 2. Duplicate Detection
Duplicate buf_idx values for the same sequence are rejected:
```
seq=10, buf_idx=2 (first)  ✓ Accepted
seq=10, buf_idx=2 (second) ✗ Rejected (duplicate)
```

### 3. Timeout-Based Flushing
Incomplete sequences are flushed after 2 seconds:
```
Time 0s:   Receive seq=20, buf_idx=0,1,2
Time 2s:   Timeout! Flush seq=20 (incomplete: 3/8 fragments)
           Try to decode anyway (may succeed with partial data)
```

### 4. Statistics Tracking
```python
stats = reassembler.get_stats()
# Returns:
{
    'total_received': 1523,      # Total fragments received
    'reassembled': 190,          # Complete packets assembled
    'incomplete_flushed': 2,     # Timed-out incomplete packets
    'duplicate_dropped': 5,      # Duplicate fragments dropped
    'pending_sequences': 3       # Currently buffered sequences
}
```

### 5. Pending Sequence Inspection
```python
pending = reassembler.get_pending_info()
# Returns:
[
    {
        'channel': 0,
        'cc_line': 'CC1',
        'seq_num': 42,
        'received_buf_idx': [0, 1, 2, 5, 6],
        'missing_buf_idx': [3, 4, 7],
        'fragment_count': 5,
        'age_seconds': 0.8
    },
    ...
]
```

## Configuration

### Reassembler Parameters

You can adjust parameters in `packet_reassembly.py`:

```python
class PacketReassembler:
    # Expected fragments per sequence
    EXPECTED_FRAGMENTS = 8

    # Timeout for incomplete sequences (seconds)
    INCOMPLETE_TIMEOUT_S = 2.0

    # Max pending sequences before cleanup
    MAX_PENDING_SEQUENCES = 100
```

## Diagnostic Output

### GUI Live Capture
When `show_raw` is enabled, you'll see:
```
[RAW] CC1 seq=42 buf=0 ts=1234ms (60B non-zero): 12 34 56 ...
[RAW] CC1 seq=42 buf=1 ts=1235ms (60B non-zero): 78 9a bc ...
...
[RAW] CC1 seq=42 buf=7 ts=1242ms (60B non-zero): de f0 12 ...
[REASSEMBLED] CC1 seq=42 fragments=8/8 complete=True
[BMC decoded 1 frame(s) from CC1]
  BMC-PD> Source_Caps header=0x1161 objs=3
```

### Trace Files
The trace log includes reassembly details:
```
@GRL_REASSEMBLED cc=CC1 seq=42 fragments=8/8 complete=True
  RAW64 buf=0 device_ts_us=1234567 edges=12 34 56 78...
  RAW64 buf=1 device_ts_us=1235012 edges=9a bc de f0...
  ...
  RAW64 buf=7 device_ts_us=1242890 edges=11 22 33 44...
  FRAME payload=16 61 11 00 12 34 56 78
```

## Benefits

✅ **Handles out-of-order packets** - No decode failures
✅ **Stitches fragmented messages** - Multi-packet PD messages work
✅ **Robust timeout handling** - Doesn't hang on missing fragments
✅ **Duplicate prevention** - Clean data stream
✅ **Diagnostic stats** - Easy debugging
✅ **Per-channel isolation** - CC1 and CC2 handled independently

## Troubleshooting

### Issue: High incomplete_flushed count
**Cause**: Packet loss on USB or firmware
**Solution**:
- Check USB cable quality
- Reduce USB polling timeout
- Check for firmware buffer overruns

### Issue: Many duplicate_dropped
**Cause**: Firmware retransmitting packets
**Solution**:
- This is normal behavior - duplicates are safely ignored
- Check firmware logs if excessive (>5% of packets)

### Issue: pending_sequences keeps growing
**Cause**: Sequence numbers advancing too fast, old sequences not completing
**Solution**:
- Reassembler auto-cleans after MAX_PENDING_SEQUENCES (100)
- Consider reducing INCOMPLETE_TIMEOUT_S if needed

## Testing

Run the example script to verify reassembly:

```bash
cd USB_Decoding/usb_pd_decoder
python usb_read.py
```

Expected output:
```
=== USB PD Sniffer with Packet Reassembly ===
[RAW] CC1 seq=5 buf=0 ...
[RAW] CC1 seq=5 buf=1 ...
...
[REASSEMBLED] CC1 seq=5 fragments=8/8 complete=True
[PD] Source_Caps header=0x1161 objs=3

--- Stats: Raw=100 Reassembled=12 PD_Decoded=15 ---
    CC1: {'total_received': 96, 'reassembled': 12, ...}
    CC2: {'total_received': 4, 'reassembled': 0, ...}
```

## API Reference

### PacketReassembler

#### `add_packet(grl_packet, device_ts_us, wall_ts_us) -> Optional[ReassembledPacket]`
Add a packet fragment. Returns complete packet if all 8 fragments received.

#### `flush_incomplete(force=False) -> List[ReassembledPacket]`
Flush timed-out incomplete sequences. If `force=True`, flushes all pending.

#### `get_stats() -> Dict[str, int]`
Returns reassembly statistics.

#### `get_pending_info() -> List[Dict]`
Returns details about pending incomplete sequences.

#### `reset()`
Clear all state and statistics.

### ReassembledPacket

#### Properties:
- `channel`: 0 (CC1) or 1 (CC2)
- `seq_num`: Sequence number (0-511)
- `fragments`: List of PacketFragment objects (sorted by buf_idx)
- `device_ts_us`: Timestamp from buf_idx=0
- `complete`: True if all 8 fragments received
- `fragment_count`: Number of fragments (0-8)
- `cc_line`: "CC1" or "CC2"

#### Methods:
- `get_concatenated_payload() -> bytes`: Returns concatenated edge data from all fragments

## Files Modified

- **`usb_pd_decoder/packet_reassembly.py`** - New module (core reassembly logic)
- **`usb_pd_decoder/gui.py`** - Updated to use reassembly in live capture
- **`usb_pd_decoder/usb_read.py`** - Example script with reassembly
- **`PACKET_REASSEMBLY.md`** - This documentation

## See Also

- `ARCHITECTURE_WALKTHROUGH.adoc` - Overall decoder architecture
- `decoders/grl_sniffer.py` - GRL packet format documentation
- `decoders/grl_bmc.py` - BMC decoder for edge timestamps
