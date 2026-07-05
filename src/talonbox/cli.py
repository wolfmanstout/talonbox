from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import click

from .names import to_tart_vm_name
from .smoke_test import SmokeTestRunner
from .talon_client import TalonClient
from .transfer import TransferService, parse_rsync_args, parse_scp_args
from .vm import VmController
from .vnc_client import shutdown_vnc_reactor

HELP_COMMAND_GROUPS = (
    (
        "VM lifecycle",
        (
            "create",
            "clone",
            "rename",
            "delete",
            "list",
            "status",
            "open",
            "start",
            "stop",
            "smoke-test",
        ),
    ),
    ("Guest shell", ("exec", "rsync", "scp")),
    ("Talon RPC", ("restart-talon", "repl", "mimic", "click", "type", "screenshot")),
)

DEFAULT_TALON_DMG_URL = "https://talonvoice.com/dl/latest/talon-mac.dmg"
DEFAULT_BASE_VM_NAME = "tahoe-base"
DEFAULT_BASE_IMAGE = "ghcr.io/cirruslabs/macos-tahoe-base:latest"
APPLE_SPEECH_MANAGER_REFERENCE_URL = "https://developer.apple.com/library/archive/documentation/mac/pdf/Sound/Speech_Manager.pdf"


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


def _echo_success(command_name: str) -> None:
    click.echo(f"{command_name} successful")


def _build_talon_client(settings: CliSettings, vm: str) -> TalonClient:
    running_vm = VmController(vm, settings.debug).get_running_vm()
    transfer_service = TransferService(running_vm)
    return TalonClient(running_vm, transfer_service)


def _build_smoke_test_runner(settings: CliSettings, source: str) -> SmokeTestRunner:
    return SmokeTestRunner(VmController(source, settings.debug))


def _is_url(value: str) -> bool:
    return urlparse(value).scheme in {"http", "https"}


def _render_create_markdown(name: str, talon_dmg: str, base: str) -> str:
    quoted_name = shlex.quote(name)
    quoted_base = shlex.quote(base)
    tart_base = to_tart_vm_name(base)
    quoted_tart_base = shlex.quote(tart_base)
    quoted_base_image = shlex.quote(DEFAULT_BASE_IMAGE)
    quoted_talon_dmg = shlex.quote(talon_dmg)
    talon_dmg_setup = (
        f"```bash\n"
        f"talonbox exec {quoted_name} -- curl -L -o /tmp/talon.dmg {quoted_talon_dmg}\n"
        f"```\n"
        if _is_url(talon_dmg)
        else f"```bash\n"
        f"talonbox scp -q {quoted_talon_dmg} {quoted_name}:/tmp/talon.dmg\n"
        f"```\n"
    )

    return f"""# Create a talonbox VM: `{name}`

This command prints setup instructions only. Creating a Talon-ready macOS VM requires manual Talon EULA acceptance and navigating GUI permission prompts that change with every macOS update.

During setup and troubleshooting, prefer `--vnc` anywhere it is available in talonbox commands. VNC screenshots capture the VM framebuffer and are more likely to show macOS permission dialogs than Talon's own screenshot APIs. Agents may use `talonbox screenshot --vnc`, `talonbox click --vnc`, and `talonbox type --vnc` to inspect and respond to ordinary macOS permission dialogs after the user has authorized the action.

## 1. Install Tart

Ensure `tart` is installed. Follow the Tart quick start as needed:

https://tart.run/quick-start/

Verify the install:

```bash
tart --version
```

Install `sshpass` for talonbox's SSH, rsync, and scp transport:

```bash
brew install cirruslabs/cli/sshpass
```

## 2. Clone or reuse the macOS base VM

The base VM name is `{base}` in talonbox commands and `{tart_base}` in Tart commands. Pass the unprefixed name to `talonbox create --base`; talonbox's `talonbox-` prefix is applied automatically when rendering Tart commands.

First check whether the talonbox base VM already exists:

```bash
tart list
```

If it already exists, reuse it. Do not create, overwrite, or delete an existing base VM during setup unless requested by the user. If the existing base VM is already verified, skip to cloning below. If you are not sure whether it is complete, skip creation and continue at the verification steps below.

If the base VM does not exist, clone it from Cirrus Labs' Tahoe base image:

```bash
tart clone {quoted_base_image} {quoted_tart_base}
```

This can take a long time to download, depending on the user's internet connection. Do not resize the clone; resizing Tart clones is complicated, and the default disk size is the supported talonbox setup path.

## 3. Verify the base VM

```bash
talonbox start --no-talon {quoted_base}
talonbox exec {quoted_base} -- whoami
```

The expected output is `admin`. Tart's Cirrus Labs macOS images use `admin` with password `admin`.

Ask for the user's help before going in circles trying to resolve issues. When asking the user to do something over VNC, look up the VNC URL first, then give the user the actual `vnc://...` URL and the simple talonbox command that opens the viewer. For base setup recovery:

```bash
talonbox status {quoted_base}
```

Then tell the user:

```text
VNC URL: vnc://...
Open it with: talonbox open {quoted_base}
```

Restart the base VM once and confirm it returns to a logged-in desktop before cloning it.

## 4. Clone and start `{name}`

After the base VM is complete, shut it down and create the Talon VM from it. Use `talonbox clone`, not `tart clone`, so talonbox can apply its normal naming.

```bash
talonbox stop --shutdown {quoted_base}
talonbox clone {quoted_base} {quoted_name}
talonbox start --no-talon {quoted_name}
```

`--no-talon` starts the VM and waits for SSH without trying to launch Talon or wait for Talon's REPL. Use it while creating or repairing a VM before Talon is fully installed and accepted.

## 5. Install Talon

Use `talonbox exec` for guest commands and `talonbox scp` for file copies.

Verify SSH access:

```bash
talonbox exec {quoted_name} -- whoami
```

Copy or download the Talon DMG:

{talon_dmg_setup}
Mount the DMG and copy Talon into `/Applications`:

```bash
talonbox exec {quoted_name} -- hdiutil attach /tmp/talon.dmg -mountpoint /Volumes/Talon
talonbox exec {quoted_name} -- cp -R /Volumes/Talon/Talon.app /Applications/
talonbox exec {quoted_name} -- hdiutil detach /Volumes/Talon
```

Start Talon through talonbox:

```bash
talonbox restart-talon {quoted_name}
```

If Talon is blocked on first-run UI before its REPL is available, request a VNC framebuffer screenshot explicitly:

```bash
talonbox screenshot --vnc {quoted_name} /tmp/talon-first-run.png
```

When handing this step to the user, run `talonbox status {name}` yourself, then include the actual `vnc://...` URL and the command `talonbox open {name}` in the same message.

The human user must review and accept the Talon EULA in the GUI manually. Agents must never try to accept the Talon EULA. Do not use `talonbox click --vnc`, `talonbox type --vnc`, AppleScript, keyboard automation, or any other automation to accept the Talon EULA.

After accepting the EULA, the user must choose a speech model manually from the Talon menu. If no speech model is installed and selected, the VM may look ready but `talonbox smoke-test` can fail when it tries to dispatch a spoken command.

Give the user this first-run checklist:

1. Accept the Talon EULA.
2. Open the Talon menu.
3. Install and select a speech model.
4. Leave microphone setup alone unless they specifically want to configure it.
5. Tell the agent when Talon is running at the desktop.

This is also the time for the user to install any other apps they expect to test Talon with.

## 6. Run the setup smoke test and grant permissions

Before the final restart, run the smoke test directly against this setup VM. This intentionally avoids a clone so the test can trigger any remaining Talon or macOS permission prompts in the VM you are preparing.

```bash
talonbox smoke-test --in-place {quoted_name}
```

If the smoke test fails or appears blocked on a GUI prompt, take a screenshot through VNC to confirm whether it is a permission prompt or something else. Permissions dialogs can be accepted using `talonbox click --vnc` and `talonbox type --vnc`, but some prompts may be more easily handled by a human over VNC.

After any permission click or typed confirmation, verify that the visible state actually changed. A successful input command only means the input was sent; macOS may show a second dialog, ignore the click because the sheet was not focused, or require keyboard confirmation. If an `Allow` button is focused but a click does not dismiss it, pressing Return through VNC can be a useful fallback:

```bash
talonbox screenshot --vnc {quoted_name} /tmp/permission-after-vnc.png
talonbox screenshot {quoted_name} /tmp/permission-after-talon.png
talonbox press --vnc {quoted_name} enter
```

Repeat the permission or prompt handling until `talonbox smoke-test --in-place {quoted_name}` passes. When in doubt, run `talonbox status {name}` yourself, then hand the step to the user with the actual VNC URL and viewer command:

```bash
talonbox status {quoted_name}
```

Tell the user:

```text
VNC URL: vnc://...
Open it with: talonbox open {quoted_name}
```

It is expected that the user will need to grant permissions for:

- Accessibility
- Camera
- Microphone
- Screen & System Audio Recording, or any screen access prompt macOS shows

After the in-place smoke test passes, take one more VNC screenshot before the reboot. If a permission prompt is still visible, clear it, verify it disappeared, and rerun the in-place smoke test before continuing:

```bash
talonbox screenshot --vnc {quoted_name} /tmp/before-reboot-vnc.png
```

## 7. Reboot and stop the VM

When the setup smoke test passes, the agent should ask the user to quit all apps and restart the VM from the macOS GUI. The user should uncheck the box to reopen windows after logging back in.

After the user reports that the VM has restarted and returned to the desktop, the agent should shut it down so it is a clean clone source:

```bash
talonbox stop --shutdown {quoted_name}
```

## 8. Smoke test the finished VM

Confirm the stopped golden VM through a temporary clone:

```bash
talonbox smoke-test {quoted_name}
```
"""


@click.group(
    name="talonbox",
    cls=TalonboxGroup,
    context_settings={"max_content_width": 100},
    help=(
        "Minimal Talon VM control primitives for coding agents.\n\n"
        "Use named macOS VMs as disposable Talon sandboxes: clone a source VM for an "
        "experiment, start it, copy scripts with `rsync` or `scp`, then drive Talon with "
        "`repl`, `mimic`, `click`, `type`, and `screenshot`.\n\n"
        "VM paths use `NAME:/absolute/path`, and "
        "`smoke-test` runs diagnostics in a temporary clone so the source VM stays clean.\n\n"
        "Some Talon-backed commands also accept `--vnc`; prefer it during setup "
        "before Talon is fully installed. "
        "Never use automation to accept the Talon EULA."
    ),
    epilog=_examples_epilog(
        "talonbox clone golden experiment",
        "talonbox start experiment",
        "talonbox rsync -a ~/.talon/user/ experiment:/Users/admin/.talon/user/",
        "talonbox exec experiment -- whoami",
        "talonbox mimic experiment 'focus chrome'",
        "talonbox click experiment 400 300",
        "talonbox type experiment 'hello from Talon'",
        "talonbox screenshot experiment /tmp/talon.png",
        "talonbox smoke-test golden",
        "talonbox open experiment",
    ),
)
@click.option(
    "--debug",
    is_flag=True,
    envvar="TALONBOX_DEBUG",
    help="Print invoked commands and failure details to stderr. Can also be enabled with TALONBOX_DEBUG=1.",
)
@click.version_option(prog_name="talonbox")
@click.pass_context
def cli(click_ctx: click.Context, debug: bool) -> None:
    _require_macos()
    click_ctx.call_on_close(shutdown_vnc_reactor)
    click_ctx.obj = CliSettings(debug=debug)


@cli.command(
    short_help="Print instructions for creating a Talon VM.",
    help=(
        "Print Markdown instructions for creating and provisioning a Talon VM.\n\n"
        "This command does not create, clone, start, or modify any VM. It fills in the "
        "VM name and Talon DMG source so a human or coding agent can work through the "
        "manual setup steps."
    ),
    epilog=_examples_epilog(
        "talonbox create golden",
        "talonbox create --base tahoe-base golden",
        "talonbox create --talon-dmg ~/Downloads/talon-beta.dmg golden-beta",
    ),
)
@click.option(
    "--base",
    metavar="NAME",
    default=DEFAULT_BASE_VM_NAME,
    show_default=True,
    help=(
        "Unprefixed talonbox base VM name to create or reuse. Tart commands use "
        "the corresponding talonbox-prefixed VM name."
    ),
)
@click.option(
    "--talon-dmg",
    "talon_dmg",
    metavar="PATH_OR_URL",
    help="Talon DMG path or URL to install. Defaults to the latest public Talon DMG.",
)
@click.argument("name", metavar="NAME")
def create(base: str, talon_dmg: str | None, name: str) -> None:
    click.echo(
        _render_create_markdown(
            name,
            talon_dmg or DEFAULT_TALON_DMG_URL,
            base,
        )
    )


@cli.command(
    short_help="Clone one VM to another.",
    help=(
        "Clone SOURCE to DEST.\n\n"
        "talonbox delegates to `tart clone`, which uses APFS copy-on-write cloning on "
        "macOS for low-overhead VM copies. The source VM must be shut down, and the "
        "destination VM must not already exist."
    ),
    epilog=_examples_epilog("talonbox clone golden experiment"),
)
@click.argument("source", metavar="SOURCE")
@click.argument("dest", metavar="DEST")
@pass_settings
def clone(settings: CliSettings, source: str, dest: str) -> None:
    VmController(source, settings.debug).clone(dest)
    _echo_success("Clone")


@cli.command(
    short_help="Rename a stopped VM.",
    help=(
        "Rename SOURCE to DEST.\n\n"
        "The source VM must be stopped, and the destination VM must not already exist."
    ),
    epilog=_examples_epilog("talonbox rename experiment experiment-old"),
)
@click.argument("source", metavar="SOURCE")
@click.argument("dest", metavar="DEST")
@pass_settings
def rename(settings: CliSettings, source: str, dest: str) -> None:
    VmController(source, settings.debug).rename(dest)
    _echo_success("Rename")


@cli.command(
    short_help="Delete a stopped VM.",
    help="Delete a stopped VM. Running VMs must be stopped first.",
    epilog=_examples_epilog("talonbox delete experiment"),
)
@click.argument("name", metavar="NAME")
@pass_settings
def delete(settings: CliSettings, name: str) -> None:
    VmController(name, settings.debug).delete()
    _echo_success("Delete")


@cli.command(name="list", short_help="List talonbox VMs.")
@pass_settings
def list_command(settings: CliSettings) -> None:
    click.echo("name\tstatus\tip\tvnc")
    for info in VmController.list_vms(debug=settings.debug):
        click.echo(
            "\t".join(
                [
                    info.name,
                    info.status,
                    info.ip_address or "-",
                    info.vnc_url or "-",
                ]
            )
        )


@cli.command(
    short_help="Print VM status and connection details.",
    help=(
        "Print whether the VM is running. When it is running, also print IP, SSH "
        "credentials, and the VNC link. This command is read-only."
    ),
    epilog=_examples_epilog("talonbox status experiment"),
)
@click.argument("name", metavar="NAME")
@pass_settings
def status(settings: CliSettings, name: str) -> None:
    vm_controller = VmController(name, settings.debug)
    _echo_vm_info(vm_controller, vm_controller.get_vm())


@cli.command(
    name="open",
    short_help="Open the VM's VNC session.",
    help=(
        "Open the VM's VNC session in the macOS Screen Sharing app.\n\n"
        "The VM must be running with a VNC URL from `talonbox start`. This is useful "
        "when Talon or macOS is waiting on GUI prompts, permissions, or first-run setup."
    ),
    epilog=_examples_epilog("talonbox open experiment"),
)
@click.argument("name", metavar="NAME")
@pass_settings
def open_command(settings: CliSettings, name: str) -> None:
    vm_controller = VmController(name, settings.debug)
    info = vm_controller.get_vm()
    if info.status != "running" or not info.vnc_url:
        raise click.ClickException(
            f"VM has no openable VNC URL: {name}. Start it with `talonbox start {name}`."
        )
    click.echo(info.vnc_url)
    result = subprocess.run(["open", info.vnc_url], check=False)
    if result.returncode:
        raise click.exceptions.Exit(result.returncode)


@cli.command(
    short_help="Start or resume an existing VM.",
    help=(
        "Start or resume an existing VM in the background, wait for SSH, and start "
        "Talon if it is not already running.\n\n"
        "This command never clones, deletes, or wipes the VM. Use `--no-talon` while "
        "creating or repairing a VM before Talon is installed or accepted."
    ),
    epilog=_examples_epilog(
        "talonbox start experiment",
        "talonbox start --no-talon experiment",
    ),
)
@click.argument("name", metavar="NAME")
@click.option(
    "--no-talon",
    is_flag=True,
    help="Start the VM and wait for SSH, but do not launch Talon or wait for its REPL.",
)
@pass_settings
def start(settings: CliSettings, name: str, no_talon: bool) -> None:
    vm_controller = VmController(name, settings.debug)
    result = vm_controller.start(require_talon=not no_talon)
    click.echo(f"start: {result.action}")
    _echo_vm_info(vm_controller, result.to_vm_info())


@cli.command(
    short_help="Restart Talon inside a running VM and reset Talon logs.",
    help=(
        "Restart Talon inside the running VM without rebooting the VM.\n\n"
        "This truncates `~/.talon/talon.log` and `/tmp/talonbox-talon.log`, then relaunches "
        "Talon in the logged-in GUI session."
    ),
    epilog=_examples_epilog("talonbox restart-talon experiment"),
)
@click.argument("name", metavar="NAME")
@pass_settings
def restart_talon(settings: CliSettings, name: str) -> None:
    VmController(name, settings.debug).restart_talon()
    _echo_success("Restart Talon")


@cli.command(
    short_help="Stop the VM if it is running.",
    help=(
        "Suspend the VM if it is running. Safe to run repeatedly.\n\n"
        "Use `--shutdown` to shut down the guest instead of suspending it."
    ),
    epilog=_examples_epilog(
        "talonbox stop experiment", "talonbox stop --shutdown experiment"
    ),
)
@click.option(
    "--shutdown",
    is_flag=True,
    help="Shut down the VM instead of suspending it.",
)
@click.argument("name", metavar="NAME")
@pass_settings
def stop(settings: CliSettings, shutdown: bool, name: str) -> None:
    VmController(name, settings.debug).stop(shutdown=shutdown)
    _echo_success("Stop")


@cli.command(
    name="smoke-test",
    short_help="Run a Talon VM diagnostic.",
    help=(
        "Run a mutating end-to-end sanity check against a temporary clone of SOURCE.\n\n"
        "The source VM must be shut down. smoke-test clones it, starts the clone, uploads a "
        "temporary Talon command bundle, runs mimic(), verifies a guest-side marker file, "
        "captures screenshots, and then stops and deletes the clone.\n\n"
        "Use `--in-place` only while creating or repairing a VM. It runs the same "
        "diagnostic directly against SOURCE, leaves the VM running for GUI prompts, and "
        "does not clone, stop, or delete the VM.\n\n"
        "Artifacts are kept under `/tmp` for debugging."
    ),
    epilog=_examples_epilog(
        "talonbox smoke-test golden",
        "talonbox smoke-test --in-place golden",
    ),
)
@click.option(
    "--in-place",
    is_flag=True,
    help="Run directly against SOURCE for setup/repair instead of using a temporary clone.",
)
@click.argument("source", metavar="SOURCE")
@pass_settings
def smoke_test(settings: CliSettings, in_place: bool, source: str) -> None:
    _build_smoke_test_runner(settings, source).run(clone=not in_place)


@cli.command(
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
    short_help="Run a command on the VM via SSH.",
    help=(
        "Run a command on the VM over SSH.\n\n"
        "Place `--` before the remote command so talonbox stops parsing options.\n\n"
        "For shell pipelines, redirects, or multiline scripts, pass one quoted shell "
        "string. Literal multiline quoted strings are often easiest to read."
    ),
    epilog=_examples_epilog(
        "talonbox exec experiment -- whoami",
        "talonbox exec experiment -- test -d ~/.talon/user",
        "talonbox exec experiment -- 'whoami\npwd'",
    ),
)
@click.argument("name", metavar="NAME")
@click.argument("command", nargs=-1, type=click.UNPROCESSED, metavar="COMMAND...")
@pass_settings
def exec_command(settings: CliSettings, name: str, command: tuple[str, ...]) -> None:
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise click.ClickException("No command provided")
    result = (
        VmController(name, settings.debug)
        .get_running_vm()
        .run_shell(
            command[0] if len(command) == 1 else list(command),
            stream=True,
            check=False,
        )
    )
    if result.returncode:
        raise click.exceptions.Exit(result.returncode)
    _echo_success("Exec")


@cli.command(
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
    short_help="Copy files between host and a VM with rsync.",
    help=(
        "Run rsync between the host and one VM.\n\n"
        "Use explicit `NAME:/absolute/path` operands for the VM side. Exactly one side may "
        "be remote, and all remote operands must name the same VM. No other remotes are "
        "permitted.\n\n"
        "Local sources may be read from anywhere, but any host-side output must stay under "
        "`/tmp`. Transfers run inside the macOS sandbox, so extra host-side writes outside "
        "that boundary fail with an obvious permission error."
    ),
    epilog=_examples_epilog(
        "talonbox rsync -a ./repo/ experiment:/Users/admin/.talon/user/repo/",
        "talonbox rsync -a experiment:/Users/admin/Pictures/ /tmp/guest-pictures/",
    ),
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED, metavar="RSYNC_ARGS...")
@pass_settings
def rsync(settings: CliSettings, args: tuple[str, ...]) -> None:
    parsed_args = parse_rsync_args(args)
    running_vm = VmController(parsed_args.vm_name, settings.debug).get_running_vm()
    returncode = TransferService(running_vm).rsync(parsed_args)
    if returncode:
        raise click.exceptions.Exit(returncode)
    _echo_success("Rsync")


@cli.command(
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
    short_help="Copy files between host and a VM with scp.",
    help=(
        "Run scp between the host and one VM.\n\n"
        "Use explicit `NAME:/absolute/path` operands for the VM side. Exactly one side may "
        "be remote, and all remote operands must name the same VM. No other remotes are "
        "permitted.\n\n"
        "Local sources may be read from anywhere, but any host-side output must stay under "
        "`/tmp`. Transfers run inside the macOS sandbox, so extra host-side writes outside "
        "that boundary fail with an obvious permission error."
    ),
    epilog=_examples_epilog(
        "talonbox scp -q ./settings.talon experiment:/Users/admin/.talon/user/settings.talon",
        "talonbox scp -q experiment:/tmp/out.png /tmp/out.png",
    ),
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED, metavar="SCP_ARGS...")
@pass_settings
def scp(settings: CliSettings, args: tuple[str, ...]) -> None:
    parsed_args = parse_scp_args(args)
    running_vm = VmController(parsed_args.vm_name, settings.debug).get_running_vm()
    returncode = TransferService(running_vm).scp(parsed_args)
    if returncode:
        raise click.exceptions.Exit(returncode)
    _echo_success("Scp")


@cli.command(
    short_help="Send Python to the VM's Talon REPL.",
    help=(
        "Send Python to the VM's Talon REPL.\n\n"
        "Pass CODE as a quoted argument. Literal multiline quoted strings are often "
        "easiest to read. Piping Python on stdin is also supported for generated input. "
        "This command is intentionally non-interactive."
    ),
    epilog=_examples_epilog(
        "talonbox repl experiment 'print(1+1)'",
        "talonbox repl experiment 'if True:\n    from talon import ui\n    print(ui.active_app())'",
    ),
)
@click.argument("name", metavar="NAME")
@click.argument("code", required=False, metavar="[CODE]")
@pass_settings
def repl(settings: CliSettings, name: str, code: str | None) -> None:
    if code is None:
        if sys.stdin.isatty():
            raise click.ClickException(
                "No code provided. Pass CODE or pipe Python into stdin."
            )
        code = sys.stdin.read()
    assert code is not None
    _build_talon_client(settings, name).repl(code)
    _echo_success("Repl")


@cli.command(
    short_help="Run a voice command through Talon's mimic().",
    context_settings={"max_content_width": 100},
    help=(
        "Send one phrase to the VM's Talon REPL as `mimic(<phrase>)`.\n\n"
        "Use `--audio` to synthesize the phrase inside the VM and replay the resulting "
        "WAV through Talon's speech engine. Raw Apple embedded speech commands such as "
        "`[[slnc 500]]` are passed through in audio mode and ignored otherwise.\n\n"
        "\b\n"
        "To learn more about commands available to the audio API, see Apple's archived "
        "Speech Manager reference:\n"
        f"{APPLE_SPEECH_MANAGER_REFERENCE_URL}"
    ),
    epilog=_examples_epilog(
        "talonbox mimic experiment 'focus chrome'",
        "talonbox mimic --audio experiment 'talonbox [[slnc 500]] smoke test'",
        "talonbox mimic experiment 'tab close'",
    ),
)
@click.option(
    "--audio",
    "audio",
    is_flag=True,
    help="Synthesize audio and replay it through Talon's speech engine.",
)
@click.argument("name", metavar="NAME")
@click.argument("command", metavar="PHRASE")
@pass_settings
def mimic(settings: CliSettings, audio: bool, name: str, command: str) -> None:
    _build_talon_client(settings, name).mimic(command, audio=audio)
    _echo_success("Mimic")


@cli.command(
    name="click",
    short_help="Click inside the VM.",
    help=(
        "Move the pointer to X,Y and click inside the VM.\n\n"
        "By default this uses Talon's mouse APIs and requires Talon's REPL. Use `--vnc` "
        "to click through the VM's VNC connection instead.\n\n"
        "Coordinates match the chosen backend. Without `--vnc`, use coordinates from "
        "Talon screenshots. With `--vnc`, use coordinates from `talonbox screenshot "
        "--vnc`; VNC screenshots may be a different pixel size."
    ),
    epilog=_examples_epilog(
        "talonbox click experiment 400 300",
        "talonbox click --button right experiment 400 300",
        "talonbox click --vnc experiment 400 300",
    ),
)
@click.option(
    "--vnc",
    "vnc",
    is_flag=True,
    help="Use the VM's VNC connection instead of Talon's mouse APIs.",
)
@click.option(
    "--button",
    type=click.Choice(["left", "middle", "right"]),
    default="left",
    show_default=True,
    help="Mouse button to click.",
)
@click.argument("name", metavar="NAME")
@click.argument("x", type=click.IntRange(min=0), metavar="X")
@click.argument("y", type=click.IntRange(min=0), metavar="Y")
@pass_settings
def click_command(
    settings: CliSettings, vnc: bool, button: str, name: str, x: int, y: int
) -> None:
    _build_talon_client(settings, name).click(
        x,
        y,
        button=button,
        vnc=vnc,
    )
    _echo_success("Click")


@cli.command(
    name="type",
    short_help="Type text inside the VM.",
    help=(
        "Type TEXT into the focused field inside the VM.\n\n"
        "By default this uses Talon's text insertion API and requires Talon's REPL. Use "
        "`--vnc` to send key presses through the VM's VNC connection instead."
    ),
    epilog=_examples_epilog(
        "talonbox type experiment 'hello from Talon'",
        "talonbox type --vnc experiment 'hello before Talon is ready'",
    ),
)
@click.option(
    "--vnc",
    "vnc",
    is_flag=True,
    help="Use the VM's VNC connection instead of Talon's text insertion API.",
)
@click.argument("name", metavar="NAME")
@click.argument("text", metavar="TEXT")
@pass_settings
def type_command(settings: CliSettings, vnc: bool, name: str, text: str) -> None:
    _build_talon_client(settings, name).type_text(text, vnc=vnc)
    _echo_success("Type")


@cli.command(
    name="press",
    short_help="Press one key inside the VM.",
    help=(
        "Press KEY inside the VM.\n\n"
        "By default this uses Talon's key action and requires Talon's REPL. Use "
        "`--vnc` to send the key through the VM's VNC connection instead."
    ),
    epilog=_examples_epilog(
        "talonbox press experiment enter",
        "talonbox press --vnc experiment enter",
        "talonbox press --vnc experiment space",
    ),
)
@click.option(
    "--vnc",
    "vnc",
    is_flag=True,
    help="Use the VM's VNC connection instead of Talon's key action.",
)
@click.argument("name", metavar="NAME")
@click.argument(
    "key",
    metavar="KEY",
    type=click.Choice(["enter", "space", "tab", "escape"]),
)
@pass_settings
def press_command(settings: CliSettings, vnc: bool, name: str, key: str) -> None:
    _build_talon_client(settings, name).press_key(key, vnc=vnc)
    _echo_success("Press")


@cli.command(
    short_help="Capture a VM screenshot and download it locally.",
    help=(
        "Capture a VM screenshot, save it to a guest temp file, download it to a host "
        "path under `/tmp`, and remove the guest temp file.\n\n"
        "By default this uses Talon's screen capture API and requires Talon's REPL. Use "
        "`--vnc` to capture the VNC framebuffer instead.\n\n"
        "The two screenshot modes may produce different pixel sizes. Use coordinates "
        "from a Talon screenshot with `talonbox click`; use coordinates from a VNC "
        "screenshot with `talonbox click --vnc`."
    ),
    epilog=_examples_epilog(
        "talonbox screenshot experiment /tmp/talon.png",
        "talonbox screenshot --vnc experiment /tmp/talon-first-run.png",
    ),
)
@click.option(
    "--vnc",
    "vnc",
    is_flag=True,
    help="Use the VM's VNC framebuffer instead of Talon's screen capture API.",
)
@click.argument("name", metavar="NAME")
@click.argument(
    "filepath", metavar="HOST_PATH", type=click.Path(dir_okay=False, path_type=Path)
)
@pass_settings
def screenshot(settings: CliSettings, vnc: bool, name: str, filepath: Path) -> None:
    _build_talon_client(settings, name).capture_screenshot(
        filepath,
        vnc=vnc,
    )
    _echo_success("Screenshot")


def main() -> int:
    cli.main(standalone_mode=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
