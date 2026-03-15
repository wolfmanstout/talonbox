from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .state import StateRecord, state_paths


class LumeError(RuntimeError):
    pass


@dataclass(slots=True)
class VmInfo:
    name: str
    status: str
    ip_address: str | None
    vnc_url: str | None = None


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
    pid: int | None = None,
    log_path: Path | None = None,
) -> VmInfo:
    deadline = time.monotonic() + timeout
    while True:
        info = get_vm_info(name, debug=debug)
        if info is None:
            raise LumeError(f"VM not found: {name}")
        if info.status == "running" and info.ip_address:
            return info
        if pid is not None and not _process_exists(pid):
            log_tail = _read_log_tail(log_path)
            raise LumeError(
                log_tail or f"lume run exited before VM became ready: {name}"
            )
        if time.monotonic() >= deadline:
            raise LumeError(f"Timed out waiting for VM to start: {name}")
        time.sleep(interval)


def spawn_vm(name: str, *, debug: bool = False) -> StateRecord:
    paths = state_paths(name)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    log_handle = paths.log_path.open("ab")
    try:
        cmd = ["lume", "run", name, "--no-display"]
        if debug:
            _debug_log(debug, f"+ {' '.join(cmd)} > {paths.log_path}")
        process = subprocess.Popen(
            cmd,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_handle.close()
    return StateRecord(
        vm=name,
        pid=process.pid,
        log_path=str(paths.log_path),
        started_at=datetime.now(UTC).isoformat(),
    )


def stop_vm(name: str, *, debug: bool = False) -> None:
    _run_lume(["stop", name], debug=debug)


def force_stop_vm(name: str, *, debug: bool = False, pid: int | None = None) -> None:
    pgids = _collect_vm_process_groups(name, pid=pid, debug=debug)
    if not pgids:
        raise LumeError(f"Unable to find local Lume process for VM: {name}")

    for pgid in pgids:
        _kill_process_group(pgid, signal.SIGTERM, debug=debug)
    time.sleep(2.0)

    remaining = {pgid for pgid in pgids if _process_group_exists(pgid)}
    for pgid in remaining:
        _kill_process_group(pgid, signal.SIGKILL, debug=debug)


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def _read_log_tail(log_path: Path | None) -> str:
    if log_path is None or not log_path.exists():
        return ""
    lines = [
        line.strip()
        for line in log_path.read_text(errors="replace").splitlines()
        if line.strip()
    ]
    if not lines:
        return ""
    return lines[-1]


def _parse_lume_json(output: str) -> object:
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        lines = output.splitlines()
        for index, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped == "[" or stripped.startswith("[{") or stripped.startswith("{"):
                return json.loads("\n".join(lines[index:]))
        raise


def _collect_vm_process_groups(name: str, *, pid: int | None, debug: bool) -> set[int]:
    pgids: set[int] = set()
    if pid is not None:
        try:
            pgids.add(os.getpgid(pid))
        except ProcessLookupError:
            pass
        except PermissionError:
            pass

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
