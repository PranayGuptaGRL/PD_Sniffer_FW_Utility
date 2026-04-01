from __future__ import annotations

import queue as _queue
import threading
import time
from typing import List, Optional

import usb.core
import usb.util
import usb.backend.libusb1

from ..models import RawFrame

# libusb error codes that mean "already configured / busy" — safe to ignore
_IGNORABLE_USB_ERRORS = ("already", "busy", "access", "resource")


def _get_backend():
    """Return a libusb backend, preferring libusb-package on Windows."""
    try:
        import libusb_package
        backend = usb.backend.libusb1.get_backend(find_library=libusb_package.find_library)
        if backend is not None:
            return backend
    except ImportError:
        pass
    return usb.backend.libusb1.get_backend()


def _driver_binding_hint() -> str:
    return "Bind a WinUSB/libusb-compatible driver to this device before capturing."


class USBDeviceCapture:
    def __init__(self, vid: int, pid: int, endpoint: int = 0x81, interface: int = 0, timeout_ms: int = 200):
        self.vid = vid
        self.pid = pid
        self.endpoint = endpoint
        self.interface = interface
        self.timeout_ms = timeout_ms
        # Persistent session state
        self._dev = None
        self._intf = None
        self._frame_queue: _queue.Queue[RawFrame] = _queue.Queue()
        self._reader_threads: List[threading.Thread] = []
        self._stop_readers = threading.Event()
        # Endpoints actually being read (populated by open())
        self.active_endpoints: List[int] = []

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def list_devices() -> List[str]:
        backend = _get_backend()
        if backend is None:
            raise RuntimeError("No USB backend found. Run: pip install libusb-package")
        rows: List[str] = []
        try:
            devices = usb.core.find(find_all=True, backend=backend)
        except usb.core.NoBackendError:
            raise RuntimeError("No USB backend found. Run: pip install libusb-package")
        for dev in devices:
            rows.append(
                f"VID=0x{dev.idVendor:04X} PID=0x{dev.idProduct:04X} "
                f"bus={getattr(dev, 'bus', '?')} addr={getattr(dev, 'address', '?')}"
            )
        return rows

    @staticmethod
    def inspect_device(vid: int, pid: int) -> List[str]:
        """Return human-readable lines describing every interface/endpoint on the device.
        Works even when the device driver is not WinUSB (read-only descriptor query).
        """
        backend = _get_backend()
        if backend is None:
            raise RuntimeError("No USB backend found. Run: pip install libusb-package")
        dev = usb.core.find(idVendor=vid, idProduct=pid, backend=backend)
        if dev is None:
            raise RuntimeError(f"Device not found: VID=0x{vid:04X} PID=0x{pid:04X}")

        lines: List[str] = [
            f"Device  VID=0x{dev.idVendor:04X}  PID=0x{dev.idProduct:04X}",
            f"  Manufacturer : {dev.manufacturer or '(none)'}",
            f"  Product      : {dev.product or '(none)'}",
        ]
        try:
            cfg = dev.get_active_configuration()
        except usb.core.USBError:
            lines.append(f"  (Cannot read configuration - {_driver_binding_hint()})")
            usb.util.dispose_resources(dev)
            return lines

        for intf in cfg:
            lines.append(
                f"  Interface {intf.bInterfaceNumber} alt={intf.bAlternateSetting} "
                f"class=0x{intf.bInterfaceClass:02X}"
            )
            for ep in intf:
                direction = "IN " if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN else "OUT"
                ep_type_map = {
                    usb.util.ENDPOINT_TYPE_CTRL: "CONTROL",
                    usb.util.ENDPOINT_TYPE_ISO:  "ISOCHRONOUS",
                    usb.util.ENDPOINT_TYPE_BULK: "BULK",
                    usb.util.ENDPOINT_TYPE_INTR: "INTERRUPT",
                }
                ep_type = ep_type_map.get(usb.util.endpoint_type(ep.bmAttributes), "UNKNOWN")
                lines.append(
                    f"    Endpoint 0x{ep.bEndpointAddress:02X}  {direction}  {ep_type:<12}  "
                    f"maxPacket={ep.wMaxPacketSize}"
                )

        usb.util.dispose_resources(dev)
        return lines

    # ------------------------------------------------------------------
    # Persistent session API  (open → read_frame loop → close)
    # ------------------------------------------------------------------

    def send_init(self, init_bytes: bytes, out_endpoint: int = 0x01, timeout_ms: int = 500) -> None:
        """Write an initialization command to the device's OUT endpoint.
        Must be called after open() and before the read loop.
        Raises RuntimeError on failure.
        """
        if self._dev is None:
            raise RuntimeError("Device not open — call open() first")
        if not init_bytes:
            return
        try:
            written = self._dev.write(out_endpoint, init_bytes, timeout=timeout_ms)
            if written != len(init_bytes):
                raise RuntimeError(
                    f"Init write incomplete: sent {written}/{len(init_bytes)} bytes "
                    f"to OUT endpoint 0x{out_endpoint:02X}"
                )
        except usb.core.USBError as exc:
            raise RuntimeError(
                f"Init command failed on OUT endpoint 0x{out_endpoint:02X}: {exc}"
            ) from exc

    def open(self, all_endpoints: bool = True) -> None:
        """Open and claim the USB interface.

        Parameters
        ----------
        all_endpoints:
            True  — discover and read from ALL IN endpoints on the interface
                    simultaneously (one reader thread per endpoint).
            False — read only from self.endpoint.
        """
        self._dev, self._intf = self._open_device()
        self._stop_readers.clear()
        self._frame_queue = _queue.Queue()
        self._reader_threads = []

        if all_endpoints:
            in_eps = [
                ep.bEndpointAddress
                for ep in self._intf
                if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN
            ]
            if not in_eps:
                in_eps = [self.endpoint]  # fallback if no IN eps found
        else:
            in_eps = [self.endpoint]

        self.active_endpoints = in_eps
        for ep_addr in in_eps:
            t = threading.Thread(
                target=self._endpoint_reader,
                args=(ep_addr,),
                daemon=True,
                name=f"usb-ep-0x{ep_addr:02X}",
            )
            t.start()
            self._reader_threads.append(t)

    def close(self) -> None:
        """Stop all reader threads and release the USB device."""
        self._stop_readers.set()
        for t in self._reader_threads:
            t.join(timeout=1.0)
        self._reader_threads = []
        if self._dev is not None:
            try:
                usb.util.release_interface(self._dev, self._intf.bInterfaceNumber)
            except Exception:
                pass
            try:
                usb.util.dispose_resources(self._dev)
            except Exception:
                pass
            self._dev = None
            self._intf = None

    def read_frame(self) -> Optional[RawFrame]:
        """Read the next available frame from any active endpoint.
        Returns None if no frame arrives within timeout_ms.
        Must call open() first.
        """
        if self._dev is None and not self._reader_threads:
            raise RuntimeError("Device not open — call open() first")
        try:
            return self._frame_queue.get(timeout=self.timeout_ms / 1000.0)
        except _queue.Empty:
            return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _endpoint_reader(self, ep_addr: int) -> None:
        """Runs in a background thread; pushes frames for one endpoint into the queue."""
        # Read buffer size: 1024 bytes (can contain up to 16 GRL packets of 64 bytes each)
        READ_BUFFER_SIZE = 1024

        while not self._stop_readers.is_set():
            try:
                data = self._dev.read(ep_addr, READ_BUFFER_SIZE, timeout=self.timeout_ms)
                payload = bytes(data)
                if payload:
                    now_us = int(time.monotonic() * 1_000_000)
                    self._frame_queue.put(
                        RawFrame(
                            timestamp_us=now_us,
                            payload=payload,
                            source=f"usb_ep0x{ep_addr:02X}",
                        )
                    )
            except usb.core.USBError as exc:
                if "timed out" in str(exc).lower():
                    continue
                if self._stop_readers.is_set():
                    break
                # Real USB error — push a sentinel so read_frame() can surface it
                self._frame_queue.put(
                    RawFrame(
                        timestamp_us=int(time.monotonic() * 1_000_000),
                        payload=b"",
                        source=f"ERROR ep0x{ep_addr:02X}: {exc}",
                    )
                )
                break
            except Exception as exc:
                if not self._stop_readers.is_set():
                    self._frame_queue.put(
                        RawFrame(
                            timestamp_us=int(time.monotonic() * 1_000_000),
                            payload=b"",
                            source=f"ERROR ep0x{ep_addr:02X}: {exc}",
                        )
                    )
                break

    def _open_device(self):
        backend = _get_backend()
        dev = usb.core.find(idVendor=self.vid, idProduct=self.pid, backend=backend)
        if dev is None:
            raise RuntimeError(f"USB device not found: VID=0x{self.vid:04X} PID=0x{self.pid:04X}")

        # On Linux, detach the kernel driver so libusb can claim the interface.
        try:
            if dev.is_kernel_driver_active(self.interface):
                dev.detach_kernel_driver(self.interface)
        except (usb.core.USBError, NotImplementedError):
            pass

        try:
            dev.set_configuration()
        except usb.core.USBError:
            pass  # Already configured by OS — claim_interface gives the real signal

        cfg = dev.get_active_configuration()
        intf = cfg[(self.interface, 0)]

        try:
            usb.util.claim_interface(dev, intf.bInterfaceNumber)
        except usb.core.USBError as exc:
            usb.util.dispose_resources(dev)
            raise RuntimeError(
                f"Cannot claim USB interface {self.interface} for "
                f"VID=0x{self.vid:04X} PID=0x{self.pid:04X}.\n"
                f"On Windows this means the device still has its original driver "
                f"and libusb cannot take ownership.\n"
                f"Fix: {_driver_binding_hint()}\n"
                f"libusb error: {exc}"
            ) from exc

        return dev, intf

    # ------------------------------------------------------------------
    # Batch capture (kept for CLI / offline use)
    # ------------------------------------------------------------------

    def capture(self, seconds: float = 3.0, max_frames: int = 500) -> List[RawFrame]:
        dev, intf = self._open_device()
        start = time.monotonic()
        frames: List[RawFrame] = []
        # Read buffer size: 1024 bytes (can contain up to 16 GRL packets of 64 bytes each)
        READ_BUFFER_SIZE = 1024

        try:
            while (time.monotonic() - start) < seconds and len(frames) < max_frames:
                try:
                    data = dev.read(self.endpoint, READ_BUFFER_SIZE, timeout=self.timeout_ms)
                    now_us = int((time.monotonic() - start) * 1_000_000)
                    payload = bytes(data)
                    if payload:
                        frames.append(RawFrame(timestamp_us=now_us, payload=payload, source="usb"))
                except usb.core.USBError as exc:
                    if "timed out" in str(exc).lower():
                        continue
                    raise
        finally:
            try:
                usb.util.release_interface(dev, intf.bInterfaceNumber)
            except Exception:
                pass
            usb.util.dispose_resources(dev)

        return frames
