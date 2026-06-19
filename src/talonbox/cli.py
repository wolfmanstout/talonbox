from __future__ import annotations

import os
import shlex
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

## 2. Create a fresh macOS base VM

Follow the Lume quickstart as needed:

https://cua.ai/docs/lume/guide/getting-started/quickstart

Unattended install is highly recommended. A 100 GB disk is recommended so the VM has enough room for macOS upgrades.

This example keeps `tahoe-base` as a clean Lume base VM outside talonbox, which makes it quicker to create additional talonbox VMs later.

```bash
lume create tahoe-base --os macos --ipsw latest --unattended tahoe --disk-size 100GB
```

This usually takes about 20 minutes.

## 3. Clone and start `{name}`

After the base VM is complete, create the Talon VM from it. Use the `talonbox-` prefix for the clone name when working directly with `lume`; omit it when running `talonbox` commands.

```bash
lume clone tahoe-base {quoted_lume_name}
lume run {quoted_lume_name}
```

## 4. Install Talon

Use `talonbox exec` for guest commands and `talonbox scp` for file copies.

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

Get the VNC URL and open it with the Mac Screen Sharing app:

```bash
talonbox status {quoted_name}
open "$(talonbox status {quoted_name} | awk '/^vnc:/ {{print $2}}')"
```

Accept the Talon EULA yourself in the GUI. Agents must never try to accept the Talon EULA for you.

## 5. Grant permissions and choose a speech model

Open System Settings in the VM and go to Privacy & Security.

Because talonbox runs Talon via Terminal, grant permissions to Terminal, not to the Talon app:

- Accessibility
- Camera

From the Talon menu, select the speech model you want to use. You do not need to set a microphone.

Install any other apps you expect to test Talon with.

## 6. Reboot and stop the VM

When setup is complete, quit all apps and restart the VM. When macOS asks, uncheck the box to reopen windows after logging back in.

After the VM has restarted and you are done with setup, stop it:

```bash
talonbox stop {quoted_name}
```

## 7. Smoke test the finished VM

Confirm the installation with:

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
    short_help="Start or resume an existing VM.",
    help=(
        "Start or resume an existing VM in the background, wait for SSH, and start "
        "Talon if it is not already running.\n\n"
        "This command never clones, deletes, or wipes the VM."
    ),
    epilog=_examples_epilog("talonbox start experiment"),
)
@click.argument("name", metavar="NAME")
@pass_settings
def start(settings: CliSettings, name: str) -> None:
    vm_controller = VmController(name, settings.debug)
    _echo_vm_info(vm_controller, vm_controller.start().to_vm_info())


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
    short_help="Run a diagnostic against a temporary clone.",
    help=(
        "Run a mutating end-to-end sanity check against a temporary clone of SOURCE.\n\n"
        "The source VM must be stopped. smoke-test clones it, starts the clone, uploads a "
        "temporary Talon command bundle, runs mimic(), verifies a guest-side marker file, "
        "captures screenshots, and then stops and deletes the clone.\n\n"
        "Artifacts are kept under `/tmp` for debugging."
    ),
    epilog=_examples_epilog("talonbox smoke-test golden"),
)
@click.argument("source", metavar="SOURCE")
@pass_settings
def smoke_test(settings: CliSettings, source: str) -> None:
    _build_smoke_test_runner(settings, source).run()


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
        "Use Talon's screen capture API inside the VM, save the image to a guest temp file, "
        "download it to a host path under `/tmp`, and remove the guest temp file."
    ),
    epilog=_examples_epilog("talonbox screenshot experiment /tmp/talon.png"),
)
@click.argument("name", metavar="NAME")
@click.argument(
    "filepath", metavar="HOST_PATH", type=click.Path(dir_okay=False, path_type=Path)
)
@pass_settings
def screenshot(settings: CliSettings, name: str, filepath: Path) -> None:
    _build_talon_client(settings, name).capture_screenshot(filepath)


def main() -> int:
    cli.main(standalone_mode=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
