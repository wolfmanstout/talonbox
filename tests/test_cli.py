from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from talonbox import cli as cli_module
from talonbox.cli import cli
from talonbox.tart import VmInfo
from talonbox.transfer import ParsedTransferArgs
from talonbox.vm import StartResult, VmController
from tests.helpers import running_vm_fixture


def test_version() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["--version"])

    assert result.exit_code == 0
    assert result.output.startswith("talonbox, version ")


def test_cli_closes_vnc_reactor_on_command_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    calls: list[str] = []

    monkeypatch.setattr(cli_module, "shutdown_vnc_reactor", lambda: calls.append("vnc"))

    result = runner.invoke(cli, ["create", "experiment"])

    assert result.exit_code == 0
    assert calls == ["vnc"]


def test_root_help_groups_commands_and_examples() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["--help"])
    output_words = " ".join(result.output.split())

    assert result.exit_code == 0
    assert "Minimal Talon VM control primitives for coding agents." in result.output
    assert "Use named macOS VMs as disposable Talon sandboxes" in result.output
    assert "VM paths use `NAME:/absolute/path`" in result.output
    assert "temporary clone so the source VM stays clean" in output_words
    assert "Some Talon-backed commands also accept `--vnc`" in result.output
    assert "prefer it during setup" in result.output
    assert "Never use automation to accept the Talon EULA." in output_words
    assert "VM lifecycle:" in result.output
    assert "clone" in result.output
    assert "delete" in result.output
    assert "list" in result.output
    assert "status" in result.output
    assert "open" in result.output
    assert "Guest shell:" in result.output
    assert "Talon RPC:" in result.output
    assert "press" in result.output
    assert "Other:" not in result.output
    assert "talonbox clone golden experiment" in result.output
    assert (
        "talonbox rsync -a ~/.talon/user/ experiment:/Users/admin/.talon/user/"
        in result.output
    )


def test_clone_help_documents_apfs_clone() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["clone", "--help"])

    assert result.exit_code == 0
    assert "APFS copy-on-write cloning" in result.output
    assert "talonbox clone golden experiment" in result.output


def test_create_help_documents_markdown_instruction_behavior() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["create", "--help"])

    assert result.exit_code == 0
    assert "Print Markdown instructions" in result.output
    assert "does not create, clone, start, or modify any VM" in result.output
    assert "--base" in result.output
    assert "Unprefixed talonbox base VM name" in result.output
    assert "--talon-dmg" in result.output
    assert "Talon DMG path or URL" in result.output
    assert "talonbox create --base tahoe-base golden" in result.output
    assert (
        "talonbox create --talon-dmg ~/Downloads/talon-beta.dmg golden-beta"
        in result.output
    )


def test_create_command_prints_default_url_instructions() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["create", "experiment"])

    assert result.exit_code == 0
    assert result.output.startswith("# Create a talonbox VM: `experiment`")
    assert "https://tart.run/quick-start/" in result.output
    assert "brew install cirruslabs/cli/sshpass" in result.output
    assert "tart --version" in result.output
    assert "base VM name is `tahoe-base` in talonbox commands" in result.output
    assert "`talonbox-tahoe-base` in Tart commands" in result.output
    assert "Pass the unprefixed name to `talonbox create --base`" in result.output
    assert "tart list" in result.output
    assert "If it already exists, reuse it." in result.output
    assert "Do not create, overwrite, or delete an existing base VM" in result.output
    assert "If the base VM does not exist, clone it" in result.output
    assert (
        "tart clone ghcr.io/cirruslabs/macos-tahoe-base:latest talonbox-tahoe-base"
        in result.output
    )
    assert "tart clone tahoe-base talonbox-tahoe-base" not in result.output
    assert "Do not resize the clone" in result.output
    assert "tart create" not in result.output
    assert "--disk-size" not in result.output
    assert "--ipsw" not in result.output
    assert "unattended" not in result.output
    assert "tart ls" not in result.output
    assert "look up the VNC URL first" in result.output
    assert "the actual `vnc://...` URL" in result.output
    assert "talonbox status tahoe-base" in result.output
    assert "talonbox open tahoe-base" in result.output
    assert "VNC URL: vnc://..." in result.output
    assert "Open it with: talonbox open tahoe-base" in result.output
    assert "Tart's Cirrus Labs macOS images use `admin`" in result.output
    assert "talonbox start --no-talon tahoe-base" in result.output
    assert "talonbox exec tahoe-base -- whoami" in result.output
    assert "talonbox exec tahoe-base -- open -a Terminal" not in result.output
    assert "returns to a logged-in desktop" in result.output
    assert "Use `talonbox clone`, not `tart clone`" in result.output
    assert "talonbox stop --shutdown tahoe-base" in result.output
    assert "talonbox clone tahoe-base experiment" in result.output
    assert "tart clone talonbox-tahoe-base talonbox-experiment" not in result.output
    assert "talonbox start --no-talon experiment" in result.output
    assert (
        "starts the VM and waits for SSH without trying to launch Talon"
        in result.output
    )
    assert "talonbox exec experiment -- whoami" in result.output
    assert (
        "talonbox exec experiment -- curl -L -o /tmp/talon.dmg https://talonvoice.com/dl/latest/talon-mac.dmg"
        in result.output
    )
    assert "install-rosetta" not in result.output
    assert "talonbox restart-talon experiment" in result.output
    assert (
        "talonbox screenshot --vnc experiment /tmp/talon-first-run.png" in result.output
    )
    assert "prefer `--vnc` anywhere it is available" in result.output
    assert "VNC screenshots capture the VM framebuffer" in result.output
    assert "talonbox click --vnc" in result.output
    assert "talonbox type --vnc" in result.output
    assert "talonbox status experiment" in result.output
    assert "talonbox open experiment" in result.output
    assert "run `talonbox status experiment` yourself" in result.output
    assert "include the actual `vnc://...` URL" in result.output
    assert "The human user must review and accept the Talon EULA" in result.output
    assert "Agents must never try to accept the Talon EULA." in result.output
    assert "Do not use `talonbox click --vnc`" in result.output
    assert "must choose a speech model manually from the Talon menu" in result.output
    assert "no speech model is installed and selected" in result.output
    assert "prompt for this" not in result.output
    assert "Give the user this first-run checklist" in result.output
    assert "Install and select a speech model" in result.output
    assert "Leave microphone setup alone" in result.output
    assert "## 6. Run the setup smoke test and grant permissions" in result.output
    assert "talonbox smoke-test --in-place experiment" in result.output
    assert "take a screenshot through VNC" in result.output
    assert "Permissions dialogs can be accepted using" in result.output
    assert "more easily handled by a human over VNC" in result.output
    assert "After any permission click or typed confirmation" in result.output
    assert "hand the step to the user with the actual VNC URL and viewer command" in (
        result.output
    )
    assert "talonbox screenshot --vnc experiment /tmp/permission-after-vnc.png" in (
        result.output
    )
    assert "talonbox screenshot experiment /tmp/permission-after-talon.png" in (
        result.output
    )
    assert "talonbox press --vnc experiment enter" in result.output
    assert (
        "It is expected that the user will need to grant permissions" in result.output
    )
    assert "Microphone" in result.output
    assert "Screen & System Audio Recording" in result.output
    assert "take one more VNC screenshot before the reboot" in result.output
    assert "talonbox screenshot --vnc experiment /tmp/before-reboot-vnc.png" in (
        result.output
    )
    assert "uncheck the box to reopen windows" in result.output
    assert "the agent should ask the user to quit all apps" in result.output
    assert "After the user reports that the VM has restarted" in result.output
    assert "## 7. Reboot and stop the VM" in result.output
    assert "talonbox stop --shutdown experiment" in result.output
    assert "## 8. Smoke test the finished VM" in result.output
    assert "talonbox smoke-test experiment" in result.output


def test_create_command_rejects_positional_talon_dmg() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["create", "experiment", "/tmp/Talon Build.dmg"])

    assert result.exit_code == 2
    assert "Got unexpected extra argument" in result.output


def test_create_command_accepts_talon_dmg_option() -> None:
    runner = CliRunner()

    result = runner.invoke(
        cli, ["create", "--talon-dmg", "/tmp/Talon Build.dmg", "experiment"]
    )

    assert result.exit_code == 0
    assert (
        "talonbox scp -q '/tmp/Talon Build.dmg' experiment:/tmp/talon.dmg"
        in result.output
    )
    assert "curl -L -o /tmp/talon.dmg" not in result.output


def test_create_command_uses_custom_base_name() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["create", "--base", "sonoma-base", "experiment"])

    assert result.exit_code == 0
    assert "base VM name is `sonoma-base` in talonbox commands" in result.output
    assert "`talonbox-sonoma-base` in Tart commands" in result.output
    assert (
        "tart clone ghcr.io/cirruslabs/macos-tahoe-base:latest talonbox-sonoma-base"
        in result.output
    )
    assert "tart clone tahoe-base talonbox-sonoma-base" not in result.output
    assert "talonbox clone sonoma-base experiment" in result.output


def test_rename_help_documents_requirements() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["rename", "--help"])

    assert result.exit_code == 0
    assert "Rename SOURCE to DEST" in result.output
    assert "source VM must be stopped" in result.output
    assert "destination VM must not already exist" in result.output
    assert "talonbox rename experiment experiment-old" in result.output


def test_start_help_documents_no_talon_mode() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["start", "--help"])
    output_words = " ".join(result.output.split())

    assert result.exit_code == 0
    assert "--no-talon" in result.output
    assert "do not launch Talon or wait for its REPL" in output_words
    assert "talonbox start --no-talon experiment" in result.output


def test_exec_help_explains_double_dash_usage() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["exec", "--help"])

    assert result.exit_code == 0
    assert "Place `--` before the remote command" in result.output
    assert "multiline scripts" in result.output
    assert "Literal multiline quoted strings" in result.output
    assert "talonbox exec experiment -- whoami" in result.output
    assert "talonbox exec experiment -- 'whoami\n  pwd'" in result.output


def test_rsync_help_uses_quiet_vm_user_path_examples() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["rsync", "--help"])

    assert result.exit_code == 0
    assert (
        "talonbox rsync -a ./repo/ experiment:/Users/admin/.talon/user/repo/"
        in result.output
    )
    assert "talonbox rsync -av" not in result.output


def test_scp_help_uses_quiet_vm_user_path_examples() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["scp", "--help"])

    assert result.exit_code == 0
    assert (
        "talonbox scp -q ./settings.talon "
        "experiment:/Users/admin/.talon/user/settings.talon" in result.output
    )
    assert "talonbox scp -q experiment:/tmp/out.png /tmp/out.png" in result.output
    assert "talonbox scp ./settings.talon" not in result.output


def test_mimic_help_works() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["mimic", "--help"])

    assert result.exit_code == 0
    assert "Send one phrase to the VM's Talon REPL" in result.output
    assert "--audio" in result.output
    assert "[[slnc 500]]" in result.output
    assert "Apple's archived Speech Manager reference" in result.output
    assert cli_module.APPLE_SPEECH_MANAGER_REFERENCE_URL in result.output


def test_mimic_command_delegates_to_talon_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    calls: list[tuple[str, str, bool]] = []

    class FakeClient:
        def mimic(self, command: str, *, audio: bool = False) -> None:
            calls.append(("mimic", command, audio))

    monkeypatch.setattr(
        cli_module, "_build_talon_client", lambda settings, name, **kwargs: FakeClient()
    )

    result = runner.invoke(cli, ["mimic", "experiment", "focus chrome"])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == "Mimic successful\n"
    assert calls == [("mimic", "focus chrome", False)]


def test_mimic_command_can_use_audio_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    calls: list[tuple[str, str, bool]] = []

    class FakeClient:
        def mimic(self, command: str, *, audio: bool = False) -> None:
            calls.append(("mimic", command, audio))

    monkeypatch.setattr(
        cli_module, "_build_talon_client", lambda settings, name, **kwargs: FakeClient()
    )

    result = runner.invoke(
        cli, ["mimic", "--audio", "experiment", "talonbox [[slnc 500]] smoke test"]
    )

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == "Mimic successful\n"
    assert calls == [("mimic", "talonbox [[slnc 500]] smoke test", True)]


def test_click_help_documents_vnc_mode() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["click", "--help"])
    output_words = " ".join(result.output.split())

    assert result.exit_code == 0
    assert "--vnc" in result.output
    assert "--button" in result.output
    assert "requires Talon's REPL" in result.output
    assert "Coordinates match the chosen backend" in output_words
    assert "Without `--vnc`, use coordinates from Talon screenshots" in output_words
    assert "With `--vnc`, use coordinates from `talonbox screenshot --vnc`" in (
        output_words
    )
    assert "talonbox click --vnc experiment 400 300" in result.output


def test_type_help_documents_vnc_mode() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["type", "--help"])

    assert result.exit_code == 0
    assert "--vnc" in result.output
    assert "requires Talon's REPL" in result.output
    assert "talonbox type --vnc experiment" in result.output


def test_press_help_documents_vnc_mode() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["press", "--help"])

    assert result.exit_code == 0
    assert "--vnc" in result.output
    assert "requires Talon's REPL" in result.output
    assert "talonbox press --vnc experiment enter" in result.output
    assert "talonbox press --vnc experiment space" in result.output


def test_screenshot_help_documents_explicit_vnc_mode() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["screenshot", "--help"])
    output_words = " ".join(result.output.split())

    assert result.exit_code == 0
    assert "--vnc" in result.output
    assert "--screencapture" not in result.output
    assert "requires Talon's REPL" in result.output
    assert "two screenshot modes may produce different pixel sizes" in output_words
    assert "Use coordinates from a Talon screenshot with `talonbox click`" in (
        output_words
    )
    assert "use coordinates from a VNC screenshot with `talonbox click --vnc`" in (
        output_words
    )
    assert (
        "talonbox screenshot --vnc experiment /tmp/talon-first-run.png" in result.output
    )


def test_smoke_test_help_mentions_clone_and_artifacts() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["smoke-test", "--help"])

    assert result.exit_code == 0
    assert "temporary clone of SOURCE" in result.output
    assert "source VM must be shut down" in result.output
    assert "--in-place" in result.output
    assert "leaves the VM running for GUI prompts" in result.output
    assert "Artifacts are kept under `/tmp`" in result.output


def test_start_command_delegates_to_vm_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    calls: list[tuple[str, bool]] = []

    def fake_start(self: VmController, *, require_talon: bool = True):
        calls.append((self.vm, require_talon))
        return StartResult(running_vm_fixture(), "booted")

    monkeypatch.setattr(cli_module.VmController, "start", fake_start)
    monkeypatch.setattr(
        cli_module.VmController,
        "format_vm_info",
        lambda self, info: ["name: experiment", "status: running"],
    )

    result = runner.invoke(cli, ["start", "experiment"])

    assert result.exit_code == 0
    assert result.output == "start: booted\nname: experiment\nstatus: running\n"
    assert calls == [("experiment", True)]


def test_start_command_can_skip_talon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    calls: list[tuple[str, bool]] = []

    def fake_start(self: VmController, *, require_talon: bool = True):
        calls.append((self.vm, require_talon))
        return StartResult(running_vm_fixture(), "resumed from suspend")

    monkeypatch.setattr(cli_module.VmController, "start", fake_start)
    monkeypatch.setattr(
        cli_module.VmController,
        "format_vm_info",
        lambda self, info: ["name: experiment", "status: running"],
    )

    result = runner.invoke(cli, ["start", "--no-talon", "experiment"])

    assert result.exit_code == 0
    assert (
        result.output
        == "start: resumed from suspend\nname: experiment\nstatus: running\n"
    )
    assert calls == [("experiment", False)]


def test_clone_command_delegates_to_vm_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        cli_module.VmController,
        "clone",
        lambda self, dest: calls.append((self.vm, dest)),
    )

    result = runner.invoke(cli, ["clone", "golden", "experiment"])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == "Clone successful\n"
    assert calls == [("golden", "experiment")]


def test_rename_command_delegates_to_vm_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        cli_module.VmController,
        "rename",
        lambda self, dest: calls.append((self.vm, dest)),
    )

    result = runner.invoke(cli, ["rename", "experiment", "experiment-old"])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == "Rename successful\n"
    assert calls == [("experiment", "experiment-old")]


def test_delete_command_delegates_each_name_to_vm_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    calls: list[str] = []

    monkeypatch.setattr(
        cli_module.VmController,
        "delete",
        lambda self: calls.append(self.vm),
    )

    result = runner.invoke(
        cli,
        ["delete", "experiment-one", "experiment-two"],
    )

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == "Delete successful\n"
    assert calls == ["experiment-one", "experiment-two"]


def test_delete_command_requires_at_least_one_name() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["delete"])

    assert result.exit_code == 2
    assert "Missing argument 'NAMES...'" in result.stderr


def test_status_command_delegates_to_vm_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        cli_module.VmController,
        "get_vm",
        lambda self: VmInfo(self.vm, "running", "192.168.64.10"),
    )
    monkeypatch.setattr(
        cli_module.VmController,
        "format_vm_info",
        lambda self, info: ["name: experiment", "status: running"],
    )

    result = runner.invoke(cli, ["status", "experiment"])

    assert result.exit_code == 0
    assert result.output == "name: experiment\nstatus: running\n"


def test_open_command_opens_vnc_url(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    calls: list[list[str]] = []
    monkeypatch.setattr(
        cli_module.VmController,
        "get_vm",
        lambda self: VmInfo(
            self.vm,
            "running",
            "192.168.64.10",
            "vnc://127.0.0.1:5901",
        ),
    )
    monkeypatch.setattr(
        cli_module.subprocess,
        "run",
        lambda cmd, check=False: (
            calls.append(cmd) or subprocess.CompletedProcess(cmd, 0)
        ),
    )

    result = runner.invoke(cli, ["open", "experiment"])

    assert result.exit_code == 0
    assert result.output == "vnc://127.0.0.1:5901\n"
    assert calls == [["open", "vnc://127.0.0.1:5901"]]


def test_restart_talon_command_delegates_to_vm_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    calls: list[str] = []

    def fake_restart_talon(self: VmController) -> None:
        calls.append(self.vm)

    monkeypatch.setattr(cli_module.VmController, "restart_talon", fake_restart_talon)

    result = runner.invoke(cli, ["restart-talon", "experiment"])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == "Restart Talon successful\n"
    assert calls == ["experiment"]


def test_stop_command_delegates_to_vm_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(
        cli_module.VmController,
        "stop",
        lambda self, *, shutdown=False: calls.append((self.vm, shutdown)),
    )

    result = runner.invoke(cli, ["stop", "--shutdown", "experiment"])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == "Stop successful\n"
    assert calls == [("experiment", True)]


def test_screenshot_command_uses_talon_capture_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    calls: list[tuple[str, str, bool]] = []

    class FakeClient:
        def capture_screenshot(self, filepath: Path, *, vnc: bool = False) -> None:
            calls.append(("capture_screenshot", str(filepath), vnc))

    monkeypatch.setattr(
        cli_module, "_build_talon_client", lambda settings, name, **kwargs: FakeClient()
    )

    result = runner.invoke(cli, ["screenshot", "experiment", "/tmp/screen.png"])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == "Screenshot successful\n"
    assert calls == [("capture_screenshot", "/tmp/screen.png", False)]


def test_screenshot_command_can_use_vnc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    calls: list[tuple[str, str, bool]] = []

    class FakeClient:
        def capture_screenshot(self, filepath: Path, *, vnc: bool = False) -> None:
            calls.append(("capture_screenshot", str(filepath), vnc))

    monkeypatch.setattr(
        cli_module, "_build_talon_client", lambda settings, name, **kwargs: FakeClient()
    )

    result = runner.invoke(
        cli, ["screenshot", "--vnc", "experiment", "/tmp/screen.png"]
    )

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == "Screenshot successful\n"
    assert calls == [("capture_screenshot", "/tmp/screen.png", True)]


def test_click_command_uses_talon_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    calls: list[tuple[str, int, int, str, bool]] = []

    class FakeClient:
        def click(
            self, x: int, y: int, *, button: str = "left", vnc: bool = False
        ) -> None:
            calls.append(("click", x, y, button, vnc))

    monkeypatch.setattr(
        cli_module, "_build_talon_client", lambda settings, name, **kwargs: FakeClient()
    )

    result = runner.invoke(cli, ["click", "experiment", "400", "300"])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == "Click successful\n"
    assert calls == [("click", 400, 300, "left", False)]


def test_click_command_can_use_vnc_and_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    calls: list[tuple[str, int, int, str, bool]] = []

    class FakeClient:
        def click(
            self, x: int, y: int, *, button: str = "left", vnc: bool = False
        ) -> None:
            calls.append(("click", x, y, button, vnc))

    monkeypatch.setattr(
        cli_module, "_build_talon_client", lambda settings, name, **kwargs: FakeClient()
    )

    result = runner.invoke(
        cli, ["click", "--vnc", "--button", "right", "experiment", "400", "300"]
    )

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == "Click successful\n"
    assert calls == [("click", 400, 300, "right", True)]


def test_type_command_uses_talon_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    calls: list[tuple[str, str, bool]] = []

    class FakeClient:
        def type_text(self, text: str, *, vnc: bool = False) -> None:
            calls.append(("type_text", text, vnc))

    monkeypatch.setattr(
        cli_module, "_build_talon_client", lambda settings, name, **kwargs: FakeClient()
    )

    result = runner.invoke(cli, ["type", "experiment", "hello"])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == "Type successful\n"
    assert calls == [("type_text", "hello", False)]


def test_type_command_can_use_vnc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    calls: list[tuple[str, str, bool]] = []

    class FakeClient:
        def type_text(self, text: str, *, vnc: bool = False) -> None:
            calls.append(("type_text", text, vnc))

    monkeypatch.setattr(
        cli_module, "_build_talon_client", lambda settings, name, **kwargs: FakeClient()
    )

    result = runner.invoke(cli, ["type", "--vnc", "experiment", "hello"])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == "Type successful\n"
    assert calls == [("type_text", "hello", True)]


@pytest.mark.parametrize("text", ["\n", " "])
def test_type_command_can_send_vnc_control_text(
    monkeypatch: pytest.MonkeyPatch,
    text: str,
) -> None:
    runner = CliRunner()
    calls: list[tuple[str, str, bool]] = []

    class FakeClient:
        def type_text(self, text: str, *, vnc: bool = False) -> None:
            calls.append(("type_text", text, vnc))

    monkeypatch.setattr(
        cli_module, "_build_talon_client", lambda settings, name, **kwargs: FakeClient()
    )

    result = runner.invoke(cli, ["type", "--vnc", "experiment", text])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == "Type successful\n"
    assert calls == [("type_text", text, True)]


@pytest.mark.parametrize("key", ["enter", "space"])
def test_press_command_can_use_vnc(
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> None:
    runner = CliRunner()
    calls: list[tuple[str, str, bool]] = []

    class FakeClient:
        def press_key(self, key: str, *, vnc: bool = False) -> None:
            calls.append(("press_key", key, vnc))

    monkeypatch.setattr(
        cli_module, "_build_talon_client", lambda settings, name, **kwargs: FakeClient()
    )

    result = runner.invoke(cli, ["press", "--vnc", "experiment", key])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == "Press successful\n"
    assert calls == [("press_key", key, True)]


def test_list_command_prints_public_vms(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    older_accessed = datetime(2026, 7, 4, 15, 14, 41, tzinfo=UTC)
    newer_accessed = datetime(2026, 7, 6, 1, 28, 15, tzinfo=UTC)
    monkeypatch.setattr(
        cli_module.VmController,
        "list_vms",
        lambda debug=False: [
            VmInfo(
                "experiment",
                "running",
                "192.168.64.10",
                "vnc://127.0.0.1:5901",
                newer_accessed,
            ),
            VmInfo("golden", "stopped", None, last_accessed=older_accessed),
        ],
    )

    result = runner.invoke(cli, ["list"])

    assert result.exit_code == 0
    older_local = cli_module._format_accessed_time(older_accessed)
    newer_local = cli_module._format_accessed_time(newer_accessed)
    headers = ["Name", "Status", "Last accessed", "IP", "VNC"]
    data_rows = [
        [
            "experiment",
            "running",
            newer_local,
            "192.168.64.10",
            "vnc://127.0.0.1:5901",
        ],
        ["golden", "stopped", older_local, "-", "-"],
    ]
    widths = [
        max(len(row[index]) for row in [headers, *data_rows])
        for index in range(len(headers))
    ]
    rows = [
        headers,
        ["-" * width for width in widths],
        *data_rows,
    ]
    assert result.output == (
        "\n".join(
            "  ".join(
                value.ljust(width) for value, width in zip(row, widths, strict=False)
            ).rstrip()
            for row in rows
        )
        + "\n"
    )


def test_format_accessed_time_uses_relative_phrasing() -> None:
    now = datetime(2026, 7, 6, 2, 0, tzinfo=UTC)

    assert cli_module._format_accessed_time(None, now=now) == "-"
    assert (
        cli_module._format_accessed_time(
            datetime(2026, 7, 6, 1, 59, 42, tzinfo=UTC), now=now
        )
        == "just now"
    )
    assert (
        cli_module._format_accessed_time(
            datetime(2026, 7, 6, 1, 40, tzinfo=UTC), now=now
        )
        == "20 minutes ago"
    )
    assert (
        cli_module._format_accessed_time(
            datetime(2026, 7, 6, 1, 0, tzinfo=UTC), now=now
        )
        == "1 hour ago"
    )
    assert (
        cli_module._format_accessed_time(
            datetime(2026, 7, 3, 2, 0, tzinfo=UTC), now=now
        )
        == "3 days ago"
    )


def test_smoke_test_command_passes_source_to_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    calls: list[str] = []

    class FakeRunner:
        def run(self, *, clone: bool = True) -> None:
            calls.append(f"run:{clone}")

    def fake_build_runner(settings: object, source: str) -> FakeRunner:
        calls.append(source)
        return FakeRunner()

    monkeypatch.setattr(cli_module, "_build_smoke_test_runner", fake_build_runner)

    result = runner.invoke(cli, ["smoke-test", "golden"])

    assert result.exit_code == 0
    assert calls == ["golden", "run:True"]


def test_smoke_test_command_can_run_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    calls: list[str] = []

    class FakeRunner:
        def run(self, *, clone: bool = True) -> None:
            calls.append(f"run:{clone}")

    def fake_build_runner(settings: object, source: str) -> FakeRunner:
        calls.append(source)
        return FakeRunner()

    monkeypatch.setattr(cli_module, "_build_smoke_test_runner", fake_build_runner)

    result = runner.invoke(cli, ["smoke-test", "--in-place", "golden"])

    assert result.exit_code == 0
    assert calls == ["golden", "run:False"]


def test_cli_rejects_non_macos_before_running_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    calls: list[str] = []

    monkeypatch.setattr(cli_module.sys, "platform", "linux")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(
        cli_module.VmController,
        "get_vm",
        lambda self: (
            calls.append("get_vm") or VmInfo(self.vm, "running", "192.168.64.10")
        ),
    )

    result = runner.invoke(cli, ["status", "experiment"])

    assert result.exit_code == 1
    assert "supports only macOS hosts" in result.output
    assert calls == []


def test_repl_reads_stdin_when_no_code(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    payloads: list[tuple[str, str]] = []

    class FakeClient:
        def repl(self, code: str) -> None:
            payloads.append(("repl", code))

    monkeypatch.setattr(
        cli_module, "_build_talon_client", lambda settings, name, **kwargs: FakeClient()
    )

    result = runner.invoke(cli, ["repl", "experiment"], input="print(1)\n")

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == "Repl successful\n"
    assert payloads == [("repl", "print(1)\n")]


def test_repl_help_prefers_quoted_code_examples() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["repl", "--help"])

    assert result.exit_code == 0
    assert "Pass CODE as a quoted argument" in result.output
    assert "Literal multiline quoted strings" in result.output
    assert (
        "talonbox repl experiment 'if True:\n"
        "      from talon import ui\n"
        "      print(ui.active_app())'" in result.output
    )
    assert "printf 'print(1+1)\\n' | talonbox repl experiment" not in result.output


def test_exec_command_reports_success_for_quiet_guest_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    running_vm = running_vm_fixture()
    calls: list[tuple[str, str | list[str]]] = []

    monkeypatch.setattr(
        cli_module.VmController, "get_running_vm", lambda self: running_vm
    )

    def fake_exec(
        command_args: str | list[str],
        stream: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((running_vm.ip_address, command_args))
        assert stream is True
        assert check is False
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(running_vm, "run_shell", fake_exec)

    result = runner.invoke(cli, ["exec", "experiment", "--", "true"])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == "Exec successful\n"
    assert calls == [("192.168.64.10", "true")]


def test_exec_command_runs_guest_shell_and_propagates_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    running_vm = running_vm_fixture()
    calls: list[tuple[str, list[str]]] = []

    monkeypatch.setattr(
        cli_module.VmController, "get_running_vm", lambda self: running_vm
    )

    def fake_exec(
        command_args: list[str],
        stream: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((running_vm.ip_address, command_args))
        assert stream is True
        assert check is False
        return subprocess.CompletedProcess([], 7, "", "")

    monkeypatch.setattr(running_vm, "run_shell", fake_exec)

    result = runner.invoke(cli, ["exec", "experiment", "--", "echo", "hi"])

    assert result.exit_code == 7
    assert calls == [("192.168.64.10", ["echo", "hi"])]


def test_rsync_command_reports_success_for_quiet_transfer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    running_vm = running_vm_fixture()
    calls: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = []

    monkeypatch.setattr(
        cli_module.VmController, "get_running_vm", lambda self: running_vm
    )

    def fake_rsync(self: object, args: ParsedTransferArgs) -> int:
        calls.append(
            (
                running_vm.ip_address,
                args.vm_name,
                args.passthrough,
                tuple(operand.raw for operand in args.operands),
            )
        )
        return 0

    monkeypatch.setattr(cli_module.TransferService, "rsync", fake_rsync)

    result = runner.invoke(
        cli,
        [
            "rsync",
            "-a",
            "./repo/",
            "experiment:/Users/admin/.talon/user/repo/",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == "Rsync successful\n"
    assert calls == [
        (
            "192.168.64.10",
            "experiment",
            ("-a",),
            ("./repo/", "experiment:/Users/admin/.talon/user/repo/"),
        )
    ]


def test_scp_command_reports_success_for_quiet_transfer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    running_vm = running_vm_fixture()
    calls: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = []

    monkeypatch.setattr(
        cli_module.VmController, "get_running_vm", lambda self: running_vm
    )

    def fake_scp(self: object, args: ParsedTransferArgs) -> int:
        calls.append(
            (
                running_vm.ip_address,
                args.vm_name,
                args.passthrough,
                tuple(operand.raw for operand in args.operands),
            )
        )
        return 0

    monkeypatch.setattr(cli_module.TransferService, "scp", fake_scp)

    result = runner.invoke(
        cli,
        [
            "scp",
            "-q",
            "./settings.talon",
            "experiment:/Users/admin/.talon/user/settings.talon",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == "Scp successful\n"
    assert calls == [
        (
            "192.168.64.10",
            "experiment",
            ("-q",),
            (
                "./settings.talon",
                "experiment:/Users/admin/.talon/user/settings.talon",
            ),
        )
    ]
