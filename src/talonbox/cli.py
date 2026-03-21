from __future__ import annotations

import shlex
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import click

from . import lume
from .talon import (
    build_mimic_payload,
    build_repl_exec_payload,
    build_screenshot_payload,
)
from .transport import (
    SSH_PASSWORD,
    SSH_USERNAME,
    RemoteCommandError,
    TransportError,
    download_from_guest,
    probe_ssh,
    run_remote_repl,
    run_remote_shell,
    run_rsync,
    run_scp,
    wait_for_talon_repl,
)

DEFAULT_VM = "talon-test"
TALON_BINARY = "/Applications/Talon.app/Contents/MacOS/Talon"
TALON_LOG = "$HOME/.talon/talon.log"
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
    "--skip-compress",
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


def _examples_epilog(*examples: str) -> str:
    body = "\n".join(f"  {example}" for example in examples)
    return f"\b\nExamples:\n{body}"


class TalonboxGroup(click.Group):
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


def _get_vm(ctx: Context) -> lume.VmInfo:
    try:
        info = lume.get_vm_info(ctx.vm, debug=ctx.debug)
    except lume.LumeError as error:
        _raise_click_error(str(error))
    if info is None:
        _raise_click_error(f"VM not found: {ctx.vm}")
    return info


def _get_running_vm_ip(ctx: Context) -> str:
    info = _get_vm(ctx)
    if info.status != "running" or not info.ip_address:
        _raise_click_error(f"VM is not running: {ctx.vm}")
    return info.ip_address


def _print_vm_info(info: lume.VmInfo) -> None:
    click.echo(f"status: {info.status}")
    if info.status == "running" and info.ip_address:
        click.echo(f"ip: {info.ip_address}")
        click.echo(f"username: {SSH_USERNAME}")
        click.echo(f"password: {SSH_PASSWORD}")
        if info.vnc_url:
            click.echo(f"vnc: {info.vnc_url}")


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
            option, has_value, _ = arg.partition("=")
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

    ip_address = _get_running_vm_ip(ctx)
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
        _build_talon_terminal_launch_command(),
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


def _build_talon_terminal_launch_command() -> str:
    script_path = "/tmp/talonbox-launch.command"
    script_body = (
        f"#!/bin/sh\nexec arch -x86_64 {TALON_BINARY} >/tmp/talonbox-talon.log 2>&1\n"
    )
    return (
        f"printf %s {shlex.quote(script_body)} > {shlex.quote(script_path)} && "
        f"chmod +x {shlex.quote(script_path)} && "
        f"open -a Terminal {shlex.quote(script_path)}"
    )


def _cleanup_failed_start(ctx: Context) -> None:
    ctx.debug_log("start failed; stopping VM")
    try:
        lume.stop_vm(ctx.vm, debug=ctx.debug)
        lume.wait_for_status(ctx.vm, "stopped", timeout=30.0, debug=ctx.debug)
    except lume.LumeError as error:
        ctx.debug_log(f"cleanup stop failed: {error}")


@click.group(
    name="talonbox",
    cls=TalonboxGroup,
    context_settings={"max_content_width": 100},
    help=(
        "Minimal Talon VM control primitives for coding agents.\n\n"
        "Use `start` to boot the VM and reset Talon to a clean state. Use `exec` and `rsync` "
        "for general guest access. Use `repl`, `mimic`, and `screenshot` for predictable "
        "Talon-native operations. Use `show` for a read-only status check; it does not modify "
        "the VM."
    ),
    epilog=_examples_epilog(
        "talonbox start",
        "talonbox restart-talon",
        "talonbox exec -- uname -a",
        "talonbox rsync -av ~/.talon/user/ guest:/Users/lume/.talon/user/",
        "talonbox scp guest:/tmp/out.png /tmp/out.png",
        "talonbox mimic 'focus chrome'",
        "talonbox screenshot /tmp/talon.png",
    ),
)
@click.option("--vm", default=DEFAULT_VM, show_default=True, help="Target VM name.")
@click.option(
    "--debug",
    is_flag=True,
    envvar="TALONBOX_DEBUG",
    help="Print invoked commands and failure details to stderr. Can also be enabled with TALONBOX_DEBUG=1.",
)
@click.version_option(prog_name="talonbox")
@click.pass_context
def cli(click_ctx: click.Context, vm: str, debug: bool) -> None:
    click_ctx.obj = Context(vm=vm, debug=debug)


@cli.command(
    short_help="Create or provision the test VM (stub for now).",
    help="Create or provision the Talon test VM.\n\nThis command is reserved for future setup automation.",
    epilog=_examples_epilog("talonbox setup"),
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
    epilog=_examples_epilog(
        "talonbox start",
        "talonbox --vm talon-test --debug start",
    ),
)
@pass_context
def start(ctx: Context) -> None:
    info = _get_vm(ctx)
    if info.status == "running":
        _raise_click_error(f"VM is already running: {ctx.vm}")
    if info.status != "stopped":
        _raise_click_error(f"VM is not stopped: {ctx.vm} ({info.status})")

    process = None
    try:
        process = lume.spawn_vm(ctx.vm, debug=ctx.debug)
        ready_vm = lume.wait_for_running_vm(
            ctx.vm,
            timeout=START_TIMEOUT_SECONDS,
            debug=ctx.debug,
            pid=process.pid,
        )
        ready_ip = ready_vm.ip_address
        assert ready_ip is not None
        probe_ssh(ready_ip, debug=ctx.debug, timeout=SSH_TIMEOUT_SECONDS)
        _restart_talon(
            ready_ip,
            debug=ctx.debug,
            wipe_user_dir=True,
            clean_logs=True,
        )
    except (lume.LumeError, RemoteCommandError, TransportError) as error:
        if process is not None:
            _cleanup_failed_start(ctx)
        _raise_click_error(str(error))

    _print_vm_info(ready_vm)


@cli.command(
    short_help="Restart Talon inside the running VM and reset Talon logs.",
    help=(
        "Restart Talon inside the running VM without rebooting the VM.\n\n"
        "This truncates `~/.talon/talon.log` and `/tmp/talonbox-talon.log`, then relaunches "
        "Talon under Rosetta through Terminal so screen capture permissions still apply."
    ),
    epilog=_examples_epilog(
        "talonbox restart-talon",
        "talonbox --debug restart-talon",
    ),
)
@pass_context
def restart_talon(ctx: Context) -> None:
    try:
        _restart_talon(
            _get_running_vm_ip(ctx),
            debug=ctx.debug,
            wipe_user_dir=False,
            clean_logs=True,
        )
    except (RemoteCommandError, TransportError) as error:
        _raise_click_error(str(error))


@cli.command(
    short_help="Stop the VM if it is running.",
    help=(
        "Log out the guest GUI session when possible, then stop the VM. Safe to run repeatedly."
    ),
    epilog=_examples_epilog(
        "talonbox stop",
        "talonbox --vm talon-test stop",
    ),
)
@pass_context
def stop(ctx: Context) -> None:
    info = _get_vm(ctx)
    if info.status != "stopped":
        if info.status == "running" and info.ip_address:
            try:
                _logout_guest_session(info.ip_address, debug=ctx.debug)
            except (RemoteCommandError, TransportError) as error:
                ctx.debug_log(f"guest logout failed: {error}")
        try:
            lume.stop_vm(ctx.vm, debug=ctx.debug)
            lume.wait_for_status(ctx.vm, "stopped", timeout=60.0, debug=ctx.debug)
        except lume.LumeError as error:
            ctx.debug_log(f"graceful stop failed: {error}")
            try:
                lume.force_stop_vm(ctx.vm, debug=ctx.debug)
                lume.wait_for_status(ctx.vm, "stopped", timeout=20.0, debug=ctx.debug)
            except lume.LumeError as force_error:
                _raise_click_error(str(force_error))


@cli.command(
    short_help="Print VM status and connection details without changing anything.",
    help=(
        "Show whether the VM is running. When it is running, also print IP, SSH credentials, "
        "and the VNC link.\n\n"
        "This command is read-only: it does not start, stop, or modify the VM, and is safe to "
        "use in sandboxed environments that permit running `lume ls`."
    ),
    epilog=_examples_epilog(
        "talonbox show",
        "talonbox --vm talon-test show",
    ),
)
@pass_context
def show(ctx: Context) -> None:
    info = _get_vm(ctx)
    _print_vm_info(info)


@cli.command(
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
    short_help="Run a command on the guest via SSH.",
    help=(
        "Run a command on the guest VM over SSH.\n\n"
        "Place `--` before the remote command so talonbox stops parsing options.\n\n"
        "For shell pipelines or redirects, pass a single quoted shell string."
    ),
    epilog=_examples_epilog(
        "talonbox exec -- whoami",
        "talonbox exec -- sh -lc 'ls -la ~/.talon'",
        'talonbox exec -- "ps aux | grep Safari"',
    ),
)
@click.argument("command", nargs=-1, type=click.UNPROCESSED, metavar="COMMAND...")
@pass_context
def exec_command(ctx: Context, command: tuple[str, ...]) -> None:
    if not command:
        _raise_click_error("No command provided")
    result = run_remote_shell(
        _get_running_vm_ip(ctx),
        command[0] if len(command) == 1 else list(command),
        debug=ctx.debug,
        stream=True,
        check=False,
    )
    if result.returncode:
        raise click.exceptions.Exit(result.returncode)


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
    epilog=_examples_epilog(
        "talonbox rsync -av ./repo/ guest:/Users/lume/.talon/user/repo/",
        "talonbox rsync -av guest:/Users/lume/Pictures/ /tmp/guest-pictures/",
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
    epilog=_examples_epilog(
        "talonbox scp ./settings.talon guest:/Users/lume/.talon/user/settings.talon",
        "talonbox scp guest:/tmp/out.png /tmp/out.png",
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
    epilog=_examples_epilog(
        "talonbox repl 'print(1+1)'",
        "printf 'print(1+1)\\n' | talonbox repl",
    ),
)
@click.argument("code", required=False, metavar="[CODE]")
@pass_context
def repl(ctx: Context, code: str | None) -> None:
    ip_address = _get_running_vm_ip(ctx)
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
    result = run_remote_repl(
        ip_address,
        build_repl_exec_payload(code),
        debug=ctx.debug,
        stream_output=True,
    )
    if result.returncode:
        raise click.exceptions.Exit(result.returncode)


@cli.command(
    short_help="Run a voice command through Talon's mimic().",
    help="Send one phrase to the guest Talon REPL as `mimic(<phrase>)`.",
    epilog=_examples_epilog(
        "talonbox mimic 'focus chrome'",
        "talonbox mimic 'tab close'",
    ),
)
@click.argument("command", metavar="PHRASE")
@pass_context
def mimic(ctx: Context, command: str) -> None:
    ip_address = _get_running_vm_ip(ctx)
    wait_for_talon_repl(
        ip_address,
        debug=ctx.debug,
        timeout=TALON_REPL_TIMEOUT_SECONDS,
    )
    result = run_remote_repl(
        ip_address,
        build_mimic_payload(command),
        debug=ctx.debug,
    )
    if result.returncode:
        raise click.exceptions.Exit(result.returncode)


@cli.command(
    short_help="Capture a screenshot in the guest and download it locally.",
    help=(
        "Use Talon's screen capture API inside the guest, save the image to a guest temp file, "
        "download it to a host path under `/tmp`, and remove the guest temp file."
    ),
    epilog=_examples_epilog(
        "talonbox screenshot /tmp/talon.png",
        "talonbox --vm talon-test screenshot /tmp/guest-screen.png",
    ),
)
@click.argument(
    "filepath", metavar="HOST_PATH", type=click.Path(dir_okay=False, path_type=Path)
)
@pass_context
def screenshot(ctx: Context, filepath: Path) -> None:
    ip_address = _get_running_vm_ip(ctx)
    filepath = _normalize_local_output_path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    remote_path = f"/tmp/talonbox-screenshot-{uuid.uuid4().hex}.png"
    try:
        wait_for_talon_repl(
            ip_address,
            debug=ctx.debug,
            timeout=TALON_REPL_TIMEOUT_SECONDS,
        )
        result = run_remote_repl(
            ip_address,
            build_screenshot_payload(remote_path),
            debug=ctx.debug,
        )
        if result.returncode:
            raise click.exceptions.Exit(result.returncode)
        download_from_guest(
            ip_address,
            remote_path,
            filepath,
            debug=ctx.debug,
        )
    except (RemoteCommandError, TransportError) as error:
        _raise_click_error(str(error))
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
