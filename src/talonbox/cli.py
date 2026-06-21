from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import click

from .names import to_lume_vm_name
from .smoke_test import SmokeTestRunner
from .talon_client import TalonClient
from .transfer import TransferService
from .vm import VmController

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
    quoted_lume_name = shlex.quote(to_lume_vm_name(name))
    quoted_lume_base = shlex.quote(to_lume_vm_name(base))
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

## 1. Install Lume

Ensure `lume` is installed. Follow the Lume installation guide as needed:

https://cua.ai/docs/lume/guide/getting-started/installation

Verify the install:

```bash
lume --version
```

Lume's unattended setup flow also requires `sshpass`. Install it with Homebrew if needed:

```bash
brew install sshpass
```

## 2. Create or reuse the macOS base VM

Follow the Lume quickstart as needed:

https://cua.ai/docs/lume/guide/getting-started/quickstart

A 100 GB disk is recommended so the VM has enough room for macOS upgrades.

The base VM name is `{base}` in talonbox commands and `{to_lume_vm_name(base)}` in Lume commands. Pass the unprefixed name to `talonbox create --base`; talonbox's `talonbox-` prefix is applied automatically when rendering Lume commands.

First check whether the base VM already exists:

```bash
lume get {quoted_lume_base}
```

If it already exists, reuse it. Do not create, overwrite, or delete an existing base VM during setup unless requested by the user. If the existing base VM is already verified, skip to cloning below. If you are not sure whether it is complete, skip creation and continue at the verification steps below.

If the base VM does not exist, create it:

```bash
lume create {quoted_lume_base} --os macos --ipsw latest --disk-size 100GB
```

This can take a long time to download, depending on the user's internet connection.

If VM creation succeeds but a later setup step fails, avoid downloading the IPSW again when possible. Re-run `lume create` with the cached IPSW path from Lume's output or temp directory in place of `latest`.

## 3. Run macOS setup

For a newly created base VM, run Lume's maintained setup preset as a separate step. `--no-display` keeps host mouse input from interfering with the automation, and `--debug` leaves screenshots and OCR output behind if the preset fails.

```bash
lume setup {quoted_lume_base} --unattended tahoe --debug --no-display
```

If setup fails, have the agent inspect the debug directory named in Lume's output. The most useful files are usually the `FAILED` screenshot and its `-ocr.json` companion. These artifacts are for diagnosis only: when the agent is blocked on a macOS GUI setup screen, it should open the VM over VNC and ask the user to complete the visible setup step.

To understand what the maintained preset was trying to do, have the agent inspect the installed Lume setup docs and preset:

```bash
lume get {quoted_lume_base}
lume setup --help
lume dump-docs
```

`lume setup --help` shows the built-in preset names, but may not print the preset file path. If the agent needs the YAML itself, search near the installed `lume` binary and common package locations:

```bash
LUME_BIN="$(command -v lume)"
find "$(dirname "$(realpath "$LUME_BIN")")/.." /Applications /opt/homebrew /usr/local -path '*/unattended-presets/tahoe.yml' -print 2>/dev/null
```

The Lume VM user is `lume`, and the default Lume password is `lume`.

Do not patch or edit Lume's unattended setup YAML during talonbox setup. If the maintained preset is stale for the current macOS Setup Assistant, use the YAML contents only to determine which setup steps remain. In particular, look for similar labels such as "Set Up Later", "Other Sign-In Options", or "Skip" when Apple Account setup appears. You may use `talonbox screenshot --vnc`, `talonbox click --vnc`, and `talonbox type --vnc` to complete Setup Assistant.

Ask for the user's help before going in circles trying to resolve issues. If the VM is stopped, start it before opening VNC. Open the VM with the Mac Screen Sharing app, ask the user to finish Setup Assistant manually, and wait for the user to reply before continuing:

```bash
lume run {quoted_lume_base}
lume get {quoted_lume_base}
open VNC_URL_FROM_LUME_GET
```

Useful user-facing checklist for manual Setup Assistant recovery:

- Choose English and United States.
- Set up as a new Mac.
- Create the local user `lume` with password `lume`.
- Skip Apple Account sign-in.
- Accept the macOS terms.
- Decline optional setup such as Location Services, Screen Time, Analytics, Siri, and FileVault.
- Reach the desktop and enable Remote Login for the `lume` user in System Settings > General > Sharing.

## 4. Finalize and verify the base VM

After SSH is available, the agent may apply any SSH-only `post_ssh_commands` from the installed Lume preset that did not run because setup was completed manually. Those commands commonly configure auto-login, `/etc/kcpassword`, screen saver and sleep settings, and auto-logout. Read them from the installed `tahoe.yml` before running them. This SSH configuration work can be handled by the agent; GUI Setup Assistant decisions should stay with the user over VNC.

Before cloning the base VM, verify it behaves like a completed unattended setup:

```bash
lume ssh {quoted_lume_base} 'whoami'
lume ssh {quoted_lume_base} 'sysadminctl -autologin status'
lume ssh {quoted_lume_base} 'test -f /etc/kcpassword'
lume ssh {quoted_lume_base} 'open -a Terminal'
```

Then restart the base VM once and confirm it returns to a logged-in desktop.

## 5. Clone and start `{name}`

After the base VM is complete, stop it and create the Talon VM from it. Use the rendered `talonbox-` prefixed names when working directly with `lume`; omit the prefix when running `talonbox` commands.

```bash
lume stop {quoted_lume_base}
lume clone {quoted_lume_base} {quoted_lume_name}
talonbox start --no-talon {quoted_name}
```

`--no-talon` starts the VM and waits for SSH without trying to launch Talon or wait for Talon's REPL. Use it while creating or repairing a VM before Talon is fully installed and accepted.

## 6. Install Talon

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

Open the VM with the Mac Screen Sharing app:

```bash
talonbox status {quoted_name}
talonbox open {quoted_name}
```

The human user must review and accept the Talon EULA in the GUI manually. Agents must never try to accept the Talon EULA. Do not use `talonbox click --vnc`, `talonbox type --vnc`, AppleScript, keyboard automation, or any other automation to accept the Talon EULA.

The user should also choose a speech model through the Talon menu. A microphone does not need to be configured. This is also the time for the user to install any other apps they expect to test Talon with.

## 7. Run the setup smoke test and grant permissions

Before the final restart, run the smoke test directly against this setup VM. This intentionally avoids a clone so the test can trigger any remaining Talon or macOS permission prompts in the VM you are preparing.

```bash
talonbox smoke-test --in-place {quoted_name}
```

If the smoke test fails or appears blocked on a GUI prompt, take a screenshot through VNC to confirm whether it is a permission prompt or something else. Permissions dialogs can be accepted using `talonbox click --vnc` and `talonbox type --vnc`, but some prompts may be more easily handled by a human over VNC.

Repeat the permission or prompt handling until `talonbox smoke-test --in-place {quoted_name}` passes. When in doubt, help the user connect to VNC:

```bash
talonbox open {quoted_name}
```

It is expected that the user will need to grant permissions for:

- Accessibility
- Camera
- Microphone
- Screen & System Audio Recording, or any screen access prompt macOS shows

## 8. Reboot and stop the VM

When the setup smoke test passes, the agent should ask the user to quit all apps and restart the VM from the macOS GUI. The user should uncheck the box to reopen windows after logging back in.

After the user reports that the VM has restarted and returned to the desktop, the agent should stop it:

```bash
talonbox stop {quoted_name}
```

## 9. Smoke test the finished VM

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
        "talonbox rsync -a ~/.talon/user/ experiment:/Users/lume/.talon/user/",
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
        "Unprefixed talonbox base VM name to create or reuse. Lume commands use "
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
        "talonbox delegates to `lume clone`, which uses APFS copy-on-write cloning on "
        "macOS for low-overhead VM copies. The source VM must be stopped, and the "
        "destination VM must not already exist."
    ),
    epilog=_examples_epilog("talonbox clone golden experiment"),
)
@click.argument("source", metavar="SOURCE")
@click.argument("dest", metavar="DEST")
@pass_settings
def clone(settings: CliSettings, source: str, dest: str) -> None:
    VmController(source, settings.debug).clone(dest)


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


@cli.command(
    short_help="Delete a stopped VM.",
    help="Delete a stopped VM. Running VMs must be stopped first.",
    epilog=_examples_epilog("talonbox delete experiment"),
)
@click.argument("name", metavar="NAME")
@pass_settings
def delete(settings: CliSettings, name: str) -> None:
    VmController(name, settings.debug).delete()


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
        "The VM must be running and Lume must report a VNC URL. This is useful when "
        "Talon or macOS is waiting on GUI prompts, permissions, or first-run setup."
    ),
    epilog=_examples_epilog("talonbox open experiment"),
)
@click.argument("name", metavar="NAME")
@pass_settings
def open_command(settings: CliSettings, name: str) -> None:
    vm_controller = VmController(name, settings.debug)
    info = vm_controller.get_vm()
    if info.status != "running" or not info.vnc_url:
        raise click.ClickException(f"VM has no openable VNC URL: {name}")
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
    _echo_vm_info(
        vm_controller,
        vm_controller.start(require_talon=not no_talon).to_vm_info(),
    )


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
    VmController(name, settings.debug).restart_talon(
        wipe_user_dir=False,
        clean_logs=True,
    )


@cli.command(
    short_help="Stop the VM if it is running.",
    help="Stop the VM if it is running. Safe to run repeatedly.",
    epilog=_examples_epilog("talonbox stop experiment"),
)
@click.argument("name", metavar="NAME")
@pass_settings
def stop(settings: CliSettings, name: str) -> None:
    VmController(name, settings.debug).stop()


@cli.command(
    name="smoke-test",
    short_help="Run a Talon VM diagnostic.",
    help=(
        "Run a mutating end-to-end sanity check against a temporary clone of SOURCE.\n\n"
        "The source VM must be stopped. smoke-test clones it, starts the clone, uploads a "
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
        "For shell pipelines or redirects, pass a single quoted shell string."
    ),
    epilog=_examples_epilog(
        "talonbox exec experiment -- whoami",
        "talonbox exec experiment -- test -d ~/.talon/user",
        "talonbox exec experiment -- pgrep -x Talon",
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
        "talonbox rsync -a ./repo/ experiment:/Users/lume/.talon/user/repo/",
        "talonbox rsync -a experiment:/Users/lume/Pictures/ /tmp/guest-pictures/",
    ),
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED, metavar="RSYNC_ARGS...")
@pass_settings
def rsync(settings: CliSettings, args: tuple[str, ...]) -> None:
    vm_name = TransferService.extract_rsync_vm_name(args)
    running_vm = VmController(vm_name, settings.debug).get_running_vm()
    returncode = TransferService(running_vm).rsync(args)
    if returncode:
        raise click.exceptions.Exit(returncode)


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
        "talonbox scp -q ./settings.talon experiment:/Users/lume/.talon/user/settings.talon",
        "talonbox scp -q experiment:/tmp/out.png /tmp/out.png",
    ),
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED, metavar="SCP_ARGS...")
@pass_settings
def scp(settings: CliSettings, args: tuple[str, ...]) -> None:
    vm_name = TransferService.extract_scp_vm_name(args)
    running_vm = VmController(vm_name, settings.debug).get_running_vm()
    returncode = TransferService(running_vm).scp(args)
    if returncode:
        raise click.exceptions.Exit(returncode)


@cli.command(
    short_help="Pipe Python into the VM's Talon REPL.",
    help=(
        "Send Python to the VM's Talon REPL.\n\n"
        "Provide CODE as an argument or pipe Python on stdin. This command is intentionally "
        "non-interactive."
    ),
    epilog=_examples_epilog(
        "talonbox repl experiment 'print(1+1)'",
        "printf 'print(1+1)\\n' | talonbox repl experiment",
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


@cli.command(
    short_help="Run a voice command through Talon's mimic().",
    help="Send one phrase to the VM's Talon REPL as `mimic(<phrase>)`.",
    epilog=_examples_epilog(
        "talonbox mimic experiment 'focus chrome'",
        "talonbox mimic experiment 'tab close'",
    ),
)
@click.argument("name", metavar="NAME")
@click.argument("command", metavar="PHRASE")
@pass_settings
def mimic(settings: CliSettings, name: str, command: str) -> None:
    _build_talon_client(settings, name).mimic(command)


@cli.command(
    name="click",
    short_help="Click inside the VM.",
    help=(
        "Move the pointer to X,Y and click inside the VM.\n\n"
        "By default this uses Talon's mouse APIs and requires Talon's REPL. Use `--vnc` "
        "to click through the VM's VNC connection instead."
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


@cli.command(
    short_help="Capture a VM screenshot and download it locally.",
    help=(
        "Capture a VM screenshot, save it to a guest temp file, download it to a host "
        "path under `/tmp`, and remove the guest temp file.\n\n"
        "By default this uses Talon's screen capture API and requires Talon's REPL. Use "
        "`--vnc` to capture the VNC framebuffer instead."
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


def main() -> int:
    cli.main(standalone_mode=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
