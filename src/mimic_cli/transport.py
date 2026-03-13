from __future__ import annotations

import shlex
import subprocess
import sys
import time
from pathlib import Path

SSH_USERNAME = "lume"
SSH_PASSWORD = "lume"
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


def _ssh_base(ip_address: str) -> list[str]:
    return [
        "sshpass",
        "-p",
        SSH_PASSWORD,
        "ssh",
        *SSH_OPTIONS,
        f"{SSH_USERNAME}@{ip_address}",
    ]


def _rsync_shell() -> str:
    return shlex.join(
        [
            "sshpass",
            "-p",
            SSH_PASSWORD,
            "ssh",
            *SSH_OPTIONS,
        ]
    )


def _scp_base() -> list[str]:
    return [
        "sshpass",
        "-p",
        SSH_PASSWORD,
        "scp",
        *SSH_OPTIONS,
    ]


def _remote_shell_command(command: str) -> str:
    return f"sh -lc {shlex.quote(command)}"


def _remote_repl_command() -> str:
    return 'sh -lc "$HOME/.talon/bin/repl"'


def run_remote_shell(
    ip_address: str,
    command: str,
    *,
    debug: bool = False,
    timeout: float | None = None,
    poll: bool = False,
) -> subprocess.CompletedProcess[str]:
    if not poll:
        cmd = [*_ssh_base(ip_address), _remote_shell_command(command)]
        if debug:
            _debug_log(debug, f"+ {_format_command(cmd)}")
        result = subprocess.run(
            cmd,
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or f"Remote command failed: {command}"
            raise RemoteCommandError(message)
        return result

    deadline = time.monotonic() + (timeout or 0)
    last_message = ""
    while True:
        cmd = [*_ssh_base(ip_address), _remote_shell_command(command)]
        if debug:
            _debug_log(debug, f"+ {_format_command(cmd)}")
        result = subprocess.run(
            cmd,
            check=False,
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return result
        last_message = result.stderr.strip() or result.stdout.strip() or f"Remote command failed: {command}"
        if time.monotonic() >= deadline:
            raise RemoteCommandError(last_message)
        time.sleep(2.0)


def run_remote_shell_streaming(
    ip_address: str,
    command_args: list[str],
    *,
    debug: bool = False,
) -> int:
    remote_command = shlex.join(command_args)
    cmd = [*_ssh_base(ip_address), _remote_shell_command(remote_command)]
    if debug:
        _debug_log(debug, f"+ {_format_command(cmd)}")
    result = subprocess.run(cmd, check=False)
    return result.returncode


def run_remote_command_streaming(
    ip_address: str,
    command: str,
    *,
    debug: bool = False,
) -> int:
    cmd = [*_ssh_base(ip_address), _remote_shell_command(command)]
    if debug:
        _debug_log(debug, f"+ {_format_command(cmd)}")
    result = subprocess.run(cmd, check=False)
    return result.returncode


def run_remote_repl(
    ip_address: str,
    payload: str,
    *,
    debug: bool = False,
    stream_output: bool = False,
) -> int:
    cmd = [*_ssh_base(ip_address), _remote_repl_command()]
    if debug:
        _debug_log(debug, f"+ {_format_command(cmd)}")
    result = subprocess.run(
        cmd,
        check=False,
        text=True,
        input=payload,
        capture_output=True,
    )
    if stream_output or result.returncode != 0:
        if result.stdout:
            sys.stdout.write(result.stdout)
        if result.stderr:
            sys.stderr.write(result.stderr)
    return result.returncode


def run_remote_repl_streaming(
    ip_address: str,
    *,
    debug: bool = False,
) -> int:
    cmd = [*_ssh_base(ip_address), _remote_repl_command()]
    if debug:
        _debug_log(debug, f"+ {_format_command(cmd)}")
    result = subprocess.run(cmd, check=False)
    return result.returncode


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
        (
            '$HOME/.talon/bin/python -c '
            '\'import os, socket; '
            'sock = socket.socket(socket.AF_UNIX); '
            'sock.connect(os.path.expanduser("~/.talon/.sys/repl.sock")); '
            "sock.close()'"
        ),
        debug=debug,
        timeout=timeout,
        poll=True,
    )


def run_rsync(
    args: list[str],
    *,
    debug: bool = False,
) -> int:
    cmd = [
        "rsync",
        "-e",
        _rsync_shell(),
        *args,
    ]
    if debug:
        _debug_log(debug, f"+ {_format_command(cmd)}")
    result = subprocess.run(cmd, check=False)
    return result.returncode


def run_scp(
    args: list[str],
    *,
    debug: bool = False,
) -> int:
    cmd = [*_scp_base(), *args]
    if debug:
        _debug_log(debug, f"+ {_format_command(cmd)}")
    result = subprocess.run(cmd, check=False)
    return result.returncode


def download_from_guest(
    ip_address: str,
    remote_path: str,
    local_path: Path,
    *,
    debug: bool = False,
) -> None:
    cmd = [
        "rsync",
        "-e",
        _rsync_shell(),
        f"{SSH_USERNAME}@{ip_address}:{remote_path}",
        str(local_path),
    ]
    if debug:
        _debug_log(debug, f"+ {_format_command(cmd)}")
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "failed to download file from guest"
        raise TransportError(message)
