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
    ("Talon RPC", ("restart-talon", "repl", "mimic", "screenshot")),
)

DEFAULT_TALON_DMG_URL = "https://talonvoice.com/dl/latest/talon-mac.dmg"


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


def _render_create_markdown(name: str, talon_dmg: str) -> str:
    quoted_name = shlex.quote(name)
    quoted_lume_name = shlex.quote(to_lume_vm_name(name))
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

This command prints setup instructions only. Creating a Talon-ready macOS VM requires human decisions, GUI permission prompts, and manual EULA acceptance.

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

## 2. Create a fresh macOS base VM

Follow the Lume quickstart as needed:

https://cua.ai/docs/lume/guide/getting-started/quickstart

A 100 GB disk is recommended so the VM has enough room for macOS upgrades.

This example keeps `tahoe-base` as a clean Lume base VM outside talonbox, which makes it quicker to create additional talonbox VMs later.

```bash
lume create tahoe-base --os macos --ipsw latest --disk-size 100GB
```

This usually takes about 20 minutes.

If VM creation succeeds but a later setup step fails, avoid downloading the IPSW again when possible. Re-run `lume create` with the cached IPSW path from Lume's output or temp directory in place of `latest`.

## 3. Run macOS setup

Run Lume's maintained setup preset as a separate step. `--no-display` keeps host mouse input from interfering with the automation, and `--debug` leaves screenshots and OCR output behind if the preset fails.

```bash
lume setup tahoe-base --unattended tahoe --debug --no-display
```

If setup fails, have the agent inspect the debug directory named in Lume's output. The most useful files are usually the `FAILED` screenshot and its `-ocr.json` companion. To understand what the maintained preset was trying to do, have the agent inspect the installed Lume setup docs and preset instead of maintaining a talonbox-specific setup script:

```bash
lume setup --help
lume dump-docs
brew list lume 2>/dev/null | rg 'unattended-presets|tahoe.yml'
```

If the maintained preset is stale for the current macOS Setup Assistant, finish Setup Assistant manually over VNC. Before cloning the base VM, make sure Remote Login is enabled for the `lume` user, the `lume` password is still `lume`, and the VM logs into the desktop automatically. `talonbox restart-talon` launches Talon through Terminal, so a logged-in GUI session is required.

## 4. Clone and start `{name}`

After the base VM is complete, stop it and create the Talon VM from it. Use the `talonbox-` prefix for the clone name when working directly with `lume`; omit it when running `talonbox` commands.

```bash
lume stop tahoe-base
lume clone tahoe-base {quoted_lume_name}
talonbox start --no-talon {quoted_name}
```

`--no-talon` starts the VM and waits for SSH without trying to launch Talon or wait for Talon's REPL. Use it while creating or repairing a VM before Talon is fully installed and accepted.

## 5. Install Talon

Use `talonbox exec` for guest commands and `talonbox scp` for file copies.

Verify SSH access:

```bash
talonbox exec {quoted_name} -- whoami
```

Install Rosetta:

```bash
talonbox exec {quoted_name} -- softwareupdate --install-rosetta --agree-to-license
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

If Talon is blocked on first-run UI before its REPL is available, request a plain macOS screenshot explicitly:

```bash
talonbox screenshot --screencapture {quoted_name} /tmp/talon-first-run.png
```

Open the VM with the Mac Screen Sharing app:

```bash
talonbox status {quoted_name}
talonbox open {quoted_name}
```

Accept the Talon EULA yourself in the GUI. Agents must never try to accept the Talon EULA for you.

## 6. Grant permissions and choose a speech model

Open System Settings in the VM and go to Privacy & Security.

Because talonbox launches Talon through Terminal, grant permissions to both Terminal and Talon wherever macOS offers both:

- Accessibility
- Camera
- Microphone
- Screen & System Audio Recording, or any screen access prompt macOS shows

macOS may require restarting Terminal or Talon after granting some permissions.

From the Talon menu, select the speech model you want to use. You do not need to configure a microphone, though macOS may still ask you to grant microphone permission.

Install any other apps you expect to test Talon with.

## 7. Run the setup smoke test

Before the final restart, run the smoke test directly against this setup VM. This intentionally avoids a clone so the test can trigger any remaining Talon or macOS permission prompts in the VM you are preparing.

```bash
talonbox smoke-test --in-place {quoted_name}
```

If the smoke test fails or appears blocked on a GUI prompt, open VNC and inspect the VM manually:

```bash
talonbox open {quoted_name}
```

Repeat the permission or prompt handling until `talonbox smoke-test --in-place {quoted_name}` passes.

## 8. Reboot and stop the VM

When the setup smoke test passes, quit all apps and restart the VM. When macOS asks, uncheck the box to reopen windows after logging back in.

After the VM has restarted and you are done with setup, stop it:

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
        "`repl`, `mimic`, and `screenshot`. VM paths use `NAME:/absolute/path`, and "
        "`smoke-test` runs diagnostics in a temporary clone so the source VM stays clean."
    ),
    epilog=_examples_epilog(
        "talonbox clone golden experiment",
        "talonbox start experiment",
        "talonbox rsync -a ~/.talon/user/ experiment:/Users/lume/.talon/user/",
        "talonbox exec experiment -- whoami",
        "talonbox mimic experiment 'focus chrome'",
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
        "talonbox create golden-beta ~/Downloads/talon-beta.dmg",
    ),
)
@click.argument("name", metavar="NAME")
@click.argument(
    "talon_dmg",
    required=False,
    metavar="[TALON_DMG]",
    default=DEFAULT_TALON_DMG_URL,
)
def create(name: str, talon_dmg: str) -> None:
    click.echo(_render_create_markdown(name, talon_dmg))


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
        "Talon under Rosetta through Terminal so screen capture permissions still apply."
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
    short_help="Capture a VM screenshot and download it locally.",
    help=(
        "Capture a VM screenshot, save it to a guest temp file, download it to a host "
        "path under `/tmp`, and remove the guest temp file.\n\n"
        "By default this uses Talon's screen capture API and requires Talon's REPL. Use "
        "`--screencapture` before Talon is fully started."
    ),
    epilog=_examples_epilog(
        "talonbox screenshot experiment /tmp/talon.png",
        "talonbox screenshot --screencapture experiment /tmp/talon-first-run.png",
    ),
)
@click.option(
    "--screencapture",
    is_flag=True,
    help="Use macOS screencapture over SSH instead of Talon's screen capture API.",
)
@click.argument("name", metavar="NAME")
@click.argument(
    "filepath", metavar="HOST_PATH", type=click.Path(dir_okay=False, path_type=Path)
)
@pass_settings
def screenshot(
    settings: CliSettings, screencapture: bool, name: str, filepath: Path
) -> None:
    _build_talon_client(settings, name).capture_screenshot(
        filepath,
        screencapture=screencapture,
    )


def main() -> int:
    cli.main(standalone_mode=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
