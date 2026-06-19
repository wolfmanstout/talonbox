from __future__ import annotations

import subprocess
from pathlib import Path

import click
import pytest

from talonbox import lume as lume_module
from talonbox import vm as vm_module
from talonbox.lume import VmInfo
from talonbox.names import to_lume_vm_name, to_public_vm_name
from talonbox.vm import VmController
from tests.helpers import fake_launch, running_vm_fixture, set_vm_statuses


def test_name_helpers_prefix_and_strip_lume_names() -> None:
    assert to_lume_vm_name("experiment") == "talonbox-experiment"
    assert to_public_vm_name("talonbox-experiment") == "experiment"
    assert to_public_vm_name("other") is None


def test_vm_controller_rejects_prefixed_public_name() -> None:
    with pytest.raises(click.ClickException, match="unprefixed"):
        VmController("talonbox-experiment", False)


def test_vm_controller_list_filters_and_strips_talonbox_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        vm_module.lume,
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
        "username: lume",
        "password: lume",
        "vnc: vnc://127.0.0.1:5901",
    ]


def test_vm_controller_clone_requires_stopped_source_and_empty_dest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller = VmController("golden", False)
    calls: list[tuple[str, object]] = []

    def fake_get_vm_info(vm: str, debug: bool = False) -> VmInfo | None:
        del debug
        calls.append(("get_vm_info", vm))
        if vm == "talonbox-golden":
            return VmInfo(vm, "stopped", None)
        return None

    monkeypatch.setattr(vm_module.lume, "get_vm_info", fake_get_vm_info)
    monkeypatch.setattr(
        vm_module.lume,
        "clone_vm",
        lambda source, target, debug=False: calls.append(
            ("clone_vm", (source, target))
        ),
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
        vm_module.lume,
        "get_vm_info",
        lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"),
    )

    with pytest.raises(click.ClickException, match="must be stopped"):
        vm_controller.clone("experiment")


def test_vm_controller_rename_clones_then_deletes_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller = VmController("experiment", False)
    calls: list[tuple[str, object]] = []

    def fake_get_vm_info(vm: str, debug: bool = False) -> VmInfo | None:
        del debug
        calls.append(("get_vm_info", vm))
        if vm == "talonbox-experiment":
            return VmInfo(vm, "stopped", None)
        return None

    monkeypatch.setattr(vm_module.lume, "get_vm_info", fake_get_vm_info)
    monkeypatch.setattr(
        vm_module.lume,
        "clone_vm",
        lambda source, target, debug=False: calls.append(
            ("clone_vm", (source, target))
        ),
    )
    monkeypatch.setattr(
        vm_module.lume,
        "delete_vm",
        lambda vm, debug=False: calls.append(("delete_vm", vm)),
    )

    vm_controller.rename("experiment-old")

    assert calls == [
        ("get_vm_info", "talonbox-experiment"),
        ("get_vm_info", "talonbox-experiment-old"),
        ("clone_vm", ("talonbox-experiment", "talonbox-experiment-old")),
        ("get_vm_info", "talonbox-experiment"),
        ("delete_vm", "talonbox-experiment"),
    ]


@pytest.mark.parametrize("status", ["running", "stopping"])
def test_vm_controller_delete_refuses_non_stopped_vm(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    vm_controller = VmController("experiment", False)
    delete_calls: list[str] = []
    monkeypatch.setattr(
        vm_module.lume,
        "get_vm_info",
        lambda vm, debug=False: VmInfo(
            vm,
            status,
            "192.168.64.10" if status == "running" else None,
        ),
    )
    monkeypatch.setattr(
        vm_module.lume,
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
        vm_module.lume,
        "get_vm_info",
        lambda vm, debug=False: VmInfo(vm, "stopped", None),
    )
    monkeypatch.setattr(
        vm_module.lume,
        "delete_vm",
        lambda vm, debug=False: calls.append(vm),
    )

    vm_controller.delete()

    assert calls == ["talonbox-experiment"]


def test_vm_controller_start_resumes_existing_vm_and_ensures_talon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller = VmController("experiment", False)
    calls: list[tuple[str, object]] = []
    probe_calls: list[float] = []
    idle_lock_calls: list[str] = []
    ensure_calls: list[str] = []
    running_vm = running_vm_fixture()

    monkeypatch.setattr(
        vm_module.lume,
        "get_vm_info",
        lambda vm, debug=False: (
            calls.append(("get_vm_info", vm)) or VmInfo(vm, "stopped", None)
        ),
    )
    monkeypatch.setattr(
        vm_module.lume,
        "spawn_vm",
        lambda vm, debug=False: calls.append(("spawn_vm", vm)) or fake_launch(),
    )
    monkeypatch.setattr(
        vm_module.lume,
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
    monkeypatch.setattr(vm_module.lume, "cleanup_launch_log", lambda log_path: None)

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
        vm_module.lume,
        "get_vm_info",
        lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"),
    )
    monkeypatch.setattr(
        vm_module.lume,
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
        vm_module.lume,
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
        vm_module.lume,
        "spawn_vm",
        lambda vm, debug=False: fake_launch(),
    )
    monkeypatch.setattr(
        vm_module.lume,
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

    assert calls == [("stop_vm", "talonbox-talon-test"), ("wait_for_status", 30.0)]


def test_vm_controller_formats_concurrency_hint_when_two_vms_are_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller = VmController("experiment", False)
    monkeypatch.setattr(
        vm_module.lume,
        "list_vms",
        lambda debug=False: [
            VmInfo("talonbox-one", "running", "192.168.64.10"),
            VmInfo("talonbox-two", "running", "192.168.64.11"),
        ],
    )

    assert vm_controller._format_start_error("lume run exited") == (
        "lume run exited\n"
        "HINT macOS Virtualization commonly allows only 2 running VMs; stop another VM and retry."
    )


def test_running_vm_restart_talon_waits_for_repl_and_sleeps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running_vm = running_vm_fixture()
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


def test_vm_controller_stop_falls_back_to_force_stop_for_stuck_vm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_controller = VmController("talon-test", False)
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(
        vm_module.lume,
        "get_vm_info",
        lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"),
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
                "Timed out waiting for VM to reach status stopped: talonbox-talon-test"
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
        ("stop_vm", "talonbox-talon-test"),
        ("wait_for_status", 60.0),
        ("force_stop_vm", "talonbox-talon-test"),
        ("wait_for_status", 20.0),
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
