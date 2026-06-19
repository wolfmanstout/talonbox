from __future__ import annotations

import subprocess
from typing import cast

import pytest

from talonbox import lume as lume_module
from talonbox.lume import VmInfo


def test_get_vm_info_surfaces_raw_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        lume_module,
        "_run_lume",
        lambda args, debug=False, capture_output=True: calls.append(args)
        or subprocess.CompletedProcess(args, 0, '{"bad"', ""),
    )

    with pytest.raises(
        lume_module.LumeError,
        match=r'Invalid JSON from `lume get talon-test --format json`: \{"bad"',
    ):
        lume_module.get_vm_info("talon-test")
    assert calls == [["get", "talon-test", "--format", "json"]]


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


def test_get_vm_info_returns_none_when_lume_get_reports_missing_vm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_lume(
        args: list[str], debug: bool = False, capture_output: bool = True
    ) -> subprocess.CompletedProcess[str]:
        del args, debug, capture_output
        raise lume_module.LumeError("Virtual machine not found: talon-test")

    monkeypatch.setattr(lume_module, "_run_lume", fake_run_lume)

    assert lume_module.get_vm_info("talon-test") is None


def test_clone_vm_delegates_to_lume_clone(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        lume_module,
        "_run_lume",
        lambda args, debug=False, capture_output=True: calls.append(args)
        or subprocess.CompletedProcess(args, 0, "", ""),
    )

    lume_module.clone_vm("talonbox-golden", "talonbox-live")

    assert calls == [["clone", "talonbox-golden", "talonbox-live"]]


def test_delete_vm_delegates_to_lume_delete_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        lume_module,
        "_run_lume",
        lambda args, debug=False, capture_output=True: calls.append(args)
        or subprocess.CompletedProcess(args, 0, "", ""),
    )

    lume_module.delete_vm("talonbox-live")

    assert calls == [["delete", "talonbox-live", "--force"]]


def test_wait_for_running_vm_reports_launch_log_when_lume_run_exits_early(
    monkeypatch: pytest.MonkeyPatch, tmp_path
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
