from __future__ import annotations

import subprocess
import zlib
from pathlib import Path

import pytest
from click.testing import CliRunner

from talonbox import cli as cli_module
from talonbox import lume as lume_module
from talonbox.cli import cli
from talonbox.lume import VmInfo
from talonbox.state import StateRecord, state_paths
from talonbox.talon import build_mimic_payload, build_repl_exec_payload
from talonbox.transport import run_rsync, run_scp


@pytest.fixture(autouse=True)
def state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TALONBOX_STATE_DIR", str(tmp_path))


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
    assert "talonbox exec -- uname -a" in result.output


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
    assert "Send one phrase to the guest Talon REPL as `mimic(<phrase>)`." in result.output


def test_show_running_vm_prints_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(cli_module.lume, "get_vm_info", lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"))

    result = runner.invoke(cli, ["show"])

    assert result.exit_code == 0
    assert result.output == "status: running\nip: 192.168.64.10\nusername: lume\npassword: lume\n"


def test_show_help_mentions_read_only_sandbox_safe_usage() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["show", "--help"])

    assert result.exit_code == 0
    assert "This command is read-only" in result.output
    assert "safe to use in sandboxed environments" in result.output


def test_start_refuses_running_vm(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(cli_module.lume, "get_vm_info", lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"))

    result = runner.invoke(cli, ["start"])

    assert result.exit_code == 1
    assert "VM is already running" in result.output


def test_start_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(cli_module.lume, "get_vm_info", lambda vm, debug=False: VmInfo(vm, "stopped", None))

    def fake_spawn(vm: str, debug: bool = False) -> StateRecord:
        calls.append(("spawn", vm))
        return StateRecord(vm=vm, pid=1234, log_path="/tmp/talon-test.log", started_at="2026-03-10T00:00:00Z")

    def fake_wait(
        vm: str,
        timeout: float,
        debug: bool = False,
        pid: int | None = None,
        log_path: Path | None = None,
    ) -> VmInfo:
        calls.append(("wait", timeout))
        calls.append(("pid", pid))
        return VmInfo(vm, "running", "192.168.64.10")

    monkeypatch.setattr(cli_module.lume, "spawn_vm", fake_spawn)
    monkeypatch.setattr(cli_module.lume, "wait_for_running_vm", fake_wait)
    monkeypatch.setattr(cli_module, "probe_ssh", lambda ip, debug=False, timeout=0: calls.append(("probe", ip)))
    monkeypatch.setattr(cli_module, "_bootstrap_talon", lambda ip, debug=False: calls.append(("bootstrap", ip)))

    result = runner.invoke(cli, ["start"])

    assert result.exit_code == 0
    assert result.output == "status: running\nip: 192.168.64.10\nusername: lume\npassword: lume\n"
    assert calls == [
        ("spawn", "talon-test"),
        ("wait", cli_module.START_TIMEOUT_SECONDS),
        ("pid", 1234),
        ("probe", "192.168.64.10"),
        ("bootstrap", "192.168.64.10"),
    ]
    assert state_paths("talon-test").state_path.exists()


def test_start_failure_cleans_up_state(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    calls: list[str] = []

    monkeypatch.setattr(cli_module.lume, "get_vm_info", lambda vm, debug=False: VmInfo(vm, "stopped", None))
    monkeypatch.setattr(
        cli_module.lume,
        "spawn_vm",
        lambda vm, debug=False: StateRecord(vm=vm, pid=1234, log_path="/tmp/talon-test.log", started_at="2026-03-10T00:00:00Z"),
    )
    monkeypatch.setattr(
        cli_module.lume,
        "wait_for_running_vm",
        lambda vm, timeout, debug=False, pid=None, log_path=None: VmInfo(vm, "running", "192.168.64.10"),
    )

    def fail_probe(ip: str, debug: bool = False, timeout: float = 0) -> None:
        raise cli_module.TransportError("ssh failed")

    monkeypatch.setattr(cli_module, "probe_ssh", fail_probe)
    monkeypatch.setattr(cli_module.lume, "stop_vm", lambda vm, debug=False: calls.append("stop"))
    monkeypatch.setattr(
        cli_module.lume,
        "wait_for_status",
        lambda vm, status, timeout, debug=False: calls.append("wait_for_status") or VmInfo(vm, "stopped", None),
    )

    result = runner.invoke(cli, ["start"])

    assert result.exit_code == 1
    assert "ssh failed" in result.output
    assert calls == ["stop", "wait_for_status"]
    assert not state_paths("talon-test").state_path.exists()


def test_terminal_launch_command_runs_talon_via_arch_in_terminal() -> None:
    command = cli_module._terminal_launch_command()

    assert "printf %s " in command
    assert "chmod +x /tmp/talonbox-launch.command" in command
    assert "open -a Terminal /tmp/talonbox-launch.command" in command
    assert "exec arch -x86_64 /Applications/Talon.app/Contents/MacOS/Talon >/tmp/talonbox-talon.log 2>&1" in command


def test_restart_talon_help_mentions_log_reset() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["restart-talon", "--help"])

    assert result.exit_code == 0
    assert "Restart Talon inside the running VM without rebooting the VM." in result.output
    assert "~/.talon/talon.log" in result.output


def test_restart_talon_restarts_without_wiping_user_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    calls: list[str] = []
    monkeypatch.setattr(cli_module.lume, "get_vm_info", lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"))

    def fake_restart(ip_address: str, *, debug: bool, wipe_user_dir: bool, clean_logs: bool) -> None:
        calls.append(ip_address)
        assert debug is False
        assert wipe_user_dir is False
        assert clean_logs is True

    monkeypatch.setattr(cli_module, "_restart_talon", fake_restart)

    result = runner.invoke(cli, ["restart-talon"])

    assert result.exit_code == 0
    assert result.output == ""
    assert calls == ["192.168.64.10"]


def test_stop_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    state = StateRecord(
        vm="talon-test",
        pid=1234,
        log_path="/tmp/talon-test.log",
        started_at="2026-03-10T00:00:00Z",
    )
    cli_module.save_state(state)
    monkeypatch.setattr(cli_module.lume, "get_vm_info", lambda vm, debug=False: VmInfo(vm, "stopped", None))
    monkeypatch.setattr(cli_module.lume, "stop_vm", lambda vm, debug=False: pytest.fail("stop_vm should not be called"))

    result = runner.invoke(cli, ["stop"])

    assert result.exit_code == 0
    assert result.output == ""
    assert not state_paths("talon-test").state_path.exists()


def test_stop_falls_back_to_force_stop_for_stuck_vm(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    calls: list[tuple[str, object]] = []
    state = StateRecord(
        vm="talon-test",
        pid=4321,
        log_path="/tmp/talon-test.log",
        started_at="2026-03-10T00:00:00Z",
    )
    cli_module.save_state(state)
    monkeypatch.setattr(cli_module.lume, "get_vm_info", lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"))
    monkeypatch.setattr(
        cli_module.lume,
        "stop_vm",
        lambda vm, debug=False: calls.append(("stop_vm", vm)),
    )

    def fake_wait_for_status(vm: str, status: str, timeout: float, debug: bool = False) -> VmInfo:
        calls.append(("wait_for_status", timeout))
        if timeout == 60.0:
            raise lume_module.LumeError("Timed out waiting for VM to reach status stopped: talon-test")
        return VmInfo(vm, "stopped", None)

    monkeypatch.setattr(cli_module.lume, "wait_for_status", fake_wait_for_status)
    monkeypatch.setattr(
        cli_module.lume,
        "force_stop_vm",
        lambda vm, debug=False, pid=None: calls.append(("force_stop_vm", pid)),
    )

    result = runner.invoke(cli, ["stop"])

    assert result.exit_code == 0
    assert result.output == ""
    assert calls == [
        ("stop_vm", "talon-test"),
        ("wait_for_status", 60.0),
        ("force_stop_vm", 4321),
        ("wait_for_status", 20.0),
    ]
    assert not state_paths("talon-test").state_path.exists()


def test_exec_passes_through_args_and_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(cli_module.lume, "get_vm_info", lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"))

    def fake_exec(ip: str, command_args: list[str], debug: bool = False) -> int:
        calls.append((ip, command_args))
        return 7

    monkeypatch.setattr(cli_module, "run_remote_shell_streaming", fake_exec)

    result = runner.invoke(cli, ["exec", "--", "echo", "hi"])

    assert result.exit_code == 7
    assert calls == [("192.168.64.10", ["echo", "hi"])]


def test_exec_single_argument_uses_shell_string(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(cli_module.lume, "get_vm_info", lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"))
    monkeypatch.setattr(cli_module, "run_remote_shell_streaming", lambda ip, command_args, debug=False: pytest.fail("argv path should not be used"))

    def fake_shell(ip: str, command: str, debug: bool = False) -> int:
        calls.append((ip, command))
        return 0

    monkeypatch.setattr(cli_module, "run_remote_command_streaming", fake_shell)

    result = runner.invoke(cli, ["exec", "--", "ps aux | grep safari"])

    assert result.exit_code == 0
    assert calls == [("192.168.64.10", "ps aux | grep safari")]


def test_rsync_help_mentions_guest_prefix() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["rsync", "--help"])

    assert result.exit_code == 0
    assert "guest:/path" in result.output
    assert "only `guest:` remote paths are allowed" in result.output


def test_scp_help_mentions_guest_prefix() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["scp", "--help"])

    assert result.exit_code == 0
    assert "guest:/path" in result.output
    assert "only `guest:` remote paths are allowed" in result.output


def test_rsync_upload_rewrites_guest_destination(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    calls: list[list[str]] = []
    monkeypatch.setattr(cli_module.lume, "get_vm_info", lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"))
    monkeypatch.setattr(
        cli_module,
        "run_rsync",
        lambda args, debug=False: calls.append(args) or 0,
    )

    result = runner.invoke(cli, ["rsync", "-av", "./repo/", "guest:/Users/lume/.talon/user/repo/"])

    assert result.exit_code == 0
    assert calls == [["-av", "./repo/", "lume@192.168.64.10:/Users/lume/.talon/user/repo/"]]


def test_rsync_download_rewrites_guest_source(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    calls: list[list[str]] = []
    monkeypatch.setattr(cli_module.lume, "get_vm_info", lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"))
    monkeypatch.setattr(
        cli_module,
        "run_rsync",
        lambda args, debug=False: calls.append(args) or 0,
    )

    result = runner.invoke(cli, ["rsync", "-av", "guest:/Users/lume/Pictures/", "./guest-pictures/"])

    assert result.exit_code == 0
    assert calls == [["-av", "lume@192.168.64.10:/Users/lume/Pictures/", "./guest-pictures/"]]


def test_rsync_allows_upload_from_outside_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    calls: list[list[str]] = []
    monkeypatch.setattr(cli_module.lume, "get_vm_info", lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"))
    monkeypatch.setattr(
        cli_module,
        "run_rsync",
        lambda args, debug=False: calls.append(args) or 0,
    )

    result = runner.invoke(cli, ["rsync", "-av", "/Users/jwstout/projects/wolfmanstout_talon/", "guest:/tmp/wolfmanstout_talon/"])

    assert result.exit_code == 0
    assert calls == [["-av", "/Users/jwstout/projects/wolfmanstout_talon/", "lume@192.168.64.10:/tmp/wolfmanstout_talon/"]]


def test_rsync_rejects_download_outside_writable_sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = CliRunner()
    monkeypatch.setattr(cli_module.lume, "get_vm_info", lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"))
    monkeypatch.setattr(cli_module, "_local_write_roots", lambda: (tmp_path / "allowed",))

    result = runner.invoke(cli, ["rsync", "-av", "guest:/tmp/out.txt", "/Users/jwstout/Downloads/out.txt"])

    assert result.exit_code == 1
    assert "outside the writable sandbox" in result.output


def test_scp_rejects_download_outside_writable_sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = CliRunner()
    monkeypatch.setattr(cli_module.lume, "get_vm_info", lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"))
    monkeypatch.setattr(cli_module, "_local_write_roots", lambda: (tmp_path / "allowed",))

    result = runner.invoke(cli, ["scp", "guest:/tmp/out.txt", "/Users/jwstout/Desktop/out.txt"])

    assert result.exit_code == 1
    assert "outside the writable sandbox" in result.output


def test_rsync_rejects_local_to_local(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(cli_module.lume, "get_vm_info", lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"))

    result = runner.invoke(cli, ["rsync", "-av", "./repo/", "./copy/"])

    assert result.exit_code == 1
    assert "Local-to-local transfers are not allowed" in result.output


def test_rsync_rejects_non_guest_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(cli_module.lume, "get_vm_info", lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"))

    result = runner.invoke(cli, ["rsync", "-av", "user@host:/tmp/x", "./copy/"])

    assert result.exit_code == 1
    assert "Only guest: remote paths are allowed" in result.output


def test_rsync_rejects_old_implicit_guest_syntax(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(cli_module.lume, "get_vm_info", lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"))

    result = runner.invoke(cli, ["rsync", "-av", "./repo/", "/Users/lume/.talon/user/repo/"])

    assert result.exit_code == 1
    assert "Local-to-local transfers are not allowed" in result.output


def test_rsync_rejects_transport_override(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(cli_module.lume, "get_vm_info", lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"))

    result = runner.invoke(cli, ["rsync", "-e", "ssh", "./repo/", "guest:/tmp/repo/"])

    assert result.exit_code == 1
    assert "Option not allowed for VM-only transfer safety: -e" in result.output


def test_scp_upload_rewrites_guest_destination(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    calls: list[list[str]] = []
    monkeypatch.setattr(cli_module.lume, "get_vm_info", lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"))
    monkeypatch.setattr(
        cli_module,
        "run_scp",
        lambda args, debug=False: calls.append(args) or 0,
    )

    result = runner.invoke(cli, ["scp", "./settings.talon", "guest:/Users/lume/.talon/user/settings.talon"])

    assert result.exit_code == 0
    assert calls == [["./settings.talon", "lume@192.168.64.10:/Users/lume/.talon/user/settings.talon"]]


def test_scp_download_rewrites_guest_source(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    calls: list[list[str]] = []
    monkeypatch.setattr(cli_module.lume, "get_vm_info", lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"))
    monkeypatch.setattr(
        cli_module,
        "run_scp",
        lambda args, debug=False: calls.append(args) or 0,
    )

    result = runner.invoke(cli, ["scp", "guest:/tmp/out.png", "/tmp/out.png"])

    assert result.exit_code == 0
    assert calls == [["lume@192.168.64.10:/tmp/out.png", "/tmp/out.png"]]


def test_scp_rejects_transport_override(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(cli_module.lume, "get_vm_info", lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"))

    result = runner.invoke(cli, ["scp", "-S", "ssh", "./settings.talon", "guest:/tmp/settings.talon"])

    assert result.exit_code == 1
    assert "Option not allowed for VM-only transfer safety: -S" in result.output


def test_scp_rejects_guest_to_guest(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(cli_module.lume, "get_vm_info", lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"))

    result = runner.invoke(cli, ["scp", "guest:/tmp/a", "guest:/tmp/b"])

    assert result.exit_code == 1
    assert "Guest-to-guest transfers are not allowed" in result.output


def test_repl_waits_for_socket_then_runs_piped_script(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    waits: list[tuple[str, float]] = []
    payloads: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(cli_module.lume, "get_vm_info", lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"))
    monkeypatch.setattr(
        cli_module,
        "wait_for_talon_repl",
        lambda ip, debug=False, timeout=0: waits.append((ip, timeout)),
    )
    monkeypatch.setattr(
        cli_module,
        "run_remote_repl",
        lambda ip, payload, debug=False, stream_output=False: payloads.append((ip, payload, stream_output)) or 0,
    )

    result = runner.invoke(cli, ["repl"], input="if True:\n    print(1)\nprint(2)\n")

    assert result.exit_code == 0
    assert waits == [("192.168.64.10", cli_module.TALON_REPL_TIMEOUT_SECONDS)]
    assert payloads == [
        (
            "192.168.64.10",
            build_repl_exec_payload("if True:\n    print(1)\nprint(2)\n"),
            True,
        )
    ]


def test_repl_accepts_inline_code(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    payloads: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(cli_module.lume, "get_vm_info", lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"))
    monkeypatch.setattr(cli_module, "wait_for_talon_repl", lambda ip, debug=False, timeout=0: None)
    monkeypatch.setattr(
        cli_module,
        "run_remote_repl",
        lambda ip, payload, debug=False, stream_output=False: payloads.append((ip, payload, stream_output)) or 0,
    )

    result = runner.invoke(cli, ["repl", "print(1+1)"])

    assert result.exit_code == 0
    assert payloads == [("192.168.64.10", build_repl_exec_payload("print(1+1)"), True)]


def test_mimic_uses_python_escaped_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    payloads: list[str] = []
    waits: list[tuple[str, float]] = []
    monkeypatch.setattr(cli_module.lume, "get_vm_info", lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"))
    monkeypatch.setattr(
        cli_module,
        "wait_for_talon_repl",
        lambda ip, debug=False, timeout=0: waits.append((ip, timeout)),
    )
    monkeypatch.setattr(
        cli_module,
        "run_remote_repl",
        lambda ip, payload, debug=False: payloads.append(payload) or 0,
    )

    result = runner.invoke(cli, ["mimic", "say \"hello\"\nworld"])

    assert result.exit_code == 0
    assert waits == [("192.168.64.10", cli_module.TALON_REPL_TIMEOUT_SECONDS)]
    assert payloads == [build_mimic_payload('say "hello"\nworld')]


def test_screenshot_uses_talon_capture_and_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = CliRunner()
    repl_payloads: list[str] = []
    downloads: list[tuple[str, str, Path]] = []
    cleanup_commands: list[str] = []
    target = tmp_path / "shots" / "screen.png"

    monkeypatch.setattr(cli_module.lume, "get_vm_info", lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"))
    monkeypatch.setattr(cli_module, "wait_for_talon_repl", lambda ip, debug=False, timeout=0: None)
    monkeypatch.setattr(
        cli_module,
        "run_remote_repl",
        lambda ip, payload, debug=False: repl_payloads.append(payload) or 0,
    )
    monkeypatch.setattr(
        cli_module,
        "download_from_guest",
        lambda ip, remote, local, debug=False: downloads.append((ip, remote, local)) or local.write_bytes(b"not-a-png"),
    )
    monkeypatch.setattr(
        cli_module,
        "run_remote_shell",
        lambda ip, command, debug=False: cleanup_commands.append(command) or subprocess.CompletedProcess([], 0, "", ""),
    )

    result = runner.invoke(cli, ["screenshot", str(target)])

    assert result.exit_code == 0
    assert target.parent.exists()
    assert "screen.capture_rect(screen.main().rect, retina=False)" in repl_payloads[0]
    assert "img.save(path) if hasattr(img, 'save') else img.write_file(path)" in repl_payloads[0]
    assert downloads[0][0] == "192.168.64.10"
    assert downloads[0][2] == target
    assert cleanup_commands[0].startswith('rm -f "/tmp/talonbox-screenshot-')


def test_screenshot_fails_for_blank_png(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "blank.png"

    monkeypatch.setattr(cli_module.lume, "get_vm_info", lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"))
    monkeypatch.setattr(cli_module, "wait_for_talon_repl", lambda ip, debug=False, timeout=0: None)
    monkeypatch.setattr(cli_module, "run_remote_repl", lambda ip, payload, debug=False: 0)
    monkeypatch.setattr(
        cli_module,
        "download_from_guest",
        lambda ip, remote, local, debug=False: local.write_bytes(_blank_png_bytes()),
    )
    monkeypatch.setattr(
        cli_module,
        "run_remote_shell",
        lambda ip, command, debug=False: subprocess.CompletedProcess([], 0, "", ""),
    )

    result = runner.invoke(cli, ["screenshot", str(target)])

    assert result.exit_code == 1
    assert "Guest screenshot was blank." in result.output
    assert not target.exists()


def test_get_vm_info_surfaces_raw_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        lume_module,
        "_run_lume",
        lambda args, debug=False, capture_output=True: subprocess.CompletedProcess(args, 0, '{"bad"', ""),
    )

    with pytest.raises(lume_module.LumeError, match=r'Invalid JSON from `lume ls --format json`: \{"bad"'):
        lume_module.get_vm_info("talon-test")


def test_get_vm_info_tolerates_log_line_before_json(monkeypatch: pytest.MonkeyPatch) -> None:
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
        lambda args, debug=False, capture_output=True: subprocess.CompletedProcess(args, 0, noisy_output, ""),
    )

    info = lume_module.get_vm_info("talon-test")

    assert info == VmInfo("talon-test", "stopped", None)


def _blank_png_bytes() -> bytes:
    scanline = b"\x00\xff\xff\xff"
    compressed = zlib.compress(scanline)
    chunks = [
        _png_chunk(b"IHDR", (1).to_bytes(4, "big") + (1).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"),
        _png_chunk(b"IDAT", compressed),
        _png_chunk(b"IEND", b""),
    ]
    return b"\x89PNG\r\n\x1a\n" + b"".join(chunks)


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + payload).to_bytes(4, "big")
    return len(payload).to_bytes(4, "big") + chunk_type + payload + crc


def test_run_rsync_uses_fixed_vm_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[list[str]] = []

    def fake_run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess[bytes]:
        recorded.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("talonbox.transport.subprocess.run", fake_run)

    returncode = run_rsync(["-av", "src/", "lume@192.168.64.10:/tmp/dest"])

    assert returncode == 0
    assert recorded == [
        [
            "rsync",
            "-e",
            "sshpass -p lume ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o BatchMode=no -o NumberOfPasswordPrompts=1 -o PasswordAuthentication=yes -o KbdInteractiveAuthentication=no -o PreferredAuthentications=password -o PubkeyAuthentication=no",
            "-av",
            "src/",
            "lume@192.168.64.10:/tmp/dest",
        ]
    ]


def test_run_scp_uses_fixed_vm_ssh_options(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[list[str]] = []

    def fake_run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess[bytes]:
        recorded.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("talonbox.transport.subprocess.run", fake_run)

    returncode = run_scp(["./settings.talon", "lume@192.168.64.10:/tmp/settings.talon"])

    assert returncode == 0
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
            "./settings.talon",
            "lume@192.168.64.10:/tmp/settings.talon",
        ]
    ]
