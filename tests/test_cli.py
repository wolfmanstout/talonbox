from __future__ import annotations

import subprocess

import pytest
from click.testing import CliRunner

from talonbox import cli as cli_module
from talonbox.cli import cli
from talonbox.lume import VmInfo
from talonbox.vm import VmController
from tests.helpers import running_vm_fixture


def test_version() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["--version"])

    assert result.exit_code == 0
    assert result.output.startswith("talonbox, version ")


def test_root_help_groups_commands_and_examples() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["--help"])
    output_words = " ".join(result.output.split())

    assert result.exit_code == 0
    assert "Minimal Talon VM control primitives for coding agents." in result.output
    assert "Use named macOS VMs as disposable Talon sandboxes" in result.output
    assert "VM paths use `NAME:/absolute/path`" in result.output
    assert "temporary clone so the source VM stays clean" in output_words
    assert "VM lifecycle:" in result.output
    assert "clone" in result.output
    assert "delete" in result.output
    assert "list" in result.output
    assert "status" in result.output
    assert "Guest shell:" in result.output
    assert "Talon RPC:" in result.output
    assert "talonbox clone golden experiment" in result.output
    assert (
        "talonbox rsync -a ~/.talon/user/ experiment:/Users/lume/.talon/user/"
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
    assert "talonbox create golden-beta ~/Downloads/talon-beta.dmg" in result.output


def test_create_command_prints_default_url_instructions() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["create", "experiment"])

    assert result.exit_code == 0
    assert result.output.startswith("# Create a talonbox VM: `experiment`")
    assert (
        "https://cua.ai/docs/lume/guide/getting-started/installation" in result.output
    )
    assert "https://cua.ai/docs/lume/guide/getting-started/quickstart" in result.output
    assert (
        "This example keeps `tahoe-base` as a clean Lume base VM outside talonbox"
        in result.output
    )
    assert (
        "lume create tahoe-base --os macos --ipsw latest --unattended tahoe --disk-size 100GB"
        in result.output
    )
    assert (
        "Use the `talonbox-` prefix for the clone name when working directly with `lume`; omit it when running `talonbox` commands."
        in result.output
    )
    assert "lume clone tahoe-base talonbox-experiment" in result.output
    assert "lume run talonbox-experiment" in result.output
    assert (
        "talonbox exec experiment -- curl -L -o /tmp/talon.dmg https://talonvoice.com/dl/latest/talon-mac.dmg"
        in result.output
    )
    assert (
        "talonbox exec experiment -- softwareupdate --install-rosetta --agree-to-license"
        in result.output
    )
    assert "talonbox restart-talon experiment" in result.output
    assert "talonbox status experiment" in result.output
    assert (
        "open \"$(talonbox status experiment | awk '/^vnc:/ {print $2}')\""
        in result.output
    )
    assert "Agents must never try to accept the Talon EULA for you." in result.output
    assert "grant permissions to Terminal, not to the Talon app" in result.output
    assert "You do not need to set a microphone." in result.output
    assert "uncheck the box to reopen windows" in result.output
    assert "talonbox stop experiment" in result.output
    assert "talonbox smoke-test experiment" in result.output


def test_create_command_prints_local_dmg_copy_instructions() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["create", "experiment", "/tmp/Talon Build.dmg"])

    assert result.exit_code == 0
    assert (
        "talonbox scp -q '/tmp/Talon Build.dmg' experiment:/tmp/talon.dmg"
        in result.output
    )
    assert "curl -L -o /tmp/talon.dmg" not in result.output


def test_rename_help_documents_requirements() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["rename", "--help"])

    assert result.exit_code == 0
    assert "Rename SOURCE to DEST" in result.output
    assert "source VM must be stopped" in result.output
    assert "destination VM must not already exist" in result.output
    assert "talonbox rename experiment experiment-old" in result.output


def test_exec_help_explains_double_dash_usage() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["exec", "--help"])

    assert result.exit_code == 0
    assert "Place `--` before the remote command" in result.output
    assert "talonbox exec experiment -- whoami" in result.output


def test_rsync_help_uses_quiet_vm_user_path_examples() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["rsync", "--help"])

    assert result.exit_code == 0
    assert (
        "talonbox rsync -a ./repo/ experiment:/Users/lume/.talon/user/repo/"
        in result.output
    )
    assert "talonbox rsync -av" not in result.output


def test_scp_help_uses_quiet_vm_user_path_examples() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["scp", "--help"])

    assert result.exit_code == 0
    assert (
        "talonbox scp -q ./settings.talon "
        "experiment:/Users/lume/.talon/user/settings.talon" in result.output
    )
    assert "talonbox scp -q experiment:/tmp/out.png /tmp/out.png" in result.output
    assert "talonbox scp ./settings.talon" not in result.output


def test_mimic_help_works() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["mimic", "--help"])

    assert result.exit_code == 0
    assert "Send one phrase to the VM's Talon REPL" in result.output


def test_smoke_test_help_mentions_clone_and_artifacts() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["smoke-test", "--help"])

    assert result.exit_code == 0
    assert "temporary clone of SOURCE" in result.output
    assert "source VM must be stopped" in result.output
    assert "Artifacts are kept under `/tmp`" in result.output


def test_start_command_delegates_to_vm_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    calls: list[str] = []

    def fake_start(self: VmController):
        calls.append(self.vm)
        return running_vm_fixture()

    monkeypatch.setattr(cli_module.VmController, "start", fake_start)
    monkeypatch.setattr(
        cli_module.VmController,
        "format_vm_info",
        lambda self, info: ["name: experiment", "status: running"],
    )

    result = runner.invoke(cli, ["start", "experiment"])

    assert result.exit_code == 0
    assert result.output == "name: experiment\nstatus: running\n"
    assert calls == ["experiment"]


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
    assert calls == [("experiment", "experiment-old")]


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


def test_list_command_prints_public_vms(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        cli_module.VmController,
        "list_vms",
        lambda debug=False: [
            VmInfo("golden", "stopped", None),
            VmInfo("experiment", "running", "192.168.64.10", "vnc://127.0.0.1:5901"),
        ],
    )

    result = runner.invoke(cli, ["list"])

    assert result.exit_code == 0
    assert result.output == (
        "name\tstatus\tip\tvnc\n"
        "golden\tstopped\t-\t-\n"
        "experiment\trunning\t192.168.64.10\tvnc://127.0.0.1:5901\n"
    )


def test_smoke_test_command_passes_source_to_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    calls: list[str] = []

    class FakeRunner:
        def run(self) -> None:
            calls.append("run")

    def fake_build_runner(settings: object, source: str) -> FakeRunner:
        calls.append(source)
        return FakeRunner()

    monkeypatch.setattr(cli_module, "_build_smoke_test_runner", fake_build_runner)

    result = runner.invoke(cli, ["smoke-test", "golden"])

    assert result.exit_code == 0
    assert calls == ["golden", "run"]


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
        cli_module, "_build_talon_client", lambda settings, name: FakeClient()
    )

    result = runner.invoke(cli, ["repl", "experiment"], input="print(1)\n")

    assert result.exit_code == 0
    assert payloads == [("repl", "print(1)\n")]


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
