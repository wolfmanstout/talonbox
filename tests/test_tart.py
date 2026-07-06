from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from talonbox import tart as tart_module
from talonbox.tart import VmInfo


def test_get_vm_info_surfaces_raw_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        tart_module,
        "_run_tart",
        lambda args, debug=False, capture_output=True: calls.append(args)
        or subprocess.CompletedProcess(args, 0, '{"bad"', ""),
    )

    with pytest.raises(
        tart_module.TartError,
        match=r'Invalid JSON from `tart list --format json`: \{"bad"',
    ):
        tart_module.get_vm_info("talon-test")
    assert calls == [["list", "--format", "json"]]


def test_get_vm_info_parses_tart_list_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = """[
  {
    "Size" : 31,
    "Name" : "talon-test",
    "Running" : false,
    "Source" : "local",
    "State" : "stopped",
    "Accessed" : "2026-07-06T01:28:15Z",
    "Disk" : 50
  }
]
"""
    monkeypatch.setattr(
        tart_module,
        "_run_tart",
        lambda args, debug=False, capture_output=True: subprocess.CompletedProcess(
            args, 0, output, ""
        ),
    )
    monkeypatch.setattr(
        tart_module, "_vnc_url_path", lambda name: tmp_path / f"{name}.vnc"
    )
    tart_module.write_vnc_url("talon-test", "vnc://127.0.0.1:5901")

    info = tart_module.get_vm_info("talon-test")

    assert info is not None
    assert info == VmInfo(
        "talon-test",
        "stopped",
        None,
        last_accessed=datetime(2026, 7, 6, 1, 28, 15, tzinfo=UTC),
    )
    assert info.last_accessed is not None
    assert info.last_accessed.isoformat() == "2026-07-06T01:28:15+00:00"
    assert info.last_accessed.tzinfo is UTC


def test_get_vm_info_resolves_ip_and_cached_vnc_for_running_vm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    list_output = """[
  {
    "Name" : "talon-test",
    "Running" : true,
    "State" : "running"
  }
]
"""
    calls: list[list[str]] = []

    def fake_run_tart(
        args: list[str], debug: bool = False, capture_output: bool = True
    ) -> subprocess.CompletedProcess[str]:
        del debug, capture_output
        calls.append(args)
        if args == ["list", "--format", "json"]:
            return subprocess.CompletedProcess(args, 0, list_output, "")
        if args == ["ip", "talon-test"]:
            return subprocess.CompletedProcess(args, 0, "192.168.64.10\n", "")
        raise AssertionError(args)

    monkeypatch.setattr(tart_module, "_run_tart", fake_run_tart)
    monkeypatch.setattr(
        tart_module, "_vnc_url_path", lambda name: tmp_path / f"{name}.vnc"
    )
    tart_module.write_vnc_url("talon-test", "vnc://127.0.0.1:5901")

    info = tart_module.get_vm_info("talon-test")

    assert info == VmInfo(
        "talon-test", "running", "192.168.64.10", "vnc://127.0.0.1:5901"
    )
    assert calls == [["list", "--format", "json"], ["ip", "talon-test"]]


def test_get_vm_info_returns_none_when_vm_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tart_module,
        "_run_tart",
        lambda args, debug=False, capture_output=True: subprocess.CompletedProcess(
            args, 0, "[]", ""
        ),
    )

    assert tart_module.get_vm_info("talon-test") is None


def test_lifecycle_commands_delegate_to_tart(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        tart_module,
        "_run_tart",
        lambda args, debug=False, capture_output=True: calls.append(args)
        or subprocess.CompletedProcess(args, 0, "", ""),
    )

    tart_module.clone_vm("talonbox-golden", "talonbox-live")
    tart_module.rename_vm("talonbox-live", "talonbox-old")
    tart_module.suspend_vm("talonbox-old")
    tart_module.shutdown_vm("talonbox-old")
    tart_module.delete_vm("talonbox-old")

    assert calls == [
        ["clone", "talonbox-golden", "talonbox-live"],
        ["rename", "talonbox-live", "talonbox-old"],
        ["suspend", "talonbox-old"],
        ["stop", "talonbox-old"],
        ["delete", "talonbox-old"],
    ]


def test_wait_for_running_vm_reports_launch_log_when_tart_run_exits_early(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_path = tmp_path / "tart-run.log"
    log_path.write_text("permission denied\nconfig.json\n", encoding="utf-8")
    launch = tart_module.VmLaunch(
        process=cast(
            subprocess.Popen[bytes], type("Process", (), {"poll": lambda self: 1})()
        ),
        log_path=log_path,
    )
    monkeypatch.setattr(
        tart_module,
        "get_vm_info",
        lambda name, debug=False: VmInfo(name, "stopped", None),
    )

    with pytest.raises(tart_module.TartError, match="permission denied"):
        tart_module.wait_for_running_vm(
            "talon-test",
            timeout=1.0,
            interval=0.0,
            launch=launch,
        )


def test_wait_for_running_vm_persists_vnc_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_path = tmp_path / "tart-run.log"
    log_path.write_text("Open vnc://127.0.0.1:5901 to connect\n", encoding="utf-8")
    launch = tart_module.VmLaunch(
        process=cast(
            subprocess.Popen[bytes], type("Process", (), {"poll": lambda self: None})()
        ),
        log_path=log_path,
    )
    monkeypatch.setattr(
        tart_module,
        "get_vm_info",
        lambda name, debug=False: VmInfo(name, "running", "192.168.64.10"),
    )
    monkeypatch.setattr(
        tart_module, "_vnc_url_path", lambda name: tmp_path / f"{name}.vnc"
    )

    info = tart_module.wait_for_running_vm(
        "talon-test",
        timeout=1.0,
        interval=0.0,
        launch=launch,
    )

    assert info.vnc_url == "vnc://127.0.0.1:5901"
    assert tart_module.read_vnc_url("talon-test") == "vnc://127.0.0.1:5901"


def test_wait_for_running_vm_requires_fresh_vnc_url_for_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_path = tmp_path / "tart-run.log"
    log_path.write_text("Booting\n", encoding="utf-8")
    launch = tart_module.VmLaunch(
        process=cast(
            subprocess.Popen[bytes], type("Process", (), {"poll": lambda self: None})()
        ),
        log_path=log_path,
    )
    monkeypatch.setattr(
        tart_module, "_vnc_url_path", lambda name: tmp_path / f"{name}.vnc"
    )
    tart_module.write_vnc_url("talon-test", "vnc://127.0.0.1:5901")
    monkeypatch.setattr(
        tart_module,
        "get_vm_info",
        lambda name, debug=False: VmInfo(
            name, "running", "192.168.64.10", tart_module.read_vnc_url(name)
        ),
    )
    extract_calls = 0

    def fake_extract_vnc_url(log_path: Path) -> str | None:
        nonlocal extract_calls
        extract_calls += 1
        if extract_calls == 1:
            return None
        return "vnc://127.0.0.1:5902"

    monkeypatch.setattr(tart_module, "_extract_vnc_url", fake_extract_vnc_url)

    info = tart_module.wait_for_running_vm(
        "talon-test",
        timeout=1.0,
        interval=0.0,
        launch=launch,
    )

    assert info.vnc_url == "vnc://127.0.0.1:5902"
    assert tart_module.read_vnc_url("talon-test") == "vnc://127.0.0.1:5902"
