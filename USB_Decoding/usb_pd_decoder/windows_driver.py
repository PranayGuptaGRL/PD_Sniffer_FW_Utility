from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

GRL_VID = 0x227F
GRL_PID = 0x0005
GRL_DEFAULT_INTERFACE = 0
GRL_DEVICE_INTERFACE_GUID = "{14A9A0A1-1FC2-4A58-8BFA-9B5B5D0E67C0}"
GRL_DRIVER_INF = "grl_sniffer_winusb.inf"
GRL_DRIVER_CAT = "grl_sniffer_winusb.cat"


@dataclass(frozen=True)
class DriverPackage:
    name: str
    vid: int
    pid: int
    interface: int
    device_interface_guid: str
    inf_path: Path
    cat_path: Path
    hardware_ids: tuple[str, ...]

    @property
    def has_inf(self) -> bool:
        return self.inf_path.is_file()

    @property
    def has_catalog(self) -> bool:
        return self.cat_path.is_file()


@dataclass(frozen=True)
class DriverInstallResult:
    ok: bool
    message: str
    stdout: str = ""
    stderr: str = ""


def is_windows() -> bool:
    return sys.platform == "win32"


def is_grl_device(vid: int, pid: int) -> bool:
    return vid == GRL_VID and pid == GRL_PID


def get_grl_driver_package() -> DriverPackage:
    driver_dir = Path(__file__).resolve().parent / "drivers"
    hardware_id = f"USB\\VID_{GRL_VID:04X}&PID_{GRL_PID:04X}"
    interface_hardware_id = f"{hardware_id}&MI_{GRL_DEFAULT_INTERFACE:02X}"
    return DriverPackage(
        name="GRL USB PD Sniffer WinUSB",
        vid=GRL_VID,
        pid=GRL_PID,
        interface=GRL_DEFAULT_INTERFACE,
        device_interface_guid=GRL_DEVICE_INTERFACE_GUID,
        inf_path=driver_dir / GRL_DRIVER_INF,
        cat_path=driver_dir / GRL_DRIVER_CAT,
        hardware_ids=(hardware_id, interface_hardware_id),
    )


def bundled_driver_hint(vid: int, pid: int) -> str:
    if is_grl_device(vid, pid):
        return (
            "Use the bundled WinUSB installer from the GUI with 'Install Driver' "
            "or run 'usbpd install-driver', or use the standalone GRL driver installer package."
        )
    return "Bind a WinUSB/libusb-compatible driver to this device before capturing."


def describe_driver_readiness() -> str:
    package = get_grl_driver_package()
    if not package.has_inf:
        return f"Bundled driver INF not found: {package.inf_path}"
    if not package.has_catalog:
        return (
            "Bundled WinUSB INF is present, but the signed catalog is missing.\n"
            f"Expected catalog: {package.cat_path}\n"
            "Create and sign the catalog before using automatic driver installation."
        )
    return (
        f"Bundled driver package ready for VID=0x{package.vid:04X} "
        f"PID=0x{package.pid:04X}."
    )


def install_prompt_text() -> str:
    package = get_grl_driver_package()
    return (
        "This will request administrator permission and add the bundled WinUSB "
        f"driver package for the GRL sniffer (VID=0x{package.vid:04X}, "
        f"PID=0x{package.pid:04X}) to the Windows driver store.\n\n"
        "If the sniffer is already connected, Windows may briefly re-enumerate it."
    )


def install_grl_driver(*, elevate: bool = True) -> DriverInstallResult:
    if not is_windows():
        return DriverInstallResult(
            ok=False,
            message="Automatic driver installation is only supported on Windows.",
        )

    package = get_grl_driver_package()
    if not package.has_inf:
        return DriverInstallResult(
            ok=False,
            message=f"Bundled driver INF not found: {package.inf_path}",
        )
    if not package.has_catalog:
        return DriverInstallResult(
            ok=False,
            message=describe_driver_readiness(),
        )

    command = _install_command(package.inf_path)
    if _is_admin():
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
    elif elevate:
        completed = _run_elevated(command)
    else:
        return DriverInstallResult(
            ok=False,
            message=(
                "Administrator rights are required to install the WinUSB driver. "
                "Rerun elevated or allow UAC elevation."
            ),
        )

    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    if completed.returncode != 0:
        details = _format_process_output(stdout, stderr)
        message = (
            "WinUSB driver installation failed.\n"
            f"Command: {' '.join(command)}"
        )
        if details:
            message = f"{message}\n\n{details}"
        return DriverInstallResult(ok=False, message=message, stdout=stdout, stderr=stderr)

    message = (
        "WinUSB driver package added successfully. Replug the GRL sniffer if it "
        "was already connected, then click Refresh USB."
    )
    details = _format_process_output(stdout, stderr)
    if details:
        message = f"{message}\n\n{details}"
    return DriverInstallResult(ok=True, message=message, stdout=stdout, stderr=stderr)


def _install_command(inf_path: Path) -> list[str]:
    return [str(_pnputil_path()), "/add-driver", str(inf_path), "/install"]


def _pnputil_path() -> Path:
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    return windir / "System32" / "pnputil.exe"


def _is_admin() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _run_elevated(command: list[str]) -> subprocess.CompletedProcess[str]:
    file_path = _ps_quote(command[0])
    args_literal = ", ".join(_ps_quote(arg) for arg in command[1:])
    ps_command = (
        f"$proc = Start-Process -FilePath {file_path} "
        f"-ArgumentList @({args_literal}) -Verb RunAs -Wait -PassThru;"
        "if ($null -eq $proc) { exit 1 };"
        "exit $proc.ExitCode"
    )
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_command],
        capture_output=True,
        text=True,
        check=False,
    )


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _format_process_output(stdout: str, stderr: str) -> str:
    chunks: list[str] = []
    if stdout:
        chunks.append(f"stdout:\n{stdout}")
    if stderr:
        chunks.append(f"stderr:\n{stderr}")
    return "\n\n".join(chunks)
