from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path
from typing import cast

import pytest

from talonbox import tart as tart_module
from talonbox import vm as vm_module
from talonbox.talon_client import REPL_OK_PREFIX, TalonClient
from talonbox.tart import VmInfo
from talonbox.transfer import TransferService
from talonbox.vm import RunningVm, VmController

REPL_OK_LINE_RE = re.compile(rf"{REPL_OK_PREFIX} [0-9a-f]+")


def unwrap_repl_payload(payload: str) -> str:
    """Return the sentinel wrapper code inside an `exec('...')` REPL payload."""
    assert payload.startswith("exec(") and payload.endswith(")\n")
    return ast.literal_eval(payload[len("exec(") : -len(")\n")])


def repl_ok_result(payload: str, stdout: str = "") -> subprocess.CompletedProcess[str]:
    """Simulate Talon's repl running the payload code to completion."""
    match = REPL_OK_LINE_RE.search(unwrap_repl_payload(payload))
    assert match is not None, "payload is missing the success sentinel"
    return subprocess.CompletedProcess([], 0, f"{stdout}{match.group(0)}\n", "")


def fake_launch(
    log_path: Path = Path("/tmp/talonbox-test.log"),
) -> tart_module.VmLaunch:
    process = cast(
        subprocess.Popen[bytes], type("Process", (), {"poll": lambda self: None})()
    )
    return tart_module.VmLaunch(process=process, log_path=log_path)


def set_vm_statuses(
    monkeypatch: pytest.MonkeyPatch,
    *statuses: tuple[str, str | None],
) -> None:
    remaining = list(statuses)

    def fake_get_vm_info(vm: str, debug: bool = False) -> VmInfo:
        del debug
        status, ip_address = remaining[0] if len(remaining) == 1 else remaining.pop(0)
        return VmInfo(vm, status, ip_address)

    monkeypatch.setattr(vm_module.tart, "get_vm_info", fake_get_vm_info)


def build_service_stack(
    vm: str = "talon-test", debug: bool = False
) -> tuple[VmController, TransferService, TalonClient]:
    vm_controller = VmController(vm, debug)
    running_vm = running_vm_fixture(debug=debug)
    transfer_service = TransferService(running_vm)
    talon_client = TalonClient(running_vm, transfer_service)
    return vm_controller, transfer_service, talon_client


def running_vm_fixture(
    ip_address: str = "192.168.64.10",
    *,
    debug: bool = False,
    vnc_url: str | None = None,
) -> RunningVm:
    return RunningVm(
        name="talon-test",
        ip_address=ip_address,
        debug=debug,
        vnc_url=vnc_url,
    )
