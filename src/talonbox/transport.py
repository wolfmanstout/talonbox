from __future__ import annotations

import shlex
import subprocess
import sys
import time
from pathlib import Path

SSH_USERNAME = "lume"
SSH_PASSWORD = "lume"
TRANSIENT_RETRY_DELAY_SECONDS = 1.0
TRANSIENT_RETRY_ATTEMPTS = 2
SSH_OPTIONS = [
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "LogLevel=ERROR",
    "-o",
    "BatchMode=no",
    "-o",
    "NumberOfPasswordPrompts=1",
    "-o",
    "PasswordAuthentication=yes",
    "-o",
    "KbdInteractiveAuthentication=no",
    "-o",
    "PreferredAuthentications=password",
    "-o",
    "PubkeyAuthentication=no",
]


class TransportError(RuntimeError):
    pass


class RemoteCommandError(TransportError):
    pass


def _debug_log(debug: bool, message: str) -> None:
    if debug:
        print(message, file=sys.stderr)


def _format_command(cmd: list[str]) -> str:
    return shlex.join(cmd)


def _sshpass_base(program: str) -> list[str]:
    return ["sshpass", "-p", SSH_PASSWORD, program]


def _ssh_base(ip_address: str) -> list[str]:
    return [*_sshpass_base("ssh"), *SSH_OPTIONS, f"{SSH_USERNAME}@{ip_address}"]


def _scp_base() -> list[str]:
    return [*_sshpass_base("scp"), *SSH_OPTIONS]


def _rsync_shell() -> str:
    return shlex.join([*_sshpass_base("ssh"), *SSH_OPTIONS])


def _remote_shell_command(command: str) -> str:
    return f"sh -lc {shlex.quote(command)}"


def _remote_repl_command() -> str:
    return 'sh -lc "$HOME/.talon/bin/repl"'


def _run_command(
    cmd: list[str],
    *,
    debug: bool = False,
    timeout: float | None = None,
    poll: bool = False,
    stream: bool = False,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    if debug:
        _debug_log(debug, f"+ {_format_command(cmd)}")

    deadline = time.monotonic() + timeout if poll and timeout is not None else None
    attempts = 0
    while True:
        result = subprocess.run(
            cmd,
            check=False,
            text=True,
            capture_output=not stream,
            timeout=None if poll else timeout,
            stdin=None if input_text is not None else subprocess.DEVNULL,
            input=input_text,
        )
        if result.returncode == 0 or not poll:
            if result.returncode == 0:
                return result
            if (
                _is_transient_transport_failure(result)
                and attempts < TRANSIENT_RETRY_ATTEMPTS
            ):
                attempts += 1
                time.sleep(TRANSIENT_RETRY_DELAY_SECONDS)
                continue
            return result
        if deadline is not None and time.monotonic() >= deadline:
            return result
        time.sleep(2.0)


def _remote_failure(
    result: subprocess.CompletedProcess[str],
    action: str,
) -> RemoteCommandError:
    message = result.stderr.strip() if result.stderr else ""
    if not message and result.stdout:
        message = result.stdout.strip()
    return RemoteCommandError(message or action)


def _transport_failure_message(result: subprocess.CompletedProcess[str]) -> str:
    return result.stderr.strip() or result.stdout.strip() or ""


def _is_transient_transport_failure(result: subprocess.CompletedProcess[str]) -> bool:
    if result.returncode != 255:
        return False
    message = _transport_failure_message(result).lower()
    needles = (
        "ssh_askpass",
        "permission denied (publickey,password,keyboard-interactive)",
        "connection reset by peer",
        "connection refused",
        "connection closed by remote host",
        "operation timed out",
        "no route to host",
        "kex_exchange_identification",
        "broken pipe",
    )
    return any(needle in message for needle in needles)


def run_remote_shell(
    ip_address: str,
    command: str | list[str],
    *,
    debug: bool = False,
    timeout: float | None = None,
    poll: bool = False,
    stream: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    remote_command = command if isinstance(command, str) else shlex.join(command)
    result = _run_command(
        [*_ssh_base(ip_address), _remote_shell_command(remote_command)],
        debug=debug,
        timeout=timeout,
        poll=poll,
        stream=stream,
    )
    if check and result.returncode != 0:
        raise _remote_failure(result, f"Remote command failed: {remote_command}")
    return result


def run_remote_repl(
    ip_address: str,
    payload: str,
    *,
    debug: bool = False,
    stream_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = _run_command(
        [*_ssh_base(ip_address), _remote_repl_command()],
        debug=debug,
        input_text=payload,
    )
    if stream_output or result.returncode != 0:
        if result.stdout:
            sys.stdout.write(result.stdout)
        if result.stderr:
            sys.stderr.write(result.stderr)
    return result


def probe_ssh(
    ip_address: str,
    *,
    debug: bool = False,
    timeout: float,
) -> None:
    run_remote_shell(
        ip_address,
        "true",
        debug=debug,
        timeout=timeout,
        poll=True,
    )


def wait_for_talon_repl(
    ip_address: str,
    *,
    debug: bool = False,
    timeout: float,
) -> None:
    run_remote_shell(
        ip_address,
        'test -S "$HOME/.talon/.sys/repl.sock"',
        debug=debug,
        timeout=timeout,
        poll=True,
    )


def _run_transfer(
    cmd: list[str],
    *,
    debug: bool = False,
) -> int:
    if debug:
        _debug_log(debug, f"+ {_format_command(cmd)}")
    result = subprocess.run(cmd, check=False)
    return result.returncode


def run_rsync(
    args: list[str],
    *,
    debug: bool = False,
) -> int:
    return _run_transfer(["rsync", "-e", _rsync_shell(), *args], debug=debug)


def run_scp(
    args: list[str],
    *,
    debug: bool = False,
) -> int:
    return _run_transfer([*_scp_base(), *args], debug=debug)


def download_from_guest(
    ip_address: str,
    remote_path: str,
    local_path: Path,
    *,
    debug: bool = False,
) -> None:
    cmd = [*_scp_base(), f"{SSH_USERNAME}@{ip_address}:{remote_path}", str(local_path)]
    result = _run_command(cmd, debug=debug)
    if result.returncode != 0:
        message = (
            _transport_failure_message(result) or "failed to download file from guest"
        )
        raise TransportError(message)
