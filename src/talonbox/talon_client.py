from __future__ import annotations

import shlex
import subprocess
import uuid
from pathlib import Path

import click

from .transfer import TransferService
from .vm import RemoteCommandError, RunningVm, TransportError


class TalonClient:
    def __init__(
        self, running_vm: RunningVm, transfer_service: TransferService
    ) -> None:
        self.running_vm = running_vm
        self.transfer_service = transfer_service

    def repl(self, code: str) -> None:
        self.running_vm.wait_for_talon_repl()
        result = self.running_vm.run_repl(
            f"exec({code!r})\n",
            stream_output=True,
        )
        if result.returncode:
            raise click.exceptions.Exit(result.returncode)

    def mimic(self, command: str) -> None:
        self.running_vm.wait_for_talon_repl()
        result = self.running_vm.run_repl(
            f"mimic({command!r})\n",
        )
        if result.returncode:
            raise click.exceptions.Exit(result.returncode)

    def capture_screenshot(
        self, filepath: Path, *, screencapture: bool = False
    ) -> None:
        filepath = self.transfer_service.normalize_local_output_path(filepath)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        remote_path = f"/tmp/talonbox-screenshot-{uuid.uuid4().hex}.png"
        try:
            if screencapture:
                self._capture_with_screencapture(remote_path, filepath)
                return
            self.running_vm.wait_for_talon_repl()
            result = self._capture_with_talon(remote_path)
            if result.returncode:
                raise click.exceptions.Exit(result.returncode)
            self.running_vm.download(remote_path, filepath)
        except (RemoteCommandError, TransportError) as error:
            raise click.ClickException(str(error)) from None
        finally:
            try:
                self.running_vm.run_shell(
                    f'rm -f "{remote_path}"',
                )
            except (RemoteCommandError, TransportError):
                pass

    def _capture_with_talon(self, remote_path: str) -> subprocess.CompletedProcess[str]:
        return self.running_vm.run_repl(
            "\n".join(
                [
                    "from talon import screen",
                    f"path = {remote_path!r}",
                    "img = screen.capture_rect(screen.main().rect, retina=False)",
                    "img.save(path) if hasattr(img, 'save') else img.write_file(path)",
                    "print(path)",
                    "",
                ]
            ),
        )

    def _capture_with_screencapture(self, remote_path: str, filepath: Path) -> None:
        self.running_vm.run_shell(f"screencapture -x {shlex.quote(remote_path)}")
        self.running_vm.download(remote_path, filepath)
