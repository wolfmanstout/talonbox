from __future__ import annotations

import subprocess
from pathlib import Path

import click
import pytest

from talonbox import vm as vm_module
from talonbox.names import to_public_vm_name, to_tart_vm_name
from talonbox.tart import VmInfo
from talonbox.vm import VmController
from tests.helpers import fake_launch, running_vm_fixture, set_vm_statuses


def test_name_helpers_prefix_and_strip_tart_names() -> None:
    assert to_tart_vm_name("experiment") == "talonbox-experiment"
    assert to_public_vm_name("talonbox-experiment") == "experiment"
    assert to_public_vm_name("other") is None


def test_vm_controller_rejects_prefixed_public_name() -> None:
    with pytest.raises(click.ClickException, match="unprefixed"):
        VmController("talonbox-experiment", False)


def test_vm_controller_list_filters_and_strips_talonbox_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        vm_module.tart,
        "list_vms",
        lambda debug=False: [
            VmInfo("not-talonbox", "stopped", None),
            VmInfo("talonbox-golden", "stopped", None),
            VmInfo("talonbox-experiment", "running", "192.168.64.10"),
        ],
    )

    assert VmController.list_vms() == [
        VmInfo("golden", "stopped", None),
        VmInfo("experiment", "running", "192.168.64.10"),
    ]


def test_vm_controller_format_vm_info_includes_name_and_vnc() -> None:
    vm_controller = VmController("talon-test", False)

    lines = vm_controller.format_vm_info(
        VmInfo("talon-test", "running", "192.168.64.10", "vnc://127.0.0.1:5901")
    )

    assert lines == [
        "name: talon-test",
        "status: running",
        "ip: 192.168.64.10",
        "username: admin",
        "password: admin",
        "vnc: vnc://127.0.0.1:5901",
    ]


def test_vm_controller_clone_requires_stopped_source_and_empty_dest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller = VmController("golden", False)
    calls: list[tuple[object, ...]] = []

    def fake_get_vm_info(vm: str, debug: bool = False) -> VmInfo | None:
        del debug
        calls.append(("get_vm_info", vm))
        if vm == "talonbox-golden":
            return VmInfo(vm, "stopped", None)
        return None

    monkeypatch.setattr(vm_module.tart, "get_vm_info", fake_get_vm_info)
    monkeypatch.setattr(
        vm_module.tart,
        "clone_vm",
        lambda source, target, debug=False: calls.append(
            ("clone_vm", (source, target))
        ),
    )
    monkeypatch.setattr(
        vm_module.VmController,
        "start",
        lambda self, require_talon=True: pytest.fail("clone should not warm up"),
    )

    vm_controller.clone("experiment")
    assert calls == [
        ("get_vm_info", "talonbox-golden"),
        ("get_vm_info", "talonbox-experiment"),
        ("clone_vm", ("talonbox-golden", "talonbox-experiment")),
    ]


def test_vm_controller_clone_rejects_running_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller = VmController("golden", False)
    monkeypatch.setattr(
        vm_module.tart,
        "get_vm_info",
        lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"),
    )

    with pytest.raises(click.ClickException) as error:
        vm_controller.clone("experiment")
    assert "Source VM must be stopped before cloning" in error.value.message
    assert "talonbox stop --shutdown golden" in error.value.message


def test_vm_controller_rename_uses_native_tart_rename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller = VmController("experiment", False)
    calls: list[tuple[object, ...]] = []

    def fake_get_vm_info(vm: str, debug: bool = False) -> VmInfo | None:
        del debug
        calls.append(("get_vm_info", vm))
        if vm == "talonbox-experiment":
            return VmInfo(vm, "stopped", None)
        return None

    monkeypatch.setattr(vm_module.tart, "get_vm_info", fake_get_vm_info)
    monkeypatch.setattr(
        vm_module.tart,
        "rename_vm",
        lambda source, target, debug=False: calls.append(
            ("rename_vm", (source, target))
        ),
    )
    monkeypatch.setattr(
        vm_module.tart,
        "clone_vm",
        lambda source, target, debug=False: pytest.fail("rename should not clone"),
    )

    vm_controller.rename("experiment-old")

    assert calls == [
        ("get_vm_info", "talonbox-experiment"),
        ("get_vm_info", "talonbox-experiment-old"),
        ("rename_vm", ("talonbox-experiment", "talonbox-experiment-old")),
    ]


@pytest.mark.parametrize("status", ["running", "stopping"])
def test_vm_controller_delete_refuses_non_stopped_vm(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    vm_controller = VmController("experiment", False)
    delete_calls: list[str] = []
    monkeypatch.setattr(
        vm_module.tart,
        "get_vm_info",
        lambda vm, debug=False: VmInfo(
            vm,
            status,
            "192.168.64.10" if status == "running" else None,
        ),
    )
    monkeypatch.setattr(
        vm_module.tart,
        "delete_vm",
        lambda vm, debug=False: delete_calls.append(vm),
    )

    with pytest.raises(click.ClickException, match="must be stopped"):
        vm_controller.delete()
    assert delete_calls == []


def test_vm_controller_delete_uses_prefixed_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller = VmController("experiment", False)
    calls: list[str] = []
    monkeypatch.setattr(
        vm_module.tart,
        "get_vm_info",
        lambda vm, debug=False: VmInfo(vm, "stopped", None),
    )
    monkeypatch.setattr(
        vm_module.tart,
        "delete_vm",
        lambda vm, debug=False: calls.append(vm),
    )

    vm_controller.delete()

    assert calls == ["talonbox-experiment"]


def test_vm_controller_delete_accepts_suspended_vm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller = VmController("experiment", False)
    calls: list[str] = []
    monkeypatch.setattr(
        vm_module.tart,
        "get_vm_info",
        lambda vm, debug=False: VmInfo(vm, "suspended", None),
    )
    monkeypatch.setattr(
        vm_module.tart,
        "delete_vm",
        lambda vm, debug=False: calls.append(vm),
    )

    vm_controller.delete()

    assert calls == ["talonbox-experiment"]


def test_vm_controller_start_resumes_existing_vm_and_ensures_talon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller = VmController("experiment", False)
    calls: list[tuple[object, ...]] = []
    probe_calls: list[float] = []
    idle_lock_calls: list[str] = []
    ensure_calls: list[str] = []
    running_vm = running_vm_fixture()

    monkeypatch.setattr(
        vm_module.tart,
        "get_vm_info",
        lambda vm, debug=False: (
            calls.append(("get_vm_info", vm)) or VmInfo(vm, "stopped", None)
        ),
    )
    monkeypatch.setattr(
        vm_module.tart,
        "spawn_vm",
        lambda vm, debug=False: calls.append(("spawn_vm", vm)) or fake_launch(),
    )
    monkeypatch.setattr(
        vm_module.tart,
        "wait_for_running_vm",
        lambda vm, timeout, debug=False, launch=None: (
            calls.append(("wait_for_running_vm", vm))
            or VmInfo(vm, "running", "192.168.64.10")
        ),
    )
    monkeypatch.setattr(vm_controller, "_running_vm_from_info", lambda info: running_vm)
    monkeypatch.setattr(
        running_vm,
        "probe_ssh",
        lambda *, timeout=0: probe_calls.append(timeout),
    )
    monkeypatch.setattr(
        running_vm,
        "prevent_idle_lock",
        lambda: idle_lock_calls.append("prevent_idle_lock"),
    )
    monkeypatch.setattr(
        running_vm,
        "ensure_talon_running",
        lambda: ensure_calls.append("ensure_talon_running"),
    )
    monkeypatch.setattr(vm_module.tart, "cleanup_launch_log", lambda log_path: None)

    assert vm_controller.start() is running_vm
    assert calls == [
        ("get_vm_info", "talonbox-experiment"),
        ("spawn_vm", "talonbox-experiment"),
        ("wait_for_running_vm", "talonbox-experiment"),
    ]
    assert probe_calls == [vm_module.SSH_TIMEOUT_SECONDS]
    assert idle_lock_calls == ["prevent_idle_lock"]
    assert ensure_calls == ["ensure_talon_running"]


def test_vm_controller_start_reuses_running_vm_without_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller = VmController("experiment", False)
    running_vm = running_vm_fixture()
    spawn_calls: list[str] = []

    monkeypatch.setattr(
        vm_module.tart,
        "get_vm_info",
        lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"),
    )
    monkeypatch.setattr(
        vm_module.tart,
        "spawn_vm",
        lambda vm, debug=False: spawn_calls.append(vm) or fake_launch(),
    )
    monkeypatch.setattr(vm_controller, "_running_vm_from_info", lambda info: running_vm)
    monkeypatch.setattr(running_vm, "probe_ssh", lambda *, timeout=0: None)
    monkeypatch.setattr(running_vm, "prevent_idle_lock", lambda: None)
    monkeypatch.setattr(running_vm, "ensure_talon_running", lambda: None)

    assert vm_controller.start() is running_vm
    assert spawn_calls == []


def test_vm_controller_start_can_skip_talon_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller = VmController("experiment", False)
    running_vm = running_vm_fixture()
    ensure_calls: list[str] = []

    monkeypatch.setattr(
        vm_module.tart,
        "get_vm_info",
        lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"),
    )
    monkeypatch.setattr(vm_controller, "_running_vm_from_info", lambda info: running_vm)
    monkeypatch.setattr(running_vm, "probe_ssh", lambda *, timeout=0: None)
    monkeypatch.setattr(running_vm, "prevent_idle_lock", lambda: None)
    monkeypatch.setattr(
        running_vm,
        "ensure_talon_running",
        lambda: ensure_calls.append("ensure_talon_running"),
    )

    assert vm_controller.start(require_talon=False) is running_vm
    assert ensure_calls == []


def test_vm_controller_start_cleans_up_failed_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller = VmController("talon-test", True)
    calls: list[object] = []
    running_vm = running_vm_fixture(debug=True)

    set_vm_statuses(monkeypatch, ("stopped", None))
    monkeypatch.setattr(
        vm_module.tart,
        "spawn_vm",
        lambda vm, debug=False: fake_launch(),
    )
    monkeypatch.setattr(
        vm_module.tart,
        "wait_for_running_vm",
        lambda vm, timeout, debug=False, launch=None: VmInfo(
            vm, "running", "192.168.64.10"
        ),
    )
    monkeypatch.setattr(vm_controller, "_running_vm_from_info", lambda info: running_vm)

    def fail_probe(*, timeout: float = 0.0) -> None:
        del timeout
        raise vm_module.TransportError("ssh failed: 192.168.64.10")

    monkeypatch.setattr(running_vm, "probe_ssh", fail_probe)
    monkeypatch.setattr(
        vm_module.tart,
        "suspend_vm",
        lambda vm, debug=False: calls.append(("suspend_vm", vm)),
    )
    monkeypatch.setattr(
        vm_module.tart,
        "wait_for_status",
        lambda vm, status, timeout, debug=False: (
            calls.append(("wait_for_status", status, timeout))
            or VmInfo(vm, "suspended", None)
        ),
    )

    with pytest.raises(click.ClickException, match="ssh failed: 192.168.64.10"):
        vm_controller.start()

    assert calls == [
        ("suspend_vm", "talonbox-talon-test"),
        ("wait_for_status", "suspended", 30.0),
    ]


def test_vm_controller_formats_concurrency_hint_when_two_vms_are_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller = VmController("experiment", False)
    monkeypatch.setattr(
        vm_module.tart,
        "list_vms",
        lambda debug=False: [
            VmInfo("talonbox-one", "running", "192.168.64.10"),
            VmInfo("talonbox-two", "running", "192.168.64.11"),
        ],
    )

    assert vm_controller._format_start_error("tart run exited") == (
        "tart run exited\n"
        "HINT macOS Virtualization commonly allows only 2 running VMs; stop another VM and retry."
    )


def test_running_vm_restart_talon_waits_for_repl_and_sleeps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running_vm = running_vm_fixture()
    calls: list[tuple[object, ...]] = []
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
    assert (
        "192.168.64.10",
        "open -a /Applications/Talon.app --stdout /tmp/talonbox-talon.log --stderr /tmp/talonbox-talon.log",
    ) in calls
    assert calls[-1] == ("192.168.64.10", "wait_for_talon_repl")
    assert sleeps == [vm_module.TALON_POST_RESTART_SETTLE_SECONDS]


def test_running_vm_restart_talon_retries_transient_launch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running_vm = running_vm_fixture()
    launch_command = (
        "open -a /Applications/Talon.app "
        "--stdout /tmp/talonbox-talon.log --stderr /tmp/talonbox-talon.log"
    )
    launch_attempts = 0
    sleeps: list[float] = []

    def fake_run_shell(command: str, **kwargs: object) -> subprocess.CompletedProcess:
        nonlocal launch_attempts
        del kwargs
        if command == launch_command:
            launch_attempts += 1
            if launch_attempts == 1:
                raise vm_module.RemoteCommandError("Launch failed")
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(running_vm, "run_shell", fake_run_shell)
    monkeypatch.setattr(running_vm, "wait_for_talon_repl", lambda **kwargs: None)
    monkeypatch.setattr(vm_module.time, "sleep", lambda seconds: sleeps.append(seconds))

    running_vm.restart_talon(wipe_user_dir=False, clean_logs=False)

    assert launch_attempts == 2
    assert sleeps == [
        vm_module.TRANSIENT_RETRY_DELAY_SECONDS,
        vm_module.TALON_POST_RESTART_SETTLE_SECONDS,
    ]


def test_vm_controller_restart_talon_reports_click_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller = VmController("experiment", False)
    running_vm = running_vm_fixture()
    monkeypatch.setattr(vm_controller, "get_running_vm", lambda: running_vm)
    monkeypatch.setattr(
        running_vm,
        "restart_talon",
        lambda *, wipe_user_dir, clean_logs: (_ for _ in ()).throw(
            vm_module.RemoteCommandError("repl not ready")
        ),
    )

    with pytest.raises(click.ClickException, match="repl not ready"):
        vm_controller.restart_talon(wipe_user_dir=False, clean_logs=True)


def test_running_vm_ensure_talon_running_skips_launch_when_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running_vm = running_vm_fixture()
    calls: list[object] = []

    monkeypatch.setattr(
        running_vm,
        "run_shell",
        lambda command, **kwargs: (
            calls.append(command) or subprocess.CompletedProcess([], 0, "", "")
        ),
    )
    monkeypatch.setattr(
        running_vm,
        "wait_for_talon_repl",
        lambda **kwargs: calls.append("wait_for_talon_repl"),
    )

    running_vm.ensure_talon_running()

    assert calls == ["pgrep -x Talon >/dev/null", "wait_for_talon_repl"]


def test_running_vm_prevent_idle_lock_writes_current_host_screensaver_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running_vm = running_vm_fixture()
    calls: list[str] = []

    monkeypatch.setattr(
        running_vm,
        "run_shell",
        lambda command, **kwargs: (
            calls.append(command) or subprocess.CompletedProcess([], 0, "", "")
        ),
    )

    running_vm.prevent_idle_lock()

    assert calls == [
        "defaults -currentHost write com.apple.screensaver idleTime -int 0 && "
        "defaults write com.apple.screensaver askForPassword -int 0 && "
        "defaults write com.apple.screensaver askForPasswordDelay -int 0",
        "killall cfprefsd >/dev/null 2>&1 || true",
    ]


def test_vm_controller_stop_suspends_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller = VmController("talon-test", False)
    calls: list[tuple[object, ...]] = []

    monkeypatch.setattr(
        vm_module.tart,
        "get_vm_info",
        lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"),
    )
    monkeypatch.setattr(
        vm_module.tart,
        "suspend_vm",
        lambda vm, debug=False: calls.append(("suspend_vm", vm)),
    )
    monkeypatch.setattr(
        vm_module.tart,
        "wait_for_status",
        lambda vm, status, timeout, debug=False: (
            calls.append(("wait_for_status", status, timeout))
            or VmInfo(vm, "suspended", None)
        ),
    )

    vm_controller.stop()

    assert calls == [
        ("suspend_vm", "talonbox-talon-test"),
        ("wait_for_status", "suspended", 60.0),
    ]


def test_vm_controller_stop_can_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller = VmController("talon-test", False)
    calls: list[tuple[object, ...]] = []

    monkeypatch.setattr(
        vm_module.tart,
        "get_vm_info",
        lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"),
    )
    monkeypatch.setattr(
        vm_module.tart,
        "shutdown_vm",
        lambda vm, debug=False: calls.append(("shutdown_vm", vm)),
    )
    monkeypatch.setattr(
        vm_module.tart,
        "wait_for_status",
        lambda vm, status, timeout, debug=False: (
            calls.append(("wait_for_status", status, timeout))
            or VmInfo(vm, "stopped", None)
        ),
    )

    vm_controller.stop(shutdown=True)

    assert calls == [
        ("shutdown_vm", "talonbox-talon-test"),
        ("wait_for_status", "stopped", 60.0),
    ]


def test_vm_controller_stop_shutdown_attempts_suspended_vm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller = VmController("talon-test", False)
    calls: list[tuple[object, ...]] = []

    monkeypatch.setattr(
        vm_module.tart,
        "get_vm_info",
        lambda vm, debug=False: VmInfo(vm, "suspended", None),
    )
    monkeypatch.setattr(
        vm_module.tart,
        "shutdown_vm",
        lambda vm, debug=False: calls.append(("shutdown_vm", vm)),
    )
    monkeypatch.setattr(
        vm_module.tart,
        "wait_for_status",
        lambda vm, status, timeout, debug=False: (
            calls.append(("wait_for_status", status, timeout))
            or VmInfo(vm, "stopped", None)
        ),
    )

    vm_controller.stop(shutdown=True)

    assert calls == [
        ("shutdown_vm", "talonbox-talon-test"),
        ("wait_for_status", "stopped", 60.0),
    ]


def test_running_vm_streamed_transport_failure_does_not_require_captured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running_vm = running_vm_fixture()

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["capture_output"] is False
        return subprocess.CompletedProcess([], 1, None, None)

    monkeypatch.setattr(vm_module.subprocess, "run", fake_run)

    result = running_vm.run_shell("false", stream=True, check=False)

    assert result.returncode == 1


def test_running_vm_download_uses_scp(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[list[str]] = []
    running_vm = running_vm_fixture()

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
            "admin",
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
            "admin@192.168.64.10:/tmp/out.png",
            "/tmp/out.png",
        ]
    ]


def test_running_vm_run_repl_retries_transient_ssh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = {"count": 0}
    running_vm = running_vm_fixture()

    def fake_run(**kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        attempts["count"] += 1
        if attempts["count"] == 1:
            return subprocess.CompletedProcess(
                [],
                255,
                "",
                "ssh_askpass: exec(/usr/X11R6/bin/ssh-askpass): No such file or directory\n"
                "admin@192.168.64.10: Permission denied (publickey,password,keyboard-interactive).",
            )
        return subprocess.CompletedProcess([], 0, "ok\n", "")

    monkeypatch.setattr(
        "talonbox.vm.subprocess.run", lambda *args, **kwargs: fake_run(**kwargs)
    )
    monkeypatch.setattr("talonbox.vm.time.sleep", lambda seconds: None)

    result = running_vm.run_repl("print('ok')\n")

    assert result.returncode == 0
    assert attempts["count"] == 2


def test_running_vm_run_repl_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running_vm = running_vm_fixture()

    def fake_run(
        cmd: list[str],
        *,
        check: bool,
        text: bool,
        capture_output: bool,
        timeout: float | None,
        stdin: object | None = None,
        input: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del check, text, capture_output, stdin, input
        raise subprocess.TimeoutExpired(
            cmd=cmd,
            timeout=timeout or 0,
        )

    monkeypatch.setattr("talonbox.vm.subprocess.run", fake_run)

    result = running_vm.run_repl("print('ok')\n")

    assert result.returncode == 124
    assert "Command timed out after 30 seconds" in result.stderr


def test_running_vm_download_retries_transient_ssh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = {"count": 0}
    running_vm = running_vm_fixture()

    def fake_run(**kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        attempts["count"] += 1
        if attempts["count"] == 1:
            return subprocess.CompletedProcess(
                [],
                255,
                "",
                "ssh_askpass: exec(/usr/X11R6/bin/ssh-askpass): No such file or directory\n"
                "admin@192.168.64.10: Permission denied (publickey,password,keyboard-interactive).",
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
    running_vm = running_vm_fixture()
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
