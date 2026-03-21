from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class LumeError(RuntimeError):
    pass


@dataclass(slots=True)
class VmInfo:
    name: str
    status: str
    ip_address: str | None
    vnc_url: str | None = None


@dataclass(slots=True)
class VmLaunch:
    process: subprocess.Popen[bytes]
    log_path: Path


def _debug_log(debug: bool, message: str) -> None:
    if debug:
        print(message, file=sys.stderr)


def _run_lume(
    args: list[str],
    *,
    debug: bool = False,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    cmd = ["lume", *args]
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
            result.stderr.strip() or result.stdout.strip() or "lume command failed"
        )
        raise LumeError(message)
    return result


def get_vm_info(name: str, *, debug: bool = False) -> VmInfo | None:
    result = _run_lume(["ls", "--format", "json"], debug=debug)
    try:
        records = _parse_lume_json(result.stdout)
    except json.JSONDecodeError as error:
        raw_output = result.stdout.strip() or "<empty stdout>"
        raise LumeError(
            f"Invalid JSON from `lume ls --format json`: {raw_output}"
        ) from error
    for record in records:
        if record.get("name") == name:
            return VmInfo(
                name=name,
                status=record.get("status", "unknown"),
                ip_address=record.get("ipAddress"),
                vnc_url=record.get("vncUrl"),
            )
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
            raise LumeError(f"VM not found: {name}")
        if info.status == expected_status:
            return info
        if time.monotonic() >= deadline:
            raise LumeError(
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
            raise LumeError(f"VM not found: {name}")
        if info.status == "running" and info.ip_address:
            return info
        if launch is not None:
            returncode = launch.process.poll()
            if returncode is not None:
                raise LumeError(
                    _format_launch_failure(
                        launch.log_path,
                        f"lume run exited before VM became ready: {name} (exit code {returncode})",
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
            raise LumeError(detail)
        time.sleep(interval)


def spawn_vm(name: str, *, debug: bool = False) -> VmLaunch:
    cmd = ["lume", "run", name, "--no-display"]
    if debug:
        _debug_log(debug, f"+ {' '.join(cmd)}")
    with tempfile.NamedTemporaryFile(
        mode="w+b",
        delete=False,
        prefix="talonbox-lume-run-",
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


def stop_vm(name: str, *, debug: bool = False) -> None:
    _run_lume(["stop", name], debug=debug)


def force_stop_vm(name: str, *, debug: bool = False) -> None:
    pgids = _collect_vm_process_groups(name, debug=debug)
    if not pgids:
        raise LumeError(f"Unable to find local Lume process for VM: {name}")

    for pgid in pgids:
        _kill_process_group(pgid, signal.SIGTERM, debug=debug)
    time.sleep(2.0)

    remaining = {pgid for pgid in pgids if _process_group_exists(pgid)}
    for pgid in remaining:
        _kill_process_group(pgid, signal.SIGKILL, debug=debug)


def cleanup_launch_log(log_path: Path) -> None:
    try:
        log_path.unlink()
    except FileNotFoundError:
        return


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


def _parse_lume_json(output: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        lines = output.splitlines()
        for index, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped == "[" or stripped.startswith("[{") or stripped.startswith("{"):
                parsed = json.loads("\n".join(lines[index:]))
                break
        else:
            raise

    if not isinstance(parsed, list):
        raise json.JSONDecodeError("Expected a JSON list", output, 0)

    records: list[dict[str, Any]] = []
    for record in parsed:
        if not isinstance(record, Mapping):
            raise json.JSONDecodeError("Expected JSON objects in list", output, 0)
        records.append(dict(record))
    return records


def _collect_vm_process_groups(name: str, *, debug: bool) -> set[int]:
    pgids: set[int] = set()

    for process in _list_processes(debug=debug):
        if f"lume run {name}" not in process.command:
            continue
        pgids.add(process.pgid)
    return {pgid for pgid in pgids if pgid > 1}


@dataclass(slots=True)
class _ProcessInfo:
    pid: int
    pgid: int
    command: str


def _list_processes(*, debug: bool) -> list[_ProcessInfo]:
    result = subprocess.run(
        ["ps", "-Ao", "pid=,pgid=,command="],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "ps command failed"
        raise LumeError(message)

    processes: list[_ProcessInfo] = []
    for line in result.stdout.splitlines():
        raw = line.strip()
        if not raw:
            continue
        parts = raw.split(None, 2)
        if len(parts) != 3:
            continue
        try:
            pid = int(parts[0])
            pgid = int(parts[1])
        except ValueError:
            continue
        processes.append(_ProcessInfo(pid=pid, pgid=pgid, command=parts[2]))
    if debug:
        _debug_log(
            debug,
            f"found {len(processes)} local processes while scanning for stuck VMs",
        )
    return processes


def _kill_process_group(pgid: int, sig: signal.Signals, *, debug: bool) -> None:
    if debug:
        _debug_log(debug, f"+ kill -{sig.name} -- -{pgid}")
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True
