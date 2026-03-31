from __future__ import annotations

import json
import queue
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from tkinter import (
    BOTH, END, LEFT, RIGHT, TOP, X, Y,
    BooleanVar, DoubleVar, IntVar, StringVar, Text, Toplevel,
    Tk, filedialog, messagebox,
)
from tkinter import ttk
from typing import List, Optional

try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
    from matplotlib.figure import Figure
    _MPL_OK = True
except ImportError:
    _MPL_OK = False

from .decoders.grl_bmc import GRLBMCDecoder
from .decoders.grl_sniffer import (
    GRL_PACKET_SIZE,
    extract_grl_direct_pd_payload,
    format_grl_packet,
    parse_grl_packet,
    parse_grl_vbus_telemetry,
    update_grl_timestamp_state,
)
from .decoders.pd import PDDecoder
from .decoders.pd_objects import parse_rdo, parse_src_caps
from .decoders.twinkie_bmc import TwinkieBMCDecoder
from .inputs.raw_file import RawFrameParser
from .inputs.twinkie_usblyzer import TwinkieUSBlyzerParser
from .models import DecodedMessage, RawFrame

GRL_VID = 0x227F
GRL_PID = 0x0005
LIVE_LOG_FLUSH_INTERVAL_S = 0.25

# USB PD 2.0 max frame = 2B header + 7*4B data objects + 4B CRC = 34 bytes.
def _friendly_type(msg_type: str) -> str:
    return msg_type


def message_line(m: DecodedMessage) -> str:
    cc = f" {m.cc_line}" if m.cc_line else ""
    dir_str = f" {m.direction}" if m.direction else ""
    return (
        f"{m.timestamp_us:>9} us |{cc}{dir_str} | {_friendly_type(m.message_type):<22} "
        f"header=0x{m.header:04X} objs={len(m.payload_words)}"
    )


class USBPDGuiApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("USB PD Decoder — GRL Sniffer")
        self.root.geometry("1380x860")

        # Settings variables
        self.mode         = StringVar(value="online")
        self.input_file   = StringVar(value="")
        self.input_format = StringVar(value="auto")
        self.out_prefix   = StringVar(value="")
        self.json_out     = BooleanVar(value=True)
        self.tick_ns      = DoubleVar(value=0.0)

        self.endpoint      = StringVar(value="0x81")
        self.interface     = IntVar(value=0)
        self.timeout_ms    = IntVar(value=200)
        self.window_s      = DoubleVar(value=1.0)
        self.auto_log_live = BooleanVar(value=True)
        self.show_raw      = BooleanVar(value=True)
        self.all_endpoints = BooleanVar(value=True)
        self.init_hex      = StringVar(value="")
        self.init_out_ep   = StringVar(value="0x01")
        self.devices       = StringVar(value="")

        # Threading
        self.log_queue:      queue.Queue[str]  = queue.Queue()
        self.pd_event_queue: queue.Queue[dict] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: Optional[threading.Thread] = None

        # Plot / PD state (main-thread only)
        self._pd_events_all: List[dict] = []
        self._scroll_live   = BooleanVar(value=True)
        self._plot_paused   = False
        self._selected_time_ms: Optional[float] = None
        self._manual_xlim: Optional[tuple[float, float]] = None
        self._t0_us: Optional[int] = None   # set by worker, read by main thread
        self._device_t0_us: Optional[int] = None
        self._event_seq: int = 0

        self._fig    = None
        self._ax1    = None
        self._ax2    = None
        self._canvas = None

        self._build_ui()
        self._refresh_usb_devices()
        self.root.after(150, self._pump_queues)
        if _MPL_OK:
            self.root.after(600, self._update_plot)

    # =========================================================================
    # UI construction
    # =========================================================================

    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=6)
        top.pack(side=TOP, fill=X)

        # Live-only operation mode
        ttk.Button(top, text="Refresh USB",    command=self._refresh_usb_devices).pack(side=LEFT, padx=3)
        ttk.Button(top, text="Inspect Device", command=self._inspect_device).pack(side=LEFT, padx=3)
        ttk.Button(top, text="Start Live",     command=self._start_live).pack(side=LEFT, padx=3)
        ttk.Button(top, text="Stop Live",      command=self._stop_live).pack(side=LEFT, padx=3)

        split = ttk.PanedWindow(self.root, orient="horizontal")
        split.pack(fill=BOTH, expand=True, padx=6, pady=4)

        left  = ttk.Frame(split, padding=4)
        right = ttk.Frame(split, padding=4)
        split.add(left,  weight=2)
        split.add(right, weight=5)

        self._build_left_panel(left)
        self._build_right_panel(right)

        self.status = StringVar(value="Ready")
        ttk.Label(self.root, textvariable=self.status,
                  relief="sunken", anchor="w").pack(side=TOP, fill=X, padx=6, pady=(0, 4))

        self._refresh_mode_view()

    def _build_left_panel(self, parent: ttk.Frame) -> None:
        # Online settings
        on = ttk.LabelFrame(parent, text="Online Capture", padding=6)
        on.pack(fill=X, pady=2)
        self.online_frame = on

        ttk.Label(on, text="Device").grid(row=0, column=0, sticky="w")
        self.device_list = ttk.Combobox(
            on, textvariable=self.devices, state="readonly", width=55)
        self.device_list.grid(row=0, column=1, columnspan=3, sticky="we", pady=2)

        ttk.Label(on, text="EP").grid(row=1, column=0, sticky="w")
        ttk.Entry(on, textvariable=self.endpoint, width=8).grid(row=1, column=1, sticky="w")
        ttk.Label(on, text="Iface").grid(row=1, column=2, sticky="e", padx=(6, 0))
        ttk.Entry(on, textvariable=self.interface, width=6).grid(row=1, column=3, sticky="w")

        ttk.Label(on, text="Timeout ms").grid(row=2, column=0, sticky="w")
        ttk.Entry(on, textvariable=self.timeout_ms, width=8).grid(row=2, column=1, sticky="w")
        ttk.Label(on, text="Window s").grid(row=2, column=2, sticky="e", padx=(6, 0))
        ttk.Entry(on, textvariable=self.window_s, width=6).grid(row=2, column=3, sticky="w")

        ttk.Checkbutton(on, text="All IN",   variable=self.all_endpoints).grid(row=3, column=0, sticky="w")
        ttk.Checkbutton(on, text="Show raw", variable=self.show_raw).grid(row=3, column=1, sticky="w")
        ttk.Checkbutton(on, text="Auto log", variable=self.auto_log_live).grid(row=3, column=2, columnspan=2, sticky="w")

        ttk.Label(on, text="Init (hex)").grid(row=4, column=0, sticky="w")
        ttk.Entry(on, textvariable=self.init_hex, width=30).grid(
            row=4, column=1, columnspan=2, sticky="we", padx=2)
        ttk.Label(on, text="OUT EP").grid(row=4, column=2, sticky="e", padx=(6, 0))
        ttk.Entry(on, textvariable=self.init_out_ep, width=7).grid(row=4, column=3, sticky="w")

        # PD Messages treeview
        pd_frame = ttk.LabelFrame(parent, text="PD Messages", padding=4)
        pd_frame.pack(fill=BOTH, expand=True, pady=4)

        cols = ("time_ms", "dir", "type", "voltage", "current", "header")
        self.pd_list = ttk.Treeview(pd_frame, columns=cols, show="headings", height=14)
        self.pd_list.heading("time_ms", text="Time (ms)")
        self.pd_list.heading("dir",     text="Dir")
        self.pd_list.heading("type",    text="Message Type")
        self.pd_list.heading("voltage", text="Voltage V")
        self.pd_list.heading("current", text="Current A")
        self.pd_list.heading("header",  text="Header")
        self.pd_list.column("time_ms", width=78,  anchor="e")
        self.pd_list.column("dir",     width=60,  anchor="center")
        self.pd_list.column("type",    width=130, anchor="w")
        self.pd_list.column("voltage", width=68,  anchor="e")
        self.pd_list.column("current", width=68,  anchor="e")
        self.pd_list.column("header",  width=66,  anchor="center")
        self.pd_list.tag_configure("src_snk",  foreground="#1a5fa8")
        self.pd_list.tag_configure("snk_src",  foreground="#b02020")
        self.pd_list.tag_configure("contract", background="#d4edda")

        pd_sb = ttk.Scrollbar(pd_frame, orient="vertical", command=self.pd_list.yview)
        self.pd_list.configure(yscrollcommand=pd_sb.set)
        self.pd_list.pack(side=LEFT, fill=BOTH, expand=True)
        pd_sb.pack(side=LEFT, fill="y")
        self.pd_list.bind("<<TreeviewSelect>>", self._on_pd_row_select)
        self._iid_to_event: dict[str, int] = {}  # treeview iid → index in _pd_events_all

        # Packet detail treeview (inline, updates on selection)
        detail_frame = ttk.LabelFrame(parent, text="Packet Detail", padding=4)
        detail_frame.pack(fill=BOTH, expand=True, pady=2)

        det_cols = ("offset", "length", "field", "value", "description", "hex")
        self._detail_tree = ttk.Treeview(
            detail_frame, columns=det_cols, show="headings", height=8)
        self._detail_tree.heading("offset", text="Offset")
        self._detail_tree.heading("length", text="Len")
        self._detail_tree.heading("field",  text="Field Name")
        self._detail_tree.heading("value",  text="Value")
        self._detail_tree.heading("description", text="Description")
        self._detail_tree.heading("hex",    text="HEX")
        self._detail_tree.column("offset", width=44,  anchor="center")
        self._detail_tree.column("length", width=32,  anchor="center")
        self._detail_tree.column("field",  width=120, anchor="w")
        self._detail_tree.column("value",  width=100, anchor="w")
        self._detail_tree.column("description", width=200, anchor="w")
        self._detail_tree.column("hex",    width=90,  anchor="w")

        det_sb = ttk.Scrollbar(detail_frame, orient="vertical",
                               command=self._detail_tree.yview)
        self._detail_tree.configure(yscrollcommand=det_sb.set)
        self._detail_tree.pack(side=LEFT, fill=BOTH, expand=True)
        det_sb.pack(side=LEFT, fill="y")

        # System log
        log_frame = ttk.LabelFrame(parent, text="System Log", padding=4)
        log_frame.pack(fill=X, pady=2)
        self._sys_log = Text(log_frame, height=5, state="disabled",
                             font=("Consolas", 8), wrap="none")
        log_sb = ttk.Scrollbar(log_frame, orient="vertical", command=self._sys_log.yview)
        self._sys_log.configure(yscrollcommand=log_sb.set)
        self._sys_log.pack(side=LEFT, fill=BOTH, expand=True)
        log_sb.pack(side=LEFT, fill="y")

    def _build_right_panel(self, parent: ttk.Frame) -> None:
        if not _MPL_OK:
            ttk.Label(
                parent,
                text="matplotlib not found.\nInstall it to see the VBUS plot:\n\n  pip install matplotlib",
                font=("Consolas", 11), justify="center",
            ).pack(expand=True)
            return

        canvas_frame = ttk.Frame(parent)
        canvas_frame.pack(fill=BOTH, expand=True)

        self._fig = Figure(figsize=(9, 5), dpi=96)
        self._ax1 = self._fig.add_subplot(111)
        self._ax2 = self._ax1.twinx()

        self._canvas = FigureCanvasTkAgg(self._fig, master=canvas_frame)
        self._canvas.get_tk_widget().pack(fill=BOTH, expand=True)
        self._canvas.mpl_connect("button_press_event", self._on_plot_interact)

        try:
            tb = NavigationToolbar2Tk(self._canvas, canvas_frame)
            tb.update()
        except Exception:
            pass

        ctrl = ttk.Frame(parent)
        ctrl.pack(fill=X, pady=3)
        ttk.Button(ctrl, text="Scroll to Live",    command=self._scroll_to_live).pack(side=LEFT, padx=4)
        ttk.Checkbutton(ctrl, text="Auto-scroll",  variable=self._scroll_live).pack(side=LEFT, padx=2)
        ttk.Button(ctrl, text="Zoom In",          command=lambda: self._adjust_plot_zoom(0.5)).pack(side=LEFT, padx=4)
        ttk.Button(ctrl, text="Zoom Out",         command=lambda: self._adjust_plot_zoom(2.0)).pack(side=LEFT, padx=4)
        ttk.Button(ctrl, text="Pause/Resume Plot", command=self._toggle_plot_pause).pack(side=LEFT, padx=4)
        ttk.Button(ctrl, text="Clear Plot",        command=self._clear_plot).pack(side=LEFT, padx=4)

        ttk.Label(ctrl, text="  Contract:").pack(side=LEFT)
        self._contract_label = StringVar(value="—")
        ttk.Label(ctrl, textvariable=self._contract_label,
                  font=("Consolas", 10, "bold"), foreground="#007700").pack(side=LEFT, padx=4)

        self._draw_empty_plot()

    # =========================================================================
    # Mode / device helpers
    # =========================================================================

    def _on_mode_change(self) -> None:
        self._refresh_usb_devices()

    def _refresh_mode_view(self) -> None:
        # Offline mode was removed intentionally.
        self._set_frame_state(self.online_frame, "normal")

    @staticmethod
    def _set_frame_state(frame: ttk.Frame, state: str) -> None:
        for child in frame.winfo_children():
            try:
                child.configure(state=state)
            except Exception:
                pass

    def _refresh_usb_devices(self) -> None:
        try:
            from .inputs.usb_capture import USBDeviceCapture
        except ModuleNotFoundError:
            self.status.set("PyUSB not installed — pip install pyusb")
            return
        try:
            rows = USBDeviceCapture.list_devices()
        except RuntimeError as exc:
            self.device_list["values"] = []
            self.devices.set("")
            self.status.set(str(exc))
            messagebox.showerror("USB backend error", str(exc))
            return
        except Exception as exc:
            self.device_list["values"] = []
            self.devices.set("")
            self.status.set(f"USB enumerate failed: {exc}")
            return

        if not rows:
            self.device_list["values"] = []
            self.devices.set("")
            self.status.set("No USB devices found")
            return

        self.device_list["values"] = rows
        grl_row = next(
            (r for r in rows
             if f"VID=0x{GRL_VID:04X}" in r and f"PID=0x{GRL_PID:04X}" in r),
            None,
        )
        self.devices.set(grl_row if grl_row else rows[0])
        note = " — GRL sniffer auto-selected" if grl_row else ""
        self.status.set(f"Found {len(rows)} USB device(s){note}")

    def _parse_vid_pid(self, text: str) -> tuple[int, int]:
        m = re.search(r"VID=0x([0-9A-Fa-f]{4})\s+PID=0x([0-9A-Fa-f]{4})", text)
        if not m:
            raise ValueError("Could not parse VID/PID from selected USB item")
        return int(m.group(1), 16), int(m.group(2), 16)

    # =========================================================================
    # Input helpers
    # =========================================================================

    def _browse_input(self) -> None:
        path = filedialog.askopenfilename(
            title="Select input .txt file",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if path:
            self.input_file.set(path)
            if not self.out_prefix.get():
                self.out_prefix.set(str(Path(path).with_suffix("")))

    def _inspect_device(self) -> None:
        if not self.devices.get():
            self.status.set("Select a USB device first (Refresh USB)")
            return
        try:
            vid, pid = self._parse_vid_pid(self.devices.get())
        except ValueError as exc:
            messagebox.showerror("Parse error", str(exc))
            return
        try:
            from .inputs.usb_capture import USBDeviceCapture
            lines = USBDeviceCapture.inspect_device(vid, pid)
        except RuntimeError as exc:
            messagebox.showerror("Inspect failed", str(exc))
            return
        except Exception as exc:
            messagebox.showerror("Inspect failed", str(exc))
            return
        for line in lines:
            self._sys_log_append(line)
        self.status.set(f"Inspected VID=0x{vid:04X} PID=0x{pid:04X}")

    @staticmethod
    def _group_grl_packets(packet_rows: list[tuple]) -> list[list[tuple]]:
        groups: list[list[tuple]] = []
        current: list[tuple] = []
        current_key = None
        last_buf_idx = -1

        for row in packet_rows:
            pkt = row[0]
            key = (pkt.channel, pkt.seq_num)
            if current and (key != current_key or pkt.buf_idx <= last_buf_idx):
                groups.append(current)
                current = []
                last_buf_idx = -1
            current.append(row)
            current_key = key
            last_buf_idx = pkt.buf_idx

        if current:
            groups.append(current)
        return groups

    @staticmethod
    def _decode_grl_packet_group(group: list[tuple], decoder: GRLBMCDecoder) -> tuple[str, int, list[RawFrame]]:
        first_pkt, ts_us, wall_ts_us = group[0]
        edge_bytes = b"".join(
            pkt.payload
            for pkt, _, _ in sorted(group, key=lambda row: row[0].buf_idx)
        )
        frames = decoder.feed(
            edge_bytes,
            ts_us,
            wall_timestamp_us=wall_ts_us,
            debug_context=USBPDGuiApp._bmc_debug_group_context(group),
        )
        for frame in frames:
            frame.source = f"{frame.source}_{first_pkt.cc_line.lower()}"
        return first_pkt.cc_line, ts_us, frames

    @staticmethod
    def _bmc_debug_group_context(group: list[tuple]) -> str:
        first_pkt, group_ts_us, wall_ts_us = group[0]
        lines = [
            (
                f"GRL_GROUP cc={first_pkt.cc_line} seq={first_pkt.seq_num} "
                f"packets={len(group)} device_ts_us={group_ts_us} wall_ts_us={wall_ts_us}"
            )
        ]
        for pkt, device_ts_us, raw_ts_us in sorted(group, key=lambda row: row[0].buf_idx):
            lines.append(
                f"  RAW64 buf={pkt.buf_idx} device_ts_us={device_ts_us} wall_ts_us={raw_ts_us} "
                f"packet={pkt.raw.hex()} edges={pkt.payload.hex()}"
            )
        return "\n".join(lines)

    @staticmethod
    def _hex_bytes(data: bytes) -> str:
        return " ".join(f"{b:02x}" for b in data)

    @staticmethod
    def _trace_group_lines(
        group: list[tuple],
        bmc_frames: list[RawFrame],
        device_t0_us: Optional[int],
    ) -> list[str]:
        first_pkt = group[0][0]
        group_ts_us = group[0][1]
        rel_group_ts_us = group_ts_us - device_t0_us if device_t0_us is not None else group_ts_us
        buf_list = ",".join(str(row[0].buf_idx) for row in group)
        lines = [
            (
                f"@GRL cc={first_pkt.cc_line} seq={first_pkt.seq_num} "
                f"bufs=[{buf_list}] rel_us={rel_group_ts_us} packets={len(group)}"
            )
        ]

        for pkt, device_ts_us, wall_ts_us in sorted(group, key=lambda row: row[0].buf_idx):
            rel_us = device_ts_us - device_t0_us if device_t0_us is not None else device_ts_us
            lines.append(
                f"  RAW buf={pkt.buf_idx} rel_us={rel_us} wall_us={wall_ts_us} "
                f"edges={USBPDGuiApp._hex_bytes(pkt.payload)}"
            )

        if not bmc_frames:
            lines.append("  DECODE none")
            return lines

        pd_decoder = PDDecoder()
        for frame in bmc_frames:
            frame_rel_us = frame.timestamp_us - device_t0_us if device_t0_us is not None else frame.timestamp_us
            meta = frame.metadata
            frame_parts = [
                f"  FRAME rel_us={frame_rel_us}",
                f"payload={frame.payload.hex()}",
                f"source={frame.source}",
            ]
            if meta:
                if "frame_anchor" in meta:
                    frame_parts.append(f"anchor={meta['frame_anchor']}")
                if "half_ui_ticks" in meta:
                    frame_parts.append(f"half_ui={meta['half_ui_ticks']:.3f}")
                if "crc_mode" in meta:
                    frame_parts.append(f"crc={meta['crc_mode']}")
                if "recovery_score" in meta:
                    frame_parts.append(f"score={meta['recovery_score']:.1f}")
            lines.append(" ".join(frame_parts))

            msgs = pd_decoder.decode([frame])
            if not msgs:
                lines.append("    PD none")
                continue
            for msg in msgs:
                lines.append(f"    PD {message_line(msg)}")

        return lines

    @staticmethod
    def _trace_direct_lines(frame: RawFrame, device_t0_us: Optional[int]) -> list[str]:
        rel_us = frame.timestamp_us - device_t0_us if device_t0_us is not None else frame.timestamp_us
        lines = [
            f"@DIRECT rel_us={rel_us} payload={USBPDGuiApp._hex_bytes(frame.payload)} source={frame.source}"
        ]
        msgs = PDDecoder().decode([frame])
        if not msgs:
            lines.append("  PD none")
            return lines
        for msg in msgs:
            lines.append(f"  PD {message_line(msg)}")
        return lines

    @staticmethod
    def _trace_vbus_lines(sample, raw_chunk: bytes, timestamp_us: int, device_t0_us: Optional[int]) -> list[str]:
        rel_us = timestamp_us - device_t0_us if device_t0_us is not None else timestamp_us
        return [
            (
                f"@VBUS rel_us={rel_us} voltage={sample.voltage_v:.6f}V "
                f"current={sample.current_a:.9f}A power={sample.power_w:.6f}W "
                f"temp={sample.temp_c:.3f}C raw={USBPDGuiApp._hex_bytes(raw_chunk)}"
            )
        ]

    @staticmethod
    def _power_log_line(sample, rel_ms: float, source: str) -> str:
        return (
            f"{rel_ms:>10.3f} | {source:>10} | {sample.voltage_v:>9.6f} | "
            f"{sample.current_a:>11.9f} | {sample.power_w:>10.6f} | {sample.temp_c:>8.3f} | "
            f"{sample.voltage_count:04X} {sample.temp_count:03X} "
            f"{(sample.current_count & 0xFFFF):04X} {sample.power_count:06X}"
        )

    # =========================================================================
    # Queue plumbing
    # =========================================================================

    def _sys_log_append(self, line: str) -> None:
        self._sys_log.configure(state="normal")
        self._sys_log.insert(END, line + "\n")
        self._sys_log.see(END)
        self._sys_log.configure(state="disabled")

    def _pump_queues(self) -> None:
        # System log lines
        while True:
            try:
                self._sys_log_append(self.log_queue.get_nowait())
            except queue.Empty:
                break

        # PD events -> Treeview + plot accumulator
        got_pd_event = False
        while True:
            try:
                ev = self.pd_event_queue.get_nowait()
            except queue.Empty:
                break

            self._pd_events_all.append(ev)
            got_pd_event = True

            if ev.get("contract_change"):
                v = ev.get("voltage_v") or 0.0
                a = ev.get("current_a") or 0.0
                self._contract_label.set(f"{v:.1f} V @ {a:.1f} A")

        if got_pd_event:
            self._pd_events_all.sort(
                key=lambda ev: (
                    ev.get("timestamp_us", 0),
                    ev.get("event_seq", 0),
                )
            )
            self._render_pd_tree()

        self.root.after(150, self._pump_queues)

    # =========================================================================
    # Plot
    # =========================================================================

    def _draw_empty_plot(self) -> None:
        if not _MPL_OK or self._fig is None:
            return
        self._fig.clear()
        self._ax1 = self._fig.add_subplot(111)
        self._ax2 = self._ax1.twinx()
        self._ax1.set_xlabel("Time (ms)")
        self._ax1.set_ylabel("VBUS Voltage (V)", color="#1a5fa8")
        self._ax2.set_ylabel("Current (A)",       color="#e07b00")
        self._ax1.set_xlim(0, 100)
        self._ax1.set_ylim(0, 25)
        self._ax2.set_ylim(0, 6)
        self._ax1.grid(True, alpha=0.3)
        self._ax1.set_title("VBUS Voltage & Current vs Time")
        self._fig.tight_layout(pad=2.0)
        if self._canvas:
            self._canvas.draw_idle()

    def _on_plot_interact(self, event) -> None:
        if event.button is not None:
            self._scroll_live.set(False)

    def _on_pd_row_select(self, _event) -> None:
        sel = self.pd_list.selection()
        if not sel:
            return
        iid = sel[0]
        t_str = self.pd_list.set(iid, "time_ms")
        try:
            self._selected_time_ms = float(t_str)
        except ValueError:
            return
        if _MPL_OK and self._canvas:
            self._redraw_plot(pan_to_selection=True)
        # Update inline detail panel
        idx = self._iid_to_event.get(iid)
        if idx is not None and idx < len(self._pd_events_all):
            self._show_packet_detail(self._pd_events_all[idx])

    def _render_pd_tree(self) -> None:
        self._iid_to_event.clear()
        for iid in self.pd_list.get_children():
            self.pd_list.delete(iid)

        last_iid = None
        for idx, ev in enumerate(self._pd_events_all):
            if ev.get("sample_type") == "vbus_telemetry":
                continue
            t_ms  = ev.get("timestamp_ms", 0.0)
            dir_  = ev.get("direction", "")
            mtype = _friendly_type(ev.get("message_type", ""))
            v_str = f"{ev['voltage_v']:.2f}" if ev.get("voltage_v") is not None else "-"
            a_str = f"{ev['current_a']:.2f}" if ev.get("current_a") is not None else "-"
            hdr   = f"0x{ev.get('header', 0):04X}"

            tags = []
            if "SRC->SNK" in dir_:
                tags.append("src_snk")
            elif "SNK->SRC" in dir_:
                tags.append("snk_src")
            if ev.get("contract_change"):
                tags.append("contract")

            last_iid = self.pd_list.insert(
                "", END,
                values=(f"{t_ms:.3f}", dir_, mtype, v_str, a_str, hdr),
                tags=tags,
            )
            self._iid_to_event[last_iid] = idx

        if self._scroll_live.get() and last_iid:
            self.pd_list.see(last_iid)

    def _show_packet_detail(self, ev: dict) -> None:
        """Populate the inline detail treeview with decoded fields for one PD message."""
        tree = self._detail_tree
        for iid in tree.get_children():
            tree.delete(iid)

        header = ev.get("header", 0)
        mtype  = ev.get("message_type", "")
        words  = ev.get("payload_words", [])

        # Helper: insert a row into the detail treeview
        def add(offset, length, field, value, desc="", hexval=""):
            tree.insert("", END, values=(offset, length, field, value, desc, hexval))

        if ev.get("sample_type") == "vbus_telemetry":
            raw_hex = ev.get("raw_hex", "")
            add("0", "4", "Keyword", "AA AA AA AA", "VBUS telemetry packet", "AA AA AA AA")
            add("4", "2", "Voltage Count", f"0x{ev.get('voltage_count', 0):04X}",
                f"{ev.get('voltage_v', 0.0):.6f} V", "")
            add("6", "2", "Temp Count", f"0x{ev.get('temp_count', 0):03X}",
                f"{ev.get('temp_c', 0.0):.3f} C", "")
            add("8", "2", "Current Count", f"0x{ev.get('current_count_raw', 0):04X}",
                f"{ev.get('current_a', 0.0):.9f} A", "")
            add("10", "3", "Power Count", f"0x{ev.get('power_count', 0):06X}",
                f"{ev.get('power_w', 0.0):.6f} W", "")
            if raw_hex:
                add("", "", "Raw Packet", raw_hex, "", raw_hex)
            if ev.get("notes"):
                add("", "", "Notes", ev["notes"], "", "")
            return

        # ── Header decode (bytes 0-1) ────────────────────────────────
        msg_type_code   = header & 0x1F
        port_data_role  = (header >> 5) & 1
        spec_rev        = (header >> 6) & 3
        port_power_role = (header >> 8) & 1
        msg_id          = (header >> 9) & 7
        num_obj         = (header >> 12) & 7
        extended        = (header >> 15) & 1

        spec_names = {0: "1.0", 1: "2.0", 2: "3.0", 3: "reserved"}
        hdr_hex = f"{header & 0xFF:02X} {(header >> 8) & 0xFF:02X}"

        add("0", "2", "PD Header", f"0x{header:04X}", mtype, hdr_hex)
        add("0.0", "5b", "  Message Type", f"{msg_type_code} (0x{msg_type_code:02X})", mtype, "")
        add("0.5", "1b", "  Data Role", f"{port_data_role}", "DFP" if port_data_role else "UFP", "")
        add("0.6", "2b", "  Spec Rev", f"{spec_rev}", spec_names.get(spec_rev, "?"), "")
        add("1.0", "1b", "  Power Role", f"{port_power_role}", "Source" if port_power_role else "Sink", "")
        add("1.1", "3b", "  Message ID", f"{msg_id}", "", "")
        add("1.4", "3b", "  Num Data Obj", f"{num_obj}", "", "")
        add("1.7", "1b", "  Extended", f"{extended}", "", "")

        # ── Data Objects ──────────────────────────────────────────────
        for i, w in enumerate(words):
            obj_offset = 2 + i * 4
            w_hex = " ".join(f"{(w >> (b*8)) & 0xFF:02X}" for b in range(4))
            add(f"{obj_offset}", "4", f"Data Object [{i+1}]", f"0x{w:08X}", "", w_hex)

            if mtype == "Source_Caps":
                pdo_type = (w >> 30) & 3
                add("", "2b", "  PDO Type", f"{pdo_type}", ["Fixed","Battery","Variable","Augmented (PPS)"][pdo_type], "")
                if pdo_type == 0:
                    v = ((w >> 10) & 0x3FF) * 0.05
                    a = (w & 0x3FF) * 0.01
                    add("", "10b", "  Voltage", f"{v:.2f} V", f"bits[19:10] = {(w>>10)&0x3FF}", "")
                    add("", "10b", "  Max Current", f"{a:.2f} A", f"bits[9:0] = {w&0x3FF}", "")
                    add("", "1b", "  Dual-Role Power", f"{(w>>29)&1}", "", "")
                    add("", "1b", "  USB Suspend", f"{(w>>28)&1}", "", "")
                    add("", "1b", "  Unconstrained", f"{(w>>27)&1}", "", "")
                    add("", "1b", "  USB Comms", f"{(w>>26)&1}", "", "")
                    add("", "1b", "  Dual-Role Data", f"{(w>>25)&1}", "", "")
                    add("", "2b", "  Peak Current", f"{(w>>20)&3}", "", "")
                elif pdo_type == 3:
                    max_v = ((w >> 17) & 0xFF) * 0.1
                    min_v = ((w >> 8) & 0xFF) * 0.1
                    max_a = (w & 0x7F) * 0.05
                    add("", "", "  Max Voltage", f"{max_v:.1f} V", "", "")
                    add("", "", "  Min Voltage", f"{min_v:.1f} V", "", "")
                    add("", "", "  Max Current", f"{max_a:.2f} A", "", "")

            elif mtype == "Sink_Caps":
                pdo_type = (w >> 30) & 3
                add("", "2b", "  PDO Type", f"{pdo_type}", ["Fixed","Battery","Variable","Augmented"][pdo_type], "")
                if pdo_type == 0:
                    v = ((w >> 10) & 0x3FF) * 0.05
                    a = (w & 0x3FF) * 0.01
                    add("", "10b", "  Voltage", f"{v:.2f} V", f"bits[19:10]", "")
                    add("", "10b", "  Op Current", f"{a:.2f} A", f"bits[9:0]", "")

            elif mtype == "Request":
                obj_pos = (w >> 28) & 0xF
                give_back = (w >> 27) & 1
                cap_mismatch = (w >> 26) & 1
                usb_comm = (w >> 25) & 1
                no_usb_suspend = (w >> 24) & 1
                unchunked = (w >> 23) & 1
                op_cur = ((w >> 10) & 0x3FF) * 0.01
                max_cur = (w & 0x3FF) * 0.01
                add("", "4b", "  Object Position", f"{obj_pos}", f"PDO #{obj_pos}", "")
                add("", "10b", "  Op Current", f"{op_cur:.2f} A", f"bits[19:10]", "")
                add("", "10b", "  Max Current", f"{max_cur:.2f} A", f"bits[9:0]", "")
                add("", "1b", "  GiveBack", f"{give_back}", "", "")
                add("", "1b", "  Cap Mismatch", f"{cap_mismatch}", "", "")
                add("", "1b", "  USB Comms", f"{usb_comm}", "", "")
                add("", "1b", "  No USB Suspend", f"{no_usb_suspend}", "", "")
                add("", "1b", "  Unchunked Ext", f"{unchunked}", "", "")

            elif mtype == "Vendor_Defined":
                if i == 0:
                    cmd_type = (w >> 6) & 3
                    cmd = w & 0x1F
                    svid = (w >> 16) & 0xFFFF
                    vdm_type = (w >> 15) & 1
                    cmd_names = {0: "reserved", 1: "REQ", 2: "ACK", 3: "NAK"}
                    add("", "16b", "  SVID", f"0x{svid:04X}", "", "")
                    add("", "1b", "  VDM Type", f"{vdm_type}",
                        "Structured" if vdm_type else "Unstructured", "")
                    add("", "2b", "  Cmd Type", f"{cmd_type}",
                        cmd_names.get(cmd_type, "?"), "")
                    add("", "5b", "  Command", f"{cmd}", "", "")

        if ev.get("notes"):
            add("", "", "Notes", ev["notes"], "", "")

    def _scroll_to_live(self) -> None:
        self._scroll_live.set(True)
        self._selected_time_ms = None
        self._manual_xlim = None
        if _MPL_OK and self._canvas:
            self._redraw_plot()

    def _adjust_plot_zoom(self, factor: float) -> None:
        if not _MPL_OK or self._canvas is None or self._ax1 is None:
            return
        self._scroll_live.set(False)
        left, right = self._ax1.get_xlim()
        width = max(10.0, (right - left) * factor)
        center = self._selected_time_ms if self._selected_time_ms is not None else (left + right) / 2.0
        new_left = max(0.0, center - (width / 2.0))
        self._manual_xlim = (new_left, new_left + width)
        self._redraw_plot()

    def _toggle_plot_pause(self) -> None:
        self._plot_paused = not self._plot_paused
        self.status.set(
            "Plot paused (capture continues)" if self._plot_paused else "Plot resumed")

    def _clear_plot(self) -> None:
        self._pd_events_all.clear()
        self._iid_to_event.clear()
        for iid in self.pd_list.get_children():
            self.pd_list.delete(iid)
        self._t0_us = None
        self._device_t0_us = None

        self._selected_time_ms = None
        self._manual_xlim = None
        self._contract_label.set("—")
        self._draw_empty_plot()

    def _update_plot(self) -> None:
        if not self._plot_paused and _MPL_OK and self._canvas and self._pd_events_all:
            self._redraw_plot()
        self.root.after(500, self._update_plot)

    def _redraw_plot(self, pan_to_selection: bool = False) -> None:
        if self._fig is None or self._canvas is None:
            return

        events = self._pd_events_all
        telemetry_evts = [e for e in events if e.get("sample_type") == "vbus_telemetry"]

        telemetry_times = [e["timestamp_ms"] for e in telemetry_evts]
        telemetry_voltages = [e.get("voltage_v") or 0.0 for e in telemetry_evts]
        telemetry_currents = [e.get("current_a") or 0.0 for e in telemetry_evts]

        if events:
            t_end = events[-1]["timestamp_ms"] * 1.05 + 10
        else:
            t_end = 100.0

        self._fig.clear()
        self._ax1 = self._fig.add_subplot(111)
        self._ax2 = self._ax1.twinx()
        ax1, ax2 = self._ax1, self._ax2

        if telemetry_times:
            ax1.plot(
                telemetry_times,
                telemetry_voltages,
                color="#1a5fa8",
                linewidth=2.2,
                linestyle="-",
                marker="o",
                markersize=2.8,
                label="Measured Voltage (V)",
                zorder=4,
            )
            ax2.plot(
                telemetry_times,
                telemetry_currents,
                color="#e07b00",
                linewidth=1.8,
                linestyle="-",
                marker="o",
                markersize=2.4,
                label="Measured Current (A)",
                zorder=4,
            )

        if self._selected_time_ms is not None:
            ax1.axvline(
                x=self._selected_time_ms,
                color="#00aa44",
                linewidth=2.2,
                alpha=0.85,
                zorder=5,
                label=f"Selected {self._selected_time_ms:.3f} ms",
            )

        ax1.set_xlabel("Time (ms)")
        ax1.set_ylabel("VBUS Voltage (V)", color="#1a5fa8")
        ax1.tick_params(axis="y", labelcolor="#1a5fa8")
        ax2.set_ylabel("Current (A)", color="#e07b00")
        ax2.tick_params(axis="y", labelcolor="#e07b00")
        ax1.set_title("VBUS Voltage & Current vs Time")
        ax1.grid(True, alpha=0.25)

        legend_handles = []
        legend_names = []
        for axis in (ax1, ax2):
            handles, names = axis.get_legend_handles_labels()
            legend_handles.extend(handles)
            legend_names.extend(names)
        if legend_handles:
            ax1.legend(legend_handles, legend_names, loc="upper left", fontsize=8)

        x_max = max(
            t_end,
            telemetry_times[-1] if telemetry_times else 0.0,
        )
        if self._scroll_live.get():
            x_min = max(0, x_max - 2000)
            x_lim = (x_min, x_max + 50)
        elif pan_to_selection and self._selected_time_ms is not None:
            half = max(200, x_max * 0.08)
            x_lim = (
                max(0, self._selected_time_ms - half),
                self._selected_time_ms + half,
            )
        elif self._manual_xlim is not None:
            x_lim = self._manual_xlim
        else:
            x_lim = (0, x_max)

        ax1.set_xlim(*x_lim)

        max_v = max(telemetry_voltages) if telemetry_voltages else 0.0
        max_a = max(telemetry_currents) if telemetry_currents else 0.0

        ax1.set_ylim(0, max(25, max_v * 1.2))
        ax2.set_ylim(min(0, min(telemetry_currents) if telemetry_currents else 0.0), max(6, max_a * 1.2))
        self._fig.tight_layout(pad=2.0)
        self._canvas.draw_idle()

    # =========================================================================
    # Offline decode
    # =========================================================================

    def _decode_offline(self) -> None:
        try:
            input_path = Path(self.input_file.get())
            if not input_path.exists():
                raise ValueError("Select a valid input .txt file")

            text = input_path.read_text(encoding="utf-8-sig")
            fmt  = self.input_format.get()
            trace_sections: list[tuple[int, list[str]]] = []
            power_samples: list[tuple[int, object, str]] = []
            if fmt == "auto":
                hex_pairs = len(re.findall(r"\b[0-9a-fA-F]{2}\b", text))
                fmt = "usblyzer" if hex_pairs >= 64 and (hex_pairs % 64 == 0) else "raw"

            if fmt == "usblyzer":
                tw_parser = TwinkieUSBlyzerParser()
                recs  = tw_parser.parse_text(text)
                gaps  = tw_parser.sequence_gaps(recs)
                tw_dec = TwinkieBMCDecoder()
                tick  = self.tick_ns.get() if self.tick_ns.get() > 0 else None
                analysis, frames = tw_dec.decode(recs, sequence_gaps=gaps, tick_ns=tick)
                self._sys_log_append(
                    f"Twinkie: records={analysis.total_records} "
                    f"edges={analysis.total_edges}")
                self._sys_log_append(
                    f"Twinkie: skipped={analysis.skipped_records} "
                    f"(non-CC: {analysis.skipped_non_cc}, zero-payload: {analysis.skipped_zero_payload})"
                )
                self._write_normalized(input_path, frames)
            else:
                raw_parser = RawFrameParser()
                frames = raw_parser.parse_lines(text.splitlines(), source="file")
                if frames and len(frames[0].payload) == GRL_PACKET_SIZE:
                    self._sys_log_append("Detected GRL 64-byte format - stripping GRL header")
                    ts_state = {
                        0: {"epoch_ms": 0, "last_expanded_ms": None},
                        1: {"epoch_ms": 0, "last_expanded_ms": None},
                    }
                    timeline_items: list[tuple[str, int, object]] = []
                    for fr in frames:
                        vbus_sample = parse_grl_vbus_telemetry(fr.payload)
                        if vbus_sample is not None:
                            power_samples.append((fr.timestamp_us, vbus_sample, "offline"))
                            timeline_items.append(("vbus", fr.timestamp_us, vbus_sample))
                            continue
                        direct_pd = extract_grl_direct_pd_payload(fr.payload)
                        if direct_pd is not None:
                            direct_frame = RawFrame(
                                timestamp_us=fr.timestamp_us,
                                payload=direct_pd,
                                source="grl_direct",
                            )
                            timeline_items.append(("direct", direct_frame.timestamp_us, direct_frame))
                            continue
                        pkt = parse_grl_packet(fr.payload)
                        if not pkt:
                            continue
                        ts_us = update_grl_timestamp_state(ts_state[pkt.channel], pkt)
                        self._sys_log_append(format_grl_packet(pkt, ts_us))
                        timeline_items.append(("grl", ts_us, (pkt, ts_us, fr.timestamp_us)))

                    trace_t0_us: Optional[int] = None
                    trace_candidates = [timestamp_us for _, timestamp_us, _ in timeline_items]
                    if trace_candidates:
                        trace_t0_us = min(trace_candidates)
                    bmc_decoders = {0: GRLBMCDecoder(), 1: GRLBMCDecoder()}
                    frames = []
                    for kind, timestamp_us, item in timeline_items:
                        if kind == "vbus":
                            for dec in bmc_decoders.values():
                                dec.reset_stream()
                            sample = item
                            trace_sections.append((
                                timestamp_us,
                                self._trace_vbus_lines(sample, sample.raw, timestamp_us, trace_t0_us),
                            ))
                            continue

                        if kind == "direct":
                            for dec in bmc_decoders.values():
                                dec.reset_stream()
                            direct_frame = item
                            trace_sections.append((
                                direct_frame.timestamp_us,
                                self._trace_direct_lines(direct_frame, trace_t0_us),
                            ))
                            frames.append(direct_frame)
                            continue

                        pkt, device_ts_us, raw_ts_us = item
                        if pkt.is_idle:
                            bmc_decoders[pkt.channel].reset_stream()
                            continue

                        row = [(pkt, device_ts_us, raw_ts_us)]
                        _, _, bmc_frames = self._decode_grl_packet_group(
                            row,
                            bmc_decoders[pkt.channel],
                        )
                        trace_sections.append((
                            device_ts_us,
                            self._trace_group_lines(row, bmc_frames, trace_t0_us),
                        ))
                        frames.extend(bmc_frames)
                    frames.sort(key=lambda fr: (fr.timestamp_us, fr.source))

            pd_messages = PDDecoder().decode(frames)
            for msg in pd_messages:
                self._sys_log_append(message_line(msg))

            if trace_sections:
                trace_lines: list[str] = []
                for _, section_lines in sorted(trace_sections, key=lambda item: item[0]):
                    trace_lines.extend(section_lines)
                    trace_lines.append("")
                self._write_trace_output(input_path, trace_lines)
            if power_samples:
                self._write_power_output(input_path, power_samples)
            self._write_outputs(input_path, pd_messages)
            self.status.set(f"Offline decode complete: {len(pd_messages)} message(s)")
        except Exception as exc:
            messagebox.showerror("Decode error", str(exc))
            self.status.set(f"Decode failed: {exc}")

    def _write_normalized(self, input_path: Path, frames: list) -> None:
        out = (Path(self.out_prefix.get()) if self.out_prefix.get()
               else input_path.with_suffix("")).with_suffix(".normalized.txt")
        lines = [f"{fr.timestamp_us} {' '.join(f'{b:02x}' for b in fr.payload)}"
                 for fr in frames]
        out.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        self._sys_log_append(f"Wrote: {out}")

    def _write_trace_output(self, input_path: Path, lines: list[str]) -> None:
        prefix = (Path(self.out_prefix.get()) if self.out_prefix.get()
                  else input_path.with_suffix(""))
        out_trace = prefix.with_suffix(".trace.txt")
        body = "\n".join(lines).rstrip()
        out_trace.write_text(body + ("\n" if body else ""), encoding="utf-8")
        self._sys_log_append(f"Wrote: {out_trace}")

    def _write_power_output(self, input_path: Path, samples: list[tuple[int, object, str]]) -> None:
        prefix = (Path(self.out_prefix.get()) if self.out_prefix.get()
                  else input_path.with_suffix(""))
        out_power = prefix.with_suffix(".power.txt")
        if samples:
            t0_us = min(sample[0] for sample in samples)
        else:
            t0_us = 0
        lines = [
            "# Time_ms   | Source     | Voltage_V | Current_A   | Power_W    | Temp_C   | Counts(V/T/I/P)"
        ]
        for timestamp_us, sample, source in sorted(samples, key=lambda item: item[0]):
            rel_ms = (timestamp_us - t0_us) / 1000.0
            lines.append(self._power_log_line(sample, rel_ms, source))
        out_power.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        self._sys_log_append(f"Wrote: {out_power}")

    def _write_outputs(self, input_path: Path, messages: list) -> None:
        prefix = (Path(self.out_prefix.get()) if self.out_prefix.get()
                  else input_path.with_suffix(""))
        out_txt = prefix.with_suffix(".decoded.txt")
        out_txt.write_text(
            "\n".join(message_line(m) for m in messages) + ("\n" if messages else ""),
            encoding="utf-8")
        self._sys_log_append(f"Wrote: {out_txt}")

        if self.json_out.get():
            out_json = prefix.with_suffix(".decoded.json")
            out_json.write_text(
                json.dumps([m.to_dict() for m in messages], indent=2), encoding="utf-8")
            self._sys_log_append(f"Wrote: {out_json}")

    # =========================================================================
    # Live capture
    # =========================================================================

    def _start_live(self) -> None:
        if self.worker and self.worker.is_alive():
            self.status.set("Live capture is already running")
            return
        if not self.devices.get():
            self._refresh_usb_devices()
            if not self.devices.get():
                return
        try:
            vid, pid  = self._parse_vid_pid(self.devices.get())
            endpoint  = int(self.endpoint.get(), 0)
            iface     = int(self.interface.get())
            timeout   = int(self.timeout_ms.get())
            out_ep    = int(self.init_out_ep.get(), 0)
            init_raw  = (bytes.fromhex(self.init_hex.get().replace(" ", ""))
                         if self.init_hex.get().strip() else b"")
        except Exception as exc:
            messagebox.showerror("Invalid config", str(exc))
            return

        self.stop_event.clear()
        self._t0_us = None
        self._device_t0_us = None
        self.worker = threading.Thread(
            target=self._live_worker,
            args=(vid, pid, endpoint, iface, timeout,
                  self.all_endpoints.get(), self.show_raw.get(),
                  self.auto_log_live.get(), init_raw, out_ep),
            daemon=True,
        )
        self.worker.start()
        self.status.set("Live capture started")

    def _stop_live(self) -> None:
        self.stop_event.set()
        self.status.set("Stopping live capture...")

    # -------------------------------------------------------------------------
    # Worker thread
    # -------------------------------------------------------------------------

    def _live_worker(
        self,
        vid: int, pid: int, endpoint: int, iface: int, timeout: int,
        all_ep: bool, show_raw: bool, auto_log_live: bool,
        init_bytes: bytes = b"", init_out_ep: int = 0x01,
    ) -> None:
        try:
            from .inputs.usb_capture import USBDeviceCapture
        except ModuleNotFoundError:
            self.log_queue.put("PyUSB not installed — pip install pyusb")
            return

        # Open live log files
        raw_fp = decoded_fp = pd_fp = trace_fp = power_fp = None
        logs_dir = None
        pd_decoder = PDDecoder()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if auto_log_live:
            logs_dir = Path.cwd() / "live_logs"
            logs_dir.mkdir(exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            raw_fp = open(logs_dir / f"{stamp}_raw.txt", "a", encoding="utf-8", buffering=64 * 1024)
            decoded_fp = open(logs_dir / f"{stamp}_decoded.txt", "a", encoding="utf-8", buffering=64 * 1024)
            pd_fp = open(logs_dir / f"{stamp}_pd.txt", "a", encoding="utf-8", buffering=64 * 1024)
            trace_fp = open(logs_dir / f"{stamp}_trace.txt", "a", encoding="utf-8", buffering=64 * 1024)
            power_fp = open(logs_dir / f"{stamp}_power.txt", "a", encoding="utf-8", buffering=64 * 1024)
            ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            raw_fp.write(
                f"# Raw USB capture — {ts_str}\n"
                f"# Format: <rel_us>  ep=<source>  <hex bytes>\n")
            decoded_fp.write(f"# Decoded USB-PD capture — {ts_str}\n")
            pd_fp.write(
                f"# PD-only log with voltage / current — {ts_str}\n"
                f"# {'Time_ms':>10} | {'Direction':>14} | {'Message_Type':<22} | "
                f"{'Voltage_V':>9} | {'Current_A':>9} | Notes\n")
            trace_fp.write(
                f"# Correlated GRL trace â€” {ts_str}\n"
                f"# Non-idle packet groups only; continuous 60-byte zero payload packets are suppressed.\n"
            )
            power_fp.write(
                f"# VBUS telemetry log - {ts_str}\n"
                f"# {'Time_ms':>10} | {'Source':>10} | {'Voltage_V':>9} | {'Current_A':>11} | "
                f"{'Power_W':>10} | {'Temp_C':>8} | Counts(V/T/I/P)\n"
            )
            self.log_queue.put(f"Logging to: {logs_dir}/{stamp}_[raw|decoded|pd|trace|power].txt")

        # PD power-contract state machine
        src_caps_pdos = []   # list[FixedPDO] from latest Source_Caps
        pending_rdo   = None # RDOInfo or None from latest Request

        # Edge-timestamp BMC decoder (handles GRL 60-byte edge payloads)
        bmc_debug_fp = None
        if logs_dir:
            bmc_debug_path = logs_dir / f"{stamp}_bmc_debug.txt"
            bmc_debug_fp = open(bmc_debug_path, "w", encoding="utf-8", buffering=64 * 1024)
            bmc_debug_fp.write(f"# BMC decode intermediate log — {ts_str}\n")
            self.log_queue.put(f"BMC debug log: {bmc_debug_path}")
        bmc_decoders = {
            0: GRLBMCDecoder(debug_file=bmc_debug_fp),  # CC1
            1: GRLBMCDecoder(debug_file=bmc_debug_fp),  # CC2
        }
        device_ts_state = {
            0: {"epoch_ms": 0, "last_expanded_ms": None},
            1: {"epoch_ms": 0, "last_expanded_ms": None},
        }
        next_flush_t = time.monotonic() + LIVE_LOG_FLUSH_INTERVAL_S

        def flush_logs(force: bool = False) -> None:
            nonlocal next_flush_t
            now = time.monotonic()
            if not force and now < next_flush_t:
                return
            for fp in (raw_fp, decoded_fp, pd_fp, trace_fp, power_fp, bmc_debug_fp):
                if fp:
                    fp.flush()
            next_flush_t = now + LIVE_LOG_FLUSH_INTERVAL_S

        def reset_bmc_streams(channel: Optional[int] = None) -> None:
            if channel is None:
                for dec in bmc_decoders.values():
                    dec.reset_stream()
                return
            bmc_decoders[channel].reset_stream()

        def emit_bmc_frames(cc_line: str, bmc_frames: list[RawFrame]) -> None:
            nonlocal decoded_total, src_caps_pdos, pending_rdo
            if show_raw:
                if cc_line:
                    self.log_queue.put(
                        f"  [BMC decoded {len(bmc_frames)} frame(s) from {cc_line}]")
                else:
                    self.log_queue.put(
                        f"  [GRL decoded {len(bmc_frames)} direct frame(s)]")

            for bmc_fr in bmc_frames:
                msg = pd_decoder.decode_frame(bmc_fr)
                if msg is None:
                    continue
                decoded_total += 1
                msg_rel_us = (
                    bmc_fr.timestamp_us - self._device_t0_us
                    if self._device_t0_us is not None
                    else bmc_fr.timestamp_us
                )
                msg_rel_ms = msg_rel_us / 1000.0
                direction_str = f"{cc_line} {msg.direction}".strip()
                v_val = None
                a_val = None
                notes = ""
                contract = False
                mtype = msg.message_type

                if mtype == "Source_Caps":
                    src_caps_pdos = parse_src_caps(msg.payload_words)
                    if src_caps_pdos:
                        notes = "PDOs: " + ", ".join(
                            f"{p.voltage_v:.0f}V@{p.current_a:.1f}A"
                            for p in src_caps_pdos
                        )
                elif mtype == "Request":
                    if msg.payload_words:
                        pending_rdo = parse_rdo(msg.payload_words[0])
                        pdo = next(
                            (p for p in src_caps_pdos
                             if p.index == pending_rdo.object_position),
                            None,
                        )
                        if pdo:
                            v_val = pdo.voltage_v
                            a_val = pending_rdo.op_current_a
                            notes = (
                                f"PDO#{pending_rdo.object_position}  "
                                f"op={pending_rdo.op_current_a:.1f}A "
                                f"max={pending_rdo.max_current_a:.1f}A"
                            )
                        else:
                            notes = f"PDO#{pending_rdo.object_position} (caps missing)"
                elif mtype == "PS_RDY":
                    if pending_rdo and src_caps_pdos:
                        pdo = next(
                            (p for p in src_caps_pdos
                             if p.index == pending_rdo.object_position),
                            None,
                        )
                        if pdo:
                            v_val = pdo.voltage_v
                            a_val = pending_rdo.op_current_a
                            contract = True
                            notes = f"[CONTRACT: {v_val:.1f}V @ {a_val:.1f}A ACTIVE]"

                self.pd_event_queue.put({
                    "event_seq": self._event_seq,
                    "timestamp_us": msg_rel_us,
                    "timestamp_ms": msg_rel_ms,
                    "direction": direction_str,
                    "message_type": mtype,
                    "header": msg.header,
                    "num_obj": len(msg.payload_words),
                    "payload_words": list(msg.payload_words),
                    "voltage_v": v_val,
                    "current_a": a_val,
                    "contract_change": contract,
                    "notes": notes,
                })
                self._event_seq += 1

                mline = message_line(msg)
                if show_raw:
                    self.log_queue.put(f"  BMC-PD> {mline}")
                if decoded_fp:
                    decoded_fp.write(f"  BMC-PD> {mline}\n")
                if pd_fp:
                    v_str = f"{v_val:.2f}V" if v_val is not None else "-"
                    a_str = f"{a_val:.2f}A" if a_val is not None else "-"
                    pd_fp.write(
                        f"{msg_rel_ms:>10.3f} | {direction_str:>14} | "
                        f"{_friendly_type(mtype):<22} | "
                        f"{v_str:>9} | {a_str:>9} | {notes}\n"
                    )

        def emit_vbus_sample(sample, timestamp_us: int, raw_chunk: bytes, source: str) -> None:
            sample_rel_us = (
                timestamp_us - self._device_t0_us
                if self._device_t0_us is not None
                else timestamp_us
            )
            sample_rel_ms = sample_rel_us / 1000.0
            if power_fp:
                power_fp.write(self._power_log_line(sample, sample_rel_ms, source) + "\n")
            if trace_fp:
                trace_fp.write(
                    "\n".join(self._trace_vbus_lines(sample, raw_chunk, timestamp_us, self._device_t0_us))
                    + "\n\n"
                )

            self.pd_event_queue.put({
                "event_seq": self._event_seq,
                "timestamp_us": sample_rel_us,
                "timestamp_ms": sample_rel_ms,
                "direction": source,
                "message_type": "VBUS_Telemetry",
                "header": 0,
                "num_obj": 0,
                "payload_words": [],
                "voltage_v": sample.voltage_v,
                "current_a": sample.current_a,
                "power_w": sample.power_w,
                "temp_c": sample.temp_c,
                "voltage_count": sample.voltage_count,
                "temp_count": sample.temp_count,
                "current_count_raw": sample.current_count & 0xFFFF,
                "power_count": sample.power_count,
                "raw_hex": sample.raw.hex(),
                "contract_change": False,
                "sample_type": "vbus_telemetry",
                "notes": (
                    f"V={sample.voltage_v:.6f}V I={sample.current_a:.9f}A "
                    f"P={sample.power_w:.6f}W T={sample.temp_c:.3f}C"
                ),
            })
            self._event_seq += 1

        capturer = USBDeviceCapture(
            vid=vid, pid=pid, endpoint=endpoint, interface=iface, timeout_ms=timeout)
        frames_total = decoded_total = 0
        last_frame_t = last_status_t = last_idle_t = time.monotonic()

        try:
            capturer.open(all_endpoints=all_ep)
            ep_list = ", ".join(f"0x{e:02X}" for e in capturer.active_endpoints)
            self.log_queue.put(
                f"Connected: VID=0x{vid:04X} PID=0x{pid:04X}  "
                f"iface={iface}  endpoints: [{ep_list}]")

            if init_bytes:
                self.log_queue.put(
                    f"Init -> OUT EP 0x{init_out_ep:02X}: "
                    + " ".join(f"{b:02x}" for b in init_bytes))
                capturer.send_init(init_bytes, out_endpoint=init_out_ep)
                self.log_queue.put("Init sent — waiting for data...")

            while not self.stop_event.is_set():
                frame = capturer.read_frame()
                now   = time.monotonic()

                # Idle / timeout
                if frame is None:
                    flush_logs()
                    if now - last_idle_t >= 5.0:
                        self.log_queue.put(
                            f"[no data for {now - last_frame_t:.0f}s on [{ep_list}]]")
                        last_idle_t = now
                    if now - last_status_t >= 1.0:
                        self.status.set(
                            f"Live: {frames_total} frames | {decoded_total} PD msgs "
                            f"| idle {now - last_frame_t:.0f}s")
                        last_status_t = now
                    continue

                # Error sentinel from reader thread
                if not frame.payload and frame.source.startswith("ERROR"):
                    self.log_queue.put(f"[reader error] {frame.source}")
                    break

                frames_total += 1
                last_frame_t  = last_idle_t = now

                # Reference timestamp (first frame = t=0)
                if self._t0_us is None:
                    self._t0_us = frame.timestamp_us
                t_rel_us = frame.timestamp_us - self._t0_us
                t_rel_ms = t_rel_us / 1000.0

                # Raw log: every USB read, byte for byte
                if raw_fp:
                    hex_str = " ".join(f"{b:02x}" for b in frame.payload)
                    raw_fp.write(f"{t_rel_us:>10}  ep={frame.source}  {hex_str}\n")

                # -------------------------------------------------------------------
                # GRL 64-byte path.
                #
                # USB read(64) returns exactly one 64-byte GRL packet.
                # If the OS ever batches multiple packets (multiples of 64), we
                # handle each 64-byte chunk independently in the for-loop below.
                # -------------------------------------------------------------------
                n = len(frame.payload)
                if n > 0 and n % GRL_PACKET_SIZE == 0:
                    for offset in range(0, n, GRL_PACKET_SIZE):
                        chunk = frame.payload[offset:offset + GRL_PACKET_SIZE]
                        vbus_sample = parse_grl_vbus_telemetry(chunk)
                        if vbus_sample is not None:
                            reset_bmc_streams()
                            sample_timestamp_us = (
                                (self._device_t0_us or 0) + t_rel_us
                                if self._device_t0_us is not None
                                else t_rel_us
                            )
                            emit_vbus_sample(vbus_sample, sample_timestamp_us, chunk, frame.source)
                            continue
                        direct_pd = extract_grl_direct_pd_payload(chunk)
                        if direct_pd is not None:
                            reset_bmc_streams()
                            direct_frame = RawFrame(
                                timestamp_us=(self._device_t0_us or 0) + t_rel_us,
                                payload=direct_pd,
                                source="grl_direct",
                            )
                            if trace_fp:
                                trace_fp.write(
                                    "\n".join(self._trace_direct_lines(direct_frame, self._device_t0_us))
                                    + "\n\n"
                                )
                            emit_bmc_frames("", [direct_frame])
                            continue
                        grl   = parse_grl_packet(chunk)
                        if grl is None:
                            continue  # next chunk

                        device_ts_us = update_grl_timestamp_state(device_ts_state[grl.channel], grl)
                        if self._device_t0_us is None:
                            self._device_t0_us = device_ts_us
                        device_rel_us = device_ts_us - self._device_t0_us
                        device_rel_ms = device_rel_us / 1000.0
                        grl_line = format_grl_packet(grl, device_rel_us)
                        if show_raw:
                            self.log_queue.put(grl_line)
                        if decoded_fp:
                            decoded_fp.write(grl_line + "\n")

                        ch = grl.channel
                        if grl.is_idle:
                            reset_bmc_streams(ch)
                            continue

                        if grl.overflow and show_raw:
                            self.log_queue.put(
                                f"  [OVF] {grl.cc_line} seq={grl.seq_num} "
                                f"buf={grl.buf_idx} - timestamp extended")

                        row = [(grl, device_ts_us, t_rel_us)]
                        cc_line, _, bmc_frames = self._decode_grl_packet_group(row, bmc_decoders[ch])
                        if trace_fp:
                            trace_fp.write(
                                "\n".join(self._trace_group_lines(row, bmc_frames, self._device_t0_us))
                                + "\n\n"
                            )
                        if bmc_frames:
                            emit_bmc_frames(cc_line, bmc_frames)
                        continue

                        bmc_frames = bmc_decoders[ch].feed(
                            grl.payload,
                            device_ts_us,
                            wall_timestamp_us=t_rel_us,
                        )

                        if not bmc_frames:
                            continue  # accumulating edges

                        # Tag frames with CC line so PDDecoder picks it up
                        for bfr in bmc_frames:
                            bfr.source = f"{bfr.source}_{grl.cc_line.lower()}"

                        if self.show_raw.get():
                            self.log_queue.put(
                                f"  [BMC decoded {len(bmc_frames)} frame(s) "
                                f"from {grl.cc_line}]")

                        for bmc_fr in bmc_frames:
                            msgs = PDDecoder().decode([bmc_fr])
                            decoded_total += len(msgs)
                            for msg in msgs:
                                # Direction from PD header, CC line from GRL packet
                                direction_str = f"{grl.cc_line} {msg.direction}"
                                v_val    = None
                                a_val    = None
                                notes    = ""
                                contract = False
                                mtype    = msg.message_type

                                if mtype == "Source_Caps":
                                    src_caps_pdos = parse_src_caps(msg.payload_words)
                                    if src_caps_pdos:
                                        notes = "PDOs: " + ", ".join(
                                            f"{p.voltage_v:.0f}V@{p.current_a:.1f}A"
                                            for p in src_caps_pdos)
                                elif mtype == "Request":
                                    if msg.payload_words:
                                        pending_rdo = parse_rdo(msg.payload_words[0])
                                        pdo = next(
                                            (p for p in src_caps_pdos
                                             if p.index == pending_rdo.object_position), None)
                                        if pdo:
                                            v_val = pdo.voltage_v
                                            a_val = pending_rdo.op_current_a
                                            notes = (f"PDO#{pending_rdo.object_position}  "
                                                     f"op={pending_rdo.op_current_a:.1f}A "
                                                     f"max={pending_rdo.max_current_a:.1f}A")
                                        else:
                                            notes = f"PDO#{pending_rdo.object_position} (caps missing)"
                                elif mtype == "PS_RDY":
                                    if pending_rdo and src_caps_pdos:
                                        pdo = next(
                                            (p for p in src_caps_pdos
                                             if p.index == pending_rdo.object_position), None)
                                        if pdo:
                                            v_val    = pdo.voltage_v
                                            a_val    = pending_rdo.op_current_a
                                            contract = True
                                            notes    = (f"[CONTRACT: {v_val:.1f}V @ "
                                                        f"{a_val:.1f}A ACTIVE]")

                                self.pd_event_queue.put({
                                    "event_seq": self._event_seq,
                                    "timestamp_us":    device_rel_us,
                                    "timestamp_ms":    device_rel_ms,
                                    "direction":       direction_str,
                                    "message_type":    mtype,
                                    "header":          msg.header,
                                    "num_obj":         len(msg.payload_words),
                                    "payload_words":   list(msg.payload_words),
                                    "voltage_v":       v_val,
                                    "current_a":       a_val,
                                    "contract_change": contract,
                                    "notes":           notes,
                                })
                                self._event_seq += 1
                                mline = message_line(msg)
                                if self.show_raw.get():
                                    self.log_queue.put(f"  BMC-PD> {mline}")
                                if decoded_fp:
                                    decoded_fp.write(f"  BMC-PD> {mline}\n")
                                if pd_fp:
                                    v_str = f"{v_val:.2f}V" if v_val is not None else "—"
                                    a_str = f"{a_val:.2f}A" if a_val is not None else "—"
                                    pd_fp.write(
                                        f"{device_rel_ms:>10.3f} | {direction_str:>14} | "
                                        f"{_friendly_type(mtype):<22} | "
                                        f"{v_str:>9} | {a_str:>9} | {notes}\n")

                    flush_logs()
                    if now - last_status_t >= 1.0:
                        self.status.set(
                            f"Live: {frames_total} frames | {decoded_total} PD msgs")
                        last_status_t = now
                    continue  # skip generic path (continues while loop)

                # Generic (non-GRL-sized) frame path
                hex_str  = " ".join(f"{b:02x}" for b in frame.payload)
                raw_line = (f"RAW [{frame.source}] [{t_rel_us} us] "
                            f"({len(frame.payload)}B)  {hex_str}")
                if show_raw:
                    self.log_queue.put(raw_line)
                if decoded_fp:
                    decoded_fp.write(raw_line + "\n")

                msg = pd_decoder.decode_frame(frame)
                if msg is not None:
                    decoded_total += 1
                    mline = message_line(msg)
                    self.log_queue.put(mline)
                    if decoded_fp:
                        decoded_fp.write(mline + "\n")

                flush_logs()
                if now - last_status_t >= 1.0:
                    self.status.set(
                        f"Live: {frames_total} frames | {decoded_total} PD msgs")
                    last_status_t = now

        except Exception as exc:
            self.log_queue.put(f"Live error: {exc}")
            self.status.set(f"Live failed: {exc}")
        finally:
            for dec in bmc_decoders.values():
                dec.reset()
            capturer.close()
            flush_logs(force=True)
            for fp in (raw_fp, decoded_fp, pd_fp, trace_fp, power_fp, bmc_debug_fp):
                if fp:
                    try:
                        fp.close()
                    except Exception:
                        pass
            self.status.set(
                f"Live stopped — {frames_total} frames, {decoded_total} PD msgs")


def main() -> int:
    root = Tk()
    USBPDGuiApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
