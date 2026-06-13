from __future__ import annotations

import subprocess
from pathlib import Path
from typing import cast

import pytest

from talonbox import lume as lume_module
from talonbox import vm as vm_module
from talonbox.lume import VmInfo
from talonbox.talon_client import TalonClient
from talonbox.transfer import TransferService
from talonbox.vm import RunningVm, VmController


def fake_launch(
    log_path: Path = Path("/tmp/talonbox-test.log"),
) -> lume_module.VmLaunch:
    process = cast(
        subprocess.Popen[bytes], type("Process", (), {"poll": lambda self: None})()
    )
    return lume_module.VmLaunch(process=process, log_path=log_path)


def set_vm_statuses(
    monkeypatch: pytest.MonkeyPatch,
    *statuses: tuple[str, str | None],
) -> None:
    remaining = list(statuses)

    def fake_get_vm_info(vm: str, debug: bool = False) -> VmInfo:
        del debug
        status, ip_address = remaining[0] if len(remaining) == 1 else remaining.pop(0)
        return VmInfo(vm, status, ip_address)

    monkeypatch.setattr(vm_module.lume, "get_vm_info", fake_get_vm_info)


def build_service_stack(
    vm: str = "talon-test", debug: bool = False
) -> tuple[VmController, TransferService, TalonClient]:
    vm_controller = VmController(vm, debug)
    running_vm = running_vm_fixture(debug=debug)
    transfer_service = TransferService(running_vm)
    talon_client = TalonClient(running_vm, transfer_service)
    return vm_controller, transfer_service, talon_client


def running_vm_fixture(
    ip_address: str = "192.168.64.10", *, debug: bool = False
) -> RunningVm:
    return RunningVm(
        name="talon-test",
        ip_address=ip_address,
        debug=debug,
    )
