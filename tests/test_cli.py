from __future__ import annotations

import subprocess

import click
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
    output_words = " ".join(
        result.output.replace("-\n  ", "-").replace("-\n", "-").split()
    )

    assert result.exit_code == 0
    assert "Minimal Talon VM control primitives for coding agents." in result.output
    assert "Use `start --resume` to boot the existing target VM" in output_words
    assert "Use `show` for a read-only status check" in output_words
    assert "VM lifecycle:" in result.output
    assert (
        "start Clone golden by default; use --resume to preserve target VM disk."
        in output_words
    )
    assert "Guest shell:" in result.output
    assert "Talon RPC:" in result.output
    assert "scp" in result.output
    assert "restart-talon" in result.output
    assert "smoke-test" in result.output
    assert "talonbox exec -- uname -a" in result.output
    assert "talonbox smoke-test" in result.output
    assert (
        "talonbox start --resume  # preserve installed apps and other guest mutations"
        in result.output
    )


def test_exec_help_explains_double_dash_usage() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["exec", "--help"])

    assert result.exit_code == 0
    assert "Place `--` before the remote command" in result.output
    assert "talonbox exec -- whoami" in result.output


def test_show_help_explains_resume_vs_clean_start() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["show", "--help"])
    output_words = " ".join(result.output.split())

    assert result.exit_code == 0
    assert "use `start --resume` to preserve an existing target VM" in output_words
    assert "`start` to replace it from the golden VM" in output_words


def test_mimic_help_works() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["mimic", "--help"])

    assert result.exit_code == 0
    assert (
        "Send one phrase to the guest Talon REPL as `mimic(<phrase>)`." in result.output
    )


def test_smoke_test_help_mentions_artifacts_and_confirmation() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["smoke-test", "--help"])

    assert result.exit_code == 0
    assert "Run a mutating end-to-end sanity check" in result.output
    assert "may stop a running VM" in result.output
    assert "Artifacts are kept under `/tmp`" in result.output
    assert "left stopped" in result.output
    assert "talonbox smoke-test --yes" in result.output


def test_start_command_delegates_to_vm_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    calls: list[tuple[str, str, bool]] = []

    def fake_start(self: VmController, *, resume: bool = False):
        calls.append((self.vm, self.golden_vm, resume))
        return running_vm_fixture()

    monkeypatch.setattr(
        cli_module.VmController,
        "start",
        fake_start,
    )
    monkeypatch.setattr(
        cli_module.VmController,
        "format_vm_info",
        lambda self, info: ["status: running", "ip: 192.168.64.10"],
    )

    result = runner.invoke(cli, ["start"])

    assert result.exit_code == 0
    assert result.output == "status: running\nip: 192.168.64.10\n"
    assert calls == [("talonbox-live", "talonbox-golden", False)]


def test_start_command_passes_resume_and_vm_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    calls: list[tuple[str, str, bool]] = []

    def fake_start(self: VmController, *, resume: bool = False):
        calls.append((self.vm, self.golden_vm, resume))
        return running_vm_fixture()

    monkeypatch.setattr(cli_module.VmController, "start", fake_start)
    monkeypatch.setattr(
        cli_module.VmController,
        "format_vm_info",
        lambda self, info: ["status: running"],
    )

    result = runner.invoke(
        cli,
        [
            "--vm",
            "target-vm",
            "--golden-vm",
            "golden-vm",
            "start",
            "--resume",
        ],
    )

    assert result.exit_code == 0
    assert calls == [("target-vm", "golden-vm", True)]


def test_show_command_delegates_to_vm_controller(
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
        lambda self, info: ["status: running", "ip: 192.168.64.10"],
    )

    result = runner.invoke(cli, ["show"])

    assert result.exit_code == 0
    assert result.output == "status: running\nip: 192.168.64.10\n"


def test_smoke_test_command_passes_yes_to_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    calls: list[bool] = []

    class FakeRunner:
        def run(self, *, yes: bool, confirm: object = click.confirm) -> None:
            del confirm
            calls.append(yes)

    monkeypatch.setattr(
        cli_module, "_build_smoke_test_runner", lambda settings: FakeRunner()
    )

    result = runner.invoke(cli, ["smoke-test", "--yes"])

    assert result.exit_code == 0
    assert calls == [True]


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
        lambda self: calls.append("get_vm")
        or VmInfo(self.vm, "running", "192.168.64.10"),
    )

    result = runner.invoke(cli, ["show"])

    assert result.exit_code == 1
    assert "supports only macOS hosts" in result.output
    assert calls == []


def test_repl_reads_stdin_when_no_code(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    payloads: list[str] = []

    class FakeClient:
        def repl(self, code: str) -> None:
            payloads.append(code)

    monkeypatch.setattr(
        cli_module, "_build_talon_client", lambda settings: FakeClient()
    )

    result = runner.invoke(cli, ["repl"], input="print(1)\n")

    assert result.exit_code == 0
    assert payloads == ["print(1)\n"]


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

    result = runner.invoke(cli, ["exec", "--", "echo", "hi"])

    assert result.exit_code == 7
    assert calls == [("192.168.64.10", ["echo", "hi"])]
