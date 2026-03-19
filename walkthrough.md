# Talonbox Code Walkthrough

*2026-03-19T02:33:05Z by Showboat 0.6.1*
<!-- showboat-id: 83b8db74-8d7f-421d-bb02-6eaa96eba140 -->

Talonbox is a local sandbox for testing Talon Voice scripts in an isolated VM. Inspired by playwright-cli, it wraps the `lume` hypervisor to spin up a macOS guest, wiping state between runs so tests are reproducible. This walkthrough traces the code from the CLI entry point all the way down to the SSH transport, following each major subsystem in order.

## Repository layout

The source lives under `src/talonbox/` and is split into five focused modules.

```bash
find src/talonbox -name '*.py' | sort | xargs wc -l | sort -rn
```

```output
 1601 total
  939 src/talonbox/cli.py
  290 src/talonbox/lume.py
  285 src/talonbox/transport.py
   59 src/talonbox/state.py
   24 src/talonbox/talon.py
    4 src/talonbox/__main__.py
    0 src/talonbox/__init__.py
```

The bulk of the logic is in `cli.py` (939 lines) with supporting modules for the VM lifecycle (`lume.py`), remote communication (`transport.py`), persistent state (`state.py`), and Talon-specific payloads (`talon.py`).

## Entry point: `__main__.py` and `cli.py`

The package exposes a single console-script entry point. `__main__.py` is two lines:

```bash
cat src/talonbox/__main__.py
```

```output
from .cli import cli

if __name__ == "__main__":
    cli()
```

And `pyproject.toml` registers the same function as the installed `talonbox` binary:

```bash
grep -A2 'project.scripts' pyproject.toml
```

```output
[project.scripts]
talonbox = "talonbox.cli:cli"

```

## CLI structure: custom Click classes

`cli.py` uses the [Click](https://click.palletsprojects.com/) framework. Two thin subclasses wrap Click's built-ins to add grouping headers and per-command examples in `--help` output.

```bash
sed -n '1,80p' src/talonbox/cli.py
```

```output
from __future__ import annotations

import shlex
import sys
import uuid
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import click

from . import lume
from .state import StateRecord, clear_state, load_state, save_state
from .talon import (
    build_mimic_payload,
    build_repl_exec_payload,
    build_screenshot_payload,
)
from .transport import (
    RemoteCommandError,
    TransportError,
    download_from_guest,
    probe_ssh,
    run_remote_command_streaming,
    run_remote_repl,
    run_remote_shell,
    run_remote_shell_streaming,
    run_rsync,
    run_scp,
    wait_for_talon_repl,
)

SSH_USERNAME = "lume"
SSH_PASSWORD = "lume"
DEFAULT_VM = "talon-test"
TALON_BINARY = "/Applications/Talon.app/Contents/MacOS/Talon"
TALON_LOG = "$HOME/.talon/talon.log"
TALON_REPL = "$HOME/.talon/bin/repl"
TALON_USER_DIR = "$HOME/.talon/user"
START_TIMEOUT_SECONDS = 180.0
SSH_TIMEOUT_SECONDS = 60.0
TALON_TIMEOUT_SECONDS = 30.0
TALON_REPL_TIMEOUT_SECONDS = 30.0
HOST_OUTPUT_ROOT = Path("/tmp")
HELP_COMMAND_GROUPS = (
    ("VM lifecycle", ("setup", "start", "restart-talon", "stop", "show")),
    ("Guest shell", ("exec", "rsync", "scp")),
    ("Talon RPC", ("repl", "mimic", "screenshot")),
)

GUEST_PREFIX = "guest:"
RSYNC_VALUE_OPTIONS = {
    "-B",
    "-f",
    "-M",
    "-T",
    "--backup-dir",
    "--block-size",
    "--bwlimit",
    "--chmod",
    "--compare-dest",
    "--compress-choice",
    "--copy-dest",
    "--exclude",
    "--exclude-from",
    "--files-from",
    "--filter",
    "--iconv",
    "--include",
    "--include-from",
    "--link-dest",
    "--log-file",
    "--log-file-format",
    "--max-size",
    "--min-size",
    "--out-format",
    "--partial-dir",
    "--password-file",
```

The file opens with a set of module-level constants that centralise every magic string: the guest credentials, VM name default, Talon binary paths, timeouts, and the allowed host output root (`/tmp`). Keeping these at the top means a configuration change is always a one-liner.

The three command groups are also declared as a constant so the `--help` formatter and the actual Click registration always agree:

```bash
grep -n 'HELP_COMMAND_GROUPS' src/talonbox/cli.py | head -5
```

```output
47:HELP_COMMAND_GROUPS = (
144:        for title, command_names in HELP_COMMAND_GROUPS:
```

```bash
sed -n '100,175p' src/talonbox/cli.py
```

```output

class TalonboxCommand(click.Command):
    def __init__(
        self, *args: Any, examples: Sequence[str] | None = None, **kwargs: Any
    ) -> None:
        self.examples = list(examples or [])
        super().__init__(*args, **kwargs)

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        self.format_usage(ctx, formatter)
        self.format_help_text(ctx, formatter)
        self.format_options(ctx, formatter)
        self.format_epilog(ctx, formatter)
        self._format_examples(formatter)

    def _format_examples(self, formatter: click.HelpFormatter) -> None:
        if not self.examples:
            return
        with formatter.section("Examples"):
            formatter.write_paragraph()
            for example in self.examples:
                formatter.write_text(example)


class TalonboxGroup(click.Group):
    command_class = TalonboxCommand

    def __init__(
        self, *args: Any, examples: Sequence[str] | None = None, **kwargs: Any
    ) -> None:
        self.examples = list(examples or [])
        super().__init__(*args, **kwargs)

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        self.format_usage(ctx, formatter)
        self.format_help_text(ctx, formatter)
        self.format_options(ctx, formatter)
        self.format_epilog(ctx, formatter)
        self._format_examples(formatter)

    def format_commands(
        self, ctx: click.Context, formatter: click.HelpFormatter
    ) -> None:
        emitted: set[str] = set()
        for title, command_names in HELP_COMMAND_GROUPS:
            rows: list[tuple[str, str]] = []
            for command_name in command_names:
                cmd = self.get_command(ctx, command_name)
                if cmd is None or cmd.hidden:
                    continue
                rows.append((command_name, cmd.get_short_help_str()))
                emitted.add(command_name)
            if rows:
                with formatter.section(title):
                    formatter.write_dl(rows)

        remaining_rows: list[tuple[str, str]] = []
        for command_name in self.list_commands(ctx):
            if command_name in emitted:
                continue
            cmd = self.get_command(ctx, command_name)
            if cmd is None or cmd.hidden:
                continue
            remaining_rows.append((command_name, cmd.get_short_help_str()))
        if remaining_rows:
            with formatter.section("Other"):
                formatter.write_dl(remaining_rows)

    def _format_examples(self, formatter: click.HelpFormatter) -> None:
        if not self.examples:
            return
        with formatter.section("Examples"):
            formatter.write_paragraph()
            for example in self.examples:
                formatter.write_text(example)

```

`TalonboxGroup.format_commands` iterates `HELP_COMMAND_GROUPS` to emit commands under their section headings, then falls through to an "Other" bucket for any commands not explicitly listed. `TalonboxCommand` adds an "Examples" section after the standard help text. Both classes are otherwise thin delegators to Click's built-ins.

The top-level group and shared `Context` dataclass:

```bash
sed -n '82,100p' src/talonbox/cli.py
```

```output
    "--suffix",
    "--temp-dir",
}
RSYNC_REJECTED_OPTIONS = {
    "-T",
    "-e",
    "--backup-dir",
    "--log-file",
    "--only-write-batch",
    "--partial-dir",
    "--rsync-path",
    "--rsh",
    "--temp-dir",
    "--write-batch",
}
SCP_VALUE_OPTIONS = {"-c", "-D", "-i", "-l", "-o", "-P", "-S", "-X"}
SCP_REJECTED_OPTIONS = {"-F", "-J", "-o", "-S"}


```

```bash
grep -n 'class Context\|^@click.group\|^def cli\|^@dataclass' src/talonbox/cli.py | head -10
```

```output
177:@dataclass(slots=True)
178:class Context:
190:@dataclass(frozen=True, slots=True)
537:@click.group(
567:def cli(click_ctx: click.Context, vm: str, debug: bool) -> None:
```

```bash
sed -n '177,215p' src/talonbox/cli.py
```

```output
@dataclass(slots=True)
class Context:
    vm: str
    debug: bool

    def debug_log(self, message: str) -> None:
        if self.debug:
            click.echo(message, err=True)


pass_context = click.make_pass_decorator(Context)


@dataclass(frozen=True, slots=True)
class TransferOperand:
    raw: str
    kind: str
    path: str


def _raise_click_error(message: str) -> NoReturn:
    raise click.ClickException(message)


def _handle_transport_error(error: Exception) -> NoReturn:
    _raise_click_error(str(error))


def _require_vm(ctx: Context) -> lume.VmInfo:
    try:
        info = lume.get_vm_info(ctx.vm, debug=ctx.debug)
    except lume.LumeError as error:
        _raise_click_error(str(error))
    if info is None:
        _raise_click_error(f"VM not found: {ctx.vm}")
    return info


def _require_running_vm(ctx: Context) -> lume.VmInfo:
```

`Context` is a plain dataclass threaded through every command via `pass_context`. It holds the chosen VM name and the debug flag; `debug_log` writes to stderr so it doesn't pollute captured stdout. `TransferOperand` is a frozen dataclass representing a parsed rsync/scp argument: its `kind` is `"guest"`, `"local"`, or `"invalid"` and drives all downstream safety checks.

## State persistence: `state.py`

Before diving into the VM lifecycle commands it helps to understand how talonbox remembers a running VM across invocations.

```bash
cat src/talonbox/state.py
```

```output
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

STATE_DIR_ENV = "TALONBOX_STATE_DIR"


@dataclass(slots=True)
class StatePaths:
    state_dir: Path
    state_path: Path
    log_path: Path


@dataclass(slots=True)
class StateRecord:
    vm: str
    pid: int
    log_path: str
    started_at: str


def get_state_dir() -> Path:
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / "talonbox"


def state_paths(vm: str) -> StatePaths:
    state_dir = get_state_dir()
    return StatePaths(
        state_dir=state_dir,
        state_path=state_dir / f"{vm}.json",
        log_path=state_dir / f"{vm}.log",
    )


def save_state(record: StateRecord) -> None:
    paths = state_paths(record.vm)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.state_path.write_text(json.dumps(asdict(record), indent=2))


def load_state(vm: str) -> StateRecord | None:
    path = state_paths(vm).state_path
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return StateRecord(**data)


def clear_state(vm: str) -> None:
    path = state_paths(vm).state_path
    if path.exists():
        path.unlink()
```

`StateRecord` stores the VM name, the OS PID of the `lume run` process, its log file path, and a start timestamp. The state file lives at `~/Library/Application Support/talonbox/<vm>.json` on macOS (overridable via `TALONBOX_STATE_DIR` for tests). `save_state`/`load_state`/`clear_state` are the only persistence primitives; everything else reads this file or writes it at start/stop boundaries.

## Lume VM management: `lume.py`

`lume` is a macOS CLI tool that manages lightweight Apple Silicon VMs. Talonbox treats it as a subprocess, parsing its JSON output to drive the VM lifecycle.

```bash
sed -n '1,80p' src/talonbox/lume.py
```

```output
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
```

```bash
sed -n '80,200p' src/talonbox/lume.py
```

```output
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


def _parse_lume_json(output: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        lines = output.splitlines()
        for index, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped == "[" or stripped.startswith("[{") or stripped.startswith("{"):
                parsed = json.loads("\n".join(lines[index:]))
```

`spawn_vm` launches `lume run <name> --no-display` in a new session (`start_new_session=True`) so it outlives the talonbox process. Both stdout and stderr go to a per-VM log file. The PID is stored in `StateRecord` so later commands can check whether the backing process is still alive.

`wait_for_running_vm` polls `lume ls --format json` every two seconds. It checks three exit conditions: the VM is running with an IP, the backing PID has disappeared (early failure), or the deadline has passed.

`force_stop_vm` collects all process groups associated with the VM and sends SIGTERM, waits two seconds, then SIGKILL to any survivors—a two-phase kill so clean-shutdown handlers have a chance to run.

`_parse_lume_json` is deliberately tolerant: `lume` sometimes emits log lines before the JSON array, so the parser scans forward to the first line that looks like a JSON array start before calling `json.loads`.

## Transport layer: `transport.py`

All communication with the running guest goes through `transport.py`. It wraps SSH, rsync, and scp, using `sshpass` for password-based authentication (the guest always has the same credentials).

```bash
sed -n '1,100p' src/talonbox/transport.py
```

```output
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
```

`_ssh_base` builds the sshpass+ssh prefix that every remote call shares. Key SSH options: `StrictHostKeyChecking=no` (VMs are ephemeral, no known-hosts), `BatchMode=no` + `NumberOfPasswordPrompts=1` + `PasswordAuthentication=yes` + `PubkeyAuthentication=no` — this combination forces password auth through sshpass and prevents any interactive prompt.

Remote commands are wrapped in `sh -lc ...` so the guest login shell initialises `PATH` and shell environment correctly even though the SSH session isn't a login shell by default.

```bash
sed -n '100,185p' src/talonbox/transport.py
```

```output
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
```

There are three execution modes:

1. **`run_remote_shell`** — blocking, captures all output. With `poll=True` it retries every two seconds until success or deadline, which is used during bootstrap to wait for Talon to start.
2. **`run_remote_shell_streaming` / `run_remote_command_streaming`** — no `capture_output`, so the guest's stdout/stderr flow straight to the terminal. Used by the `exec` command so the caller sees live output.
3. **`run_remote_repl`** — pipes `payload` as stdin to the Talon REPL process. The REPL socket is a local Unix socket inside the guest; the SSH session just invokes `/root/.talon/bin/repl` which connects to it.

```bash
sed -n '185,286p' src/talonbox/transport.py
```

```output
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
```

`probe_ssh` and `wait_for_talon_repl` are polling helpers built on `run_remote_shell(poll=True)`. `probe_ssh` just runs `true`; `wait_for_talon_repl` uses Talon's bundled Python to attempt a Unix socket connection to `~/.talon/.sys/repl.sock` — if the socket isn't open yet the connect raises and the loop retries.

`run_rsync` injects the full sshpass+ssh invocation as rsync's `-e` (remote shell) argument, so rsync uses the same credential management as plain SSH. `download_from_guest` wraps this for the common "fetch one file" case used by the screenshot command.

## Talon payloads: `talon.py`

Talonbox communicates with Talon by writing Python source to the REPL's stdin. `talon.py` is a small collection of payload builders.

```bash
cat src/talonbox/talon.py
```

```output
from __future__ import annotations


def build_mimic_payload(command: str) -> str:
    return f"mimic({command!r})\n"


def build_repl_exec_payload(code: str) -> str:
    if not code.endswith("\n"):
        code += "\n"
    return f"exec(compile({code!r}, '<talonbox>', 'exec'))\n"


def build_screenshot_payload(remote_path: str) -> str:
    return "\n".join(
        [
            "from talon import screen",
            f"path = {remote_path!r}",
            "img = screen.capture_rect(screen.main().rect, retina=False)",
            "img.save(path) if hasattr(img, 'save') else img.write_file(path)",
            "print(path)",
            "",
        ]
    )
```

`build_mimic_payload` wraps the phrase in `mimic()`, which is Talon's built-in for replaying voice commands programmatically.

`build_repl_exec_payload` uses Python's `compile`+`exec` pattern. The filename argument `'<talonbox>'` appears in tracebacks, making errors easier to attribute.

`build_screenshot_payload` is the most interesting: `screen.main().rect` gets the main screen bounds; `retina=False` returns pixel coordinates rather than point coordinates (so the saved image matches physical pixels); the dual `save`/`write_file` check handles the fact that different Talon versions ship different image APIs; `print(path)` lets the host side confirm the guest wrote the file before trying to download it.

## VM lifecycle commands: `start`, `stop`, `show`

Now we can read the high-level commands in `cli.py` that orchestrate everything above.

```bash
grep -n '^def start\|^def stop\|^def show\|^def restart_talon\|^def _bootstrap' src/talonbox/cli.py
```

```output
491:def _bootstrap_talon(ip_address: str, debug: bool) -> None:
595:def start(ctx: Context) -> None:
638:def restart_talon(ctx: Context) -> None:
663:def stop(ctx: Context) -> None:
703:def show(ctx: Context) -> None:
```

```bash
sed -n '491,540p' src/talonbox/cli.py
```

```output
def _bootstrap_talon(ip_address: str, debug: bool) -> None:
    _restart_talon(
        ip_address,
        debug=debug,
        wipe_user_dir=True,
        clean_logs=True,
    )


def _logout_guest_session(ip_address: str, *, debug: bool) -> None:
    run_remote_shell(
        ip_address,
        "launchctl bootout gui/$(id -u)",
        debug=debug,
        timeout=15.0,
    )
    run_remote_shell(
        ip_address,
        "while pgrep -x Talon >/dev/null; do sleep 1; done",
        debug=debug,
        timeout=15.0,
    )


def _terminal_launch_command() -> str:
    script_path = "/tmp/talonbox-launch.command"
    script_body = (
        f"#!/bin/sh\nexec arch -x86_64 {TALON_BINARY} >/tmp/talonbox-talon.log 2>&1\n"
    )
    return (
        f"printf %s {shlex.quote(script_body)} > {shlex.quote(script_path)} && "
        f"chmod +x {shlex.quote(script_path)} && "
        f"open -a Terminal {shlex.quote(script_path)}"
    )


def _cleanup_failed_start(ctx: Context, state: StateRecord) -> None:
    ctx.debug_log("start failed; stopping VM and clearing local state")
    try:
        lume.stop_vm(ctx.vm, debug=ctx.debug)
        lume.wait_for_status(ctx.vm, "stopped", timeout=30.0, debug=ctx.debug)
    except lume.LumeError as error:
        ctx.debug_log(f"cleanup stop failed: {error}")
    clear_state(ctx.vm)


@click.group(
    name="talonbox",
    cls=TalonboxGroup,
    context_settings={"max_content_width": 100},
```

```bash
sed -n '595,705p' src/talonbox/cli.py
```

```output
def start(ctx: Context) -> None:
    info = _require_vm(ctx)
    if info.status == "running":
        _raise_click_error(f"VM is already running: {ctx.vm}")
    if info.status != "stopped":
        _raise_click_error(f"VM is not stopped: {ctx.vm} ({info.status})")

    state: StateRecord | None = None
    try:
        state = lume.spawn_vm(ctx.vm, debug=ctx.debug)
        save_state(state)
        ready_vm = lume.wait_for_running_vm(
            ctx.vm,
            timeout=START_TIMEOUT_SECONDS,
            debug=ctx.debug,
            pid=state.pid,
            log_path=Path(state.log_path),
        )
        ready_ip = _require_vm_ip(ready_vm, ctx.vm)
        probe_ssh(ready_ip, debug=ctx.debug, timeout=SSH_TIMEOUT_SECONDS)
        _bootstrap_talon(ready_ip, debug=ctx.debug)
    except (lume.LumeError, RemoteCommandError, TransportError) as error:
        if state is not None:
            _cleanup_failed_start(ctx, state)
        _handle_transport_error(error)

    _print_vm_info(ready_vm)


@cli.command(
    name="restart-talon",
    short_help="Restart Talon inside the running VM and reset Talon logs.",
    help=(
        "Restart Talon inside the running VM without rebooting the VM.\n\n"
        "This truncates `~/.talon/talon.log` and `/tmp/talonbox-talon.log`, then relaunches "
        "Talon under Rosetta through Terminal so screen capture permissions still apply."
    ),
    examples=(
        "  talonbox restart-talon",
        "  talonbox --debug restart-talon",
    ),
)
@pass_context
def restart_talon(ctx: Context) -> None:
    info = _require_running_vm(ctx)
    try:
        _restart_talon(
            _require_vm_ip(info, ctx.vm),
            debug=ctx.debug,
            wipe_user_dir=False,
            clean_logs=True,
        )
    except (RemoteCommandError, TransportError) as error:
        _handle_transport_error(error)


@cli.command(
    short_help="Stop the VM if it is running.",
    help=(
        "Log out the guest GUI session when possible, then stop the VM and clear "
        "talonbox local state. Safe to run repeatedly."
    ),
    examples=(
        "  talonbox stop",
        "  talonbox --vm talon-test stop",
    ),
)
@pass_context
def stop(ctx: Context) -> None:
    info = _require_vm(ctx)
    if info.status != "stopped":
        state = load_state(ctx.vm)
        if info.status == "running" and info.ip_address:
            try:
                _logout_guest_session(_require_vm_ip(info, ctx.vm), debug=ctx.debug)
            except (RemoteCommandError, TransportError) as error:
                ctx.debug_log(f"guest logout failed: {error}")
        try:
            lume.stop_vm(ctx.vm, debug=ctx.debug)
            lume.wait_for_status(ctx.vm, "stopped", timeout=60.0, debug=ctx.debug)
        except lume.LumeError as error:
            ctx.debug_log(f"graceful stop failed: {error}")
            try:
                lume.force_stop_vm(
                    ctx.vm,
                    debug=ctx.debug,
                    pid=state.pid if state is not None else None,
                )
                lume.wait_for_status(ctx.vm, "stopped", timeout=20.0, debug=ctx.debug)
            except lume.LumeError as force_error:
                _raise_click_error(str(force_error))
    clear_state(ctx.vm)


@cli.command(
    short_help="Print VM status and connection details without changing anything.",
    help=(
        "Show whether the VM is running. When it is running, also print IP, SSH credentials, "
        "and the VNC link.\n\n"
        "This command is read-only: it does not start, stop, or modify the VM, and is safe to "
        "use in sandboxed environments that permit running `lume ls`."
    ),
    examples=(
        "  talonbox show",
        "  talonbox --vm talon-test show",
    ),
)
@pass_context
def show(ctx: Context) -> None:
    info = _require_vm(ctx)
    _print_vm_info(info)
```

### `start`

The start sequence is:
1. Assert VM exists and is stopped (not already running).
2. `lume.spawn_vm` → saves `StateRecord` immediately so the PID is recorded even if a later step fails.
3. `wait_for_running_vm` polls until the VM has an IP.
4. `probe_ssh` polls until SSH accepts connections.
5. `_bootstrap_talon` wipes `~/.talon/user` and restarts Talon under Rosetta via Terminal (so screen capture permissions survive).
6. On any error: `_cleanup_failed_start` tries a graceful `lume stop` and deletes the state file.

Talon must be launched through macOS Terminal (via `open -a Terminal script.command`) rather than directly over SSH because screen capture APIs like `screen.capture_rect` require a GUI session with the appropriate permissions granted to Terminal.

### `stop`

Stop is idempotent (safe to call repeatedly):
1. If VM is running: attempt `_logout_guest_session` (sends `launchctl bootout gui/$(id -u)` to cleanly shut down the GUI session) — failure is only debug-logged, not fatal.
2. `lume stop` + wait up to 60 s.
3. If graceful stop fails: fall back to `force_stop_vm` (SIGTERM → SIGKILL on process groups) + wait 20 s.
4. Always: `clear_state` deletes the local JSON file.

The logout step is a best-effort clean shutdown; if the VM is already stopped or SSH is unreachable the fallback path still succeeds.

## Transfer safety: `rsync` and `scp` commands

Talonbox allows the user to pass arbitrary rsync/scp arguments but must prevent any write to host files outside `/tmp`. This is enforced by `_prepare_transfer_args` before the subprocess is invoked.

```bash
grep -n '^def _classify\|^def _rewrite\|^def _split\|^def _prepare\|^def _normalize\|^def _is_relative\|^def _is_blank' src/talonbox/cli.py
```

```output
238:def _split_transfer_args(
294:def _classify_transfer_operand(raw: str) -> TransferOperand:
309:def _prepare_transfer_args(
353:def _rewrite_transfer_operand(ip_address: str, operand: TransferOperand) -> str:
359:def _normalize_local_output_path(raw_path: str | Path) -> Path:
383:def _is_relative_to(path: Path, root: Path) -> bool:
391:def _is_blank_png(filepath: Path) -> bool:
```

```bash
sed -n '238,400p' src/talonbox/cli.py
```

```output
def _split_transfer_args(
    args: Sequence[str],
    *,
    value_options: set[str],
    rejected_options: set[str],
) -> tuple[list[str], list[str]]:
    passthrough: list[str] = []
    positionals: list[str] = []
    index = 0
    parsing_options = True

    while index < len(args):
        arg = args[index]
        if parsing_options and arg == "--":
            passthrough.append(arg)
            parsing_options = False
            index += 1
            continue
        if not parsing_options or not arg.startswith("-") or arg == "-":
            positionals.append(arg)
            index += 1
            continue

        if arg.startswith("--"):
            option, has_value, attached_value = arg.partition("=")
            if option in rejected_options:
                _raise_click_error(
                    f"Option not allowed for VM-only transfer safety: {option}"
                )
            passthrough.append(arg)
            index += 1
            if has_value or option not in value_options:
                continue
            if index >= len(args):
                _raise_click_error(f"Option requires a value: {option}")
            passthrough.append(args[index])
            index += 1
            continue

        short_option = arg[:2]
        if short_option in rejected_options:
            _raise_click_error(
                f"Option not allowed for VM-only transfer safety: {short_option}"
            )
        passthrough.append(arg)
        index += 1
        if short_option not in value_options or len(arg) > 2:
            continue
        if index >= len(args):
            _raise_click_error(f"Option requires a value: {short_option}")
        passthrough.append(args[index])
        index += 1

    return passthrough, positionals


def _classify_transfer_operand(raw: str) -> TransferOperand:
    if raw.startswith(GUEST_PREFIX):
        path = raw[len(GUEST_PREFIX) :]
        if not path:
            _raise_click_error("Guest path must not be empty: guest:/path")
        if not path.startswith("/"):
            _raise_click_error(f"Guest path must be absolute: {raw}")
        return TransferOperand(raw=raw, kind="guest", path=path)
    if raw.startswith("rsync://"):
        _raise_click_error(f"Only guest: remote paths are allowed: {raw}")
    if ":" in raw:
        _raise_click_error(f"Only guest: remote paths are allowed: {raw}")
    return TransferOperand(raw=raw, kind="local", path=raw)


def _prepare_transfer_args(
    ctx: Context,
    args: Sequence[str],
    *,
    value_options: set[str],
    rejected_options: set[str],
) -> list[str]:
    passthrough, positionals = _split_transfer_args(
        args,
        value_options=value_options,
        rejected_options=rejected_options,
    )
    if len(positionals) < 2:
        _raise_click_error("Transfer requires at least one source and one destination")

    info = _require_running_vm(ctx)
    sources = [_classify_transfer_operand(arg) for arg in positionals[:-1]]
    destination = _classify_transfer_operand(positionals[-1])

    source_kinds = {source.kind for source in sources}
    if len(source_kinds) != 1:
        _raise_click_error("Mixed local and guest sources are not allowed")
    source_kind = next(iter(source_kinds))
    if source_kind == destination.kind:
        if source_kind == "local":
            _raise_click_error(
                "Local-to-local transfers are not allowed; use guest: paths for the VM"
            )
        _raise_click_error("Guest-to-guest transfers are not allowed")
    if destination.kind == "local":
        destination = TransferOperand(
            raw=destination.raw,
            kind=destination.kind,
            path=str(_normalize_local_output_path(destination.path)),
        )

    ip_address = _require_vm_ip(info, ctx.vm)
    rewritten = [
        _rewrite_transfer_operand(ip_address, operand)
        for operand in [*sources, destination]
    ]
    return [*passthrough, *rewritten]


def _rewrite_transfer_operand(ip_address: str, operand: TransferOperand) -> str:
    if operand.kind == "local":
        return operand.path
    return f"{SSH_USERNAME}@{ip_address}:{operand.path}"


def _normalize_local_output_path(raw_path: str | Path) -> Path:
    destination = Path(raw_path).expanduser()
    if not destination.is_absolute():
        destination = Path.cwd() / destination

    try:
        resolved_destination = destination.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        _raise_click_error(f"Unable to resolve local output path {raw_path!s}: {error}")

    host_output_root = _host_output_root()
    if _is_relative_to(resolved_destination, host_output_root):
        return resolved_destination

    _raise_click_error(
        "Local output paths must stay under /tmp. "
        "Symlinks that escape /tmp are not allowed."
    )


def _host_output_root() -> Path:
    return HOST_OUTPUT_ROOT.resolve(strict=False)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_blank_png(filepath: Path) -> bool:
    data = filepath.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False

    offset = 8
    width = height = color_type = None
    idat_chunks: list[bytes] = []
    while offset + 8 <= len(data):
        chunk_length = int.from_bytes(data[offset : offset + 4], "big")
```

### How transfer safety works

**`_split_transfer_args`** manually tokenises the argument list into passthrough flags and positional operands. It rejects any option in `RSYNC_REJECTED_OPTIONS` / `SCP_REJECTED_OPTIONS` (options that could create files on the host outside `/tmp`, like `--log-file`, `--temp-dir`, `--backup-dir`, or the rsync remote-shell override `-e`).

**`_classify_transfer_operand`** converts each positional into a `TransferOperand`:
- `guest:/absolute/path` → kind `"guest"`
- bare path → kind `"local"`
- anything with a bare `:` (raw SSH notation) or `rsync://` → error

**`_prepare_transfer_args`** enforces the constraints:
- At least one source + one destination.
- All sources must be the same kind (no mixed local+guest).
- Source kind ≠ destination kind (no local→local or guest→guest).
- Local destinations are run through `_normalize_local_output_path`.

**`_normalize_local_output_path`** resolves the path (following symlinks via `Path.resolve`) and then checks `_is_relative_to(resolved, /tmp)`. Because it resolves symlinks before checking, a symlink that points outside `/tmp` is rejected even though its location is inside `/tmp`.

Finally, guest operands are rewritten from `guest:/path` to `lume@<ip>:/path` for the actual subprocess call.

## Screenshot command and blank PNG detection

The screenshot command is the most involved Talon RPC call because it crosses the host/guest boundary twice: once to run the capture, once to download the result.

```bash
grep -n '^def screenshot\|^def _is_blank' src/talonbox/cli.py
```

```output
391:def _is_blank_png(filepath: Path) -> bool:
889:def screenshot(ctx: Context, filepath: Path) -> None:
```

```bash
sed -n '889,940p' src/talonbox/cli.py
```

```output
def screenshot(ctx: Context, filepath: Path) -> None:
    info = _require_running_vm(ctx)
    ip_address = _require_vm_ip(info, ctx.vm)
    filepath = _normalize_local_output_path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    remote_path = f"/tmp/talonbox-screenshot-{uuid.uuid4().hex}.png"
    try:
        wait_for_talon_repl(
            ip_address,
            debug=ctx.debug,
            timeout=TALON_REPL_TIMEOUT_SECONDS,
        )
        returncode = run_remote_repl(
            ip_address,
            build_screenshot_payload(remote_path),
            debug=ctx.debug,
        )
        if returncode:
            raise click.exceptions.Exit(returncode)
        download_from_guest(
            ip_address,
            remote_path,
            filepath,
            debug=ctx.debug,
        )
        if _is_blank_png(filepath):
            filepath.unlink(missing_ok=True)
            _raise_click_error(
                "Guest screenshot was blank. The VM display may not be rendering; guest-side "
                "screen capture currently appears unavailable."
            )
    except (RemoteCommandError, TransportError) as error:
        _handle_transport_error(error)
    finally:
        try:
            run_remote_shell(
                ip_address,
                f'rm -f "{remote_path}"',
                debug=ctx.debug,
            )
        except (RemoteCommandError, TransportError):
            pass


def main() -> int:
    cli.main(standalone_mode=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

```bash
sed -n '391,490p' src/talonbox/cli.py
```

```output
def _is_blank_png(filepath: Path) -> bool:
    data = filepath.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False

    offset = 8
    width = height = color_type = None
    idat_chunks: list[bytes] = []
    while offset + 8 <= len(data):
        chunk_length = int.from_bytes(data[offset : offset + 4], "big")
        chunk_type = data[offset + 4 : offset + 8]
        chunk_data_start = offset + 8
        chunk_data_end = chunk_data_start + chunk_length
        if chunk_data_end + 4 > len(data):
            return False
        chunk_data = data[chunk_data_start:chunk_data_end]
        if chunk_type == b"IHDR":
            width = int.from_bytes(chunk_data[0:4], "big")
            height = int.from_bytes(chunk_data[4:8], "big")
            color_type = chunk_data[9]
        elif chunk_type == b"IDAT":
            idat_chunks.append(chunk_data)
        elif chunk_type == b"IEND":
            break
        offset = chunk_data_end + 4

    if width is None or height is None or color_type not in {2, 6} or not idat_chunks:
        return False

    try:
        decoded = zlib.decompress(b"".join(idat_chunks))
    except zlib.error:
        return False

    bytes_per_pixel = 3 if color_type == 2 else 4
    stride = width * bytes_per_pixel
    expected_length = height * (1 + stride)
    if len(decoded) < expected_length:
        return False

    first_pixel: bytes | None = None
    for row_index in range(height):
        row_start = row_index * (1 + stride)
        if decoded[row_start] != 0:
            return False
        row = decoded[row_start + 1 : row_start + 1 + stride]
        for pixel_start in range(0, len(row), bytes_per_pixel):
            pixel = row[pixel_start : pixel_start + bytes_per_pixel]
            if first_pixel is None:
                first_pixel = pixel
                continue
            if pixel != first_pixel:
                return False
    return first_pixel is not None


def _restart_talon(
    ip_address: str,
    *,
    debug: bool,
    wipe_user_dir: bool,
    clean_logs: bool,
) -> None:
    run_remote_shell(
        ip_address,
        "pkill -x Talon >/dev/null 2>&1 || true",
        debug=debug,
    )
    if clean_logs:
        run_remote_shell(
            ip_address,
            f'mkdir -p "$HOME/.talon" && : > {TALON_LOG} && : > /tmp/talonbox-talon.log',
            debug=debug,
        )
    run_remote_shell(ip_address, 'mkdir -p "$HOME/.talon/user"', debug=debug)
    if wipe_user_dir:
        run_remote_shell(
            ip_address,
            'find "$HOME/.talon/user" -mindepth 1 -maxdepth 1 -exec rm -rf {} +',
            debug=debug,
        )
    run_remote_shell(
        ip_address,
        _terminal_launch_command(),
        debug=debug,
    )
    run_remote_shell(
        ip_address,
        "pgrep -x Talon >/dev/null",
        debug=debug,
        timeout=TALON_TIMEOUT_SECONDS,
        poll=True,
    )
    wait_for_talon_repl(
        ip_address,
        debug=debug,
        timeout=TALON_REPL_TIMEOUT_SECONDS,
    )


```

### Screenshot flow

1. Validate the local output path is under `/tmp` and create parent dirs.
2. Generate a unique remote temp path with `uuid.uuid4().hex`.
3. Wait for the Talon REPL socket to be open.
4. Send `build_screenshot_payload(remote_path)` via SSH stdin to the REPL.
5. Download the PNG from the guest with `download_from_guest` (rsync).
6. Run `_is_blank_png` — if blank, delete the file and raise an error.
7. **Always** (`finally` block): delete the guest temp file via `rm -f`.

### Blank PNG detection

`_is_blank_png` parses the PNG binary format directly (no third-party library) to detect a fully black or single-colour screenshot, which indicates the VM display is not rendering. It:
- Verifies the PNG signature bytes.
- Walks the chunk stream, collecting `IDAT` (image data) chunks and reading dimensions/colour type from `IHDR`.
- Decompresses all `IDAT` chunks with `zlib.decompress`.
- Walks the raw pixel rows (each prefixed by a filter-type byte). Only `filter_type == 0` (None) is considered — if any row uses a non-zero filter the image is not blank.
- Checks that every pixel is identical to the first pixel. A uniform image (all black, all white, etc.) is considered blank.

## `exec`, `repl`, and `mimic` commands

These three guest-shell / Talon-RPC commands are thin wrappers around the transport layer.

```bash
grep -n '^def exec_command\|^def repl\|^def mimic' src/talonbox/cli.py
```

```output
725:def exec_command(ctx: Context, command: tuple[str, ...]) -> None:
824:def repl(ctx: Context, code: str | None) -> None:
857:def mimic(ctx: Context, command: str) -> None:
```

```bash
sed -n '725,890p' src/talonbox/cli.py
```

```output
def exec_command(ctx: Context, command: tuple[str, ...]) -> None:
    if not command:
        _raise_click_error("No command provided")
    info = _require_running_vm(ctx)
    ip_address = _require_vm_ip(info, ctx.vm)
    if len(command) == 1:
        returncode = run_remote_command_streaming(
            ip_address,
            command[0],
            debug=ctx.debug,
        )
    else:
        returncode = run_remote_shell_streaming(
            ip_address,
            list(command),
            debug=ctx.debug,
        )
    if returncode:
        raise click.exceptions.Exit(returncode)


@cli.command(
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
    short_help="Copy files between host and guest with rsync.",
    help=(
        "Run rsync between the host and the guest VM.\n\n"
        "Use explicit `guest:/path` operands for the VM side. Exactly one side may be remote, "
        "and only `guest:` remote paths are allowed. No other remotes are permitted.\n\n"
        "Local sources may be read from anywhere, but any host-side output must stay under "
        "`/tmp`. Rsync options that create extra host-side files are rejected."
    ),
    examples=(
        "  talonbox rsync -av ./repo/ guest:/Users/lume/.talon/user/repo/",
        "  talonbox rsync -av guest:/Users/lume/Pictures/ /tmp/guest-pictures/",
    ),
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED, metavar="RSYNC_ARGS...")
@pass_context
def rsync(ctx: Context, args: tuple[str, ...]) -> None:
    rewritten_args = _prepare_transfer_args(
        ctx,
        args,
        value_options=RSYNC_VALUE_OPTIONS,
        rejected_options=RSYNC_REJECTED_OPTIONS,
    )
    returncode = run_rsync(
        rewritten_args,
        debug=ctx.debug,
    )
    if returncode:
        raise click.exceptions.Exit(returncode)


@cli.command(
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
    short_help="Copy files between host and guest with scp.",
    help=(
        "Run scp between the host and the guest VM.\n\n"
        "Use explicit `guest:/path` operands for the VM side. Exactly one side may be remote, "
        "and only `guest:` remote paths are allowed. No other remotes are permitted.\n\n"
        "Local sources may be read from anywhere, but any host-side output must stay under "
        "`/tmp`."
    ),
    examples=(
        "  talonbox scp ./settings.talon guest:/Users/lume/.talon/user/settings.talon",
        "  talonbox scp guest:/tmp/out.png /tmp/out.png",
    ),
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED, metavar="SCP_ARGS...")
@pass_context
def scp(ctx: Context, args: tuple[str, ...]) -> None:
    rewritten_args = _prepare_transfer_args(
        ctx,
        args,
        value_options=SCP_VALUE_OPTIONS,
        rejected_options=SCP_REJECTED_OPTIONS,
    )
    returncode = run_scp(
        rewritten_args,
        debug=ctx.debug,
    )
    if returncode:
        raise click.exceptions.Exit(returncode)


@cli.command(
    short_help="Pipe Python into the guest Talon REPL.",
    help=(
        "Send Python to the guest Talon REPL.\n\n"
        "Provide CODE as an argument or pipe Python on stdin. This command is intentionally "
        "non-interactive."
    ),
    examples=(
        "  talonbox repl 'print(1+1)'",
        "  printf 'print(1+1)\\n' | talonbox repl",
    ),
)
@click.argument("code", required=False, metavar="[CODE]")
@pass_context
def repl(ctx: Context, code: str | None) -> None:
    info = _require_running_vm(ctx)
    ip_address = _require_vm_ip(info, ctx.vm)
    wait_for_talon_repl(
        ip_address,
        debug=ctx.debug,
        timeout=TALON_REPL_TIMEOUT_SECONDS,
    )
    if code is None:
        if sys.stdin.isatty():
            _raise_click_error("No code provided. Pass CODE or pipe Python into stdin.")
        code = sys.stdin.read()
    assert code is not None
    returncode = run_remote_repl(
        ip_address,
        build_repl_exec_payload(code),
        debug=ctx.debug,
        stream_output=True,
    )
    if returncode:
        raise click.exceptions.Exit(returncode)


@cli.command(
    short_help="Run a voice command through Talon's mimic().",
    help="Send one phrase to the guest Talon REPL as `mimic(<phrase>)`.",
    examples=(
        "  talonbox mimic 'focus chrome'",
        "  talonbox mimic 'tab close'",
    ),
)
@click.argument("command", metavar="PHRASE")
@pass_context
def mimic(ctx: Context, command: str) -> None:
    info = _require_running_vm(ctx)
    ip_address = _require_vm_ip(info, ctx.vm)
    wait_for_talon_repl(
        ip_address,
        debug=ctx.debug,
        timeout=TALON_REPL_TIMEOUT_SECONDS,
    )
    returncode = run_remote_repl(
        ip_address,
        build_mimic_payload(command),
        debug=ctx.debug,
    )
    if returncode:
        raise click.exceptions.Exit(returncode)


@cli.command(
    short_help="Capture a screenshot in the guest and download it locally.",
    help=(
        "Use Talon's screen capture API inside the guest, save the image to a guest temp file, "
        "download it to a host path under `/tmp`, and remove the guest temp file."
    ),
    examples=(
        "  talonbox screenshot /tmp/talon.png",
        "  talonbox --vm talon-test screenshot /tmp/guest-screen.png",
    ),
)
@click.argument(
    "filepath", metavar="HOST_PATH", type=click.Path(dir_okay=False, path_type=Path)
)
@pass_context
def screenshot(ctx: Context, filepath: Path) -> None:
    info = _require_running_vm(ctx)
```

`exec` dispatches based on argument count: a single string is passed to `run_remote_command_streaming` (the shell interprets it), while multiple tokens go to `run_remote_shell_streaming` (they are joined with `shlex.join` before the remote shell receives them). Both stream output directly to the terminal and propagate the remote exit code.

`repl` accepts code as an argument or reads it from stdin (but errors if stdin is a tty with no argument). It wraps the code in `compile`+`exec` before sending, so multi-statement snippets work correctly, and streams the REPL's output back.

`mimic` is the simplest Talon RPC command: it just builds `mimic(<phrase>)` and pipes it to the REPL — no payload wrapping needed because `mimic` is a top-level Talon built-in.

## Security model summary

Talonbox enforces a strict write boundary: **no caller-triggered writes to host files outside `/tmp`**. This protects the developer's machine against accidental or malicious side effects when talonbox is invoked by a coding agent or CI script.

```bash
grep -n 'HOST_OUTPUT_ROOT\|_normalize_local_output_path\|_is_relative_to\|RSYNC_REJECTED\|SCP_REJECTED\|symlink' src/talonbox/cli.py | head -20
```

```output
46:HOST_OUTPUT_ROOT = Path("/tmp")
85:RSYNC_REJECTED_OPTIONS = {
98:SCP_REJECTED_OPTIONS = {"-F", "-J", "-o", "-S"}
342:            path=str(_normalize_local_output_path(destination.path)),
359:def _normalize_local_output_path(raw_path: str | Path) -> Path:
370:    if _is_relative_to(resolved_destination, host_output_root):
380:    return HOST_OUTPUT_ROOT.resolve(strict=False)
383:def _is_relative_to(path: Path, root: Path) -> bool:
768:        rejected_options=RSYNC_REJECTED_OPTIONS,
800:        rejected_options=SCP_REJECTED_OPTIONS,
892:    filepath = _normalize_local_output_path(filepath)
```

The security model has four layers:

| Layer | Mechanism |
|---|---|
| Path confinement | `_normalize_local_output_path` resolves the full canonical path and calls `_is_relative_to(resolved, /tmp)`. Symlinks are followed before the check, so a symlink inside `/tmp` pointing outside is rejected. |
| Option filtering | `RSYNC_REJECTED_OPTIONS` and `SCP_REJECTED_OPTIONS` block flags like `-e` (remote-shell override), `--log-file`, `--temp-dir`, `--backup-dir` that could direct writes to arbitrary host paths. |
| Remote path isolation | Only `guest:/` prefixed paths are accepted as remote operands; bare `host:path` notation and `rsync://` URIs are rejected. |
| No cross-transfer | Local→local and guest→guest transfers are blocked; every transfer must cross the host/guest boundary in exactly one direction. |

Execution (SSH, REPL payloads, mimic) is intentionally unrestricted — the guest is an ephemeral sandbox and callers are expected to run arbitrary code inside it.

## End-to-end data flow: putting it all together

A typical test session looks like:

```bash
cat /tmp/session_example.sh
```

```output
# 1. Boot the VM and start Talon
talonbox start

# 2. Push a Talon script into the user directory
talonbox rsync -av ./my_script.talon guest:/Users/lume/.talon/user/my_script.talon

# 3. Run a voice command through the REPL
talonbox mimic "focus chrome"

# 4. Run arbitrary Python in the Talon context
talonbox repl "from talon import app; print(app.name())"

# 5. Capture a screenshot to verify the result
talonbox screenshot /tmp/result.png

# 6. Tear down
talonbox stop
```

Each command goes through the same call stack:



State flows in one direction only: the lume VM spawns with a PID saved to disk, every subsequent command re-reads the VM IP from `lume ls`, and the state file is deleted on stop. There is no long-lived talonbox daemon.

Each command goes through the same call stack:

  CLI command (cli.py)
    → _require_running_vm() → lume.get_vm_info() → [lume ls --format json]
    → transport function (transport.py)
        → sshpass ssh lume@<ip> sh -lc <command>
            → guest process

State flows in one direction only: the lume VM spawns with a PID saved to disk, every subsequent command re-reads the VM IP from `lume ls`, and the state file is deleted on stop. There is no long-lived talonbox daemon.

## Testing strategy

The test suite in `tests/test_talonbox.py` mirrors the production code structure.

```bash
grep -c 'def test_' tests/test_talonbox.py
```

```output
49
```

```bash
grep '^class Test\|^def test_' tests/test_talonbox.py | head -60
```

```output
def test_version() -> None:
def test_root_help_groups_commands_and_examples() -> None:
def test_exec_help_explains_double_dash_usage() -> None:
def test_mimic_help_works() -> None:
def test_show_running_vm_prints_auth(monkeypatch: pytest.MonkeyPatch) -> None:
def test_show_running_vm_prints_vnc_link_when_available(
def test_show_help_mentions_read_only_sandbox_safe_usage() -> None:
def test_start_refuses_running_vm(monkeypatch: pytest.MonkeyPatch) -> None:
def test_start_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
def test_start_failure_cleans_up_state(monkeypatch: pytest.MonkeyPatch) -> None:
def test_terminal_launch_command_runs_talon_via_arch_in_terminal() -> None:
def test_restart_talon_help_mentions_log_reset() -> None:
def test_restart_talon_restarts_without_wiping_user_dir(
def test_stop_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
def test_stop_logs_out_guest_session_before_stopping_vm(
def test_stop_continues_when_guest_logout_fails(
def test_stop_falls_back_to_force_stop_for_stuck_vm(
def test_exec_passes_through_args_and_exit_code(
def test_exec_single_argument_uses_shell_string(
def test_rsync_help_mentions_guest_prefix() -> None:
def test_scp_help_mentions_guest_prefix() -> None:
def test_rsync_upload_rewrites_guest_destination(
def test_rsync_download_rewrites_guest_source(monkeypatch: pytest.MonkeyPatch) -> None:
def test_rsync_allows_upload_from_outside_workspace(
def test_rsync_rejects_download_outside_tmp(monkeypatch: pytest.MonkeyPatch) -> None:
def test_scp_rejects_download_outside_tmp(monkeypatch: pytest.MonkeyPatch) -> None:
def test_rsync_rejects_guest_relative_path(monkeypatch: pytest.MonkeyPatch) -> None:
def test_rsync_rejects_local_to_local(monkeypatch: pytest.MonkeyPatch) -> None:
def test_rsync_rejects_non_guest_remote(monkeypatch: pytest.MonkeyPatch) -> None:
def test_rsync_rejects_old_implicit_guest_syntax(
def test_rsync_rejects_transport_override(monkeypatch: pytest.MonkeyPatch) -> None:
def test_rsync_rejects_host_write_option(monkeypatch: pytest.MonkeyPatch) -> None:
def test_scp_upload_rewrites_guest_destination(monkeypatch: pytest.MonkeyPatch) -> None:
def test_scp_download_rewrites_guest_source(monkeypatch: pytest.MonkeyPatch) -> None:
def test_scp_rejects_transport_override(monkeypatch: pytest.MonkeyPatch) -> None:
def test_scp_rejects_guest_to_guest(monkeypatch: pytest.MonkeyPatch) -> None:
def test_rsync_rejects_symlink_escape_from_tmp(
def test_scp_rejects_symlink_escape_from_tmp(
def test_repl_waits_for_socket_then_runs_piped_script(
def test_repl_accepts_inline_code(monkeypatch: pytest.MonkeyPatch) -> None:
def test_mimic_uses_python_escaped_payload(monkeypatch: pytest.MonkeyPatch) -> None:
def test_screenshot_uses_talon_capture_and_download(
def test_screenshot_fails_for_blank_png(
def test_screenshot_rejects_output_outside_tmp(monkeypatch: pytest.MonkeyPatch) -> None:
def test_get_vm_info_surfaces_raw_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
def test_get_vm_info_tolerates_log_line_before_json(
def test_get_vm_info_reads_vnc_url(monkeypatch: pytest.MonkeyPatch) -> None:
def test_run_rsync_uses_fixed_vm_shell(monkeypatch: pytest.MonkeyPatch) -> None:
def test_run_scp_uses_fixed_vm_ssh_options(monkeypatch: pytest.MonkeyPatch) -> None:
```

49 tests across seven categories. The testing approach is notable for three reasons:

1. **No subprocess execution** — every `lume`, `transport`, and system call is monkeypatched. Tests verify what arguments would have been passed, not the real side effects.
2. **Argument capture pattern** — many tests replace a function with one that appends its arguments to a list, then assert on that list after running the CLI via Click's `CliRunner`.
3. **Security regression tests** — symlink escapes, rejected options, guest-relative paths, non-guest remotes, and local-to-local transfers each have their own dedicated test, making the security invariants part of the test contract.

```bash
sed -n '1,60p' tests/test_talonbox.py
```

```output
from __future__ import annotations

import subprocess
import zlib
from pathlib import Path

import pytest
from click.testing import CliRunner

from talonbox import cli as cli_module
from talonbox import lume as lume_module
from talonbox.cli import cli
from talonbox.lume import VmInfo
from talonbox.state import StateRecord, state_paths
from talonbox.talon import build_mimic_payload, build_repl_exec_payload
from talonbox.transport import run_rsync, run_scp


@pytest.fixture(autouse=True)
def state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TALONBOX_STATE_DIR", str(tmp_path))


def test_version() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert result.output.startswith("talonbox, version ")


def test_root_help_groups_commands_and_examples() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "Minimal Talon VM control primitives for coding agents." in result.output
    assert "Use `show` for a read-only status check" in result.output
    assert "VM lifecycle:" in result.output
    assert "Guest shell:" in result.output
    assert "Talon RPC:" in result.output
    assert "scp" in result.output
    assert "restart-talon" in result.output
    assert "talonbox exec -- uname -a" in result.output


def test_exec_help_explains_double_dash_usage() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["exec", "--help"])

    assert result.exit_code == 0
    assert "Place `--` before the remote command" in result.output
    assert "talonbox exec -- whoami" in result.output


def test_mimic_help_works() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["mimic", "--help"])
```

The `autouse` fixture at the top redirects the state directory to `tmp_path` for every test, preventing any test from touching the real `~/Library/Application Support/talonbox` directory. This is the only persistent side effect that needed isolation — lume and SSH never actually run.

## Conclusion

Talonbox is a focused tool with a clear layering:



Each module has a single responsibility and no upward dependencies. The security invariants (host write confinement, option filtering, symlink escape prevention) are concentrated in a handful of functions in `cli.py` and tested exhaustively. Adding a new Talon RPC command means writing a payload builder in `talon.py` and a thin Click command in `cli.py` — everything else is already in place.

## Conclusion

Talonbox is a focused tool with a clear layering:

    cli.py           <- command definitions, safety validation, orchestration
      +-- lume.py    <- VM lifecycle (spawn, wait, stop, force-stop)
      +-- transport.py <- SSH / rsync / scp execution
      +-- state.py   <- PID + metadata persistence
      +-- talon.py   <- REPL payload generation

Each module has a single responsibility and no upward dependencies. The security invariants (host write confinement, option filtering, symlink escape prevention) are concentrated in a handful of functions in cli.py and tested exhaustively. Adding a new Talon RPC command means writing a payload builder in talon.py and a thin Click command in cli.py -- everything else is already in place.
