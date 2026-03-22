from __future__ import annotations

import subprocess
from pathlib import Path
from typing import cast

import click
import pytest
from click.testing import CliRunner

from talonbox import cli as cli_module
from talonbox import lume as lume_module
from talonbox import transfer as transfer_module
from talonbox import vm as vm_module
from talonbox.cli import cli
from talonbox.lume import VmInfo
from talonbox.smoke_test import SmokeTestRunner
from talonbox.talon_client import TalonClient
from talonbox.transfer import TransferService
from talonbox.vm import RunningVm, VmController


def _fake_launch(
    log_path: Path = Path("/tmp/talonbox-test.log"),
) -> lume_module.VmLaunch:
    process = cast(
        subprocess.Popen[bytes], type("Process", (), {"poll": lambda self: None})()
    )
    return lume_module.VmLaunch(process=process, log_path=log_path)


def _set_vm_statuses(
    monkeypatch: pytest.MonkeyPatch,
    *statuses: tuple[str, str | None],
) -> None:
    remaining = list(statuses)

    def fake_get_vm_info(vm: str, debug: bool = False) -> VmInfo:
        del debug
        status, ip_address = remaining[0] if len(remaining) == 1 else remaining.pop(0)
        return VmInfo(vm, status, ip_address)

    monkeypatch.setattr(vm_module.lume, "get_vm_info", fake_get_vm_info)


def _build_service_stack(
    vm: str = "talon-test", debug: bool = False
) -> tuple[VmController, TransferService, TalonClient]:
    vm_controller = VmController(vm, debug)
    running_vm = _running_vm(debug=debug)
    transfer_service = TransferService(running_vm)
    talon_client = TalonClient(running_vm, transfer_service)
    return vm_controller, transfer_service, talon_client


def _running_vm(ip_address: str = "192.168.64.10", *, debug: bool = False) -> RunningVm:
    return RunningVm(
        name="talon-test",
        ip_address=ip_address,
        debug=debug,
    )


def test_version() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["--version"])

    assert result.exit_code == 0
    assert result.output.startswith("talonbox, version ")


def test_root_help_groups_commands_and_examples() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "Minimal Talon VM control primitives for coding agents." in result.output
    assert "Use `show` for a read-only status check" in result.output
    assert "VM lifecycle:" in result.output
    assert "Guest shell:" in result.output
    assert "Talon RPC:" in result.output
    assert "scp" in result.output
    assert "restart-talon" in result.output
    assert "smoke-test" in result.output
    assert "talonbox exec -- uname -a" in result.output
    assert "talonbox smoke-test" in result.output


def test_exec_help_explains_double_dash_usage() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["exec", "--help"])

    assert result.exit_code == 0
    assert "Place `--` before the remote command" in result.output
    assert "talonbox exec -- whoami" in result.output


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
    monkeypatch.setattr(
        cli_module.VmController,
        "start",
        lambda self: _running_vm(),
    )
    monkeypatch.setattr(
        cli_module.VmController,
        "format_vm_info",
        lambda self, info: ["status: running", "ip: 192.168.64.10"],
    )

    result = runner.invoke(cli, ["start"])

    assert result.exit_code == 0
    assert result.output == "status: running\nip: 192.168.64.10\n"


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


def test_vm_controller_format_vm_info_includes_vnc() -> None:
    vm_controller = VmController("talon-test", False)

    lines = vm_controller.format_vm_info(
        VmInfo("talon-test", "running", "192.168.64.10", "vnc://127.0.0.1:5901")
    )

    assert lines == [
        "status: running",
        "ip: 192.168.64.10",
        "username: lume",
        "password: lume",
        "vnc: vnc://127.0.0.1:5901",
    ]


def test_vm_controller_start_boots_vm_and_restarts_talon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller = VmController("talon-test", False)
    probe_calls: list[float] = []
    restart_calls: list[tuple[bool, bool]] = []
    running_vm = _running_vm()

    _set_vm_statuses(monkeypatch, ("stopped", None))
    monkeypatch.setattr(
        vm_module.lume,
        "spawn_vm",
        lambda vm, debug=False: _fake_launch(),
    )
    monkeypatch.setattr(
        vm_module.lume,
        "wait_for_running_vm",
        lambda vm, timeout, debug=False, launch=None: VmInfo(
            vm, "running", "192.168.64.10"
        ),
    )
    monkeypatch.setattr(
        vm_controller,
        "_running_vm_from_info",
        lambda info: running_vm,
    )
    monkeypatch.setattr(
        running_vm,
        "probe_ssh",
        lambda *, timeout=0: probe_calls.append(timeout),
    )
    monkeypatch.setattr(
        running_vm,
        "restart_talon",
        lambda *, wipe_user_dir, clean_logs: restart_calls.append(
            (wipe_user_dir, clean_logs)
        ),
    )
    monkeypatch.setattr(
        vm_module.lume,
        "cleanup_launch_log",
        lambda log_path: None,
    )

    info = vm_controller.start()

    assert info is running_vm
    assert probe_calls == [vm_module.SSH_TIMEOUT_SECONDS]
    assert restart_calls == [(True, True)]


def test_vm_controller_start_cleans_up_failed_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller = VmController("talon-test", True)
    calls: list[object] = []
    running_vm = _running_vm(debug=True)

    _set_vm_statuses(monkeypatch, ("stopped", None))
    monkeypatch.setattr(
        vm_module.lume,
        "spawn_vm",
        lambda vm, debug=False: _fake_launch(),
    )
    monkeypatch.setattr(
        vm_module.lume,
        "wait_for_running_vm",
        lambda vm, timeout, debug=False, launch=None: VmInfo(
            vm, "running", "192.168.64.10"
        ),
    )

    monkeypatch.setattr(
        vm_controller,
        "_running_vm_from_info",
        lambda info: running_vm,
    )

    def fail_probe(*, timeout: float = 0.0) -> None:
        del timeout
        raise vm_module.TransportError("ssh failed: 192.168.64.10")

    monkeypatch.setattr(running_vm, "probe_ssh", fail_probe)
    monkeypatch.setattr(
        vm_module.lume,
        "stop_vm",
        lambda vm, debug=False: calls.append(("stop_vm", vm)),
    )
    monkeypatch.setattr(
        vm_module.lume,
        "wait_for_status",
        lambda vm, status, timeout, debug=False: (
            calls.append(("wait_for_status", timeout)) or VmInfo(vm, "stopped", None)
        ),
    )

    with pytest.raises(click.ClickException, match="ssh failed: 192.168.64.10"):
        vm_controller.start()

    assert calls == [("stop_vm", "talon-test"), ("wait_for_status", 30.0)]


def test_running_vm_restart_talon_waits_for_repl_and_sleeps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running_vm = _running_vm()
    calls: list[tuple[str, object]] = []
    sleeps: list[float] = []

    monkeypatch.setattr(
        running_vm,
        "run_shell",
        lambda command, **kwargs: (
            calls.append((running_vm.ip_address, command))
            or subprocess.CompletedProcess([], 0, "", "")
        ),
    )
    monkeypatch.setattr(
        running_vm,
        "wait_for_talon_repl",
        lambda **kwargs: calls.append((running_vm.ip_address, "wait_for_talon_repl")),
    )
    monkeypatch.setattr(vm_module.time, "sleep", lambda seconds: sleeps.append(seconds))

    running_vm.restart_talon(
        wipe_user_dir=True,
        clean_logs=True,
    )

    assert calls[0] == ("192.168.64.10", "pkill -x Talon >/dev/null 2>&1 || true")
    assert calls[-1] == ("192.168.64.10", "wait_for_talon_repl")
    assert sleeps == [vm_module.TALON_POST_RESTART_SETTLE_SECONDS]


def test_vm_controller_stop_falls_back_to_force_stop_for_stuck_vm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller = VmController("talon-test", False)
    calls: list[tuple[str, object]] = []
    running_vm = _running_vm()

    monkeypatch.setattr(
        vm_module.lume,
        "get_vm_info",
        lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"),
    )
    monkeypatch.setattr(vm_controller, "_running_vm_from_info", lambda info: running_vm)
    monkeypatch.setattr(
        running_vm,
        "logout_guest_session",
        lambda: calls.append(("logout_guest_session", running_vm.ip_address)),
    )
    monkeypatch.setattr(
        vm_module.lume,
        "stop_vm",
        lambda vm, debug=False: calls.append(("stop_vm", vm)),
    )

    def fake_wait_for_status(
        vm: str, status: str, timeout: float, debug: bool = False
    ) -> VmInfo:
        del status, debug
        calls.append(("wait_for_status", timeout))
        if timeout == 60.0:
            raise lume_module.LumeError(
                "Timed out waiting for VM to reach status stopped: talon-test"
            )
        return VmInfo(vm, "stopped", None)

    monkeypatch.setattr(vm_module.lume, "wait_for_status", fake_wait_for_status)
    monkeypatch.setattr(
        vm_module.lume,
        "force_stop_vm",
        lambda vm, debug=False: calls.append(("force_stop_vm", vm)),
    )

    vm_controller.stop()

    assert calls == [
        ("logout_guest_session", "192.168.64.10"),
        ("stop_vm", "talon-test"),
        ("wait_for_status", 60.0),
        ("force_stop_vm", "talon-test"),
        ("wait_for_status", 20.0),
    ]


def test_write_smoke_test_bundle_includes_action_docstring(tmp_path: Path) -> None:
    vm_controller, _, _ = _build_service_stack()
    runner = SmokeTestRunner(vm_controller)

    runner.write_bundle(tmp_path, "/tmp/marker.txt", "token")

    assert "user.talonbox_smoke_test()" in (
        tmp_path / "talonbox_smoke_test.talon"
    ).read_text(encoding="utf-8")
    assert '"""Write the talonbox smoke-test marker file."""' in (
        tmp_path / "talonbox_smoke_test.py"
    ).read_text(encoding="utf-8")


def test_trigger_smoke_test_visual_change_uses_guest_dialog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller, _, _ = _build_service_stack()
    runner = SmokeTestRunner(vm_controller)
    calls: list[tuple[str, str]] = []
    running_vm = _running_vm()
    monkeypatch.setattr(
        running_vm,
        "run_shell",
        lambda command, **kwargs: (
            calls.append((running_vm.ip_address, command))
            or subprocess.CompletedProcess([], 0, "", "")
        ),
    )

    runner.trigger_visual_change(running_vm, "abc123")

    assert calls == [
        (
            "192.168.64.10",
            'nohup osascript -e \'display dialog "talonbox screenshot test abc123" '
            'buttons {"OK"} default button 1 giving up after 15\' '
            ">/tmp/talonbox-smoke-test-dialog-abc123.log 2>&1 & sleep 1",
        )
    ]


def test_verify_smoke_test_screenshots_differ_rejects_identical_files(
    tmp_path: Path,
) -> None:
    vm_controller, _, _ = _build_service_stack()
    runner = SmokeTestRunner(vm_controller)
    before = tmp_path / "before.png"
    after = tmp_path / "after.png"
    before.write_bytes(b"same")
    after.write_bytes(b"same")

    with pytest.raises(click.ClickException, match="did not change"):
        runner.verify_screenshots_differ(before, after)


def test_smoke_test_runner_cancellation_leaves_running_vm_untouched(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vm_controller, _, _ = _build_service_stack()
    runner = SmokeTestRunner(vm_controller)

    monkeypatch.setattr(
        vm_controller,
        "get_vm",
        lambda: VmInfo("talon-test", "running", "192.168.64.10"),
    )
    monkeypatch.setattr(
        vm_controller,
        "stop",
        lambda: pytest.fail("stop should not be called"),
    )

    with pytest.raises(click.exceptions.Exit) as error:
        runner.run(yes=False, confirm=lambda prompt, default=False: False)

    captured = capsys.readouterr()
    assert error.value.exit_code == 1
    assert "VM talon-test is already running." in captured.out
    assert "FAIL smoke-test canceled by user; VM left running." in captured.out


def test_smoke_test_runner_success_runs_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vm_controller, _, _ = _build_service_stack()
    runner = SmokeTestRunner(vm_controller, host_output_root=tmp_path.resolve())
    steps: list[str] = []
    running_vm = _running_vm()
    transfer_service = TransferService(running_vm)

    states = [VmInfo("talon-test", "stopped", None)]
    monkeypatch.setattr(
        vm_controller,
        "get_vm",
        lambda: states[0],
    )
    monkeypatch.setattr(
        vm_controller,
        "start",
        lambda: steps.append("start") or running_vm,
    )
    monkeypatch.setattr(
        runner,
        "_build_transfer_service",
        lambda running_vm_arg: transfer_service,
    )
    monkeypatch.setattr(
        transfer_service,
        "rsync",
        lambda args: steps.append("rsync") or 0,
    )
    monkeypatch.setattr(
        vm_controller,
        "restart_talon",
        lambda *, wipe_user_dir, clean_logs: steps.append(
            f"restart:{wipe_user_dir}:{clean_logs}"
        ),
    )

    class FakeClient:
        def mimic(self, command: str) -> None:
            steps.append(f"mimic:{command}")

        def capture_screenshot(self, path: Path) -> None:
            steps.append(f"capture:{path.name}")
            path.write_bytes(b"\x89PNG\r\n\x1a\npayload")

    monkeypatch.setattr(
        runner,
        "_build_talon_client",
        lambda running_vm_arg, transfer_service_arg: FakeClient(),
    )
    monkeypatch.setattr(
        runner,
        "verify_marker",
        lambda running_vm_arg, marker_path, token: steps.append("verify_marker"),
    )
    monkeypatch.setattr(
        runner,
        "trigger_visual_change",
        lambda running_vm_arg, token: steps.append("show_dialog"),
    )
    monkeypatch.setattr(
        runner,
        "verify_screenshots_differ",
        lambda before, after: steps.append("verify_diff"),
    )
    monkeypatch.setattr(
        vm_controller,
        "stop",
        lambda: steps.append("stop"),
    )

    runner.run(yes=False)

    captured = capsys.readouterr()
    assert "ARTIFACT " in captured.out
    assert "PASS Smoke test completed successfully." in captured.out
    assert steps == [
        "start",
        "rsync",
        "restart:False:True",
        "mimic:talonbox smoke test",
        "verify_marker",
        "capture:screenshot-before-dialog.png",
        "show_dialog",
        "capture:screenshot-after-dialog.png",
        "verify_diff",
        "stop",
    ]
    artifact_dir = next(tmp_path.iterdir())
    assert (artifact_dir / "bundle" / "talonbox_smoke_test.talon").exists()


def test_smoke_test_runner_failure_after_start_still_stops_vm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vm_controller, _, _ = _build_service_stack()
    runner = SmokeTestRunner(vm_controller, host_output_root=tmp_path.resolve())
    stop_calls: list[str] = []
    transfer_service = TransferService(_running_vm())

    monkeypatch.setattr(
        vm_controller,
        "get_vm",
        lambda: VmInfo("talon-test", "stopped", None),
    )
    monkeypatch.setattr(
        vm_controller,
        "start",
        lambda: _running_vm(),
    )
    monkeypatch.setattr(
        runner,
        "_build_transfer_service",
        lambda running_vm_arg: transfer_service,
    )
    monkeypatch.setattr(transfer_service, "rsync", lambda args: 0)
    monkeypatch.setattr(
        vm_controller,
        "restart_talon",
        lambda *, wipe_user_dir, clean_logs: (_ for _ in ()).throw(
            click.ClickException("talon restart failed")
        ),
    )
    monkeypatch.setattr(vm_controller, "stop", lambda: stop_calls.append("stop"))

    with pytest.raises(click.exceptions.Exit) as error:
        runner.run(yes=False)

    captured = capsys.readouterr()
    assert error.value.exit_code == 1
    assert (
        "FAIL Restart Talon to load the uploaded bundle: talon restart failed"
        in captured.out
    )
    assert (
        "HINT inspect guest logs at ~/.talon/talon.log and /tmp/talonbox-talon.log."
        in captured.out
    )
    assert stop_calls == ["stop"]


def test_smoke_test_runner_rejects_invalid_screenshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vm_controller, _, _ = _build_service_stack()
    runner = SmokeTestRunner(vm_controller, host_output_root=tmp_path.resolve())
    stop_calls: list[str] = []
    running_vm = _running_vm()
    transfer_service = TransferService(running_vm)

    monkeypatch.setattr(
        vm_controller,
        "get_vm",
        lambda: VmInfo("talon-test", "stopped", None),
    )
    monkeypatch.setattr(
        vm_controller,
        "start",
        lambda: running_vm,
    )
    monkeypatch.setattr(
        runner,
        "_build_transfer_service",
        lambda running_vm_arg: transfer_service,
    )
    monkeypatch.setattr(transfer_service, "rsync", lambda args: 0)
    monkeypatch.setattr(
        vm_controller,
        "restart_talon",
        lambda *, wipe_user_dir, clean_logs: None,
    )

    class FakeClient:
        def mimic(self, command: str) -> None:
            return None

        def capture_screenshot(self, path: Path) -> None:
            path.write_bytes(b"not-a-png")

    monkeypatch.setattr(
        runner,
        "_build_talon_client",
        lambda running_vm_arg, transfer_service_arg: FakeClient(),
    )
    monkeypatch.setattr(
        runner, "verify_marker", lambda running_vm_arg, marker_path, token: None
    )
    monkeypatch.setattr(vm_controller, "stop", lambda: stop_calls.append("stop"))

    with pytest.raises(click.exceptions.Exit) as error:
        runner.run(yes=False)

    captured = capsys.readouterr()
    assert error.value.exit_code == 1
    assert (
        "FAIL Validate the baseline screenshot artifact: Smoke test screenshot was not a PNG file"
        in captured.out
    )
    assert "HINT inspect the saved screenshot at" in captured.out
    assert stop_calls == ["stop"]


def test_transfer_service_rsync_rewrites_guest_destination() -> None:
    _, transfer_service, _ = _build_service_stack()

    args = transfer_service.prepare_rsync_args(
        ["-av", "./repo/", "guest:/Users/lume/.talon/user/repo/"]
    )

    assert args == [
        "-av",
        "./repo/",
        "lume@192.168.64.10:/Users/lume/.talon/user/repo/",
    ]


def test_transfer_service_scp_download_rewrites_guest_source() -> None:
    _, transfer_service, _ = _build_service_stack()

    args = transfer_service.prepare_scp_args(["guest:/tmp/out.png", "/tmp/out.png"])

    assert args == [
        "lume@192.168.64.10:/tmp/out.png",
        str(Path("/tmp/out.png").resolve(strict=False)),
    ]


def test_transfer_service_rejects_transport_override() -> None:
    _, transfer_service, _ = _build_service_stack()

    with pytest.raises(click.ClickException, match="Option not allowed"):
        transfer_service.prepare_rsync_args(
            ["-e", "ssh", "./repo/", "guest:/tmp/repo/"]
        )


def test_transfer_service_allows_rsync_host_write_flag_inside_sandbox() -> None:
    _, transfer_service, _ = _build_service_stack()

    args = transfer_service.prepare_rsync_args(
        ["--log-file=/tmp/talonbox-rsync.log", "./repo/", "guest:/tmp/repo/"]
    )

    assert args == [
        "--log-file=/tmp/talonbox-rsync.log",
        "./repo/",
        "lume@192.168.64.10:/tmp/repo/",
    ]


def test_transfer_service_rejects_guest_to_guest() -> None:
    _, transfer_service, _ = _build_service_stack()

    with pytest.raises(click.ClickException, match="Guest-to-guest"):
        transfer_service.prepare_scp_args(["guest:/tmp/a", "guest:/tmp/b"])


def test_transfer_service_rejects_local_to_local() -> None:
    _, transfer_service, _ = _build_service_stack()

    with pytest.raises(
        click.ClickException, match="Local-to-local transfers are not allowed"
    ):
        transfer_service.prepare_rsync_args(
            ["-av", "./repo/", "/Users/lume/.talon/user/repo/"]
        )


def test_transfer_service_rejects_symlink_escape_from_tmp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, transfer_service, _ = _build_service_stack()
    escape_root = tmp_path.resolve()
    outside_dir = tmp_path.parent / "outside"
    outside_dir.mkdir()
    (escape_root / "link").symlink_to(outside_dir, target_is_directory=True)

    monkeypatch.setattr(transfer_service, "_host_output_root", lambda: escape_root)

    with pytest.raises(
        click.ClickException, match="Symlinks that escape /tmp are not allowed."
    ):
        transfer_service.prepare_rsync_args(
            ["-av", "guest:/tmp/out.txt", str(escape_root / "link" / "out.txt")]
        )


def test_exec_command_runs_guest_shell_and_propagates_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    running_vm = _running_vm()
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


def test_talon_client_repl_waits_for_socket_then_runs_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller, transfer_service, talon_client = _build_service_stack()
    waits: list[tuple[str, float]] = []
    payloads: list[tuple[str, str, bool]] = []

    monkeypatch.setattr(
        talon_client.running_vm,
        "wait_for_talon_repl",
        lambda *, timeout=vm_module.TALON_REPL_TIMEOUT_SECONDS: waits.append(
            (talon_client.running_vm.ip_address, timeout)
        ),
    )
    monkeypatch.setattr(
        talon_client.running_vm,
        "run_repl",
        lambda payload, stream_output=False: (
            payloads.append(
                (talon_client.running_vm.ip_address, payload, stream_output)
            )
            or subprocess.CompletedProcess([], 0, "", "")
        ),
    )

    talon_client.repl("if True:\n    print(1)\nprint(2)\n")

    assert waits == [("192.168.64.10", vm_module.TALON_REPL_TIMEOUT_SECONDS)]
    assert payloads == [
        (
            "192.168.64.10",
            "exec('if True:\\n    print(1)\\nprint(2)\\n')\n",
            True,
        )
    ]


def test_talon_client_mimic_uses_python_escaped_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller, transfer_service, talon_client = _build_service_stack()
    waits: list[tuple[str, float]] = []
    payloads: list[str] = []

    monkeypatch.setattr(
        talon_client.running_vm,
        "wait_for_talon_repl",
        lambda *, timeout=vm_module.TALON_REPL_TIMEOUT_SECONDS: waits.append(
            (talon_client.running_vm.ip_address, timeout)
        ),
    )
    monkeypatch.setattr(
        talon_client.running_vm,
        "run_repl",
        lambda payload, stream_output=False: (
            payloads.append(payload) or subprocess.CompletedProcess([], 0, "", "")
        ),
    )

    talon_client.mimic('say "hello"\nworld')

    assert waits == [("192.168.64.10", vm_module.TALON_REPL_TIMEOUT_SECONDS)]
    assert payloads == ["mimic('say \"hello\"\\nworld')\n"]


def test_talon_client_screenshot_uses_talon_capture_and_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    vm_controller, transfer_service, talon_client = _build_service_stack()
    repl_payloads: list[str] = []
    downloads: list[tuple[str, str, Path]] = []
    cleanup_commands: list[str] = []
    target = tmp_path / "shots" / "screen.png"

    monkeypatch.setattr(
        transfer_service, "_host_output_root", lambda: tmp_path.resolve()
    )
    monkeypatch.setattr(
        talon_client.running_vm,
        "wait_for_talon_repl",
        lambda *, timeout=0: None,
    )
    monkeypatch.setattr(
        talon_client.running_vm,
        "run_repl",
        lambda payload, stream_output=False: (
            repl_payloads.append(payload) or subprocess.CompletedProcess([], 0, "", "")
        ),
    )
    monkeypatch.setattr(
        talon_client.running_vm,
        "download",
        lambda remote, local: (
            downloads.append((talon_client.running_vm.ip_address, remote, local))
            or local.write_bytes(b"not-a-png")
        ),
    )
    monkeypatch.setattr(
        talon_client.running_vm,
        "run_shell",
        lambda command, **kwargs: (
            cleanup_commands.append(command)
            or subprocess.CompletedProcess([], 0, "", "")
        ),
    )

    talon_client.capture_screenshot(target)

    assert target.parent.exists()
    assert "screen.capture_rect(screen.main().rect, retina=False)" in repl_payloads[0]
    assert (
        "img.save(path) if hasattr(img, 'save') else img.write_file(path)"
        in repl_payloads[0]
    )
    assert downloads[0][0] == "192.168.64.10"
    assert downloads[0][2] == target
    assert cleanup_commands[0].startswith('rm -f "/tmp/talonbox-screenshot-')


def test_talon_client_screenshot_rejects_output_outside_tmp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller, transfer_service, talon_client = _build_service_stack()

    with pytest.raises(
        click.ClickException, match="Local output paths must stay under /tmp"
    ):
        talon_client.capture_screenshot(Path("/Users/jwstout/Desktop/guest-screen.png"))


def test_get_vm_info_surfaces_raw_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        lume_module,
        "_run_lume",
        lambda args, debug=False, capture_output=True: subprocess.CompletedProcess(
            args, 0, '{"bad"', ""
        ),
    )

    with pytest.raises(
        lume_module.LumeError,
        match=r'Invalid JSON from `lume ls --format json`: \{"bad"',
    ):
        lume_module.get_vm_info("talon-test")


def test_get_vm_info_tolerates_log_line_before_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    noisy_output = """[2026-03-11T06:55:51Z] INFO: Cleaned up stale session file name=talon-test
[
  {
    "name": "talon-test",
    "status": "stopped",
    "ipAddress": null
  }
]
"""
    monkeypatch.setattr(
        lume_module,
        "_run_lume",
        lambda args, debug=False, capture_output=True: subprocess.CompletedProcess(
            args, 0, noisy_output, ""
        ),
    )

    info = lume_module.get_vm_info("talon-test")

    assert info == VmInfo("talon-test", "stopped", None)


def test_get_vm_info_reads_vnc_url(monkeypatch: pytest.MonkeyPatch) -> None:
    output = """[
  {
    "name": "talon-test",
    "status": "running",
    "ipAddress": "192.168.64.10",
    "vncUrl": "vnc://127.0.0.1:5901"
  }
]
"""
    monkeypatch.setattr(
        lume_module,
        "_run_lume",
        lambda args, debug=False, capture_output=True: subprocess.CompletedProcess(
            args, 0, output, ""
        ),
    )

    info = lume_module.get_vm_info("talon-test")

    assert info == VmInfo(
        "talon-test", "running", "192.168.64.10", "vnc://127.0.0.1:5901"
    )


def test_wait_for_running_vm_reports_launch_log_when_lume_run_exits_early(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_path = tmp_path / "lume-run.log"
    log_path.write_text("permission denied\nconfig.json\n", encoding="utf-8")
    launch = lume_module.VmLaunch(
        process=cast(
            subprocess.Popen[bytes], type("Process", (), {"poll": lambda self: 1})()
        ),
        log_path=log_path,
    )
    monkeypatch.setattr(
        lume_module,
        "get_vm_info",
        lambda name, debug=False: VmInfo(name, "stopped", None),
    )

    with pytest.raises(lume_module.LumeError, match="permission denied"):
        lume_module.wait_for_running_vm(
            "talon-test",
            timeout=1.0,
            interval=0.0,
            launch=launch,
        )


def test_transfer_service_rsync_uses_fixed_vm_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[list[str]] = []
    _, transfer_service, _ = _build_service_stack()
    monkeypatch.setattr(
        transfer_service,
        "_sandbox_command_prefix",
        lambda: ["sandbox-exec", "-p", "(profile)"],
    )

    def fake_run(
        cmd: list[str], check: bool = False
    ) -> subprocess.CompletedProcess[bytes]:
        recorded.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("talonbox.transfer.subprocess.run", fake_run)

    returncode = transfer_service.rsync(["-av", "src/", "guest:/tmp/dest"])

    assert returncode == 0
    assert recorded == [
        [
            "sandbox-exec",
            "-p",
            "(profile)",
            "rsync",
            "-e",
            "sshpass -p lume ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o BatchMode=no -o NumberOfPasswordPrompts=1 -o PasswordAuthentication=yes -o KbdInteractiveAuthentication=no -o PreferredAuthentications=password -o PubkeyAuthentication=no",
            "-av",
            "src/",
            "lume@192.168.64.10:/tmp/dest",
        ]
    ]


def test_transfer_service_scp_uses_fixed_vm_ssh_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[list[str]] = []
    _, transfer_service, _ = _build_service_stack()
    monkeypatch.setattr(
        transfer_service,
        "_sandbox_command_prefix",
        lambda: ["sandbox-exec", "-p", "(profile)"],
    )

    def fake_run(
        cmd: list[str], check: bool = False
    ) -> subprocess.CompletedProcess[bytes]:
        recorded.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("talonbox.transfer.subprocess.run", fake_run)

    returncode = transfer_service.scp(["./settings.talon", "guest:/tmp/settings.talon"])

    assert returncode == 0
    assert recorded == [
        [
            "sandbox-exec",
            "-p",
            "(profile)",
            "sshpass",
            "-p",
            "lume",
            "scp",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "BatchMode=no",
            "-o",
            "NumberOfPasswordPrompts=1",
            "-o",
            "PasswordAuthentication=yes",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "PreferredAuthentications=password",
            "-o",
            "PubkeyAuthentication=no",
            "./settings.talon",
            "lume@192.168.64.10:/tmp/settings.talon",
        ]
    ]


def test_transfer_service_sandbox_profile_allows_tmp_and_dev(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, transfer_service, _ = _build_service_stack()

    monkeypatch.setattr(transfer_module, "HOST_OUTPUT_ROOT", Path("/tmp"))
    monkeypatch.setattr(
        transfer_service, "_host_output_root", lambda: Path("/private/tmp")
    )

    profile = transfer_service._sandbox_profile()

    assert "(deny file-write*)" in profile
    assert '(allow file-write* (subpath "/private/tmp"))' in profile
    assert '(allow file-write* (subpath "/tmp"))' in profile
    assert '(allow file-write* (subpath "/dev"))' in profile


def test_running_vm_download_uses_scp(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[list[str]] = []
    running_vm = _running_vm()

    def fake_run(
        cmd: list[str],
        check: bool = False,
        capture_output: bool = True,
        text: bool = True,
        timeout: float | None = None,
        stdin: object | None = None,
        input: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del timeout, stdin, input
        recorded.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("talonbox.vm.subprocess.run", fake_run)

    running_vm.download("/tmp/out.png", Path("/tmp/out.png"))

    assert recorded == [
        [
            "sshpass",
            "-p",
            "lume",
            "scp",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "BatchMode=no",
            "-o",
            "NumberOfPasswordPrompts=1",
            "-o",
            "PasswordAuthentication=yes",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "PreferredAuthentications=password",
            "-o",
            "PubkeyAuthentication=no",
            "lume@192.168.64.10:/tmp/out.png",
            "/tmp/out.png",
        ]
    ]


def test_running_vm_run_repl_retries_transient_ssh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = {"count": 0}
    running_vm = _running_vm()

    def fake_run(**kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        attempts["count"] += 1
        if attempts["count"] == 1:
            return subprocess.CompletedProcess(
                [],
                255,
                "",
                "ssh_askpass: exec(/usr/X11R6/bin/ssh-askpass): No such file or directory\n"
                "lume@192.168.64.10: Permission denied (publickey,password,keyboard-interactive).",
            )
        return subprocess.CompletedProcess([], 0, "ok\n", "")

    monkeypatch.setattr(
        "talonbox.vm.subprocess.run", lambda *args, **kwargs: fake_run(**kwargs)
    )
    monkeypatch.setattr("talonbox.vm.time.sleep", lambda seconds: None)

    result = running_vm.run_repl("print('ok')\n")

    assert result.returncode == 0
    assert attempts["count"] == 2


def test_running_vm_download_retries_transient_ssh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = {"count": 0}
    running_vm = _running_vm()

    def fake_run(**kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        attempts["count"] += 1
        if attempts["count"] == 1:
            return subprocess.CompletedProcess(
                [],
                255,
                "",
                "ssh_askpass: exec(/usr/X11R6/bin/ssh-askpass): No such file or directory\n"
                "lume@192.168.64.10: Permission denied (publickey,password,keyboard-interactive).",
            )
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(
        "talonbox.vm.subprocess.run", lambda *args, **kwargs: fake_run(**kwargs)
    )
    monkeypatch.setattr("talonbox.vm.time.sleep", lambda seconds: None)

    running_vm.download("/tmp/out.png", Path("/tmp/out.png"))

    assert attempts["count"] == 2


def test_running_vm_wait_for_talon_repl_checks_socket_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running_vm = _running_vm()
    calls: list[tuple[str | list[str], float, bool, bool]] = []

    def fake_run_shell(
        command: str | list[str],
        *,
        timeout: float | None = None,
        poll: bool = False,
        stream: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del stream
        calls.append((command, timeout or 0.0, poll, check))
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(running_vm, "run_shell", fake_run_shell)

    running_vm.wait_for_talon_repl(timeout=12.0)

    assert calls == [('test -S "$HOME/.talon/.sys/repl.sock"', 12.0, True, True)]
