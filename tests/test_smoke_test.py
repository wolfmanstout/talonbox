from __future__ import annotations

from pathlib import Path

import click
import pytest

from talonbox.lume import VmInfo
from talonbox.smoke_test import SmokeTestRunner
from talonbox.transfer import TransferService
from tests.helpers import build_service_stack, running_vm_fixture


@pytest.fixture(autouse=True)
def forbid_real_lume_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_run_lume(*args: object, **kwargs: object) -> None:
        pytest.fail("smoke-test unit tests must mock Lume interactions")

    monkeypatch.setattr("talonbox.lume._run_lume", fail_run_lume)


def test_write_smoke_test_bundle_includes_marker_and_visual_actions(
    tmp_path: Path,
) -> None:
    vm_controller, _, _ = build_service_stack()
    runner = SmokeTestRunner(vm_controller)

    runner.write_bundle(tmp_path, "/tmp/marker.txt", "token")

    talon_text = (tmp_path / "talonbox_smoke_test.talon").read_text(encoding="utf-8")
    assert "user.talonbox_smoke_test()" in talon_text
    assert "user.talonbox_smoke_test_show_visual_marker()" in talon_text
    python_text = (tmp_path / "talonbox_smoke_test.py").read_text(encoding="utf-8")
    assert '"""Write the talonbox smoke-test marker file."""' in (python_text)
    assert "Canvas.from_screen(screen.main())" in python_text
    assert 'canvas.paint.color = "FF00FF"' in python_text
    assert 'canvas.paint.color = "00FF00"' in python_text


def test_trigger_smoke_test_visual_change_uses_talon_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller, _, talon_client = build_service_stack()
    runner = SmokeTestRunner(vm_controller)
    calls: list[str] = []
    monkeypatch.setattr(talon_client, "mimic", lambda command: calls.append(command))

    runner.trigger_visual_change(talon_client)

    assert calls == ["talonbox smoke visual test"]


def test_run_marker_mimic_until_verified_retries_until_marker_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller, _, _ = build_service_stack()
    runner = SmokeTestRunner(vm_controller)
    running_vm = running_vm_fixture()
    calls: list[str] = []

    class FakeClient:
        def mimic(self, command: str) -> None:
            calls.append(f"mimic:{command}")

    failures_remaining = 1

    def fake_verify_marker(running_vm_arg, marker_path: str, token: str) -> None:
        nonlocal failures_remaining
        calls.append(f"verify:{marker_path}:{token}")
        if failures_remaining:
            failures_remaining -= 1
            raise click.ClickException("marker missing")

    monkeypatch.setattr(runner, "verify_marker", fake_verify_marker)
    monkeypatch.setattr(
        "talonbox.smoke_test.time.sleep",
        lambda delay: calls.append(f"sleep:{delay}"),
    )

    runner.run_marker_mimic_until_verified(
        FakeClient(), running_vm, "/tmp/marker.txt", "token", retry_delay=0.25
    )

    assert calls == [
        "mimic:talonbox smoke test",
        "verify:/tmp/marker.txt:token",
        "sleep:0.25",
        "mimic:talonbox smoke test",
        "verify:/tmp/marker.txt:token",
    ]


def test_run_marker_mimic_until_verified_fails_after_bounded_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller, _, _ = build_service_stack()
    runner = SmokeTestRunner(vm_controller)
    running_vm = running_vm_fixture()
    calls: list[str] = []

    class FakeClient:
        def mimic(self, command: str) -> None:
            calls.append(command)

    monkeypatch.setattr(
        runner,
        "verify_marker",
        lambda running_vm_arg, marker_path, token: (_ for _ in ()).throw(
            click.ClickException("marker missing")
        ),
    )
    monkeypatch.setattr("talonbox.smoke_test.time.sleep", lambda delay: None)

    with pytest.raises(click.ClickException, match="after 3 mimic attempts"):
        runner.run_marker_mimic_until_verified(
            FakeClient(),
            running_vm,
            "/tmp/marker.txt",
            "token",
            attempts=3,
            retry_delay=0,
        )

    assert calls == [
        "talonbox smoke test",
        "talonbox smoke test",
        "talonbox smoke test",
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
    temp_controller = vm_controller.for_vm("temp")

    monkeypatch.setattr(vm_controller, "for_vm", lambda name: temp_controller)
    monkeypatch.setattr(
        temp_controller,
        "get_vm",
        lambda: VmInfo("temp", "stopped", None),
    )
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
        lambda args: steps.append(f"rsync:{args[0]}") or 0,
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
        "run_marker_mimic_until_verified",
        lambda talon_client_arg, running_vm_arg, marker_path, token: steps.append(
            "mimic_until_marker"
        ),
    )
    monkeypatch.setattr(
        runner,
        "trigger_visual_change",
        lambda talon_client_arg: steps.append("show_visual_change"),
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
        "rsync:-a",
        "restart:False:True",
        "mimic_until_marker",
        "capture:screenshot-before-visual-change.png",
        "show_visual_change",
        "capture:screenshot-after-visual-change.png",
        "verify_diff",
        "stop",
        "delete",
    ]
    artifact_dir = next(tmp_path.iterdir())
    assert (artifact_dir / "bundle" / "talonbox_smoke_test.talon").exists()


def test_smoke_test_runner_can_run_in_place_without_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vm_controller, _, _ = build_service_stack()
    runner = SmokeTestRunner(vm_controller, host_output_root=tmp_path.resolve())
    steps: list[str] = []
    running_vm = running_vm_fixture()
    transfer_service = TransferService(running_vm)

    monkeypatch.setattr(
        vm_controller,
        "get_vm",
        lambda: VmInfo("talon-test", "running", "192.168.64.10"),
    )
    monkeypatch.setattr(
        vm_controller,
        "for_vm",
        lambda name: pytest.fail("in-place smoke test should not create a temp VM"),
    )
    monkeypatch.setattr(
        vm_controller,
        "clone",
        lambda dest: pytest.fail("in-place smoke test should not clone"),
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
        lambda args: steps.append(f"rsync:{args[0]}") or 0,
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
        "run_marker_mimic_until_verified",
        lambda talon_client_arg, running_vm_arg, marker_path, token: steps.append(
            "mimic_until_marker"
        ),
    )
    monkeypatch.setattr(
        runner,
        "trigger_visual_change",
        lambda talon_client_arg: steps.append("show_visual_change"),
    )
    monkeypatch.setattr(
        runner,
        "verify_screenshots_differ",
        lambda before, after: steps.append("verify_diff"),
    )
    monkeypatch.setattr(
        vm_controller,
        "stop",
        lambda: pytest.fail("in-place smoke test should leave the VM running"),
    )
    monkeypatch.setattr(
        vm_controller,
        "delete",
        lambda: pytest.fail("in-place smoke test should not delete the VM"),
    )

    runner.run(clone=False)

    captured = capsys.readouterr()
    assert "PASS Smoke test completed successfully." in captured.out
    assert steps == [
        "start",
        "rsync:-a",
        "restart:False:True",
        "mimic_until_marker",
        "capture:screenshot-before-visual-change.png",
        "show_visual_change",
        "capture:screenshot-after-visual-change.png",
        "verify_diff",
    ]


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
    monkeypatch.setattr(
        temp_controller,
        "get_vm",
        lambda: VmInfo("temp", "stopped", None),
    )
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
    monkeypatch.setattr(
        temp_controller,
        "get_vm",
        lambda: VmInfo("temp", "running", "192.168.64.10", "vnc://127.0.0.1:5901"),
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
    assert "HINT open the VM over VNC with `talonbox open temp`." in captured.out
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
    monkeypatch.setattr(
        temp_controller,
        "get_vm",
        lambda: VmInfo("temp", "stopped", None),
    )
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
        runner,
        "run_marker_mimic_until_verified",
        lambda talon_client_arg, running_vm_arg, marker_path, token: None,
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
