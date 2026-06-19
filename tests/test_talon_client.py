from __future__ import annotations

import subprocess
from pathlib import Path

import click
import pytest

from talonbox import vm as vm_module
from tests.helpers import build_service_stack


def test_talon_client_repl_waits_for_socket_then_runs_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, talon_client = build_service_stack()
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
    assert repl_payloads[0].startswith("exec(")
    assert repl_payloads[0].endswith(")\n")
    assert "screen.capture_rect(screen.main().rect, retina=False)" in repl_payloads[0]
    assert (
        "img.save(path) if hasattr(img, 'save') else img.write_file(path)"
        in repl_payloads[0]
    )
    assert downloads[0][0] == "192.168.64.10"
    assert downloads[0][2] == target
    assert cleanup_commands[0].startswith('rm -f "/tmp/talonbox-screenshot-')


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


def test_talon_client_screenshot_can_use_explicit_screencapture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, transfer_service, talon_client = build_service_stack()
    shell_commands: list[str] = []
    downloads: list[tuple[str, str, Path]] = []
    target = tmp_path / "shots" / "screen.png"

    monkeypatch.setattr(
        transfer_service, "_host_output_root", lambda: tmp_path.resolve()
    )
    monkeypatch.setattr(
        talon_client.running_vm,
        "wait_for_talon_repl",
        lambda *, timeout=0: pytest.fail("screencapture should not wait for REPL"),
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
        "download",
        lambda remote, local: (
            downloads.append((talon_client.running_vm.ip_address, remote, local))
            or local.write_bytes(b"not-a-png")
        ),
    )

    talon_client.capture_screenshot(target, screencapture=True)

    assert target.parent.exists()
    assert shell_commands[0].startswith("screencapture -x /tmp/talonbox-screenshot-")
    assert shell_commands[1].startswith('rm -f "/tmp/talonbox-screenshot-')
    assert downloads[0][0] == "192.168.64.10"
    assert downloads[0][2] == target


def test_talon_client_screenshot_rejects_output_outside_tmp() -> None:
    _, _, talon_client = build_service_stack()

    with pytest.raises(
        click.ClickException, match="Local output paths must stay under /tmp"
    ):
        talon_client.capture_screenshot(Path("/private/var/guest-screen.png"))
