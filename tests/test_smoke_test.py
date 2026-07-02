from __future__ import annotations

import subprocess
from pathlib import Path

import click
import pytest

from talonbox.smoke_test import SmokeTestRunner
from talonbox.tart import VmInfo
from talonbox.transfer import TransferService
from tests.helpers import build_service_stack, running_vm_fixture


def write_ppm(path: Path, width: int, height: int, pixels: bytes) -> None:
    path.write_bytes(f"P6\n{width} {height}\n255\n".encode() + pixels)


@pytest.fixture(autouse=True)
def forbid_real_tart_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_run_tart(*args: object, **kwargs: object) -> None:
        pytest.fail("smoke-test unit tests must mock Tart interactions")

    monkeypatch.setattr("talonbox.tart._run_tart", fail_run_tart)


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


def test_trigger_smoke_test_visual_change_calls_talon_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller, _, talon_client = build_service_stack()
    runner = SmokeTestRunner(vm_controller)
    calls: list[str] = []
    monkeypatch.setattr(talon_client, "repl", lambda code: calls.append(code))

    runner.trigger_visual_change(talon_client)

    assert calls == [
        "from talon import actions\n"
        "actions.user.talonbox_smoke_test_show_visual_marker()"
    ]


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
    monkeypatch.setattr(
        runner,
        "diagnose_mimic_failure",
        lambda running_vm_arg,
        marker_path,
        token: " Confirm a speech model is selected.",
    )
    monkeypatch.setattr("talonbox.smoke_test.time.sleep", lambda delay: None)

    with pytest.raises(click.ClickException, match="speech model"):
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


def test_run_marker_mimic_until_verified_diagnoses_missing_speech_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller, _, _ = build_service_stack()
    runner = SmokeTestRunner(vm_controller)
    running_vm = running_vm_fixture()

    class FakeClient:
        def mimic(self, command: str) -> None:
            return None

    monkeypatch.setattr(
        runner,
        "verify_marker",
        lambda running_vm_arg, marker_path, token: (_ for _ in ()).throw(
            click.ClickException("marker missing")
        ),
    )
    monkeypatch.setattr("talonbox.smoke_test.time.sleep", lambda delay: None)
    monkeypatch.setattr(
        running_vm,
        "run_repl",
        lambda payload: subprocess.CompletedProcess(
            [],
            0,
            "talonbox_direct_action_ok=True\n",
            "",
        ),
    )

    with pytest.raises(click.ClickException) as error:
        runner.run_marker_mimic_until_verified(
            FakeClient(),
            running_vm,
            "/tmp/marker.txt",
            "token",
            attempts=1,
            retry_delay=0,
        )

    assert "action is loaded and works when called directly" in error.value.message
    assert "Speech Recognition" in error.value.message
    assert "selected model" in error.value.message


def test_verify_visual_marker_present_accepts_marker_colors(tmp_path: Path) -> None:
    vm_controller, _, _ = build_service_stack()
    runner = SmokeTestRunner(vm_controller)
    screenshot = tmp_path / "marker.ppm"
    magenta = bytes([255, 0, 255]) * 60
    green = bytes([0, 255, 0]) * 60
    other = bytes([1, 2, 3]) * 280
    write_ppm(screenshot, 20, 20, magenta + green + other)

    runner.verify_visual_marker_present(screenshot)


def test_verify_visual_marker_present_rejects_unrelated_change(tmp_path: Path) -> None:
    vm_controller, _, _ = build_service_stack()
    runner = SmokeTestRunner(vm_controller)
    screenshot = tmp_path / "icloud.ppm"
    pixels = bytes([255, 255, 255]) * 399 + bytes([0, 0, 0])
    write_ppm(screenshot, 20, 20, pixels)

    with pytest.raises(click.ClickException, match="did not contain"):
        runner.verify_visual_marker_present(screenshot)


def test_verify_talon_capture_matches_vnc_accepts_scaled_match(
    tmp_path: Path,
) -> None:
    vm_controller, _, _ = build_service_stack()
    runner = SmokeTestRunner(vm_controller)
    talon = tmp_path / "talon.ppm"
    vnc = tmp_path / "vnc.ppm"
    black = bytes([0, 0, 0])
    red = bytes([255, 0, 0])
    green = bytes([0, 255, 0])
    blue = bytes([0, 0, 255])
    write_ppm(talon, 2, 2, black + red + green + blue)
    write_ppm(
        vnc,
        4,
        4,
        black * 2
        + red * 2
        + black * 2
        + red * 2
        + green * 2
        + blue * 2
        + green * 2
        + blue * 2,
    )

    runner.verify_talon_capture_matches_vnc(talon, vnc)


def test_verify_talon_capture_matches_vnc_rejects_missing_desktop(
    tmp_path: Path,
) -> None:
    vm_controller, _, _ = build_service_stack()
    runner = SmokeTestRunner(vm_controller)
    talon = tmp_path / "talon.ppm"
    vnc = tmp_path / "vnc.ppm"
    write_ppm(talon, 10, 10, bytes([0, 0, 0]) * 100)
    write_ppm(vnc, 20, 20, bytes([255, 255, 255]) * 400)

    with pytest.raises(click.ClickException, match="did not match the VNC"):
        runner.verify_talon_capture_matches_vnc(talon, vnc)


def test_verify_talon_capture_matches_vnc_rejects_small_overlay_delta(
    tmp_path: Path,
) -> None:
    vm_controller, _, _ = build_service_stack()
    runner = SmokeTestRunner(vm_controller)
    talon = tmp_path / "talon.ppm"
    vnc = tmp_path / "vnc.ppm"
    black = bytes([0, 0, 0])
    white = bytes([255, 255, 255])
    write_ppm(talon, 10, 10, black * 100)
    write_ppm(vnc, 10, 10, white * 3 + black * 97)

    with pytest.raises(click.ClickException, match="mismatched pixel ratio"):
        runner.verify_talon_capture_matches_vnc(talon, vnc)


def test_start_desktop_capture_probe_shows_dialog_asynchronously(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller, _, _ = build_service_stack()
    runner = SmokeTestRunner(vm_controller)
    running_vm = running_vm_fixture()
    calls: list[str] = []
    sleeps: list[float] = []
    monkeypatch.setattr(running_vm, "run_shell", lambda command: calls.append(command))
    monkeypatch.setattr(
        "talonbox.smoke_test.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )

    runner.start_desktop_capture_probe(running_vm, "probe-token")

    assert len(calls) == 1
    assert "nohup osascript -e" in calls[0]
    assert "talonbox desktop capture probe probe-token" in calls[0]
    assert "giving up after 30" in calls[0]
    assert calls[0].endswith(">/tmp/talonbox-desktop-capture-probe.log 2>&1 &")
    assert sleeps == [1.0]


def test_close_desktop_capture_probe_kills_only_smoke_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller, _, _ = build_service_stack()
    runner = SmokeTestRunner(vm_controller)
    running_vm = running_vm_fixture()
    calls: list[str] = []
    monkeypatch.setattr(running_vm, "run_shell", lambda command: calls.append(command))

    runner.close_desktop_capture_probe(running_vm)

    assert calls == [
        "pkill -f 'talonbox desktop capture [p]robe' >/dev/null 2>&1 || true"
    ]


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
    assert "talonbox stop --shutdown talon-test" in captured.out


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
        lambda name: (
            steps.append(f"for_vm:{name.startswith('smoke-test-')}") or temp_controller
        ),
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

        def capture_screenshot(self, path: Path, *, vnc: bool = False) -> None:
            steps.append(f"capture{'-vnc' if vnc else ''}:{path.name}")
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
        "start_desktop_capture_probe",
        lambda running_vm_arg, token: steps.append("show_desktop_probe"),
    )
    monkeypatch.setattr(
        runner,
        "verify_talon_capture_matches_vnc",
        lambda talon_screenshot, vnc_screenshot: steps.append(
            "verify_complete_capture"
        ),
    )
    monkeypatch.setattr(
        runner,
        "close_desktop_capture_probe",
        lambda running_vm_arg: steps.append("close_desktop_probe"),
    )
    monkeypatch.setattr(
        runner,
        "trigger_visual_change",
        lambda talon_client_arg: steps.append("show_visual_change"),
    )
    monkeypatch.setattr(
        runner,
        "verify_visual_marker_present",
        lambda screenshot: steps.append("verify_visual_marker"),
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
        "show_desktop_probe",
        "capture:screenshot-desktop-probe-talon.ppm",
        "capture-vnc:screenshot-desktop-probe-vnc.ppm",
        "capture:screenshot-desktop-probe-talon.png",
        "capture-vnc:screenshot-desktop-probe-vnc.png",
        "close_desktop_probe",
        "verify_complete_capture",
        "mimic_until_marker",
        "capture:screenshot-before-visual-change.png",
        "show_visual_change",
        "capture:screenshot-after-visual-change.png",
        "capture:screenshot-after-visual-change.ppm",
        "verify_visual_marker",
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

        def capture_screenshot(self, path: Path, *, vnc: bool = False) -> None:
            steps.append(f"capture{'-vnc' if vnc else ''}:{path.name}")
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
        "start_desktop_capture_probe",
        lambda running_vm_arg, token: steps.append("show_desktop_probe"),
    )
    monkeypatch.setattr(
        runner,
        "verify_talon_capture_matches_vnc",
        lambda talon_screenshot, vnc_screenshot: steps.append(
            "verify_complete_capture"
        ),
    )
    monkeypatch.setattr(
        runner,
        "close_desktop_capture_probe",
        lambda running_vm_arg: steps.append("close_desktop_probe"),
    )
    monkeypatch.setattr(
        runner,
        "trigger_visual_change",
        lambda talon_client_arg: steps.append("show_visual_change"),
    )
    monkeypatch.setattr(
        runner,
        "verify_visual_marker_present",
        lambda screenshot: steps.append("verify_visual_marker"),
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
        "show_desktop_probe",
        "capture:screenshot-desktop-probe-talon.ppm",
        "capture-vnc:screenshot-desktop-probe-vnc.ppm",
        "capture:screenshot-desktop-probe-talon.png",
        "capture-vnc:screenshot-desktop-probe-vnc.png",
        "close_desktop_probe",
        "verify_complete_capture",
        "mimic_until_marker",
        "capture:screenshot-before-visual-change.png",
        "show_visual_change",
        "capture:screenshot-after-visual-change.png",
        "capture:screenshot-after-visual-change.ppm",
        "verify_visual_marker",
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

        def capture_screenshot(self, path: Path, *, vnc: bool = False) -> None:
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
    monkeypatch.setattr(
        runner,
        "start_desktop_capture_probe",
        lambda running_vm_arg, token: None,
    )
    monkeypatch.setattr(
        runner,
        "verify_talon_capture_matches_vnc",
        lambda talon_screenshot, vnc_screenshot: None,
    )
    monkeypatch.setattr(
        runner,
        "close_desktop_capture_probe",
        lambda running_vm_arg: None,
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
