from __future__ import annotations

import shlex
import sys
import uuid
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

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
    run_rsync_to_guest,
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
HELP_COMMAND_GROUPS = (
    ("VM lifecycle", ("setup", "start", "restart-talon", "stop", "show")),
    ("Guest shell", ("exec", "rsync")),
    ("Talon RPC", ("repl", "mimic", "screenshot")),
)


class MimicCommand(click.Command):
    def __init__(self, *args: object, examples: Sequence[str] | None = None, **kwargs: object) -> None:
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


class MimicGroup(click.Group):
    command_class = MimicCommand

    def __init__(self, *args: object, examples: Sequence[str] | None = None, **kwargs: object) -> None:
        self.examples = list(examples or [])
        super().__init__(*args, **kwargs)

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        self.format_usage(ctx, formatter)
        self.format_help_text(ctx, formatter)
        self.format_options(ctx, formatter)
        self.format_epilog(ctx, formatter)
        self._format_examples(formatter)

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
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


@dataclass(slots=True)
class Context:
    vm: str
    debug: bool

    def debug_log(self, message: str) -> None:
        if self.debug:
            click.echo(message, err=True)


pass_context = click.make_pass_decorator(Context)


def _raise_click_error(message: str) -> None:
    raise click.ClickException(message)


def _handle_transport_error(error: Exception) -> None:
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
    info = _require_vm(ctx)
    if info.status != "running" or not info.ip_address:
        _raise_click_error(f"VM is not running: {ctx.vm}")
    return info


def _print_vm_info(info: lume.VmInfo) -> None:
    click.echo(f"status: {info.status}")
    if info.status == "running" and info.ip_address:
        click.echo(f"ip: {info.ip_address}")
        click.echo(f"username: {SSH_USERNAME}")
        click.echo(f"password: {SSH_PASSWORD}")


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
            f'mkdir -p "$HOME/.talon" && : > {TALON_LOG} && : > /tmp/mimic-talon.log',
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


def _bootstrap_talon(ip_address: str, debug: bool) -> None:
    _restart_talon(
        ip_address,
        debug=debug,
        wipe_user_dir=True,
        clean_logs=True,
    )


def _terminal_launch_command() -> str:
    script_path = "/tmp/mimic-launch.command"
    script_body = (
        "#!/bin/sh\n"
        f"exec arch -x86_64 {TALON_BINARY} >/tmp/mimic-talon.log 2>&1\n"
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
    name="mimic-cli",
    cls=MimicGroup,
    context_settings={"max_content_width": 100},
    help=(
        "Minimal Talon VM control primitives for coding agents.\n\n"
        "Use `start` to boot the VM and reset Talon to a clean state. Use `exec` and `rsync` "
        "for general guest access. Use `repl`, `mimic`, and `screenshot` for predictable "
        "Talon-native operations. Use `show` for a read-only status check; it does not modify "
        "the VM."
    ),
    examples=(
        "  mimic-cli start",
        "  mimic-cli restart-talon",
        "  mimic-cli exec -- uname -a",
        "  mimic-cli rsync -av ~/.talon/user/ /Users/lume/.talon/user/",
        "  mimic-cli mimic 'focus chrome'",
        "  mimic-cli screenshot /tmp/talon.png",
    ),
)
@click.option("--vm", default=DEFAULT_VM, show_default=True, help="Target VM name.")
@click.option(
    "--debug",
    is_flag=True,
    envvar="MIMIC_DEBUG",
    help="Print invoked commands and failure details to stderr. Can also be enabled with MIMIC_DEBUG=1.",
)
@click.version_option(prog_name="mimic-cli")
@click.pass_context
def cli(click_ctx: click.Context, vm: str, debug: bool) -> None:
    click_ctx.obj = Context(vm=vm, debug=debug)


@cli.command(
    short_help="Create or provision the test VM (stub for now).",
    help="Create or provision the Talon test VM.\n\nThis command is reserved for future setup automation.",
    examples=("  mimic-cli setup",),
)
def setup() -> None:
    _raise_click_error("setup is not implemented yet")


@cli.command(
    short_help="Boot the VM, wipe the Talon user dir, and restart Talon.",
    help=(
        "Start the VM in the background, wait for SSH, clear the guest Talon user directory, "
        "and relaunch Talon under Rosetta.\n\n"
        "Talon is launched through Terminal so guest Screen Recording permissions apply to "
        "the process that captures screenshots.\n\n"
        "The command fails if the VM is already running."
    ),
    examples=(
        "  mimic-cli start",
        "  mimic-cli --vm talon-test --debug start",
    ),
)
@pass_context
def start(ctx: Context) -> None:
    info = _require_vm(ctx)
    if info.status == "running":
        _raise_click_error(f"VM is already running: {ctx.vm}")
    if info.status != "stopped":
        _raise_click_error(f"VM is not stopped: {ctx.vm} ({info.status})")

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
        probe_ssh(ready_vm.ip_address, debug=ctx.debug, timeout=SSH_TIMEOUT_SECONDS)
        _bootstrap_talon(ready_vm.ip_address, debug=ctx.debug)
    except (lume.LumeError, RemoteCommandError, TransportError) as error:
        if "state" in locals():
            _cleanup_failed_start(ctx, state)
        _handle_transport_error(error)

    _print_vm_info(ready_vm)


@cli.command(
    name="restart-talon",
    short_help="Restart Talon inside the running VM and reset Talon logs.",
    help=(
        "Restart Talon inside the running VM without rebooting the VM.\n\n"
        "This truncates `~/.talon/talon.log` and `/tmp/mimic-talon.log`, then relaunches "
        "Talon under Rosetta through Terminal so screen capture permissions still apply."
    ),
    examples=(
        "  mimic-cli restart-talon",
        "  mimic-cli --debug restart-talon",
    ),
)
@pass_context
def restart_talon(ctx: Context) -> None:
    info = _require_running_vm(ctx)
    try:
        _restart_talon(
            info.ip_address,
            debug=ctx.debug,
            wipe_user_dir=False,
            clean_logs=True,
        )
    except (RemoteCommandError, TransportError) as error:
        _handle_transport_error(error)


@cli.command(
    short_help="Stop the VM if it is running.",
    help="Stop the VM and clear mimic-cli local state. Safe to run repeatedly.",
    examples=(
        "  mimic-cli stop",
        "  mimic-cli --vm talon-test stop",
    ),
)
@pass_context
def stop(ctx: Context) -> None:
    info = _require_vm(ctx)
    if info.status != "stopped":
        state = load_state(ctx.vm)
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
        "Show whether the VM is running. When it is running, also print IP and SSH credentials.\n\n"
        "This command is read-only: it does not start, stop, or modify the VM, and is safe to "
        "use in sandboxed environments that permit running `lume ls`."
    ),
    examples=(
        "  mimic-cli show",
        "  mimic-cli --vm talon-test show",
    ),
)
@pass_context
def show(ctx: Context) -> None:
    info = _require_vm(ctx)
    _print_vm_info(info)


@cli.command(
    name="exec",
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
    short_help="Run a command on the guest via SSH.",
    help=(
        "Run a command on the guest VM over SSH.\n\n"
        "Place `--` before the remote command so mimic-cli stops parsing options.\n\n"
        "For shell pipelines or redirects, pass a single quoted shell string."
    ),
    examples=(
        "  mimic-cli exec -- whoami",
        "  mimic-cli exec -- sh -lc 'ls -la ~/.talon'",
        '  mimic-cli exec -- "ps aux | grep Safari"',
    ),
)
@click.argument("command", nargs=-1, type=click.UNPROCESSED, metavar="COMMAND...")
@pass_context
def exec_command(ctx: Context, command: tuple[str, ...]) -> None:
    if not command:
        _raise_click_error("No command provided")
    info = _require_running_vm(ctx)
    if len(command) == 1:
        returncode = run_remote_command_streaming(
            info.ip_address,
            command[0],
            debug=ctx.debug,
        )
    else:
        returncode = run_remote_shell_streaming(
            info.ip_address,
            list(command),
            debug=ctx.debug,
        )
    if returncode:
        raise click.exceptions.Exit(returncode)


@cli.command(
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
    short_help="Copy files from host to guest with rsync.",
    help=(
        "Run rsync from the host into the guest VM.\n\n"
        "Pass normal rsync flags and sources. The final argument is treated as the guest "
        "destination path and will be rewritten to `lume@<ip>:<dest>`."
    ),
    examples=(
        "  mimic-cli rsync -av ~/.talon/user/ /Users/lume/.talon/user/",
        "  mimic-cli rsync --delete repo/ /Users/lume/work/repo/",
    ),
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED, metavar="RSYNC_ARGS...")
@pass_context
def rsync(ctx: Context, args: tuple[str, ...]) -> None:
    if len(args) < 2:
        _raise_click_error("rsync requires at least one source and one destination")
    info = _require_running_vm(ctx)
    returncode = run_rsync_to_guest(
        info.ip_address,
        list(args),
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
        "  mimic-cli repl 'print(1+1)'",
        "  printf 'print(1+1)\\n' | mimic-cli repl",
    ),
)
@click.argument("code", required=False, metavar="[CODE]")
@pass_context
def repl(ctx: Context, code: str | None) -> None:
    info = _require_running_vm(ctx)
    wait_for_talon_repl(
        info.ip_address,
        debug=ctx.debug,
        timeout=TALON_REPL_TIMEOUT_SECONDS,
    )
    if code is None:
        if sys.stdin.isatty():
            _raise_click_error("No code provided. Pass CODE or pipe Python into stdin.")
        code = sys.stdin.read()
    returncode = run_remote_repl(
        info.ip_address,
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
        "  mimic-cli mimic 'focus chrome'",
        "  mimic-cli mimic 'tab close'",
    ),
)
@click.argument("command", metavar="PHRASE")
@pass_context
def mimic(ctx: Context, command: str) -> None:
    info = _require_running_vm(ctx)
    wait_for_talon_repl(
        info.ip_address,
        debug=ctx.debug,
        timeout=TALON_REPL_TIMEOUT_SECONDS,
    )
    returncode = run_remote_repl(
        info.ip_address,
        build_mimic_payload(command),
        debug=ctx.debug,
    )
    if returncode:
        raise click.exceptions.Exit(returncode)


@cli.command(
    short_help="Capture a screenshot in the guest and download it locally.",
    help=(
        "Use Talon's screen capture API inside the guest, save the image to a guest temp file, "
        "download it to the host path you provide, and remove the guest temp file."
    ),
    examples=(
        "  mimic-cli screenshot /tmp/talon.png",
        "  mimic-cli --vm talon-test screenshot ./artifacts/guest-screen.png",
    ),
)
@click.argument("filepath", metavar="HOST_PATH", type=click.Path(dir_okay=False, path_type=Path))
@pass_context
def screenshot(ctx: Context, filepath: Path) -> None:
    info = _require_running_vm(ctx)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    remote_path = f"/tmp/mimic-screenshot-{uuid.uuid4().hex}.png"
    try:
        wait_for_talon_repl(
            info.ip_address,
            debug=ctx.debug,
            timeout=TALON_REPL_TIMEOUT_SECONDS,
        )
        returncode = run_remote_repl(
            info.ip_address,
            build_screenshot_payload(remote_path),
            debug=ctx.debug,
        )
        if returncode:
            raise click.exceptions.Exit(returncode)
        download_from_guest(
            info.ip_address,
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
                info.ip_address,
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
