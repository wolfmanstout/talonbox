from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, cast

import click
import pytest

from talonbox import vm as vm_module
from tests.helpers import build_service_stack, repl_ok_result, unwrap_repl_payload


def test_talon_client_repl_waits_for_socket_then_runs_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, talon_client = build_service_stack()
    waits: list[tuple[str, float]] = []
    payloads: list[tuple[str, str]] = []

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
        lambda payload: (
            payloads.append((talon_client.running_vm.ip_address, payload))
            or repl_ok_result(payload)
        ),
    )

    talon_client.repl("if True:\n    print(1)\nprint(2)\n")

    assert waits == [("192.168.64.10", vm_module.TALON_REPL_TIMEOUT_SECONDS)]
    assert payloads[0][0] == "192.168.64.10"
    wrapper = unwrap_repl_payload(payloads[0][1])
    assert "exec('if True:\\n    print(1)\\nprint(2)\\n')" in wrapper
    assert "print(traceback.format_exc())" in wrapper


def test_talon_client_mimic_uses_python_escaped_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, talon_client = build_service_stack()
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
        lambda payload: payloads.append(payload) or repl_ok_result(payload),
    )

    talon_client.mimic('say "hello"\nworld')

    assert waits == [("192.168.64.10", vm_module.TALON_REPL_TIMEOUT_SECONDS)]
    inner_code = "mimic('say \"hello\" world')"
    assert f"exec({inner_code!r})" in unwrap_repl_payload(payloads[0])


def test_talon_client_mimic_strips_embedded_speech_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, talon_client = build_service_stack()
    payloads: list[str] = []

    monkeypatch.setattr(
        talon_client.running_vm,
        "wait_for_talon_repl",
        lambda *, timeout=vm_module.TALON_REPL_TIMEOUT_SECONDS: None,
    )
    monkeypatch.setattr(
        talon_client.running_vm,
        "run_repl",
        lambda payload: payloads.append(payload) or repl_ok_result(payload),
    )

    talon_client.mimic("talonbox [[slnc 500]] smoke [[rate 180]] [[volm +0.2]] test")

    assert "mimic('talonbox smoke test')" in unwrap_repl_payload(payloads[0])


def test_talon_client_mimic_audio_synthesizes_replays_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, talon_client = build_service_stack()
    waits: list[tuple[str, float]] = []
    shell_commands: list[str | list[str]] = []
    payloads: list[str] = []

    monkeypatch.setattr(
        talon_client.running_vm,
        "wait_for_talon_repl",
        lambda *, timeout=vm_module.TALON_REPL_TIMEOUT_SECONDS: waits.append(
            (talon_client.running_vm.ip_address, timeout)
        ),
    )

    def fake_run_shell(
        command: str | list[str],
        *,
        timeout: float | None = None,
        poll: bool = False,
        stream: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del timeout, poll, stream, check
        shell_commands.append(command)
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(talon_client.running_vm, "run_shell", fake_run_shell)
    monkeypatch.setattr(
        talon_client.running_vm,
        "run_repl",
        lambda payload: payloads.append(payload) or repl_ok_result(payload),
    )
    talon_client.mimic("talonbox [[slnc 500]] smoke test", audio=True)

    assert waits == [("192.168.64.10", vm_module.TALON_REPL_TIMEOUT_SECONDS)]
    say_command = cast(list[str], shell_commands[0])
    cleanup_command = cast(list[str], shell_commands[1])
    assert say_command[0:4] == [
        "say",
        "-o",
        say_command[2],
        "--data-format=LEI16@16000",
    ]
    assert say_command[4] == "talonbox [[slnc 500]] smoke test"
    assert say_command[2].startswith("/tmp/talonbox-mimic-audio-")
    assert say_command[2].endswith(".wav")
    assert "from talon import actions" in payloads[0]
    assert "actions.speech.replay(path)" in payloads[0]
    assert "time.sleep" not in payloads[0]
    assert cleanup_command == ["rm", "-f", say_command[2]]


def test_talon_client_mimic_audio_wraps_synthesis_failure_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, talon_client = build_service_stack()
    shell_commands: list[str | list[str]] = []

    monkeypatch.setattr(
        talon_client.running_vm,
        "wait_for_talon_repl",
        lambda *, timeout=vm_module.TALON_REPL_TIMEOUT_SECONDS: None,
    )

    def fake_run_shell(
        command: str | list[str],
        *,
        timeout: float | None = None,
        poll: bool = False,
        stream: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del timeout, poll, stream, check
        shell_commands.append(command)
        if isinstance(command, list) and command[0] == "say":
            raise vm_module.RemoteCommandError("say failed")
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(talon_client.running_vm, "run_shell", fake_run_shell)

    with pytest.raises(click.ClickException, match="say failed"):
        talon_client.mimic("talonbox smoke test", audio=True)

    assert len(shell_commands) == 2
    say_command = cast(list[str], shell_commands[0])
    cleanup_command = cast(list[str], shell_commands[1])
    assert cleanup_command == ["rm", "-f", say_command[2]]


def test_talon_client_mimic_audio_propagates_replay_exit_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, talon_client = build_service_stack()
    shell_commands: list[str | list[str]] = []

    monkeypatch.setattr(
        talon_client.running_vm,
        "wait_for_talon_repl",
        lambda *, timeout=vm_module.TALON_REPL_TIMEOUT_SECONDS: None,
    )
    monkeypatch.setattr(
        talon_client.running_vm,
        "run_shell",
        lambda command, **kwargs: (
            shell_commands.append(command) or subprocess.CompletedProcess([], 0, "", "")
        ),
    )
    monkeypatch.setattr(
        talon_client.running_vm,
        "run_repl",
        lambda payload, stream_output=False: subprocess.CompletedProcess(
            [], 7, "", "replay failed"
        ),
    )

    with pytest.raises(click.exceptions.Exit) as error:
        talon_client.mimic("talonbox smoke test", audio=True)

    assert error.value.exit_code == 7
    say_command = cast(list[str], shell_commands[0])
    cleanup_command = cast(list[str], shell_commands[1])
    assert cleanup_command == ["rm", "-f", say_command[2]]


def test_talon_client_click_uses_talon_mouse_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, talon_client = build_service_stack()
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
        lambda payload: payloads.append(payload) or repl_ok_result(payload),
    )

    talon_client.click(123, 456, button="right")

    assert waits == [("192.168.64.10", vm_module.TALON_REPL_TIMEOUT_SECONDS)]
    assert "from talon import ctrl" in payloads[0]
    assert "ctrl.mouse_move(123, 456)" in payloads[0]
    assert "ctrl.mouse_click(button=1)" in payloads[0]


def test_talon_client_type_uses_talon_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, talon_client = build_service_stack()
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
        lambda payload: payloads.append(payload) or repl_ok_result(payload),
    )

    text = 'hello "world"\n'
    talon_client.type_text(text)

    assert waits == [("192.168.64.10", vm_module.TALON_REPL_TIMEOUT_SECONDS)]
    expected_code = f"from talon import actions\nactions.insert({text!r})\n"
    assert f"exec({expected_code!r})" in unwrap_repl_payload(payloads[0])


def test_talon_client_press_key_uses_talon_key_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, talon_client = build_service_stack()
    payloads: list[str] = []

    monkeypatch.setattr(
        talon_client.running_vm,
        "wait_for_talon_repl",
        lambda *, timeout=vm_module.TALON_REPL_TIMEOUT_SECONDS: None,
    )
    monkeypatch.setattr(
        talon_client.running_vm,
        "run_repl",
        lambda payload: payloads.append(payload) or repl_ok_result(payload),
    )

    talon_client.press_key("enter")

    wrapper = unwrap_repl_payload(payloads[0])
    assert "from talon import actions" in wrapper
    assert "actions.key('enter')" in wrapper


def test_talon_client_screenshot_uses_talon_capture_and_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, transfer_service, talon_client = build_service_stack()
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
        lambda payload: repl_payloads.append(payload) or repl_ok_result(payload),
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
    assert repl_payloads[0].startswith("exec(")
    assert repl_payloads[0].endswith(")\n")
    wrapper = unwrap_repl_payload(repl_payloads[0])
    assert "screen.capture_rect(screen.main().rect, retina=False)" in wrapper
    assert "img.save(path) if hasattr(img, 'save') else img.write_file(path)" in wrapper
    assert downloads[0][0] == "192.168.64.10"
    assert downloads[0][2] == target
    assert cleanup_commands[0].startswith('rm -f "/tmp/talonbox-screenshot-')


def test_talon_client_screenshot_preserves_requested_capture_suffix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, transfer_service, talon_client = build_service_stack()
    repl_payloads: list[str] = []
    downloads: list[tuple[str, Path]] = []
    target = tmp_path / "shots" / "screen.ppm"

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
        lambda payload: repl_payloads.append(payload) or repl_ok_result(payload),
    )
    monkeypatch.setattr(
        talon_client.running_vm,
        "download",
        lambda remote, local: downloads.append((remote, local)),
    )
    monkeypatch.setattr(
        talon_client.running_vm,
        "run_shell",
        lambda command, **kwargs: subprocess.CompletedProcess([], 0, "", ""),
    )

    talon_client.capture_screenshot(target)

    assert len(downloads) == 1
    assert downloads[0][1] == target
    assert downloads[0][0].endswith(".ppm")
    wrapper = unwrap_repl_payload(repl_payloads[0])
    assert "pixels = bytes(img.__array_interface__['data'])" in wrapper
    assert "rgb[target] = pixels[source]" in wrapper
    assert "rgb[target + 2] = pixels[source + 2]" in wrapper
    assert "rgb[target] = pixels[source + 2]" not in wrapper
    assert "header = f" in wrapper
    assert "P6\\\\n" in wrapper


def test_talon_client_screenshot_surfaces_repl_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, transfer_service, talon_client = build_service_stack()
    target = tmp_path / "shots" / "screen.ppm"

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
        lambda payload, stream_output=False: subprocess.CompletedProcess(
            [],
            0,
            "Traceback (most recent call last):\nRuntimeError: capture failed\n",
            "",
        ),
    )
    monkeypatch.setattr(
        talon_client.running_vm,
        "download",
        lambda remote, local: pytest.fail("download should not run after traceback"),
    )
    monkeypatch.setattr(
        talon_client.running_vm,
        "run_shell",
        lambda command, **kwargs: subprocess.CompletedProcess([], 0, "", ""),
    )

    with pytest.raises(click.ClickException, match="capture failed"):
        talon_client.capture_screenshot(target)


def test_talon_client_screenshot_requires_talon_repl_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, transfer_service, talon_client = build_service_stack()
    target = tmp_path / "shots" / "screen.png"

    monkeypatch.setattr(
        transfer_service, "_host_output_root", lambda: tmp_path.resolve()
    )

    def fail_wait(*, timeout: float = vm_module.TALON_REPL_TIMEOUT_SECONDS) -> None:
        del timeout
        raise vm_module.RemoteCommandError("Remote command failed: test -S repl.sock")

    monkeypatch.setattr(talon_client.running_vm, "wait_for_talon_repl", fail_wait)
    monkeypatch.setattr(
        talon_client.running_vm,
        "run_shell",
        lambda command, **kwargs: subprocess.CompletedProcess([], 0, "", ""),
    )

    with pytest.raises(click.ClickException, match="test -S repl.sock"):
        talon_client.capture_screenshot(target)


def test_talon_client_screenshot_delegates_to_vnc_client(tmp_path: Path) -> None:
    _, _, talon_client = build_service_stack()
    calls: list[Path] = []
    target = tmp_path / "shots" / "screen.png"

    class FakeVncClient:
        def capture_screenshot(self, filepath: Path) -> None:
            calls.append(filepath)

    talon_client.vnc_client = cast(Any, FakeVncClient())

    talon_client.capture_screenshot(target, vnc=True)

    assert calls == [target]


def test_talon_client_click_delegates_to_vnc_client() -> None:
    _, _, talon_client = build_service_stack()
    calls: list[tuple[int, int, str]] = []

    class FakeVncClient:
        def click(self, x: int, y: int, *, button: str) -> None:
            calls.append((x, y, button))

    talon_client.vnc_client = cast(Any, FakeVncClient())

    talon_client.click(123, 456, button="middle", vnc=True)

    assert calls == [(123, 456, "middle")]


def test_talon_client_type_delegates_to_vnc_client() -> None:
    _, _, talon_client = build_service_stack()
    calls: list[str] = []

    class FakeVncClient:
        def type_text(self, text: str) -> None:
            calls.append(text)

    talon_client.vnc_client = cast(Any, FakeVncClient())

    talon_client.type_text("hello", vnc=True)

    assert calls == ["hello"]


def test_talon_client_press_key_delegates_to_vnc_client() -> None:
    _, _, talon_client = build_service_stack()
    calls: list[str] = []

    class FakeVncClient:
        def press_key(self, key: str) -> None:
            calls.append(key)

    talon_client.vnc_client = cast(Any, FakeVncClient())

    talon_client.press_key("enter", vnc=True)

    assert calls == ["enter"]


def test_talon_client_screenshot_rejects_output_outside_tmp() -> None:
    _, _, talon_client = build_service_stack()

    with pytest.raises(
        click.ClickException, match="Local output paths must stay under /tmp"
    ):
        talon_client.capture_screenshot(Path("/private/var/guest-screen.png"))


TRACEBACK_OUTPUT = (
    "Traceback (most recent call last):\nNotImplementedError: talon action failed\n"
)


@pytest.mark.parametrize(
    "invoke",
    [
        lambda client: client.mimic("garbage phrase"),
        lambda client: client.click(123, 456),
        lambda client: client.type_text("hello"),
        lambda client: client.press_key("enter"),
    ],
    ids=["mimic", "click", "type", "press"],
)
def test_talon_client_surfaces_repl_traceback_despite_zero_exit(
    monkeypatch: pytest.MonkeyPatch,
    invoke: Any,
) -> None:
    _, _, talon_client = build_service_stack()

    monkeypatch.setattr(
        talon_client.running_vm,
        "wait_for_talon_repl",
        lambda *, timeout=vm_module.TALON_REPL_TIMEOUT_SECONDS: None,
    )
    monkeypatch.setattr(
        talon_client.running_vm,
        "run_repl",
        lambda payload, stream_output=False: subprocess.CompletedProcess(
            [], 0, TRACEBACK_OUTPUT, ""
        ),
    )

    with pytest.raises(click.ClickException, match="talon action failed"):
        invoke(talon_client)


def test_talon_client_repl_fails_on_traceback_in_streamed_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, talon_client = build_service_stack()

    monkeypatch.setattr(
        talon_client.running_vm,
        "wait_for_talon_repl",
        lambda *, timeout=vm_module.TALON_REPL_TIMEOUT_SECONDS: None,
    )
    monkeypatch.setattr(
        talon_client.running_vm,
        "run_repl",
        lambda payload, stream_output=False: subprocess.CompletedProcess(
            [], 0, TRACEBACK_OUTPUT, ""
        ),
    )

    with pytest.raises(
        click.ClickException, match="raised an exception in Talon's REPL"
    ):
        talon_client.repl("raise NotImplementedError('talon action failed')")


def test_talon_client_repl_echoes_output_without_sentinel(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, talon_client = build_service_stack()

    monkeypatch.setattr(
        talon_client.running_vm,
        "wait_for_talon_repl",
        lambda *, timeout=vm_module.TALON_REPL_TIMEOUT_SECONDS: None,
    )
    monkeypatch.setattr(
        talon_client.running_vm,
        "run_repl",
        lambda payload: repl_ok_result(payload, stdout="2\n"),
    )

    talon_client.repl("print(1+1)")

    captured = capsys.readouterr()
    assert captured.out == "2\n"
    assert "talonbox-repl-ok" not in captured.out
