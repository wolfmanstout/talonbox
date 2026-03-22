from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import click

from .smoke_test import SmokeTestRunner
from .talon_client import TalonClient
from .transfer import TransferService
from .vm import VmController

DEFAULT_VM = "talon-test"
HELP_COMMAND_GROUPS = (
    ("VM lifecycle", ("setup", "start", "restart-talon", "smoke-test", "stop", "show")),
    ("Guest shell", ("exec", "rsync", "scp")),
    ("Talon RPC", ("repl", "mimic", "screenshot")),
)


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
class CliSettings:
    vm: str
    debug: bool


pass_settings = click.make_pass_decorator(CliSettings)


def _require_macos() -> None:
    if sys.platform == "darwin" or "PYTEST_CURRENT_TEST" in os.environ:
        return
    raise click.ClickException("talonbox currently supports only macOS hosts.")


def _echo_vm_info(vm_controller: VmController, info: object) -> None:
    assert hasattr(info, "status")
    for line in vm_controller.format_vm_info(info):  # type: ignore[arg-type]
        click.echo(line)


def _build_talon_client(settings: CliSettings) -> TalonClient:
    vm_controller = VmController(settings.vm, settings.debug)
    running_vm = vm_controller.get_running_vm()
    transfer_service = TransferService(running_vm)
    return TalonClient(running_vm, transfer_service)


def _build_smoke_test_runner(settings: CliSettings) -> SmokeTestRunner:
    vm_controller = VmController(settings.vm, settings.debug)
    return SmokeTestRunner(vm_controller)


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
        "talonbox smoke-test",
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
    _require_macos()
    click_ctx.obj = CliSettings(vm=vm, debug=debug)


@cli.command(
    short_help="Create or provision the test VM (stub for now).",
    help="Create or provision the Talon test VM.\n\nThis command is reserved for future setup automation.",
    epilog=_examples_epilog("talonbox setup"),
)
def setup() -> None:
    raise click.ClickException("setup is not implemented yet")


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
@pass_settings
def start(settings: CliSettings) -> None:
    vm_controller = VmController(settings.vm, settings.debug)
    _echo_vm_info(vm_controller, vm_controller.start().to_vm_info())


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
@pass_settings
def restart_talon(settings: CliSettings) -> None:
    VmController(settings.vm, settings.debug).restart_talon(
        wipe_user_dir=False,
        clean_logs=True,
    )


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
@pass_settings
def stop(settings: CliSettings) -> None:
    VmController(settings.vm, settings.debug).stop()


@cli.command(
    name="smoke-test",
    short_help="Run a basic end-to-end diagnostic against the Talon VM.",
    help=(
        "Run a mutating end-to-end sanity check for talonbox.\n\n"
        "This command may stop a running VM, starts the VM cleanly, uploads a temporary Talon "
        "command bundle, runs mimic(), verifies a guest-side marker file, captures a screenshot, "
        "and stops the VM again.\n\n"
        "Artifacts are kept under `/tmp` for debugging, and the VM is left stopped after the run."
    ),
    epilog=_examples_epilog(
        "talonbox smoke-test",
        "talonbox --debug smoke-test",
        "talonbox smoke-test --yes",
    ),
)
@click.option(
    "-y",
    "--yes",
    is_flag=True,
    help="Skip the confirmation prompt if the VM is already running.",
)
@pass_settings
def smoke_test(settings: CliSettings, yes: bool) -> None:
    _build_smoke_test_runner(settings).run(yes=yes)


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
@pass_settings
def show(settings: CliSettings) -> None:
    vm_controller = VmController(settings.vm, settings.debug)
    _echo_vm_info(vm_controller, vm_controller.get_vm())


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
@pass_settings
def exec_command(settings: CliSettings, command: tuple[str, ...]) -> None:
    if not command:
        raise click.ClickException("No command provided")
    result = (
        VmController(settings.vm, settings.debug)
        .get_running_vm()
        .run_shell(
            command[0] if len(command) == 1 else list(command),
            stream=True,
            check=False,
        )
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
        "`/tmp`. Transfers run inside the macOS sandbox, so extra host-side writes outside "
        "that boundary fail with an obvious permission error."
    ),
    epilog=_examples_epilog(
        "talonbox rsync -av ./repo/ guest:/Users/lume/.talon/user/repo/",
        "talonbox rsync -av guest:/Users/lume/Pictures/ /tmp/guest-pictures/",
    ),
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED, metavar="RSYNC_ARGS...")
@pass_settings
def rsync(settings: CliSettings, args: tuple[str, ...]) -> None:
    running_vm = VmController(settings.vm, settings.debug).get_running_vm()
    returncode = TransferService(running_vm).rsync(args)
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
        "`/tmp`. Transfers run inside the macOS sandbox, so extra host-side writes outside "
        "that boundary fail with an obvious permission error."
    ),
    epilog=_examples_epilog(
        "talonbox scp ./settings.talon guest:/Users/lume/.talon/user/settings.talon",
        "talonbox scp guest:/tmp/out.png /tmp/out.png",
    ),
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED, metavar="SCP_ARGS...")
@pass_settings
def scp(settings: CliSettings, args: tuple[str, ...]) -> None:
    running_vm = VmController(settings.vm, settings.debug).get_running_vm()
    returncode = TransferService(running_vm).scp(args)
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
@pass_settings
def repl(settings: CliSettings, code: str | None) -> None:
    if code is None:
        if sys.stdin.isatty():
            raise click.ClickException(
                "No code provided. Pass CODE or pipe Python into stdin."
            )
        code = sys.stdin.read()
    assert code is not None
    _build_talon_client(settings).repl(code)


@cli.command(
    short_help="Run a voice command through Talon's mimic().",
    help="Send one phrase to the guest Talon REPL as `mimic(<phrase>)`.",
    epilog=_examples_epilog(
        "talonbox mimic 'focus chrome'",
        "talonbox mimic 'tab close'",
    ),
)
@click.argument("command", metavar="PHRASE")
@pass_settings
def mimic(settings: CliSettings, command: str) -> None:
    _build_talon_client(settings).mimic(command)


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
@pass_settings
def screenshot(settings: CliSettings, filepath: Path) -> None:
    _build_talon_client(settings).capture_screenshot(filepath)


def main() -> int:
    cli.main(standalone_mode=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
