from __future__ import annotations

import re
import subprocess
import uuid
from pathlib import Path

import click

from .transfer import TransferService
from .vm import RemoteCommandError, RunningVm, TransportError
from .vnc_client import VncClient

TALON_MOUSE_BUTTONS = {
    "left": 0,
    "right": 1,
    "middle": 2,
}
EMBEDDED_SPEECH_COMMAND_RE = re.compile(r"\[\[.*?\]\]")


def strip_embedded_speech_commands(command: str) -> str:
    return " ".join(EMBEDDED_SPEECH_COMMAND_RE.sub(" ", command).split())


class TalonClient:
    def __init__(
        self,
        running_vm: RunningVm,
        transfer_service: TransferService,
        vnc_client: VncClient | None = None,
    ) -> None:
        self.running_vm = running_vm
        self.transfer_service = transfer_service
        self.vnc_client = vnc_client or VncClient(running_vm, transfer_service)

    def repl(self, code: str) -> None:
        self.running_vm.wait_for_talon_repl()
        result = self.running_vm.run_repl(
            f"exec({code!r})\n",
            stream_output=True,
        )
        if result.returncode:
            raise click.exceptions.Exit(result.returncode)

    def mimic(self, command: str, *, audio: bool = False) -> None:
        if audio:
            self.mimic_audio(command)
            return

        self.running_vm.wait_for_talon_repl()
        command = strip_embedded_speech_commands(command)
        result = self.running_vm.run_repl(
            f"mimic({command!r})\n",
        )
        if result.returncode:
            raise click.exceptions.Exit(result.returncode)

    def mimic_audio(self, command: str) -> None:
        self.running_vm.wait_for_talon_repl()
        remote_path = f"/tmp/talonbox-mimic-audio-{uuid.uuid4().hex}.wav"
        try:
            self.running_vm.run_shell(
                [
                    "say",
                    "-o",
                    remote_path,
                    "--data-format=LEI16@16000",
                    command,
                ]
            )
            code = "\n".join(
                [
                    "from talon import actions",
                    f"path = {remote_path!r}",
                    "actions.speech.replay(path)",
                    "",
                ]
            )
            result = self.running_vm.run_repl(f"exec({code!r})\n")
            if result.returncode:
                raise click.exceptions.Exit(result.returncode)
        except (RemoteCommandError, TransportError) as error:
            raise click.ClickException(str(error)) from None
        finally:
            try:
                self.running_vm.run_shell(["rm", "-f", remote_path])
            except (RemoteCommandError, TransportError):
                pass

    def click(self, x: int, y: int, *, button: str = "left", vnc: bool = False) -> None:
        if vnc:
            self.vnc_client.click(x, y, button=button)
            return

        talon_button = TALON_MOUSE_BUTTONS[button]
        code = "\n".join(
            [
                "from talon import ctrl",
                f"ctrl.mouse_move({x}, {y})",
                f"ctrl.mouse_click(button={talon_button})",
                "",
            ]
        )
        self.running_vm.wait_for_talon_repl()
        result = self.running_vm.run_repl(f"exec({code!r})\n")
        if result.returncode:
            raise click.exceptions.Exit(result.returncode)

    def type_text(self, text: str, *, vnc: bool = False) -> None:
        if vnc:
            self.vnc_client.type_text(text)
            return

        code = "\n".join(
            [
                "from talon import actions",
                f"actions.insert({text!r})",
                "",
            ]
        )
        self.running_vm.wait_for_talon_repl()
        result = self.running_vm.run_repl(f"exec({code!r})\n")
        if result.returncode:
            raise click.exceptions.Exit(result.returncode)

    def capture_screenshot(self, filepath: Path, *, vnc: bool = False) -> None:
        if vnc:
            self.vnc_client.capture_screenshot(filepath)
            return
        filepath = self.transfer_service.normalize_local_output_path(filepath)
        filepath.parent.mkdir(parents=True, exist_ok=True)

        suffix = filepath.suffix if filepath.suffix else ".png"
        remote_path = f"/tmp/talonbox-screenshot-{uuid.uuid4().hex}{suffix}"
        try:
            self.running_vm.wait_for_talon_repl()
            result = self._capture_with_talon(remote_path)
            if result.returncode:
                raise click.exceptions.Exit(result.returncode)
            if "Traceback (most recent call last)" in (result.stdout or ""):
                raise click.ClickException(result.stdout.strip())
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
        if remote_path.endswith(".ppm"):
            return self._capture_with_talon_ppm(remote_path)

        code = "\n".join(
            [
                "from talon import screen",
                f"path = {remote_path!r}",
                "img = screen.capture_rect(screen.main().rect, retina=False)",
                "img.save(path) if hasattr(img, 'save') else img.write_file(path)",
                "print(path)",
                "",
            ]
        )
        return self.running_vm.run_repl(f"exec({code!r})\n")

    def _capture_with_talon_ppm(
        self, remote_path: str
    ) -> subprocess.CompletedProcess[str]:
        code = "\n".join(
            [
                "from pathlib import Path",
                "from talon import screen",
                f"path = {remote_path!r}",
                "img = screen.capture_rect(screen.main().rect, retina=False)",
                "expected = img.width * img.height * 4",
                "pixels = bytes(img.__array_interface__['data'])",
                "if len(pixels) != expected:",
                "    raise RuntimeError(f'unexpected screenshot byte count: {len(pixels)} != {expected}')",
                "rgb = bytearray(img.width * img.height * 3)",
                "for source in range(0, len(pixels), 4):",
                "    target = (source // 4) * 3",
                "    rgb[target] = pixels[source]",
                "    rgb[target + 1] = pixels[source + 1]",
                "    rgb[target + 2] = pixels[source + 2]",
                "header = f'P6\\n{img.width} {img.height}\\n255\\n'.encode()",
                "Path(path).write_bytes(header + rgb)",
                "print(path)",
                "",
            ]
        )
        return self.running_vm.run_repl(f"exec({code!r})\n")
