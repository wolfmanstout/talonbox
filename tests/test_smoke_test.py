from __future__ import annotations

import subprocess
from pathlib import Path

import click
import pytest

from talonbox.lume import VmInfo
from talonbox.smoke_test import SmokeTestRunner
from talonbox.transfer import TransferService
from tests.helpers import build_service_stack, running_vm_fixture


def test_write_smoke_test_bundle_includes_action_docstring(tmp_path: Path) -> None:
    vm_controller, _, _ = build_service_stack()
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
    vm_controller, _, _ = build_service_stack()
    runner = SmokeTestRunner(vm_controller)
    calls: list[tuple[str, str]] = []
    running_vm = running_vm_fixture()
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
    vm_controller, _, _ = build_service_stack()
    runner = SmokeTestRunner(vm_controller)
    before = tmp_path / "before.png"
    after = tmp_path / "after.png"
    before.write_bytes(b"same")
    after.write_bytes(b"same")

    with pytest.raises(click.ClickException, match="did not change"):
        runner.verify_screenshots_differ(before, after)


def test_smoke_test_runner_rejects_running_source_without_mutating_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vm_controller, _, _ = build_service_stack()
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
    monkeypatch.setattr(
        vm_controller,
        "clone",
        lambda dest: pytest.fail("clone should not be called"),
    )

    with pytest.raises(click.exceptions.Exit) as error:
        runner.run()

    captured = capsys.readouterr()
    assert error.value.exit_code == 1
    assert "Source VM must be stopped before smoke-test" in captured.out


def test_smoke_test_runner_success_runs_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vm_controller, _, _ = build_service_stack()
    runner = SmokeTestRunner(vm_controller, host_output_root=tmp_path.resolve())
    steps: list[str] = []
    running_vm = running_vm_fixture()
    transfer_service = TransferService(running_vm)

    states = [VmInfo("talon-test", "stopped", None)]
    monkeypatch.setattr(
        vm_controller,
        "get_vm",
        lambda: states[0],
    )
    temp_controller = vm_controller.for_vm("temp")
    monkeypatch.setattr(
        vm_controller,
        "for_vm",
        lambda name: steps.append(f"for_vm:{name.startswith('smoke-test-')}")
        or temp_controller,
    )
    monkeypatch.setattr(
        vm_controller,
        "clone",
        lambda dest: steps.append(f"clone:{dest.startswith('smoke-test-')}"),
    )
    monkeypatch.setattr(
        temp_controller,
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
        temp_controller,
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
        temp_controller,
        "stop",
        lambda: steps.append("stop"),
    )
    monkeypatch.setattr(
        temp_controller,
        "delete",
        lambda: steps.append("delete"),
    )

    runner.run()

    captured = capsys.readouterr()
    assert "ARTIFACT " in captured.out
    assert "PASS Smoke test completed successfully." in captured.out
    assert steps == [
        "for_vm:True",
        "clone:True",
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
        "delete",
    ]
    artifact_dir = next(tmp_path.iterdir())
    assert (artifact_dir / "bundle" / "talonbox_smoke_test.talon").exists()


def test_smoke_test_runner_failure_after_start_still_stops_vm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vm_controller, _, _ = build_service_stack()
    runner = SmokeTestRunner(vm_controller, host_output_root=tmp_path.resolve())
    stop_calls: list[str] = []
    transfer_service = TransferService(running_vm_fixture())

    monkeypatch.setattr(
        vm_controller,
        "get_vm",
        lambda: VmInfo("talon-test", "stopped", None),
    )
    temp_controller = vm_controller.for_vm("temp")
    monkeypatch.setattr(vm_controller, "for_vm", lambda name: temp_controller)
    monkeypatch.setattr(vm_controller, "clone", lambda dest: None)
    monkeypatch.setattr(
        temp_controller,
        "start",
        lambda: running_vm_fixture(),
    )
    monkeypatch.setattr(
        runner,
        "_build_transfer_service",
        lambda running_vm_arg: transfer_service,
    )
    monkeypatch.setattr(transfer_service, "rsync", lambda args: 0)
    monkeypatch.setattr(
        temp_controller,
        "restart_talon",
        lambda *, wipe_user_dir, clean_logs: (_ for _ in ()).throw(
            click.ClickException("talon restart failed")
        ),
    )
    monkeypatch.setattr(temp_controller, "stop", lambda: stop_calls.append("stop"))
    monkeypatch.setattr(temp_controller, "delete", lambda: stop_calls.append("delete"))

    with pytest.raises(click.exceptions.Exit) as error:
        runner.run()

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
    assert stop_calls == ["stop", "delete"]


def test_smoke_test_runner_rejects_invalid_screenshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vm_controller, _, _ = build_service_stack()
    runner = SmokeTestRunner(vm_controller, host_output_root=tmp_path.resolve())
    stop_calls: list[str] = []
    running_vm = running_vm_fixture()
    transfer_service = TransferService(running_vm)

    monkeypatch.setattr(
        vm_controller,
        "get_vm",
        lambda: VmInfo("talon-test", "stopped", None),
    )
    temp_controller = vm_controller.for_vm("temp")
    monkeypatch.setattr(vm_controller, "for_vm", lambda name: temp_controller)
    monkeypatch.setattr(vm_controller, "clone", lambda dest: None)
    monkeypatch.setattr(
        temp_controller,
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
        temp_controller,
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
    monkeypatch.setattr(temp_controller, "stop", lambda: stop_calls.append("stop"))
    monkeypatch.setattr(temp_controller, "delete", lambda: stop_calls.append("delete"))

    with pytest.raises(click.exceptions.Exit) as error:
        runner.run()

    captured = capsys.readouterr()
    assert error.value.exit_code == 1
    assert (
        "FAIL Validate the baseline screenshot artifact: Smoke test screenshot was not a PNG file"
        in captured.out
    )
    assert "HINT inspect the saved screenshot at" in captured.out
    assert stop_calls == ["stop", "delete"]
