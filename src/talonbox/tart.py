from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class TartError(RuntimeError):
    pass


@dataclass(slots=True)
class VmInfo:
    name: str
    status: str
    ip_address: str | None
    vnc_url: str | None = None
    last_accessed: datetime | None = None


@dataclass(slots=True)
class VmLaunch:
    process: subprocess.Popen[bytes]
    log_path: Path


VNC_URL_PATTERN = re.compile(r"vnc://\S+")


def _debug_log(debug: bool, message: str) -> None:
    if debug:
        print(message, file=sys.stderr)


def _run_tart(
    args: list[str],
    *,
    debug: bool = False,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    cmd = ["tart", *args]
    if debug:
        _debug_log(debug, f"+ {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        check=False,
        text=True,
        capture_output=capture_output,
    )
    if result.returncode != 0:
        message = (
            result.stderr.strip() or result.stdout.strip() or "tart command failed"
        )
        raise TartError(message)
    return result


def list_vms(*, debug: bool = False) -> list[VmInfo]:
    result = _run_tart(["list", "--format", "json"], debug=debug)
    try:
        records = _parse_tart_json(result.stdout)
    except json.JSONDecodeError as error:
        raw_output = result.stdout.strip() or "<empty stdout>"
        raise TartError(
            f"Invalid JSON from `tart list --format json`: {raw_output}"
        ) from error
    return [_vm_info_from_record(record, debug=debug) for record in records]


def get_vm_info(name: str, *, debug: bool = False) -> VmInfo | None:
    for info in list_vms(debug=debug):
        if info.name == name:
            return info
    return None


def wait_for_status(
    name: str,
    expected_status: str,
    *,
    timeout: float,
    interval: float = 2.0,
    debug: bool = False,
) -> VmInfo:
    deadline = time.monotonic() + timeout
    while True:
        info = get_vm_info(name, debug=debug)
        if info is None:
            raise TartError(f"VM not found: {name}")
        if info.status == expected_status:
            return info
        if time.monotonic() >= deadline:
            raise TartError(
                f"Timed out waiting for VM to reach status {expected_status}: {name}"
            )
        time.sleep(interval)


def wait_for_running_vm(
    name: str,
    *,
    timeout: float,
    interval: float = 2.0,
    debug: bool = False,
    launch: VmLaunch | None = None,
) -> VmInfo:
    deadline = time.monotonic() + timeout
    while True:
        info = get_vm_info(name, debug=debug)
        if info is None:
            raise TartError(f"VM not found: {name}")
        vnc_url = _extract_vnc_url(launch.log_path) if launch is not None else None
        if vnc_url:
            write_vnc_url(name, vnc_url)
            info.vnc_url = vnc_url
        if info.status == "running" and info.ip_address and (launch is None or vnc_url):
            return info
        if launch is not None:
            returncode = launch.process.poll()
            if returncode is not None:
                raise TartError(
                    _format_launch_failure(
                        launch.log_path,
                        f"tart run exited before VM became ready: {name} (exit code {returncode})",
                    )
                )
        if time.monotonic() >= deadline:
            detail = (
                _format_launch_failure(
                    launch.log_path,
                    f"Timed out waiting for VM to start: {name}",
                )
                if launch is not None
                else f"Timed out waiting for VM to start: {name}"
            )
            raise TartError(detail)
        time.sleep(interval)


def spawn_vm(name: str, *, debug: bool = False) -> VmLaunch:
    cmd = [
        "tart",
        "run",
        "--suspendable",
        "--no-graphics",
        "--vnc-experimental",
        name,
    ]
    if debug:
        _debug_log(debug, f"+ {' '.join(cmd)}")
    with tempfile.NamedTemporaryFile(
        mode="w+b",
        delete=False,
        prefix="talonbox-tart-run-",
        suffix=".log",
        dir="/tmp",
    ) as log_file:
        process = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return VmLaunch(process=process, log_path=Path(log_file.name))


def clone_vm(source_name: str, target_name: str, *, debug: bool = False) -> None:
    _run_tart(["clone", source_name, target_name], debug=debug)


def rename_vm(source_name: str, target_name: str, *, debug: bool = False) -> None:
    _run_tart(["rename", source_name, target_name], debug=debug)


def delete_vm(name: str, *, debug: bool = False) -> None:
    _run_tart(["delete", name], debug=debug)


def suspend_vm(name: str, *, debug: bool = False) -> None:
    _run_tart(["suspend", name], debug=debug)


def shutdown_vm(name: str, *, debug: bool = False) -> None:
    _run_tart(["stop", name], debug=debug)


def cleanup_launch_log(log_path: Path) -> None:
    try:
        log_path.unlink()
    except FileNotFoundError:
        return


def write_vnc_url(name: str, vnc_url: str) -> None:
    # O_NOFOLLOW refuses to write through a symlink planted at this
    # world-writable, predictable /tmp path. It only applies to the final path
    # component, so the standard /tmp -> /private/tmp symlink on macOS is
    # still traversed.
    path = _vnc_url_path(name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError:
        path.unlink(missing_ok=True)
        fd = os.open(path, flags | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{vnc_url}\n")


def read_vnc_url(name: str) -> str | None:
    try:
        fd = os.open(_vnc_url_path(name), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        # A symlink or unreadable file here is not a VNC URL we wrote.
        return None
    with os.fdopen(fd, encoding="utf-8") as handle:
        value = handle.read().strip()
    return value or None


def _vnc_url_path(name: str) -> Path:
    safe_name = name.replace("/", "_").replace(":", "_")
    return Path("/tmp") / f"talonbox-vnc-{safe_name}.txt"


def _format_launch_failure(log_path: Path, summary: str) -> str:
    detail = _read_launch_log(log_path)
    if not detail:
        return summary
    return f"{summary}\n{detail}\nstartup log: {log_path}"


def _read_launch_log(log_path: Path, *, max_lines: int = 20) -> str:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""

    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    return "\n".join(lines[-max_lines:])


def _extract_vnc_url(log_path: Path) -> str | None:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    match = VNC_URL_PATTERN.search(text)
    return match.group(0).rstrip(".") if match else None


def _parse_tart_json(output: str) -> list[dict[str, Any]]:
    parsed = json.loads(output)
    if not isinstance(parsed, list):
        raise json.JSONDecodeError("Expected a JSON list", output, 0)

    records: list[dict[str, Any]] = []
    for record in parsed:
        if not isinstance(record, Mapping):
            raise json.JSONDecodeError("Expected JSON objects in list", output, 0)
        records.append(dict(record))
    return records


def _vm_info_from_record(record: Mapping[str, Any], *, debug: bool) -> VmInfo:
    name = str(record.get("Name", ""))
    status = str(record.get("State") or "unknown").lower()
    last_accessed = _parse_tart_datetime(record.get("Accessed"))
    ip_address = None
    if status == "running":
        ip_address = _resolve_ip(name, debug=debug)
    vnc_url = read_vnc_url(name) if status == "running" else None
    return VmInfo(
        name=name,
        status=status,
        ip_address=ip_address,
        vnc_url=vnc_url,
        last_accessed=last_accessed,
    )


def _parse_tart_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _resolve_ip(name: str, *, debug: bool) -> str | None:
    try:
        result = _run_tart(["ip", name], debug=debug)
    except TartError:
        return None
    ip_address = result.stdout.strip()
    return ip_address or None
