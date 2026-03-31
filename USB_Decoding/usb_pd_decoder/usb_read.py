import time
import pathlib
import usb.core
import usb.util

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

count = 0
while True:
    try:
        data = dev.read(ep_in.bEndpointAddress,
                        ep_in.wMaxPacketSize,
                        timeout=1000)

        if data:
            count += 1

            # Convert all bytes to decimal
            #dec_string = " ".join(str(b) for b in data)

            # print(f"Packet {count}: {dec_string}")
            
            # Convert to HEX format
            hex_string = " ".join(f"{b:02X}" for b in data)

            print(f"Packet {count}: {hex_string}")

    except usb.core.USBTimeoutError:
        pass

    time.sleep(0.001)