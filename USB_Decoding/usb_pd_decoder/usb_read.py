import time
import pathlib
import usb.core
import usb.util
from decoders.grl_sniffer import parse_grl_packet, format_grl_packet, update_grl_timestamp_state
from decoders.grl_bmc import GRLBMCDecoder
from decoders.pd import PDDecoder
from packet_reassembly import PacketReassembler

VID = 0x227F
PID = 0x0005

def pick_backend():
    """
    libusb-win32 driver generally works best with libusb0 backend.
    We'll try libusb0 first, then fall back to libusb1.
    """
    try:
        import libusb
        root = pathlib.Path(libusb.__file__).parent

        # Try libusb0 (libusb-win32)
        try:
            import usb.backend.libusb0 as libusb0
            # Common DLL names for libusb0 backend:
            # - libusb0.dll (libusb-win32)
            # - libusb0_x64.dll (some bundles)
            dll_candidates = list(root.rglob("libusb0*.dll"))
            if dll_candidates:
                dll_path = str(dll_candidates[0])
                be = libusb0.get_backend(find_library=lambda _: dll_path)
                if be:
                    print("Using backend: libusb0  DLL:", dll_path)
                    return be
        except Exception:
            pass

        # Fallback: libusb-1.0
        try:
            import usb.backend.libusb1 as libusb1
            dll_candidates = list(root.rglob("libusb-1.0.dll"))
            if dll_candidates:
                dll_path = str(dll_candidates[0])
                be = libusb1.get_backend(find_library=lambda _: dll_path)
                if be:
                    print("Using backend: libusb1  DLL:", dll_path)
                    return be
        except Exception:
            pass

    except Exception:
        pass

    # Last fallback: let PyUSB try default discovery
    print("Using backend: default (no forced DLL)")
    return None

backend = pick_backend()

dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
if dev is None:
    raise SystemExit(
        f"Device not found (VID={VID:04X} PID={PID:04X}).\n"
        f"- Confirm Device Manager Hardware Ids show VID_227F&PID_0005\n"
        f"- Close any app using the device\n"
        f"- Ensure libusb-win32 driver is bound to THIS device node"
    )

print(f"Found device: VID={VID:04X} PID={PID:04X}")

# Set configuration
dev.set_configuration()
cfg = dev.get_active_configuration()

# Find first BULK IN endpoint
ep_in = None
chosen_intf = None

for intf in cfg:
    try:
        usb.util.claim_interface(dev, intf.bInterfaceNumber)
    except Exception:
        pass

    for ep in intf:
        is_in = usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN
        is_bulk = usb.util.endpoint_type(ep.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK
        if is_in and is_bulk:
            ep_in = ep
            chosen_intf = intf
            break

    if ep_in:
        break

if ep_in is None:
    raise SystemExit("No BULK IN endpoint found. Check your firmware endpoints (e.g. 0x81).")

print(f"Using Interface={chosen_intf.bInterfaceNumber}, EP_IN=0x{ep_in.bEndpointAddress:02X}, MaxPkt={ep_in.wMaxPacketSize}")

# Initialize reassemblers and decoders
reassemblers = {0: PacketReassembler(), 1: PacketReassembler()}
bmc_decoders = {0: GRLBMCDecoder(), 1: GRLBMCDecoder()}
pd_decoder = PDDecoder()
device_ts_state = {
    0: {"epoch_ms": 0, "last_expanded_ms": None},
    1: {"epoch_ms": 0, "last_expanded_ms": None},
}

count = 0
reassembled_count = 0
decoded_pd_count = 0

print("\n=== USB PD Sniffer with Packet Reassembly ===")
print("Legend:")
print("  [RAW] - Raw 64-byte packet from USB endpoint")
print("  [REASSEMBLED] - Complete message with all 8 fragments")
print("  [PD] - Decoded USB PD message")
print("=" * 60)

# Read buffer size: 1024 bytes (can contain up to 16 GRL packets of 64 bytes each)
READ_BUFFER_SIZE = 1024
GRL_PACKET_SIZE = 64

while True:
    try:
        data = dev.read(ep_in.bEndpointAddress,
                        READ_BUFFER_SIZE,
                        timeout=1000)

        if data:
            # Process all 64-byte chunks in the received data
            data_bytes = bytes(data)
            num_packets = len(data_bytes) // GRL_PACKET_SIZE

            # Handle case where data is not a multiple of 64
            if len(data_bytes) % GRL_PACKET_SIZE != 0:
                print(f"[WARNING] Received {len(data_bytes)} bytes (not multiple of 64)")

            # Process each 64-byte GRL packet
            for i in range(num_packets):
                chunk = data_bytes[i * GRL_PACKET_SIZE:(i + 1) * GRL_PACKET_SIZE]
                count += 1

                # Parse GRL packet
                grl = parse_grl_packet(chunk)
                if grl is None:
                    hex_string = " ".join(f"{b:02X}" for b in chunk)
                    print(f"[RAW] Packet {count}: {hex_string}")
                    continue

                # Update timestamp
                device_ts_us = update_grl_timestamp_state(device_ts_state[grl.channel], grl)

                # Show raw packet
                print(f"[RAW] {format_grl_packet(grl, device_ts_us)}")

                # Skip idle packets
                if grl.is_idle:
                    reassemblers[grl.channel].reset()
                    bmc_decoders[grl.channel].reset_stream()
                    continue

                # Add to reassembler
                reassembled = reassemblers[grl.channel].add_packet(grl, device_ts_us, device_ts_us)

                # Check for incomplete packets (timeout-based flush)
                incomplete_packets = reassemblers[grl.channel].flush_incomplete()
                for incomplete in incomplete_packets:
                    print(f"[INCOMPLETE] {incomplete.cc_line} seq={incomplete.seq_num} "
                          f"fragments={incomplete.fragment_count}/8 (timeout)")
                    # Try to decode anyway
                    edge_bytes = incomplete.get_concatenated_payload()
                    frames = bmc_decoders[grl.channel].feed(edge_bytes, incomplete.device_ts_us)
                    for frame in frames:
                        messages = pd_decoder.decode([frame])
                        for msg in messages:
                            decoded_pd_count += 1
                            print(f"[PD] {msg.message_type} header=0x{msg.header:04X} objs={len(msg.payload_words)}")

                # If we got a complete reassembled packet
                if reassembled is not None:
                    reassembled_count += 1
                    print(f"[REASSEMBLED] {reassembled.cc_line} seq={reassembled.seq_num} "
                          f"fragments={reassembled.fragment_count}/8 complete={reassembled.complete}")

                    # Decode the reassembled packet
                    edge_bytes = reassembled.get_concatenated_payload()
                    frames = bmc_decoders[grl.channel].feed(edge_bytes, reassembled.device_ts_us)

                    for frame in frames:
                        messages = pd_decoder.decode([frame])
                        for msg in messages:
                            decoded_pd_count += 1
                            print(f"[PD] {msg.message_type} header=0x{msg.header:04X} objs={len(msg.payload_words)}")

                # Show stats every 100 packets
                if count % 100 == 0:
                    stats = reassemblers[0].get_stats()
                    stats1 = reassemblers[1].get_stats()
                    print(f"\n--- Stats: Raw={count} Reassembled={reassembled_count} PD_Decoded={decoded_pd_count} ---")
                    print(f"    CC1: {stats}, CC2: {stats1}\n")

    except usb.core.USBTimeoutError:
        pass
    except KeyboardInterrupt:
        print("\n\nStopping capture...")
        break

    time.sleep(0.001)

print(f"\nFinal Stats:")
print(f"  Total raw packets: {count}")
print(f"  Reassembled packets: {reassembled_count}")
print(f"  Decoded PD messages: {decoded_pd_count}")
for ch in [0, 1]:
    stats = reassemblers[ch].get_stats()
    print(f"  CC{ch+1}: {stats}")